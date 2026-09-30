"""5.2.2 GUI notes on 07f1ba6: an editor switched to a non-previewed tree
type gets no extra render before its area redraws (#49), and GN field
swatches don't re-render for upstream edits that can't change them."""
import bpy


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    mod._reset_cache()
    mod._state["src_hint"] = []
    mod._state["editors"].clear()


def test_timer_drops_an_editor_switched_to_another_tree_type(mod):
    st = mod._state
    props = _props()
    orig = mod.timer._live_space_ptrs, mod.timer._live_space_kinds, mod.queue._render_item
    rendered = []
    mod.timer._live_space_ptrs = lambda: {777}
    # Same space pointer, but it now shows a Texture Node tree.
    mod.timer._live_space_kinds = lambda: {777: None}
    mod.queue._render_item = lambda item, res, p: rendered.append(item["key"]) or object()
    try:
        _clear(mod)
        st["editors"][777] = {"tree": 5, "kind": mod.KIND_GEO, "path": [5], "hint": [],
                              "pinned": False, "ctx": "c", "visible": set(),
                              "priority": set()}
        st["queue"][:] = [{"key": "5:n%d|#c" % i, "node": "n%d" % i, "hash": "h",
                           "kind": mod.KIND_GEO} for i in range(5)]
        st["queued_keys"] = {it["key"] for it in st["queue"]}
        st["dirty"] = False
        mod._timer()
        assert not rendered, "rendered %r for an editor that switched away" % rendered
        assert 777 not in st["editors"] and not st["queue"]
    finally:
        mod.timer._live_space_ptrs, mod.timer._live_space_kinds, mod.queue._render_item = orig
        _clear(mod)


def test_editor_switched_to_another_kind_is_forgotten(mod):
    st = mod._state
    orig = mod.timer._live_space_ptrs, mod.timer._live_space_kinds
    mod.timer._live_space_ptrs = lambda: {778, 779}
    mod.timer._live_space_kinds = lambda: {778: mod.KIND_SHADER, 779: mod.KIND_GEO}
    try:
        _clear(mod)
        for k in (778, 779):
            st["editors"][k] = {"tree": k, "kind": mod.KIND_GEO, "path": [k], "hint": [],
                                "pinned": False, "visible": set(), "priority": set()}
        mod.timer._prune_editors()
        assert set(st["editors"]) == {779}, set(st["editors"])
    finally:
        mod.timer._live_space_ptrs, mod.timer._live_space_kinds = orig
        _clear(mod)


def test_upstream_edit_does_not_rerender_a_geo_field_swatch(mod):
    ng = bpy.data.node_groups.new("NPV_tr6_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    n = ng.nodes
    gi, go = n.new("NodeGroupInput"), n.new("NodeGroupOutput")
    noise = n.new("ShaderNodeTexNoise"); noise.name = "Noise"
    math = n.new("ShaderNodeMath"); math.name = "Math"; math.operation = "MULTIPLY"
    comb = n.new("ShaderNodeCombineXYZ"); comb.name = "Comb"
    setp = n.new("GeometryNodeSetPosition"); setp.name = "SP"
    ng.links.new(noise.outputs["Fac"], math.inputs[0])
    ng.links.new(math.outputs[0], comb.inputs["Z"])
    ng.links.new(gi.outputs[0], setp.inputs["Geometry"])
    ng.links.new(comb.outputs[0], setp.inputs["Offset"])
    ng.links.new(setp.outputs[0], go.inputs[0])
    me = bpy.data.meshes.new("NPV_tr6_mesh")
    ob = bpy.data.objects.new("NPV_tr6_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    props = _props()
    props.preview_geometry = True
    try:
        _clear(mod)
        mod._state["src_hint"] = [("OBJ", ob.name)]
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert {"Noise", "Math", "Comb", "SP"} <= {it["node"] for it in mod._state["queue"]}
        for it in mod._state["queue"]:
            mod._state["textures"][it["key"]] = object()
            mod._state["hashes"][it["key"]] = it["hash"]
        mod._state["queue"].clear()
        mod._state["queued_keys"].clear()

        noise.inputs["Scale"].default_value = 9.0
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        again = {it["node"] for it in mod._state["queue"]}
        # Noise's own swatch and the geometry downstream change; Math / Comb
        # render with their own input values and can't.
        assert again == {"Noise", "SP"}, again

        for it in mod._state["queue"]:
            mod._state["textures"][it["key"]] = object()
            mod._state["hashes"][it["key"]] = it["hash"]
        mod._state["queue"].clear()
        mod._state["queued_keys"].clear()
        math.inputs[1].default_value = 0.7        # the swatch's own input
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert {it["node"] for it in mod._state["queue"]} == {"Math", "SP"}
    finally:
        props.preview_geometry = False
        _clear(mod)
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)
