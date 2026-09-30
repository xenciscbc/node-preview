"""Findings from the 5.2.2 GUI run of 027badf: Auto Update back on catches
up (#39), the cache limit holds right after renders (#40), GN field swatches
don't depend on the object's data, and Edit Mode reuses the fingerprint."""
import bpy


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    mod._reset_cache()
    mod._state["src_hint"] = []
    mod._state["editors"].clear()


def test_auto_update_back_on_marks_dirty(mod):
    props = _props()
    props.auto_update = False
    mod._state["dirty"] = False
    props.auto_update = True
    assert mod._state["dirty"], "turning Auto Update on did not catch up (#39)"


def test_cache_is_pruned_right_after_renders_over_the_limit(mod):
    st = mod._state
    props = _props()
    mat = bpy.data.materials.new("NPV_tl_prune")
    orig = mod.queue._max_textures, mod.queue.process_queue, mod.sources._resolve_active
    mod.queue._max_textures = lambda: 4
    mod.sources._resolve_active = lambda: (None, None, None)
    tree = mat.node_tree

    def render_some(p):
        for i in range(3):
            k = mod.common._skey(tree, "r%d_%d" % (len(st["textures"]), i), None)
            st["textures"][k] = object()
            mod.queue._touch(k)
        return True

    mod.queue.process_queue = render_some
    try:
        _clear(mod)
        st["prune_in"] = 20                 # not due on the 3 s cadence
        for _ in range(3):
            mod._timer()
            assert len(st["textures"]) <= 4, \
                "cache over its limit between prunes: %d (#40)" % len(st["textures"])
    finally:
        mod.queue._max_textures, mod.queue.process_queue, mod.sources._resolve_active = orig
        _clear(mod)
        bpy.data.materials.remove(mat)


def _gn_with_field():
    ng = bpy.data.node_groups.new("NPV_tl_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi, go = ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "Xform"
    noise = ng.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    ng.links.new(gi.outputs[0], tr.inputs[0])
    ng.links.new(tr.outputs[0], go.inputs[0])
    me = bpy.data.meshes.new("NPV_tl_mesh")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    ob = bpy.data.objects.new("NPV_tl_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    return ng, ob, me


def test_mesh_edit_does_not_rerender_field_swatches(mod):
    ng, ob, me = _gn_with_field()
    props = _props()
    props.preview_geometry = True
    try:
        _clear(mod)
        mod._state["src_hint"] = [("OBJ", ob.name)]
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert {it["node"] for it in mod._state["queue"]} >= {"Xform", "Noise"}
        for it in mod._state["queue"]:
            mod._state["textures"][it["key"]] = object()
            mod._state["hashes"][it["key"]] = it["hash"]
        mod._state["queue"].clear()
        mod._state["queued_keys"].clear()
        me.vertices[0].co = (0.0, 0.0, 2.0)
        mod.queue._mark_data_changed(mod._idref(me))
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert [it["node"] for it in mod._state["queue"]] == ["Xform"], \
            [it["node"] for it in mod._state["queue"]]
    finally:
        props.preview_geometry = False
        _clear(mod)
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


def test_edit_mode_reuses_the_fingerprint(mod):
    me = bpy.data.meshes.new("NPV_tl_em")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    try:
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        first = mod.queue._data_sig(me)
        calls = []
        orig = mod.queue._compute_data_sig
        mod.queue._compute_data_sig = lambda d, o=None: calls.append(1) or orig(d, o)
        try:
            mod.queue._mark_data_changed(mod._idref(me))
            # A mesh in Edit Mode (is_editmode) returns the cached value.
            proxy = type("M", (), {"name": me.name, "library": None, "is_editmode": True})()
            assert mod.queue._data_sig(proxy) == first and not calls, \
                "fingerprinted a mesh in Edit Mode"
            assert mod.queue._data_sig(me) is not None and calls, \
                "change not picked up after leaving Edit Mode"
        finally:
            mod.queue._compute_data_sig = orig
    finally:
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        bpy.data.meshes.remove(me)
