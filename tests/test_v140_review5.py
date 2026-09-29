"""5.2.2 GUI verification of the fourth review: the zone boundary matches
Blender's (a node that only feeds into a zone is outside it), and an editor
that is forgotten takes its pending renders and the no-editor fallback
with it."""
import bpy


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    mod._reset_cache()
    mod._state["src_hint"] = []
    mod._state["editors"].clear()
    mod._state["active_tree_ptr"] = None
    mod._state["active_kind"] = None
    mod._state["active_path"] = None


def _gn_tree(name):
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi, go = ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "Xform"
    ng.links.new(gi.outputs[0], tr.inputs[0])
    ng.links.new(tr.outputs[0], go.inputs[0])
    return ng


def test_node_feeding_a_zone_and_the_output_is_outside(mod):
    ng = _gn_tree("NPV_tr5_zone")
    try:
        nodes, links = ng.nodes, ng.links
        go = next(n for n in nodes if n.bl_idname == "NodeGroupOutput")
        zi = nodes.new("GeometryNodeRepeatInput")
        zo = nodes.new("GeometryNodeRepeatOutput")
        zi.pair_with_output(zo)
        join_in = nodes.new("GeometryNodeJoinGeometry")
        join_in.name = "JoinIn"
        grid = nodes.new("GeometryNodeMeshGrid")
        grid.name = "Grid"
        join_out = nodes.new("GeometryNodeJoinGeometry")
        join_out.name = "JoinOut"
        links.new(nodes["Xform"].outputs[0], zi.inputs[1])
        links.new(zi.outputs[1], join_in.inputs[0])
        links.new(grid.outputs[0], join_in.inputs[0])
        links.new(join_in.outputs[0], zo.inputs[0])
        links.new(zo.outputs[0], join_out.inputs[0])
        links.new(grid.outputs[0], join_out.inputs[0])
        links.new(join_out.outputs[0], go.inputs[0])
        mod._zone_cache.clear()
        inside = {n.name for n in nodes if mod._in_zone(n)}
        assert "JoinIn" in inside and zi.name in inside, inside
        assert not inside & {"Grid", "JoinOut", "Xform", zo.name}, inside
    finally:
        mod._zone_cache.clear()
        bpy.data.node_groups.remove(ng)


def _item(key):
    return {"kind": 0, "src": "", "src_type": "", "tree": "", "root": "",
            "chain": [], "node": "N", "out": None, "key": key, "hash": 1}


def test_forgotten_editor_drops_its_queue_and_the_fallback(mod):
    class Space:
        def __init__(self, ptr):
            self.ptr = ptr

        def as_pointer(self):
            return self.ptr

    try:
        _clear(mod)
        eds = mod._state["editors"]
        eds[1] = {"tree": 11, "kind": mod.KIND_SHADER, "path": [11], "hint": [],
                  "pinned": False, "ctx": "a", "visible": set(), "priority": set()}
        eds[2] = {"tree": 22, "kind": mod.KIND_SHADER, "path": [22], "hint": [],
                  "pinned": False, "ctx": "b", "visible": set(), "priority": set()}
        mod._state["active_tree_ptr"] = 22
        mod._state["active_kind"] = mod.KIND_SHADER
        mod._state["active_path"] = [22]
        keys = ["11:N#a", "22:N#b"]
        for k in keys:
            mod._state["queue"].append(_item(k))
            mod._state["queued_keys"].add(k)
        mod._forget_editor(Space(1))
        left = [it["key"] for it in mod._state["queue"]]
        assert left == ["22:N#b"], left
        assert mod._state["queued_keys"] == {"22:N#b"}
        assert mod._state["active_tree_ptr"] == 22, "another editor is still open"
        # The last one: its renders go, and the last drawn tree is no
        # fallback target any more (it kept rendering for a hidden editor).
        mod._forget_editor(Space(2))
        assert not mod._state["queue"] and not mod._state["queued_keys"]
        assert mod._state["active_tree_ptr"] is None
        assert mod._state["active_path"] is None
        assert all(t[0] is None for t in mod._editor_targets())
        assert not mod._in_view("22:N#b")
    finally:
        _clear(mod)


def test_pruned_editor_drops_its_queue(mod):
    orig = mod._live_space_ptrs
    try:
        _clear(mod)
        mod._state["editors"][7] = {"tree": 77, "kind": mod.KIND_SHADER, "path": [77],
                                    "hint": [], "pinned": False, "ctx": "",
                                    "visible": set(), "priority": set()}
        mod._state["active_tree_ptr"] = 77
        mod._state["active_kind"] = mod.KIND_SHADER
        mod._state["active_path"] = [77]
        mod._state["queue"].append(_item("77:N#x"))
        mod._state["queued_keys"].add("77:N#x")
        mod._live_space_ptrs = lambda: set()
        mod._prune_editors()
        assert not mod._state["editors"]
        assert not mod._state["queue"] and not mod._state["queued_keys"]
        assert mod._state["active_tree_ptr"] is None
    finally:
        mod._live_space_ptrs = orig
        _clear(mod)
