"""Compositor previews render the scene once and composite many.

Each compositor preview used to render the whole scene (the tree's Render
Layers / Cryptomatte nodes make the render pipeline run the engine first).
Now one plain render of the scene is cached as a multilayer EXR and, in the
throwaway tree copy, Render Layers nodes become Image nodes on it and
Cryptomatte nodes read it as an image. These tests check that the result is
pixel-identical to the per-node render, that N previews make one engine
render, the cases that keep rendering per node, when the cache is rebuilt,
and that nothing of the user's is touched or leaked.
"""
import contextlib
import os
import tempfile

import bmesh
import bpy

from npv_testutil import capture_renders, datablock_names, mean_rgb

RES = 32


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def engine_renders():
    """Counts the renders that ran the engine (not only the compositor): a
    composite-only render reports just 'Compositing' as its stats."""
    runs = []

    def pre(*_a):
        runs.append([])

    def stats(stat, *_a):
        if runs:
            runs[-1].append(str(stat))

    h = bpy.app.handlers
    h.render_pre.append(pre)
    h.render_stats.append(stats)
    counter = type("Count", (), {})()
    counter.runs = runs
    counter.engine = lambda: sum(1 for r in runs if any(s != "Compositing" for s in r))
    try:
        yield counter
    finally:
        h.render_pre.remove(pre)
        h.render_stats.remove(stats)


@contextlib.contextmanager
def per_node_path(mod):
    """Force the per-node scene render (the path before the cache)."""
    orig = mod.renderers._comp_cache_eligible
    mod.renderers._comp_cache_eligible = lambda tree, scene: False
    try:
        yield
    finally:
        mod.renderers._comp_cache_eligible = orig


def _preview(mod, scene, node, out_id=None, chain=None, res=RES):
    with capture_renders(mod) as shots:
        ok = mod.renderers.render_compositor(scene, node, res, scene.npv, out_id, chain)
    assert ok and shots, "no preview for %s %s" % (node, out_id or "")
    return shots[0]


def _both(mod, scene, node, out_id=None, chain=None):
    """(per-node render pixels, cached-path pixels) of one preview."""
    with per_node_path(mod):
        old = _preview(mod, scene, node, out_id, chain)
    new = _preview(mod, scene, node, out_id, chain)
    return old, new


def _assert_same(old, new, what):
    assert len(old) == len(new), "%s: size differs" % what
    diff = max(abs(a - b) for a, b in zip(old, new))
    assert diff == 0.0, "%s: cached preview differs by %g" % (what, diff)


def _drop(mod):
    mod.preview_scene.drop_comp_cache()


def _cached(mod):
    c = mod._state["comp_cache"]
    return c is not None and os.path.isfile(c["path"])


@contextlib.contextmanager
def comp_scene(mod, engine="BLENDER_EEVEE"):
    """The default scene with a second, emissive object and material, a
    second view layer, the common passes on, and an empty compositor tree.
    Everything is put back afterwards."""
    scene = bpy.context.scene
    r = scene.render
    vl = scene.view_layers[0]
    pass_names = ("use_pass_z", "use_pass_mist", "use_pass_normal",
                  "use_pass_ambient_occlusion", "use_pass_diffuse_color",
                  "use_pass_emit", "use_pass_cryptomatte_object",
                  "use_pass_cryptomatte_material")
    saved = (r.engine, scene.frame_current, scene.cycles.samples,
             scene.eevee.taa_render_samples,
             {p: getattr(vl, p) for p in pass_names},
             vl.cycles.denoising_store_passes)
    r.engine = engine
    scene.cycles.samples = 4
    scene.cycles.device = "CPU"
    scene.eevee.taa_render_samples = 4
    for p in pass_names:
        setattr(vl, p, True)
    vl.cycles.denoising_store_passes = True
    me = bpy.data.meshes.new("NPV_tcc_mesh")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=6, radius=0.7)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("NPV_tcc_ball", me)
    ob.location = (1.4, -1.2, 0.8)
    scene.collection.objects.link(ob)
    mat = bpy.data.materials.new("NPV_tcc_mat")
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (0.1, 0.8, 0.2, 1.0)
    bsdf.inputs["Emission Color"].default_value = (0.9, 0.3, 0.0, 1.0)
    bsdf.inputs["Emission Strength"].default_value = 2.0
    me.materials.append(mat)
    vl2 = scene.view_layers.new("NPV_tcc_layer2")
    vl2.use_pass_cryptomatte_object = True
    vl2.use_pass_z = True
    # The second layer looks different: the cube is left out of it.
    for lc in vl2.layer_collection.children:
        lc.exclude = True
    tree = bpy.data.node_groups.new("NPV_tcc_tree", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    tree.nodes.new("NodeGroupOutput").name = "Out"
    scene.compositing_node_group = tree
    try:
        yield scene, tree, vl, vl2, mat
    finally:
        scene.compositing_node_group = None
        bpy.data.node_groups.remove(tree)
        scene.view_layers.remove(vl2)
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.materials.remove(mat)
        (r.engine, frame, scene.cycles.samples, scene.eevee.taa_render_samples,
         passes, vl.cycles.denoising_store_passes) = saved
        scene.frame_set(frame)
        for p, v in passes.items():
            setattr(vl, p, v)
        for k in ("textures", "hashes", "queue", "queued_keys", "failed"):
            mod._state[k].clear()
        _drop(mod)


def _rl(tree, scene, layer=None, name="RL"):
    rl = tree.nodes.new("CompositorNodeRLayers")
    rl.name = name
    rl.scene = scene
    if layer is not None:
        rl.layer = layer
    return rl


def _out(tree, sock):
    go = tree.nodes["Out"]
    tree.links.new(sock, go.inputs[0])


def _node(tree, idn, name, **inputs):
    n = tree.nodes.new(idn)
    n.name = name
    for k, v in inputs.items():
        n.inputs[k].default_value = v
    return n


def _render_result_size(scene):
    img = next((i for i in bpy.data.images if i.type == "RENDER_RESULT"), None)
    if img is None:
        return None
    path = os.path.join(tempfile.gettempdir(), "npv_tcc_probe.png")
    img.save_render(path, scene=scene)
    probe = bpy.data.images.load(path)
    try:
        return tuple(probe.size)
    finally:
        bpy.data.images.remove(probe)
        os.remove(path)


def _tree_state(tree):
    return (sorted((n.name, n.bl_idname, n.mute) for n in tree.nodes),
            sorted((l.from_node.name, l.from_socket.identifier,
                    l.to_node.name, l.to_socket.identifier) for l in tree.links))


# --------------------------------------------------------------------------- #
#  Equivalence
# --------------------------------------------------------------------------- #
def _passes_tree(tree, scene):
    rl = _rl(tree, scene)
    defocus = _node(tree, "CompositorNodeDefocus", "Defocus")
    tree.links.new(rl.outputs["Image"], defocus.inputs["Image"])
    tree.links.new(rl.outputs["Depth"], defocus.inputs["Z"])
    blur = _node(tree, "CompositorNodeBlur", "Blur", Size=(3.0, 3.0))
    tree.links.new(rl.outputs["Image"], blur.inputs["Image"])
    mix = _node(tree, "ShaderNodeMix", "Mix")
    mix.data_type = "RGBA"
    mix.inputs["Factor"].default_value = 0.5
    tree.links.new(rl.outputs["Normal"], mix.inputs["A"])
    tree.links.new(rl.outputs["Emission"], mix.inputs["B"])
    _out(tree, blur.outputs[0])
    return rl


def test_cached_passes_match_per_node_render(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        rl = _passes_tree(tree, scene)
        # (EEVEE has no AO pass: the Cycles test covers it.)
        outs = [o.identifier for o in rl.outputs if o.enabled and o.identifier in
                ("Image", "Alpha", "Depth", "Mist", "Normal", "Diffuse Color",
                 "Emission")]
        assert len(outs) == 7, outs
        for oid in outs:
            old, new = _both(mod, scene, "RL", oid)
            _assert_same(old, new, "Render Layers %s" % oid)
        for name in ("Defocus", "Blur", "Mix"):
            old, new = _both(mod, scene, name)
            _assert_same(old, new, name)
        assert _cached(mod), "previews did not use the cached render"


def test_cached_denoise_matches_per_node_render_cycles(mod):
    with comp_scene(mod, "CYCLES") as (scene, tree, vl, vl2, mat):
        _drop(mod)
        rl = _rl(tree, scene)
        dn = _node(tree, "CompositorNodeDenoise", "Denoise")
        tree.links.new(rl.outputs["Image"], dn.inputs["Image"])
        tree.links.new(rl.outputs["Denoising Normal"], dn.inputs["Normal"])
        tree.links.new(rl.outputs["Denoising Albedo"], dn.inputs["Albedo"])
        _out(tree, dn.outputs[0])
        for node, oid in (("Denoise", None), ("RL", "Image"), ("RL", "Depth"),
                          ("RL", "AO"), ("RL", "Denoising Normal")):
            old, new = _both(mod, scene, node, oid)
            _assert_same(old, new, "Cycles %s %s" % (node, oid or ""))
        assert _cached(mod)


def test_cached_cryptomatte_matches_per_node_render(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        rl = _rl(tree, scene)
        cms = []
        for name, layer, matte in (
                ("CryptoObj", vl.name + ".CryptoObject", "NPV_tcc_ball"),
                ("CryptoMat", vl.name + ".CryptoMaterial", "NPV_tcc_mat"),
                ("CryptoL2", vl2.name + ".CryptoObject", "NPV_tcc_ball")):
            cm = tree.nodes.new("CompositorNodeCryptomatteV2")
            cm.name = name
            cm.scene = scene
            cm.source = "RENDER"
            cm.layer_name = layer
            cm.matte_id = matte
            tree.links.new(rl.outputs["Image"], cm.inputs["Image"])
            cms.append(cm)
        _out(tree, cms[0].outputs["Image"])
        for cm in cms:
            for oid in ("Image", "Matte"):
                old, new = _both(mod, scene, cm.name, oid)
                _assert_same(old, new, "%s %s" % (cm.name, oid))
                if oid == "Matte":
                    assert any(v > 0.5 for v in new[0::4]), \
                        "%s matte is empty: the test picks nothing" % cm.name
        assert _cached(mod)
        # The user's nodes still read the render.
        assert all(cm.source == "RENDER" and cm.scene == scene for cm in cms)


def test_cached_two_view_layers_match_per_node_render(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        a = _rl(tree, scene, vl.name, "RL A")
        b = _rl(tree, scene, vl2.name, "RL B")
        ao = _node(tree, "CompositorNodeAlphaOver", "Over")
        tree.links.new(a.outputs["Image"], ao.inputs["Background"])
        tree.links.new(b.outputs["Image"], ao.inputs["Foreground"])
        mix = _node(tree, "ShaderNodeMix", "Depths")
        mix.data_type = "RGBA"
        tree.links.new(a.outputs["Depth"], mix.inputs["A"])
        tree.links.new(b.outputs["Depth"], mix.inputs["B"])
        _out(tree, ao.outputs[0])
        pa = pb = None
        for node in ("Over", "Depths", "RL A", "RL B"):
            old, new = _both(mod, scene, node)
            _assert_same(old, new, node)
            if node == "RL A":
                pa = new
            elif node == "RL B":
                pb = new
        assert pa != pb, "both view layers rendered the same: the test proves nothing"


def _group_setup(tree, scene):
    """Render Layers outside, previewed nodes inside an entered group."""
    grp = bpy.data.node_groups.new("NPV_tcc_grp", "CompositorNodeTree")
    grp.interface.new_socket("In", in_out="INPUT", socket_type="NodeSocketColor")
    grp.interface.new_socket("Out", in_out="OUTPUT", socket_type="NodeSocketColor")
    gi = grp.nodes.new("NodeGroupInput")
    go = grp.nodes.new("NodeGroupOutput")
    blur = _node(grp, "CompositorNodeBlur", "Inner Blur", Size=(2.0, 2.0))
    mix = _node(grp, "ShaderNodeMix", "Inner Mix")
    mix.data_type = "RGBA"
    mix.inputs["Factor"].default_value = 0.3
    mix.inputs["B"].default_value = (0.0, 0.0, 1.0, 1.0)
    grp.links.new(gi.outputs[0], blur.inputs["Image"])
    grp.links.new(blur.outputs[0], mix.inputs["A"])
    grp.links.new(mix.outputs["Result"], go.inputs[0])
    rl = _rl(tree, scene)
    inst = tree.nodes.new("CompositorNodeGroup")
    inst.node_tree = grp
    inst.name = "G"
    tree.links.new(rl.outputs["Image"], inst.inputs[0])
    _out(tree, inst.outputs[0])
    tree.nodes.active = inst
    return grp


def test_cached_group_chain_matches_per_node_render(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        grp = _group_setup(tree, scene)
        try:
            for node in ("Inner Blur", "Inner Mix"):
                old, new = _both(mod, scene, node, chain=["G"])
                _assert_same(old, new, "group %s" % node)
            old, new = _both(mod, scene, "G")
            _assert_same(old, new, "group node")
            assert _cached(mod)
        finally:
            bpy.data.node_groups.remove(grp)


# --------------------------------------------------------------------------- #
#  One scene render for N nodes
# --------------------------------------------------------------------------- #
def test_one_scene_render_for_many_previews(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _passes_tree(tree, scene)
        nodes = [("RL", "Image"), ("RL", "Depth"), ("Defocus", None),
                 ("Blur", None), ("Mix", None)]
        with engine_renders() as cold:
            for node, oid in nodes:
                _preview(mod, scene, node, oid)
        assert cold.engine() == 1, "cold cache: %d engine renders for %d previews: %r" \
            % (cold.engine(), len(nodes), cold.runs)
        assert len(cold.runs) == len(nodes) + 1, len(cold.runs)
        with engine_renders() as warm:
            for node, oid in nodes:
                _preview(mod, scene, node, oid)
        assert warm.engine() == 0, "warm cache rendered the scene %d times" % warm.engine()
        # Through the queue too, as the timer renders them: at the Quality
        # setting's size, so once more for that size, then never.
        mod._state["queue"].clear()
        mod._state["queued_keys"].clear()
        mod._state["hashes"].clear()
        mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, scene.npv, path=[tree])
        queued = len(mod._state["queue"])
        assert queued >= 4, queued
        with engine_renders() as q, capture_renders(mod) as shots:
            for _ in range(queued):
                if not mod._state["queue"]:
                    break
                mod.queue.process_queue(scene.npv)
        assert not mod._state["queue"] and len(shots) == queued, (len(shots), queued)
        assert q.engine() == 1, "%d queued previews rendered the scene %d times" \
            % (queued, q.engine())


def test_per_node_path_renders_scene_per_node(mod):
    # The baseline the cache improves on (and what the fallbacks keep).
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _passes_tree(tree, scene)
        with per_node_path(mod), engine_renders() as c:
            for node in ("Defocus", "Blur", "Mix"):
                _preview(mod, scene, node)
        assert c.engine() == 3, c.engine()


def test_tree_without_render_layers_renders_no_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        rgb = _node(tree, "CompositorNodeRGB", "RGB")
        rgb.outputs[0].default_value = (0.9, 0.1, 0.1, 1.0)
        _out(tree, rgb.outputs[0])
        with engine_renders() as c:
            px = _preview(mod, scene, "RGB")
        assert c.engine() == 0 and mod._state["comp_cache"] is None, \
            "rendered the scene for a tree that doesn't read it"
        r, g, b = mean_rgb(px)
        assert r > g + 0.3 and r > b + 0.3, (r, g, b)


# --------------------------------------------------------------------------- #
#  Fallbacks: same as before
# --------------------------------------------------------------------------- #
def _assert_fallback(mod, scene, node, chain=None):
    _drop(mod)
    with per_node_path(mod):
        old = _preview(mod, scene, node, chain=chain)
    new = _preview(mod, scene, node, chain=chain)
    assert mod._state["comp_cache"] is None, "%s used the cached render" % node
    assert not os.path.exists(mod.preview_scene.comp_cache_path())
    _assert_same(old, new, "fallback %s" % node)


def test_render_layers_inside_group_falls_back(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        grp = bpy.data.node_groups.new("NPV_tcc_rlgrp", "CompositorNodeTree")
        grp.interface.new_socket("Out", in_out="OUTPUT", socket_type="NodeSocketColor")
        go = grp.nodes.new("NodeGroupOutput")
        inner = _rl(grp, scene, name="Inner RL")
        blur = _node(grp, "CompositorNodeBlur", "Inner Blur", Size=(2.0, 2.0))
        grp.links.new(inner.outputs["Image"], blur.inputs["Image"])
        grp.links.new(blur.outputs[0], go.inputs[0])
        try:
            inst = tree.nodes.new("CompositorNodeGroup")
            inst.node_tree = grp
            inst.name = "G"
            # A top-level Render Layers node too: the group alone decides.
            rl = _rl(tree, scene)
            mix = _node(tree, "ShaderNodeMix", "Mix")
            mix.data_type = "RGBA"
            tree.links.new(rl.outputs["Image"], mix.inputs["A"])
            tree.links.new(inst.outputs[0], mix.inputs["B"])
            _out(tree, mix.outputs["Result"])
            tree.nodes.active = inst
            _assert_fallback(mod, scene, "G")
            _assert_fallback(mod, scene, "Mix")
            _assert_fallback(mod, scene, "Inner Blur", chain=["G"])
        finally:
            bpy.data.node_groups.remove(grp)


def test_render_layers_of_another_scene_falls_back(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        other = bpy.data.scenes.new("NPV_tcc_other")
        other.collection.objects.link(scene.camera)
        other.camera = scene.camera
        try:
            rl = _rl(tree, other)
            _out(tree, rl.outputs["Image"])
            _assert_fallback(mod, scene, "RL")
        finally:
            bpy.data.scenes.remove(other)


def test_muted_render_layers_falls_back(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        rl = _rl(tree, scene)
        blur = _node(tree, "CompositorNodeBlur", "Blur", Size=(2.0, 2.0))
        tree.links.new(rl.outputs["Image"], blur.inputs["Image"])
        _out(tree, blur.outputs[0])
        rl.mute = True
        _assert_fallback(mod, scene, "Blur")


def test_muted_cryptomatte_falls_back(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        rl = _rl(tree, scene)
        cm = tree.nodes.new("CompositorNodeCryptomatteV2")
        cm.name = "Crypto"
        cm.scene = scene
        cm.layer_name = vl.name + ".CryptoObject"
        tree.links.new(rl.outputs["Image"], cm.inputs["Image"])
        blur = _node(tree, "CompositorNodeBlur", "Blur", Size=(2.0, 2.0))
        tree.links.new(cm.outputs["Image"], blur.inputs["Image"])
        _out(tree, blur.outputs[0])
        cm.mute = True
        _assert_fallback(mod, scene, "Blur")


def test_cache_lacking_a_view_layer_falls_back(mod):
    # A view layer switched off for rendering is not in the cached render;
    # a Render Layers node reading it keeps rendering the scene per node.
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        rl = _rl(tree, scene, vl2.name, "RL B")
        _out(tree, rl.outputs["Image"])
        vl2.use = False
        with per_node_path(mod):
            old = _preview(mod, scene, "RL B")
        new = _preview(mod, scene, "RL B")
        _assert_same(old, new, "view layer missing from the cache")


# --------------------------------------------------------------------------- #
#  Invalidation
# --------------------------------------------------------------------------- #
def _flush():
    bpy.context.view_layer.update()


def _ball_tree(tree, scene):
    rl = _rl(tree, scene)
    blur = _node(tree, "CompositorNodeBlur", "Blur", Size=(1.0, 1.0))
    tree.links.new(rl.outputs["Image"], blur.inputs["Image"])
    _out(tree, blur.outputs[0])
    return blur


def test_preview_renders_do_not_drop_the_cache(mod):
    # The scene copies a preview makes tag the user's objects; those updates
    # must not read as scene edits.
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _ball_tree(tree, scene)
        _preview(mod, scene, "Blur")
        key = mod._state["comp_cache"]["key"]
        _flush()
        assert mod._state["comp_cache"] is not None, "a preview dropped the cache"
        assert mod._state["comp_cache"]["key"] == key


def test_compositor_edit_reuses_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        blur = _ball_tree(tree, scene)
        before = _preview(mod, scene, "Blur")
        blur.inputs["Size"].default_value = (4.0, 4.0)
        _flush()
        assert _cached(mod), "a compositor edit dropped the cache"
        with engine_renders() as c:
            after = _preview(mod, scene, "Blur")
        assert c.engine() == 0, "a compositor edit re-rendered the scene: %r" % c.runs
        assert before != after, "the edit did not reach the preview"
        with per_node_path(mod):
            old = _preview(mod, scene, "Blur")
        _assert_same(old, after, "after a compositor edit")


@contextlib.contextmanager
def _unseen_object(scene, image=False):
    """A red-emitting cube 'RVobj' in its own collection 'RVcol', in front of
    the rest of the scene. With ``image`` the red comes from a generated
    image (yielded instead of the BSDF)."""
    col = bpy.data.collections.new("NPV_tcc_RVcol")
    scene.collection.children.link(col)
    me = bpy.data.meshes.new("NPV_tcc_RVmesh")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.4)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("NPV_tcc_RVobj", me)
    col.objects.link(ob)
    mat = bpy.data.materials.new("NPV_tcc_RVmat")
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Emission Color"].default_value = (1.0, 0.0, 0.0, 1.0)
    bsdf.inputs["Emission Strength"].default_value = 5.0
    img = None
    if image:
        img = bpy.data.images.new("NPV_tcc_RVimg", 8, 8)
        img.generated_color = (1.0, 0.0, 0.0, 1.0)
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
    me.materials.append(mat)
    try:
        yield col, ob, (img if image else bsdf)
    finally:
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.materials.remove(mat)
        bpy.data.collections.remove(col)
        if img is not None:
            bpy.data.images.remove(img)


def _unseen_edit_case(mod, scene, tree, rl_layer, edit, quiet=True):
    """Preview (cache built), edit what the window's view layer doesn't
    evaluate, edit the compositor tree only, preview again: the second
    preview must show the edited scene, as the per-node render does, and so
    render the scene once more. ``quiet``: the edit reaches the depsgraph
    handler with no updates at all (else alongside another update)."""
    _drop(mod)
    rl = _rl(tree, scene, rl_layer)
    blur = _node(tree, "CompositorNodeBlur", "Blur", Size=(2.0, 2.0))
    tree.links.new(rl.outputs["Image"], blur.inputs["Image"])
    _out(tree, blur.outputs[0])
    # What a stale cache would show: the unedited scene at the final size.
    with per_node_path(mod):
        stale = _preview(mod, scene, "Blur")
    blur.inputs["Size"].default_value = (1.0, 1.0)
    _flush()
    _preview(mod, scene, "Blur")
    assert _cached(mod)
    seen = []
    probe = lambda _s, dg: seen.append(len(dg.updates))
    bpy.app.handlers.depsgraph_update_post.append(probe)
    try:
        edit()
        _flush()
    finally:
        bpy.app.handlers.depsgraph_update_post.remove(probe)
    if quiet:
        assert seen == [0], "the edit reached the view layer's updates: %r" % seen
    else:
        assert seen and seen[0] > 0, "no other update came along: %r" % seen
    blur.inputs["Size"].default_value = (2.0, 2.0)
    _flush()
    with engine_renders() as c:
        after = _preview(mod, scene, "Blur")
    with per_node_path(mod):
        new = _preview(mod, scene, "Blur")
    assert new != stale, "the edit changed nothing"
    _assert_same(new, after, "after an unseen scene edit")
    assert c.engine() == 1, "the cache was not rebuilt (%d scene renders)" % c.engine()


def _blue(bsdf):
    return lambda: setattr(bsdf.inputs["Emission Color"], "default_value",
                           (0.0, 0.0, 1.0, 1.0))


def _blue_image(img):
    return lambda: setattr(img, "generated_color", (0.0, 0.0, 1.0, 1.0))


def test_edit_in_excluded_collection_rebuilds_cache(mod):
    # (a) The object's collection is excluded from the window's view layer,
    # and a Render Layers node reads the view layer that renders it.
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        with _unseen_object(scene) as (col, ob, bsdf):
            vl.layer_collection.children[col.name].exclude = True
            assert not vl2.layer_collection.children[col.name].exclude
            _flush()
            _unseen_edit_case(mod, scene, tree, vl2.name, _blue(bsdf))


def test_edit_of_viewport_hidden_object_rebuilds_cache(mod):
    # (b) Hidden in the viewport, still rendered.
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        with _unseen_object(scene) as (col, ob, bsdf):
            ob.hide_viewport = True
            _flush()
            _unseen_edit_case(mod, scene, tree, vl.name, _blue(bsdf))


def test_move_of_viewport_hidden_object_rebuilds_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        with _unseen_object(scene) as (col, ob, bsdf):
            ob.hide_viewport = True
            _flush()
            _unseen_edit_case(mod, scene, tree, vl.name,
                              lambda: setattr(ob, "location", (0.0, 0.0, 3.5)))


def test_image_edit_of_viewport_hidden_object_rebuilds_cache(mod):
    # The image is used only by the hidden object: its edit flags IMAGE only.
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        with _unseen_object(scene, image=True) as (col, ob, img):
            ob.hide_viewport = True
            _flush()
            _unseen_edit_case(mod, scene, tree, vl.name, _blue_image(img))


def test_image_edit_in_excluded_collection_rebuilds_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        with _unseen_object(scene, image=True) as (col, ob, img):
            vl.layer_collection.children[col.name].exclude = True
            _flush()
            _unseen_edit_case(mod, scene, tree, vl2.name, _blue_image(img))


def test_unseen_edit_with_a_selection_rebuilds_cache(mod):
    # A hidden object's material edit and a selection in one evaluation: the
    # update lists the scene (the selection), not the edit.
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        with _unseen_object(scene) as (col, ob, bsdf):
            ob.hide_viewport = True
            _flush()
            cube = bpy.data.objects["Cube"]

            def edit():
                _blue(bsdf)()
                cube.select_set(not cube.select_get())

            _unseen_edit_case(mod, scene, tree, vl.name, edit, quiet=False)


def test_hidden_objects_left_alone_keep_cache(mod):
    # The content check must not see changes where there are none: previews
    # and compositor edits with hidden / excluded objects reuse the cache.
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        with _unseen_object(scene, image=True) as (col, ob, img):
            ob.hide_viewport = True
            blur = _ball_tree(tree, scene)
            _drop(mod)
            _preview(mod, scene, "Blur")
            blur.inputs["Size"].default_value = (3.0, 3.0)
            _flush()
            bpy.data.objects["Cube"].select_set(True)
            _flush()
            with engine_renders() as c:
                _preview(mod, scene, "Blur")
                _preview(mod, scene, "Blur")
            assert c.engine() == 0, "an unchanged hidden object rebuilt the cache"


def test_scene_edit_rebuilds_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _ball_tree(tree, scene)
        _preview(mod, scene, "Blur")
        assert _cached(mod)
        bsdf = mat.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Emission Color"].default_value = (0.0, 0.2, 1.0, 1.0)
        _flush()
        assert mod._state["comp_cache"] is None, "a material edit kept the cache"
        with engine_renders() as c:
            new = _preview(mod, scene, "Blur")
        assert c.engine() == 1, c.engine()
        with per_node_path(mod):
            old = _preview(mod, scene, "Blur")
        _assert_same(old, new, "after a material edit")


def test_object_move_rebuilds_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _ball_tree(tree, scene)
        before = _preview(mod, scene, "Blur")
        bpy.data.objects["NPV_tcc_ball"].location.x -= 0.8
        _flush()
        assert mod._state["comp_cache"] is None, "moving an object kept the cache"
        after = _preview(mod, scene, "Blur")
        assert before != after, "the move did not reach the preview"


def test_scene_settings_and_frame_change_the_key(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _ball_tree(tree, scene)
        _preview(mod, scene, "Blur")
        key = mod._state["comp_cache"]["key"]
        props = scene.npv
        k = mod.renderers._comp_cache_key
        # Selecting objects (a scene update) changes nothing.
        bpy.data.objects["NPV_tcc_ball"].select_set(True)
        _flush()
        assert k(scene, RES, props) == key
        assert k(scene, RES * 2, props) != key, "resolution not in the key"
        vl.use_pass_mist = False
        assert k(scene, RES, props) != key, "a pass toggle not in the key"
        vl.use_pass_mist = True
        scene.frame_set(scene.frame_current + 3)
        assert k(scene, RES, props) != key, "the frame not in the key"
        with engine_renders() as c:
            _preview(mod, scene, "Blur")
        assert c.engine() == 1, "a frame change kept the old scene render"


def _set_working_space(name):
    if bpy.data.colorspace.working_space == name:
        return          # the operator cancels a switch to the current space
    assert bpy.ops.wm.set_working_color_space(
        working_space=name, convert_colors=False) == {"FINISHED"}
    assert bpy.data.colorspace.working_space == name


def test_working_space_change_rebuilds_cache(mod):
    # Switching the blend file's working colour space sends a depsgraph update
    # with no updates in it: the cache key has to catch it.
    saved = bpy.data.colorspace.working_space
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        blur = _ball_tree(tree, scene)
        try:
            _set_working_space("ACEScg")
            _preview(mod, scene, "Blur")
            assert _cached(mod)
            _set_working_space("Linear Rec.709")
            blur.inputs["Size"].default_value = (2.0, 2.0)
            _flush()
            with engine_renders() as c:
                new = _preview(mod, scene, "Blur")
            assert c.engine() == 1, \
                "the edit composited from the other working space's render"
            with per_node_path(mod):
                old = _preview(mod, scene, "Blur")
            _assert_same(old, new, "after a working space change")
        finally:
            _set_working_space(saved)


def test_refresh_rebuilds_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _ball_tree(tree, scene)
        _preview(mod, scene, "Blur")
        assert _cached(mod)
        mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, scene.npv, force=False,
                                path=[tree])
        assert _cached(mod), "an ordinary rebuild dropped the cache"
        mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, scene.npv, force=True,
                                path=[tree])
        assert mod._state["comp_cache"] is None, "Refresh kept the cache"
        assert not os.path.exists(mod.preview_scene.comp_cache_path())


def test_export_keeps_thumbnail_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _ball_tree(tree, scene)
        _preview(mod, scene, "Blur")
        key = mod._state["comp_cache"]["key"]
        dst = os.path.join(tempfile.gettempdir(), "npv_tcc_export.png")
        mod._state["export_to"] = dst
        try:
            assert mod.renderers.render_compositor(scene, "Blur", 64, scene.npv)
        finally:
            mod._state["export_to"] = None
        assert os.path.isfile(dst)
        os.remove(dst)
        assert mod._state["comp_cache"]["key"] == key, "export replaced the cache"


# --------------------------------------------------------------------------- #
#  Leaks and the user's data
# --------------------------------------------------------------------------- #
def test_cached_previews_leave_user_data_alone(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        rl = _rl(tree, scene)
        cm = tree.nodes.new("CompositorNodeCryptomatteV2")
        cm.name = "Crypto"
        cm.scene = scene
        cm.layer_name = vl.name + ".CryptoObject"
        cm.matte_id = "NPV_tcc_ball"
        tree.links.new(rl.outputs["Image"], cm.inputs["Image"])
        viewer = tree.nodes.new("CompositorNodeViewer")
        viewer.name = "Viewer"
        tree.links.new(cm.outputs["Image"], viewer.inputs[0])
        _out(tree, cm.outputs["Image"])
        r = scene.render
        saved_res = (r.resolution_x, r.resolution_y, r.resolution_percentage)
        r.resolution_x, r.resolution_y, r.resolution_percentage = 120, 60, 100
        try:
            bpy.ops.render.render()
            rr_size = _render_result_size(scene)
            assert rr_size == (120, 60), rr_size
            viewer_img = bpy.data.images.get("Viewer Node")
            viewer_ptr = viewer_img.as_pointer() if viewer_img else None
            settings = (r.resolution_x, r.resolution_y, r.resolution_percentage,
                        r.engine, r.use_compositing, r.film_transparent,
                        r.image_settings.media_type, r.image_settings.file_format,
                        r.filepath)
            tstate = _tree_state(tree)
            snap = datablock_names()
            for node, oid in (("RL", "Image"), ("RL", "Depth"), ("Crypto", "Matte"),
                              ("Crypto", "Image")):
                _preview(mod, scene, node, oid)
            assert _cached(mod)
            assert datablock_names() == snap, "cached previews leaked datablocks"
            assert _tree_state(tree) == tstate, "user compositor tree modified"
            assert scene.compositing_node_group == tree
            assert cm.source == "RENDER" and cm.scene == scene and cm.image is None
            assert rl.scene == scene
            assert (r.resolution_x, r.resolution_y, r.resolution_percentage,
                    r.engine, r.use_compositing, r.film_transparent,
                    r.image_settings.media_type, r.image_settings.file_format,
                    r.filepath) == settings, "user render settings modified"
            assert _render_result_size(scene) == (120, 60), \
                "a cached preview overwrote the user's Render Result"
            v = bpy.data.images.get("Viewer Node")
            assert (v.as_pointer() if v else None) == viewer_ptr, "Viewer image replaced"
            path = mod._state["comp_cache"]["path"]
            mod.preview_scene.drop_comp_cache()
            assert not os.path.exists(path), "cache file left behind"
        finally:
            r.resolution_x, r.resolution_y, r.resolution_percentage = saved_res


def test_reset_cache_drops_comp_cache(mod):
    with comp_scene(mod) as (scene, tree, vl, vl2, mat):
        _drop(mod)
        _ball_tree(tree, scene)
        _preview(mod, scene, "Blur")
        path = mod._state["comp_cache"]["path"]
        mod.queue._reset_cache()        # Clear Cache, file load, unregister
        assert mod._state["comp_cache"] is None and not os.path.exists(path)
