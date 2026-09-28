"""Regressions fixed in v1.1.7."""
import os
import tempfile

import bpy


# --- multiple Geometry Nodes modifiers ------------------------------------ #
def _gn(name, prim, **settings):
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    p = ng.nodes.new(prim)
    p.name = "Shape"  # same node name in both trees on purpose
    for k, v in settings.items():
        p.inputs[k].default_value = v
    ng.links.new(p.outputs[0], go.inputs[0])
    return ng


def _rendered_vertex_counts(mod, run):
    """Run ``run()`` and record the evaluated vertex count of every non-preview
    mesh object in the preview scene at render time."""
    counts = []
    orig = mod._render_scene

    def spy(scn):
        vl = scn.view_layers[0]
        with bpy.context.temp_override(scene=scn, view_layer=vl):
            dg = bpy.context.evaluated_depsgraph_get()
            dg.update()
            for o in scn.collection.objects:
                if o.type == "MESH" and not o.name.startswith("NPV_preview"):
                    counts.append(len(o.evaluated_get(dg).data.vertices))
        raise RuntimeError("stop")

    mod._render_scene = spy
    try:
        run()
    except RuntimeError:
        pass
    finally:
        mod._render_scene = orig
    return counts


def test_second_gn_modifier_previews_its_own_tree(mod):
    ng_a = _gn("NPV_test_A", "GeometryNodeMeshCube")                    # 8 verts
    ng_b = _gn("NPV_test_B", "GeometryNodeMeshGrid", **{"Vertices X": 4, "Vertices Y": 4})  # 16
    me = bpy.data.meshes.new("NPV_test_mm")
    ob = bpy.data.objects.new("NPV_test_mm", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("A", "NODES").node_group = ng_a
    ob.modifiers.new("B", "NODES").node_group = ng_b
    props = bpy.context.scene.npv
    try:
        got_b = _rendered_vertex_counts(
            mod, lambda: mod.render_geo(ob, "Shape", 32, props, tree=ng_b))
        got_a = _rendered_vertex_counts(
            mod, lambda: mod.render_geo(ob, "Shape", 32, props, tree=ng_a))
    finally:
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng_a)
        bpy.data.node_groups.remove(ng_b)
    assert got_b == [16], "2nd modifier's node rendered %r verts, want [16]" % got_b
    # Later modifiers must not run on top of the previewed node's output.
    assert got_a == [8], "1st modifier's node rendered %r verts, want [8]" % got_a


def test_queue_carries_the_tree(mod):
    ng = _gn("NPV_test_Q", "GeometryNodeMeshCube")
    me = bpy.data.meshes.new("NPV_test_q")
    ob = bpy.data.objects.new("NPV_test_q", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("Q", "NODES").node_group = ng
    st = mod._state
    try:
        mod.rebuild_queue(ng, mod.KIND_GEO, bpy.context.scene.npv, force=True)
        assert st["queue"], "nothing queued"
        assert all(it.get("tree") == ng.name for it in st["queue"]), st["queue"]
    finally:
        for k in ("textures", "hashes", "queue", "queued_keys"):
            st[k].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


# --- preview datablocks must not be saved into the user's file ------------ #
def _saved_names(path):
    with bpy.data.libraries.load(path) as (src, _dst):
        return list(src.scenes) + list(src.objects) + list(src.worlds)


def test_preview_scene_is_not_saved(mod):
    mod.ensure_preview_scene(32)
    path = os.path.join(tempfile.mkdtemp(), "npv_save.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    leaked = [n for n in _saved_names(path) if n.startswith("NPV_")]
    assert not leaked, "preview datablocks saved into the .blend: %r" % leaked


def test_preview_scene_from_old_file_is_removed_on_load(mod):
    # Simulate a file saved by an older version (no save_pre cleanup).
    saved = [h for h in bpy.app.handlers.save_pre if h.__module__ == mod.__name__]
    for h in saved:
        bpy.app.handlers.save_pre.remove(h)
    try:
        mod.ensure_preview_scene(32)
        path = os.path.join(tempfile.mkdtemp(), "npv_old.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    finally:
        for h in saved:
            bpy.app.handlers.save_pre.append(h)
    assert any(n.startswith("NPV_") for n in _saved_names(path)), "setup failed"
    bpy.ops.wm.open_mainfile(filepath=path)
    left = [s.name for s in bpy.data.scenes if s.name.startswith("NPV_")]
    assert not left, "preview scene from an old file still present: %r" % left


# --- compositor previews must not use the user's full render quality ------ #
def test_compositor_preview_uses_low_samples(mod):
    scene = bpy.context.scene
    tree = bpy.data.node_groups.new("NPV_test_c", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    go = tree.nodes.new("NodeGroupOutput")
    rl = tree.nodes.new("CompositorNodeRLayers")
    rl.name = "RL"
    tree.links.new(rl.outputs[0], go.inputs[0])
    scene.compositing_node_group = tree
    saved = (scene.render.engine, scene.cycles.samples, scene.eevee.taa_render_samples)
    seen = []
    orig = mod._render_scene

    def spy(s):
        seen.append((s.render.engine, s.cycles.samples, s.eevee.taa_render_samples,
                     s.render.use_motion_blur))
        raise RuntimeError("stop")

    mod._render_scene = spy
    try:
        for engine in ("CYCLES", "BLENDER_EEVEE"):
            scene.render.engine = engine
            scene.cycles.samples = 4096
            scene.eevee.taa_render_samples = 512
            scene.render.use_motion_blur = True
            try:
                mod.render_compositor(scene, "RL", 32, scene.npv)
            except RuntimeError:
                pass
    finally:
        mod._render_scene = orig
        scene.render.engine, scene.cycles.samples, scene.eevee.taa_render_samples = saved
        scene.render.use_motion_blur = False
        scene.compositing_node_group = None
        bpy.data.node_groups.remove(tree)
    assert len(seen) == 2, seen
    (e1, cyc, _ee1, mb1), (e2, _cyc2, eev, mb2) = seen
    assert e1 == "CYCLES" and cyc <= 16, "Cycles preview samples = %d" % cyc
    assert e2 == "BLENDER_EEVEE" and eev <= 16, "EEVEE preview samples = %d" % eev
    assert not mb1 and not mb2, "motion blur left on for previews"
    assert scene.cycles.samples == saved[1], "user samples modified"


# --- Quality change re-renders -------------------------------------------- #
def test_quality_change_requeues(mod):
    mat = bpy.data.materials.new("NPV_test_q")
    mat.node_tree.nodes.new("ShaderNodeTexNoise")
    props = bpy.context.scene.npv
    st = mod._state
    old = props.resolution
    try:
        mod.rebuild_queue(mat.node_tree, mod.KIND_SHADER, props)
        for it in st["queue"]:
            st["textures"][it["key"]] = object()
            st["hashes"][it["key"]] = it["hash"]
        st["queue"].clear()
        st["queued_keys"].clear()
        props.resolution = "256" if old != "256" else "64"
        mod.rebuild_queue(mat.node_tree, mod.KIND_SHADER, props)
        assert st["queue"], "changing Quality did not re-queue previews"
    finally:
        props.resolution = old
        for k in ("textures", "hashes", "queue", "queued_keys"):
            st[k].clear()
        bpy.data.materials.remove(mat)


# --- previews render at the user's current frame -------------------------- #
def test_preview_scene_follows_current_frame(mod):
    bpy.context.scene.frame_current = 37
    try:
        scn, _p, _s = mod.ensure_preview_scene(32)
        assert scn.frame_current == 37, "preview scene at frame %d" % scn.frame_current
    finally:
        bpy.context.scene.frame_current = 1
