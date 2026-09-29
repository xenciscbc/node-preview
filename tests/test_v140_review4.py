"""Fourth v1.4.0 review + 5.2.2 GUI note: earlier GN modifiers' trees, shape
keys, topology-only edits, editors that stop showing previews, and nodes
inside zones (black previews)."""
import bpy


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    mod._reset_cache()
    mod._state["src_hint"] = []
    mod._state["editors"].clear()


def _gn_tree(name, with_input=True):
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi, go = ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "Xform"
    ng.links.new(gi.outputs[0], tr.inputs[0])
    ng.links.new(tr.outputs[0], go.inputs[0])
    return ng


def _mesh_obj(name):
    me = bpy.data.meshes.new(name + "_mesh")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)], [],
                   [(0, 1, 3, 2)])
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return me, ob


def _remove(ob, *trees):
    me = ob.data
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(me)
    for t in trees:
        bpy.data.node_groups.remove(t)


# 1. Earlier GN modifier's tree
def test_editing_an_earlier_modifiers_tree_changes_the_hash(mod):
    a, b = _gn_tree("NPV_tr4_A"), _gn_tree("NPV_tr4_B")
    me, ob = _mesh_obj("NPV_tr4_obj")
    ob.modifiers.new("A", "NODES").node_group = a
    ob.modifiers.new("B", "NODES").node_group = b
    try:
        _clear(mod)
        before = mod._geo_source_sig(ob.name, b)
        a.nodes["Xform"].inputs["Scale"].default_value = (2.0, 2.0, 2.0)
        assert mod._geo_source_sig(ob.name, b) != before, \
            "editing modifier A's tree did not change B's previews"
        # ... but B's own tree is not part of it (only the edited node and
        # its downstream re-render there).
        before = mod._geo_source_sig(ob.name, b)
        b.nodes["Xform"].inputs["Scale"].default_value = (3.0, 3.0, 3.0)
        assert mod._geo_source_sig(ob.name, b) == before
    finally:
        _clear(mod)
        _remove(ob, a, b)


# 2. Shape keys
def test_shape_key_value_and_shape_change_the_fingerprint(mod):
    me, ob = _mesh_obj("NPV_tr4_sk")
    try:
        ob.shape_key_add(name="Basis")
        kb = ob.shape_key_add(name="Up")
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        ref = mod._idref(me)
        s0 = mod._data_sig(me, ob)
        kb.value = 0.6
        mod._mark_data_changed(ref)
        s1 = mod._data_sig(me, ob)
        assert s1 != s0, "shape key value not seen"
        kb.data[0].co = (0.0, 0.0, 1.0)
        mod._mark_data_changed(ref)
        assert mod._data_sig(me, ob) != s1, "editing a non-Basis key not seen"
        # The update Blender sends for a slider names the Key, whose user is
        # the mesh.
        key = me.shape_keys
        before = mod._state["data_gen"].get(ref, 0)
        upd = type("U", (), {"id": key, "is_updated_transform": False,
                             "is_updated_geometry": False, "is_updated_shading": False})()
        mod._on_depsgraph(bpy.context.scene, type("DG", (), {"updates": (upd,)})())
        assert mod._state["data_gen"].get(ref, 0) > before, "Key update not mapped to its mesh"
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


# 3. Topology-only edits
def test_topology_only_edit_changes_the_fingerprint(mod):
    me, ob = _mesh_obj("NPV_tr4_topo")
    try:
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        s0 = mod._data_sig(me, ob)
        me.flip_normals()          # same positions and counts, new winding
        mod._mark_data_changed(mod._idref(me))
        assert mod._data_sig(me, ob) != s0, "Flip Normals not seen"
        s1 = mod._data_sig(me, ob)
        me.vertices[0].select = not me.vertices[0].select
        mod._mark_data_changed(mod._idref(me))
        assert mod._data_sig(me, ob) == s1, "selection changed the fingerprint"
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


# 4. Editors that stop showing previews
def test_editor_is_forgotten_when_it_stops_showing_previews(mod):
    class Space:
        type, tree_type, shader_type, edit_tree, path = \
            "NODE_EDITOR", "TextureNodeTree", "OBJECT", None, ()

        def as_pointer(self):
            return 4242

    class Ctx:
        space_data, scene = Space(), bpy.context.scene

    saved = mod.bpy
    try:
        _clear(mod)
        mod._state["editors"][4242] = {"tree": 1, "kind": mod.KIND_SHADER, "path": [1],
                                       "hint": [], "pinned": False,
                                       "visible": set(), "priority": set()}
        mod.bpy = type("B", (), {"context": Ctx(), "types": bpy.types, "data": bpy.data})
        mod.draw_callback()
        assert 4242 not in mod._state["editors"], "unsupported tree kept being rebuilt"
        # Preview type switched off: its editors are no rebuild targets.
        mod.bpy = saved
        props = _props()
        mod._state["editors"][4243] = {"tree": 1, "kind": mod.KIND_GEO, "path": [1],
                                       "hint": [], "pinned": False,
                                       "visible": set(), "priority": set()}
        props.preview_geometry = False
        orig = mod._live_space_ptrs
        mod._live_space_ptrs = lambda: None
        try:
            targets = mod._editor_targets()
        finally:
            mod._live_space_ptrs = orig
        assert all(t[1] != mod.KIND_GEO for t in targets), targets
    finally:
        mod.bpy = saved
        _clear(mod)


# 5. Zones
def test_nodes_inside_a_zone_get_no_preview(mod):
    ng = _gn_tree("NPV_tr4_zone")
    props = _props()
    props.preview_geometry = True
    me, ob = _mesh_obj("NPV_tr4_zobj")
    ob.modifiers.new("GN", "NODES").node_group = ng
    try:
        nodes, links = ng.nodes, ng.links
        go = next(n for n in nodes if n.bl_idname == "NodeGroupOutput")
        zi = nodes.new("GeometryNodeRepeatInput")
        zo = nodes.new("GeometryNodeRepeatOutput")
        zi.pair_with_output(zo)
        inner = nodes.new("GeometryNodeTransform")
        inner.name = "Inner"
        cube = nodes.new("GeometryNodeMeshCube")
        cube.name = "JoinedIn"
        join = nodes.new("GeometryNodeJoinGeometry")
        join.name = "Join"
        after = nodes.new("GeometryNodeTransform")
        after.name = "After"
        links.new(nodes["Xform"].outputs[0], zi.inputs[1])
        links.new(zi.outputs[1], inner.inputs[0])
        links.new(inner.outputs[0], join.inputs[0])
        links.new(cube.outputs[0], join.inputs[0])
        links.new(join.outputs[0], zo.inputs[0])
        links.new(zo.outputs[0], after.inputs[0])
        links.new(after.outputs[0], go.inputs[0])
        mod._zone_cache.clear()
        inside = {n.name for n in nodes if mod._in_zone(n)}
        assert {zi.name, "Inner", "Join"} <= inside, inside
        # A node that only feeds into the zone is outside it (Blender's zone
        # frame doesn't include it; it can be wired to the Group Output).
        assert not inside & {"Xform", zo.name, "After", "JoinedIn"}, inside

        _clear(mod)
        mod._state["src_hint"] = [("OBJ", ob.name)]
        mod.rebuild_queue(ng, mod.KIND_GEO, props)
        queued = {it["node"] for it in mod._state["queue"]}
        assert {"Xform", zo.name, "After", "JoinedIn"} <= queued, queued
        assert not queued & inside, "nodes inside the zone queued: %r" % (queued & inside)
        assert mod.export_job(ng, mod.KIND_GEO, props, nodes["Inner"]) is None
    finally:
        props.preview_geometry = False
        _clear(mod)
        _remove(ob, ng)
