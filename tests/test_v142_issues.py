"""Regression tests for the issues in tests/open_issues.md (NPV-01 .. NPV-11;
NPV-12 is in test_build_version.py). Each failed on 1.4.1."""
import types

import bpy
from mathutils import Euler

from npv_testutil import capture_renders, opaque_rgb


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    st = mod._state
    for k in ("textures", "hashes", "failed", "queue", "queued_keys", "values"):
        st[k].clear()


def _pretend_rendered(mod):
    """Every queued item rendered with its hash; the queue is empty."""
    st = mod._state
    for it in st["queue"]:
        st["textures"][it["key"]] = object()
        st["hashes"][it["key"]] = it["hash"]
    st["queue"].clear()
    st["queued_keys"].clear()


def _queued_nodes(mod):
    return {it["node"] for it in mod._state["queue"]}


def _queued_outs(mod, node):
    return {it["out"] for it in mod._state["queue"] if it["node"] == node}


def _gn_object(name, build):
    """Temp object (empty mesh) with a GN modifier; ``build(ng, go)`` fills
    the tree. Returns (object, tree)."""
    ng = bpy.data.node_groups.new(name + "_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    build(ng, go)
    me = bpy.data.meshes.new(name + "_mesh")
    ob = bpy.data.objects.new(name + "_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    return ob, ng


def _remove_gn(ob, ng):
    me = ob.data
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(me)
    bpy.data.node_groups.remove(ng)


def _cone(ng, go):
    cone = ng.nodes.new("GeometryNodeMeshCone")
    cone.name = "Cone"
    ng.links.new(cone.outputs["Mesh"], go.inputs[0])


# --------------------------------------------------------------------------- #
#  NPV-01 / NPV-02: the preview lights follow the previewed output
# --------------------------------------------------------------------------- #
def _color_bsdf_group():
    grp = bpy.data.node_groups.new("NPV_t_npv01_grp", "ShaderNodeTree")
    grp.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    grp.interface.new_socket("BSDF", in_out="OUTPUT", socket_type="NodeSocketShader")
    gout = grp.nodes.new("NodeGroupOutput")
    rgb = grp.nodes.new("ShaderNodeRGB")
    bsdf = grp.nodes.new("ShaderNodeBsdfDiffuse")
    grp.links.new(rgb.outputs[0], gout.inputs["Color"])
    grp.links.new(bsdf.outputs[0], gout.inputs["BSDF"])
    return grp


def _group_material(grp, link_color=False):
    mat = bpy.data.materials.new("NPV_t_npv01")
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    g = nt.nodes.new("ShaderNodeGroup")
    g.name = "G"
    g.node_tree = grp
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(g.outputs["BSDF"], out.inputs["Surface"])
    if link_color:
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        nt.links.new(g.outputs["Color"], mix.inputs["A"])
    return mat


def test_npv01_light_sig_follows_previewed_socket(mod):
    grp = _color_bsdf_group()
    mat = _group_material(grp)
    nt = mat.node_tree
    props = _props()
    old_shape = props.shader_shape
    try:
        _clear(mod)
        mod.queue.rebuild_queue(nt, mod.common.KIND_SHADER, props)
        assert "G" in _queued_nodes(mod)
        _pretend_rendered(mod)
        props.shader_shape = "CUBE" if old_shape != "CUBE" else "SPHERE"
        mod.queue.rebuild_queue(nt, mod.common.KIND_SHADER, props)
        assert "G" in _queued_nodes(mod), \
            "shape change did not re-queue a node previewed through its shader output"
    finally:
        props.shader_shape = old_shape
        _clear(mod)
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(grp)


def test_npv01_show_all_outputs_relights_only_the_shader_output(mod):
    grp = _color_bsdf_group()
    mat = _group_material(grp, link_color=True)
    nt = mat.node_tree
    props = _props()
    old = props.sun_strength
    props.show_all_outputs = True
    try:
        _clear(mod)
        mod.queue.rebuild_queue(nt, mod.common.KIND_SHADER, props)
        g = nt.nodes["G"]
        color_id, bsdf_id = g.outputs["Color"].identifier, g.outputs["BSDF"].identifier
        assert _queued_outs(mod, "G") == {color_id, bsdf_id}, _queued_outs(mod, "G")
        _pretend_rendered(mod)
        props.sun_strength = old + 3.0
        mod.queue.rebuild_queue(nt, mod.common.KIND_SHADER, props)
        assert _queued_outs(mod, "G") == {bsdf_id}, \
            "Key Light change should re-queue the lit BSDF output only: %r" % _queued_outs(mod, "G")
    finally:
        props.sun_strength = old
        props.show_all_outputs = False
        _clear(mod)
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(grp)


def test_npv02_world_volume_follows_key_light(mod):
    world = bpy.data.worlds.new("NPV_t_npv02")
    world.use_nodes = True
    nt = world.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    vol = nt.nodes.new("ShaderNodeVolumeScatter")
    vol.name = "Vol"
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.name = "Bg"
    wout = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(vol.outputs[0], wout.inputs["Volume"])
    nt.links.new(bg.outputs[0], wout.inputs["Surface"])
    props = _props()
    old = props.sun_strength
    try:
        _clear(mod)
        mod.queue.rebuild_queue(nt, mod.common.KIND_WORLD, props)
        assert {"Vol", "Bg"} <= _queued_nodes(mod)
        _pretend_rendered(mod)
        props.sun_strength = old + 3.0
        mod.queue.rebuild_queue(nt, mod.common.KIND_WORLD, props)
        assert "Vol" in _queued_nodes(mod), "Key Light change did not re-queue the volume"
        assert "Bg" not in _queued_nodes(mod), "Key Light change re-queued a background"
    finally:
        props.sun_strength = old
        _clear(mod)
        bpy.data.worlds.remove(world)


# --------------------------------------------------------------------------- #
#  NPV-03: image updates are counted while previews are off
# --------------------------------------------------------------------------- #
def test_npv03_image_updates_counted_while_auto_update_off(mod):
    img = bpy.data.images.new("NPV_t_npv03", 8, 8)
    props = _props()
    upd = types.SimpleNamespace(id=img, is_updated_geometry=False,
                                is_updated_transform=False, is_updated_shading=True)
    dg = types.SimpleNamespace(updates=[upd])
    st = mod._state
    try:
        for attr in ("auto_update", "enabled"):
            setattr(props, attr, False)
            st["dirty"] = False
            before = st["img_gen"].get(img.name, 0)
            mod.timer._on_depsgraph(bpy.context.scene, dg)
            assert st["img_gen"].get(img.name, 0) == before + 1, \
                "image update while %s is off was not counted" % attr
            assert not st["dirty"], "previews are off: nothing should be re-hashed yet"
            setattr(props, attr, True)
    finally:
        props.auto_update = True
        props.enabled = True
        st["img_gen"].pop(img.name, None)
        bpy.data.images.remove(img)


# --------------------------------------------------------------------------- #
#  NPV-04: an editor switching tree drops the old tree's pending renders
# --------------------------------------------------------------------------- #
def test_npv04_switching_tree_drops_old_pending_renders(mod):
    def _mat(name):
        m = bpy.data.materials.new(name)
        noise = m.node_tree.nodes.new("ShaderNodeTexNoise")
        noise.name = "Noise"
        return m

    def _space(ptr, mat):
        return type("Space", (), {"pin": False, "id": mat, "id_from": None,
                                  "as_pointer": lambda self: ptr})()

    a, b = _mat("NPV_t_npv04_a"), _mat("NPV_t_npv04_b")
    ctx = type("Ctx", (), {"active_object": None})()
    props = _props()
    st = mod._state
    K = mod.common.KIND_SHADER
    ta, tb = a.node_tree, b.node_tree
    pa = "%d:" % ta.as_pointer()
    try:
        _clear(mod)
        st["editors"].clear()
        one = _space(4242, a)
        mod.drawing._record_editor(ctx, one, ta.as_pointer(), K, [ta.as_pointer()], props, ta)
        mod.queue.rebuild_queue(ta, K, props)
        assert any(it["key"].startswith(pa) for it in st["queue"])
        # The editor now shows material b.
        one.id = b
        mod.drawing._record_editor(ctx, one, tb.as_pointer(), K, [tb.as_pointer()], props, tb)
        mod.queue.rebuild_queue(tb, K, props)
        left = [it["key"] for it in st["queue"] if it["key"].startswith(pa)]
        assert not left, "renders of the tree the editor left are still queued: %r" % left

        # With a second editor still showing a, they stay.
        _clear(mod)
        st["editors"].clear()
        one.id = a
        two = _space(4343, a)
        for sp in (one, two):
            mod.drawing._record_editor(ctx, sp, ta.as_pointer(), K, [ta.as_pointer()], props, ta)
        mod.queue.rebuild_queue(ta, K, props)
        one.id = b
        mod.drawing._record_editor(ctx, one, tb.as_pointer(), K, [tb.as_pointer()], props, tb)
        assert any(it["key"].startswith(pa) for it in st["queue"]), \
            "another editor still shows the tree: its pending renders must stay"
    finally:
        st["editors"].clear()
        _clear(mod)
        bpy.data.materials.remove(a)
        bpy.data.materials.remove(b)


# --------------------------------------------------------------------------- #
#  NPV-05: back to the shown state dequeues
# --------------------------------------------------------------------------- #
def test_npv05_undo_to_shown_hash_dequeues(mod):
    mat = bpy.data.materials.new("NPV_t_npv05")
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    props = _props()
    st = mod._state
    K = mod.common.KIND_SHADER
    try:
        _clear(mod)
        mod.queue.rebuild_queue(nt, K, props)
        _pretend_rendered(mod)
        scale = noise.inputs["Scale"]
        scale.default_value += 1.0          # edit -> queued with hash B
        mod.queue.rebuild_queue(nt, K, props)
        assert "Noise" in _queued_nodes(mod)
        scale.default_value -= 1.0          # undo -> back to the shown hash A
        mod.queue.rebuild_queue(nt, K, props)
        assert "Noise" not in _queued_nodes(mod), \
            "node still queued (with the undone hash) after returning to the shown state"
        assert st["queued_keys"] == {it["key"] for it in st["queue"]}
    finally:
        _clear(mod)
        bpy.data.materials.remove(mat)


# --------------------------------------------------------------------------- #
#  NPV-06: link mute
# --------------------------------------------------------------------------- #
def test_npv06_link_mute_changes_hash(mod):
    mat = bpy.data.materials.new("NPV_t_npv06")
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    link = nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    try:
        h0 = mod.hashing.upstream_hash(ramp, {})
        s0 = mod.hashing.tree_signature(nt)
        link.is_muted = True
        h1 = mod.hashing.upstream_hash(ramp, {})
        s1 = mod.hashing.tree_signature(nt)
        assert h1 != h0, "link mute not in upstream_hash"
        assert s1 != s0, "link mute not in tree_signature"
        # Muted, the input reads its own value.
        ramp.inputs["Fac"].default_value = 0.25
        assert mod.hashing.upstream_hash(ramp, {}) != h1, "muted link's input value not hashed"
        assert mod.hashing.tree_signature(nt) != s1
        link.is_muted = False
        assert mod.hashing.upstream_hash(ramp, {}) == h0, "unmuted: back to the linked hash"
    finally:
        bpy.data.materials.remove(mat)


# --------------------------------------------------------------------------- #
#  NPV-07: what an ID socket points at
# --------------------------------------------------------------------------- #
def test_npv07_gn_image_socket_follows_paint(mod):
    img = bpy.data.images.new("NPV_t_npv07", 8, 8)
    ng = bpy.data.node_groups.new("NPV_t_npv07_gn", "GeometryNodeTree")
    tex = ng.nodes.new("GeometryNodeImageTexture")
    tex.inputs["Image"].default_value = img
    st = mod._state
    try:
        h0 = mod.hashing.upstream_hash(tex, {})
        assert "0x" not in repr(mod.hashing._socket_default(tex.inputs["Image"])), \
            "ID socket hashed by memory address"
        st["img_gen"][img.name] = st["img_gen"].get(img.name, 0) + 1   # a paint stroke
        assert mod.hashing.upstream_hash(tex, {}) != h0, \
            "paint on an image in a GN Image socket did not change the hash"
    finally:
        st["img_gen"].pop(img.name, None)
        bpy.data.node_groups.remove(ng)
        bpy.data.images.remove(img)


def test_npv07_object_info_follows_the_other_object(mod):
    me2 = bpy.data.meshes.new("NPV_t_npv07_other_mesh")
    me2.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    other = bpy.data.objects.new("NPV_t_npv07_other", me2)
    bpy.context.scene.collection.objects.link(other)

    def build(ng, go):
        info = ng.nodes.new("GeometryNodeObjectInfo")
        info.name = "Info"
        info.inputs["Object"].default_value = other
        ng.links.new(info.outputs["Geometry"], go.inputs[0])

    ob, ng = _gn_object("NPV_t_npv07", build)
    st = mod._state
    try:
        info = ng.nodes["Info"]
        h0 = mod.hashing.upstream_hash(info, {})
        assert other.name in st["xform_watch"], "the other object's moves are not watched"
        me2.vertices[1].co.x = 3.0          # edit the other object's mesh
        mod.queue._mark_data_changed(mod.sources._idref(me2))   # as _on_depsgraph does
        h1 = mod.hashing.upstream_hash(info, {})
        assert h1 != h0, "editing the object an Object Info node reads did not change the hash"
        other.location.x += 2.0
        bpy.context.view_layer.update()
        assert mod.hashing.upstream_hash(info, {}) != h1, \
            "moving the object an Object Info node reads did not change the hash"
        # A move of a watched object re-hashes; any other move doesn't.
        mv = lambda o: types.SimpleNamespace(updates=[types.SimpleNamespace(
            id=o, is_updated_geometry=False, is_updated_transform=True,
            is_updated_shading=False)])
        st["dirty"] = False
        mod.timer._on_depsgraph(bpy.context.scene, mv(ob))
        assert not st["dirty"], "moving a plain GN object re-hashed"
        mod.timer._on_depsgraph(bpy.context.scene, mv(other))
        assert st["dirty"], "moving the object Object Info reads did not re-hash"
    finally:
        st["xform_watch"].discard(other.name)
        _remove_gn(ob, ng)
        bpy.data.objects.remove(other)
        bpy.data.meshes.remove(me2)


# --------------------------------------------------------------------------- #
#  NPV-08: GN previews have a neutral transform (unless the tree reads it)
# --------------------------------------------------------------------------- #
def _render(mod, ob, node):
    bpy.context.view_layer.update()
    with capture_renders(mod) as shots:
        assert mod.renderers.render_geo(ob, node, 48, _props())
    return shots[0]


def test_npv08_geo_preview_ignores_rotation_mode(mod):
    ob, ng = _gn_object("NPV_t_npv08", _cone)
    try:
        base = _render(mod, ob, "Cone")
        ob.rotation_mode = "QUATERNION"
        ob.rotation_quaternion = (0.7071068, 0.7071068, 0.0, 0.0)  # 90 deg about X
        assert _render(mod, ob, "Cone") == base, "quaternion rotation changed the geometry preview"
    finally:
        _remove_gn(ob, ng)


def test_npv08_geo_preview_ignores_scale_parent_constraints(mod):
    ob, ng = _gn_object("NPV_t_npv08b", _cone)
    empty = bpy.data.objects.new("NPV_t_npv08b_empty", None)
    bpy.context.scene.collection.objects.link(empty)
    empty.rotation_euler = Euler((1.0, 0.4, 0.0))
    try:
        base = _render(mod, ob, "Cone")
        cases = [
            ("non-uniform scale", lambda: setattr(ob, "scale", (1, 1, 3)),
             lambda: setattr(ob, "scale", (1, 1, 1))),
            ("delta rotation", lambda: setattr(ob, "delta_rotation_euler", (1.2, 0, 0)),
             lambda: setattr(ob, "delta_rotation_euler", (0, 0, 0))),
            ("axis-angle", lambda: (setattr(ob, "rotation_mode", "AXIS_ANGLE"),
                                    setattr(ob, "rotation_axis_angle", (1.2, 1, 0, 0))),
             lambda: setattr(ob, "rotation_mode", "XYZ")),
            ("rotated parent", lambda: setattr(ob, "parent", empty),
             lambda: setattr(ob, "parent", None)),
        ]
        for what, do, undo in cases:
            do()
            try:
                assert _render(mod, ob, "Cone") == base, "%s changed the geometry preview" % what
            finally:
                undo()
        c = ob.constraints.new("COPY_ROTATION")
        c.target = empty
        try:
            assert _render(mod, ob, "Cone") == base, "a rotating constraint changed the preview"
            assert not c.mute, "the user's constraint was changed"
        finally:
            ob.constraints.remove(c)
        assert ob.parent is None and tuple(ob.scale) == (1, 1, 1)
    finally:
        _remove_gn(ob, ng)
        bpy.data.objects.remove(empty)


def test_npv08_tree_reading_self_transform_keeps_it(mod):
    def build(ng, go):
        me = ng.nodes.new("GeometryNodeSelfObject")
        info = ng.nodes.new("GeometryNodeObjectInfo")
        info.transform_space = "RELATIVE"
        ng.links.new(me.outputs[0], info.inputs["Object"])
        tr = ng.nodes.new("GeometryNodeTransform")
        tr.name = "Moved"
        cone = ng.nodes.new("GeometryNodeMeshCone")
        ng.links.new(cone.outputs["Mesh"], tr.inputs["Geometry"])
        ng.links.new(info.outputs["Location"], tr.inputs["Translation"])
        ng.links.new(tr.outputs["Geometry"], go.inputs[0])

    ob, ng = _gn_object("NPV_t_npv08c", build)
    plain, png = _gn_object("NPV_t_npv08d", _cone)
    props = _props()
    props.preview_geometry = True
    st = mod._state
    K = mod.common.KIND_GEO
    try:
        assert mod.renderers.reads_object_transform(ng)
        assert not mod.renderers.reads_object_transform(png)
        for tree, obj, node, expect in ((ng, ob, "Moved", True), (png, plain, "Cone", False)):
            _clear(mod)
            st["src_hint"] = [("OBJ", obj.name)]
            mod.queue.rebuild_queue(tree, K, props)
            assert node in _queued_nodes(mod)
            _pretend_rendered(mod)
            obj.location.x += 2.0
            bpy.context.view_layer.update()
            mod.queue.rebuild_queue(tree, K, props)
            assert (node in _queued_nodes(mod)) == expect, \
                "%s: moving the object %s re-queue it" % (
                    tree.name, "did not" if expect else "should not")
        assert ob.name in st["xform_watch"] and plain.name not in st["xform_watch"]
    finally:
        props.preview_geometry = False
        st["src_hint"] = []
        st["xform_watch"].clear()
        _clear(mod)
        _remove_gn(ob, ng)
        _remove_gn(plain, png)


# --------------------------------------------------------------------------- #
#  NPV-09: group interface settings
# --------------------------------------------------------------------------- #
def test_npv09_interface_settings_in_signature(mod):
    ng = bpy.data.node_groups.new("NPV_t_npv09", "GeometryNodeTree")
    vec = ng.interface.new_socket("Offset", in_out="INPUT", socket_type="NodeSocketVector")
    flt = ng.interface.new_socket("Amount", in_out="INPUT", socket_type="NodeSocketFloat")
    try:
        s0 = mod.hashing.tree_signature(ng)
        vec.default_input = "POSITION"
        s1 = mod.hashing.tree_signature(ng)
        assert s1 != s0, "Default Input change not in tree_signature"
        flt.max_value = 0.5
        s2 = mod.hashing.tree_signature(ng)
        assert s2 != s1, "interface max not in tree_signature"
        flt.name = "Renamed"
        flt.description = "Only a tooltip"
        assert mod.hashing.tree_signature(ng) == s2, "renaming a socket changed the signature"
    finally:
        bpy.data.node_groups.remove(ng)


# --------------------------------------------------------------------------- #
#  NPV-10: viewport visibility of the modifiers
# --------------------------------------------------------------------------- #
def test_npv10_geo_preview_uses_viewport_visibility(mod):
    def build(ng, go):
        cube = ng.nodes.new("GeometryNodeMeshCube")
        cube.name = "Cube"
        ng.links.new(cube.outputs["Mesh"], go.inputs[0])

    ob, ng = _gn_object("NPV_t_npv10", build)
    m = ob.modifiers[0]
    m.show_render = False
    try:
        px = _render(mod, ob, "Cube")
        assert len(opaque_rgb(px)) / (len(px) / 4) > 0.05, \
            "preview is empty: the GN modifier was skipped because its Render toggle is off"
        assert m.show_render is False, "the user's modifier was changed"
    finally:
        _remove_gn(ob, ng)


# --------------------------------------------------------------------------- #
#  NPV-11: a compositor tree shared by two scenes
# --------------------------------------------------------------------------- #
def test_npv11_shared_comp_tree_prefers_the_window_scene(mod):
    tree = bpy.data.node_groups.new("NPV_t_npv11", "CompositorNodeTree")
    a = bpy.data.scenes.new("NPV_t_npv11_A")
    b = bpy.data.scenes.new("NPV_t_npv11_B")
    a.compositing_node_group = tree
    b.compositing_node_group = tree
    st = mod._state
    saved = st["src_hint"]
    space = type("Space", (), {"pin": False, "id": None, "id_from": None,
                               "tree_type": "CompositorNodeTree"})()
    try:
        for scene in (b, a):
            ctx = type("Ctx", (), {"active_object": None, "scene": scene})()
            st["src_hint"] = mod.drawing._editor_hint(ctx, space)
            assert mod.sources.resolve_source(tree, mod.common.KIND_COMP) == \
                ("SCENE", scene.name), "previews a scene the window doesn't show"
    finally:
        st["src_hint"] = saved
        bpy.data.scenes.remove(a)
        bpy.data.scenes.remove(b)
        bpy.data.node_groups.remove(tree)
