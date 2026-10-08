"""NPV-07 follow-up: what a preview hashes of an object it reads from another
place (an Object / Collection socket, an Object / Collection input node, a
modifier panel input). The object's data always counts; its transform only
when the reading node uses it."""
import bpy


def _other(name):
    me = bpy.data.meshes.new(name + "_mesh")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def _remove(ob):
    me = ob.data
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(me)


def _tree(name):
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    return ng, go


def _info(ng, go, space="ORIGINAL", use_location=False):
    info = ng.nodes.new("GeometryNodeObjectInfo")
    info.name = "Info"
    info.transform_space = space
    if use_location:
        tr = ng.nodes.new("GeometryNodeTransform")
        ng.links.new(info.outputs["Geometry"], tr.inputs["Geometry"])
        ng.links.new(info.outputs["Location"], tr.inputs["Translation"])
        ng.links.new(tr.outputs["Geometry"], go.inputs[0])
    else:
        ng.links.new(info.outputs["Geometry"], go.inputs[0])
    return info


def _h(mod, node):
    return mod.hashing.upstream_hash(node, {})


def _move(ob):
    ob.location.x += 2.0
    bpy.context.view_layer.update()


def _edit_mesh(mod, ob):
    ob.data.vertices[1].co.x += 3.0
    # As in the UI: the edit is evaluated before the previews re-hash (the
    # mesh's derived data, part of its fingerprint, updates only then).
    bpy.context.view_layer.update()
    mod.queue._mark_data_changed(mod.sources._idref(ob.data))   # as _on_depsgraph does


def _watch(mod):
    w = mod._state["xform_watch"]
    w.clear()
    return w


# --------------------------------------------------------------------------- #
#  Object Info with its Object socket filled in
# --------------------------------------------------------------------------- #
def test_object_info_original_geometry_only_ignores_a_move(mod):
    other = _other("NPV_t143_a")
    ng, go = _tree("NPV_t143_a_gn")
    info = _info(ng, go)
    info.inputs["Object"].default_value = other
    try:
        watch = _watch(mod)
        h0 = _h(mod, info)
        assert other.name not in watch, "Original mode, Geometry only: the move needn't be watched"
        _move(other)
        assert _h(mod, info) == h0, "Original mode, Geometry only: a move re-renders for nothing"
        info.inputs["As Instance"].default_value = True
        h1 = _h(mod, info)
        _move(other)
        assert _h(mod, info) == h1, "Original mode, As Instance: a move re-renders for nothing"
        _edit_mesh(mod, other)
        assert _h(mod, info) != h1, "the other object's mesh edit must still re-render"
    finally:
        _watch(mod)
        bpy.data.node_groups.remove(ng)
        _remove(other)


def test_object_info_relative_follows_a_move(mod):
    other = _other("NPV_t143_b")
    ng, go = _tree("NPV_t143_b_gn")
    info = _info(ng, go, space="RELATIVE")
    info.inputs["Object"].default_value = other
    try:
        watch = _watch(mod)
        h0 = _h(mod, info)
        assert other.name in watch
        _move(other)
        assert _h(mod, info) != h0, "Relative mode: moving the object must re-render"
    finally:
        _watch(mod)
        bpy.data.node_groups.remove(ng)
        _remove(other)


def test_object_info_original_with_location_follows_a_move(mod):
    other = _other("NPV_t143_c")
    ng, go = _tree("NPV_t143_c_gn")
    info = _info(ng, go, use_location=True)
    info.inputs["Object"].default_value = other
    try:
        watch = _watch(mod)
        h0 = _h(mod, info)
        assert other.name in watch
        _move(other)
        assert _h(mod, info) != h0, "Location output used: moving the object must re-render"
    finally:
        _watch(mod)
        bpy.data.node_groups.remove(ng)
        _remove(other)


# --------------------------------------------------------------------------- #
#  Object / Collection input nodes (the ID is a node property)
# --------------------------------------------------------------------------- #
def test_object_input_node_follows_a_mesh_edit(mod):
    other = _other("NPV_t143_d")
    ng, go = _tree("NPV_t143_d_gn")
    info = _info(ng, go)
    src = ng.nodes.new("GeometryNodeInputObject")
    src.object = other
    ng.links.new(src.outputs["Object"], info.inputs["Object"])
    try:
        _watch(mod)
        h0 = _h(mod, info)
        _edit_mesh(mod, other)
        assert _h(mod, info) != h0, \
            "Object input node -> Object Info: editing the object's mesh did not change the hash"
        h1 = _h(mod, info)
        _move(other)
        assert _h(mod, info) == h1, "Original mode, Geometry only: a move re-renders for nothing"
    finally:
        _watch(mod)
        bpy.data.node_groups.remove(ng)
        _remove(other)


def test_object_input_node_relative_follows_a_move(mod):
    other = _other("NPV_t143_e")
    ng, go = _tree("NPV_t143_e_gn")
    info = _info(ng, go, space="RELATIVE")
    src = ng.nodes.new("GeometryNodeInputObject")
    src.object = other
    ng.links.new(src.outputs["Object"], info.inputs["Object"])
    try:
        watch = _watch(mod)
        h0 = _h(mod, info)
        assert other.name in watch, "the move of an object read in Relative mode is not watched"
        _move(other)
        assert _h(mod, info) != h0, \
            "Object input node -> Object Info (Relative): moving the object did not change the hash"
    finally:
        _watch(mod)
        bpy.data.node_groups.remove(ng)
        _remove(other)


def test_collection_input_node_follows_a_member_move(mod):
    other = _other("NPV_t143_f")
    col = bpy.data.collections.new("NPV_t143_f_col")
    col.objects.link(other)
    ng, go = _tree("NPV_t143_f_gn")
    cinfo = ng.nodes.new("GeometryNodeCollectionInfo")
    src = ng.nodes.new("GeometryNodeInputCollection")
    src.collection = col
    ng.links.new(src.outputs[0], cinfo.inputs["Collection"])
    ng.links.new(cinfo.outputs[0], go.inputs[0])
    try:
        _watch(mod)
        h0 = _h(mod, cinfo)
        _move(other)
        assert _h(mod, cinfo) != h0, \
            "Collection input node -> Collection Info: moving a member did not change the hash"
    finally:
        _watch(mod)
        bpy.data.node_groups.remove(ng)
        bpy.data.collections.remove(col)
        _remove(other)


# --------------------------------------------------------------------------- #
#  Collection Info with its Collection socket filled in
# --------------------------------------------------------------------------- #
def test_collection_info_follows_a_member_move(mod):
    other = _other("NPV_t143_g")
    col = bpy.data.collections.new("NPV_t143_g_col")
    col.objects.link(other)
    ng, go = _tree("NPV_t143_g_gn")
    cinfo = ng.nodes.new("GeometryNodeCollectionInfo")
    cinfo.inputs["Collection"].default_value = col
    ng.links.new(cinfo.outputs[0], go.inputs[0])
    try:
        watch = _watch(mod)
        h0 = _h(mod, cinfo)
        assert other.name in watch
        _move(other)
        assert _h(mod, cinfo) != h0, "Collection Info: moving a member must re-render"
    finally:
        _watch(mod)
        bpy.data.node_groups.remove(ng)
        bpy.data.collections.remove(col)
        _remove(other)


# --------------------------------------------------------------------------- #
#  An object passed in on the modifier panel
# --------------------------------------------------------------------------- #
def _modifier_setup(name, space):
    other = _other(name + "_other")
    ng, go = _tree(name + "_gn")
    item = ng.interface.new_socket("Obj", in_out="INPUT", socket_type="NodeSocketObject")
    gi = ng.nodes.new("NodeGroupInput")
    info = _info(ng, go, space=space)
    ng.links.new(gi.outputs[item.identifier], info.inputs["Object"])
    me = bpy.data.meshes.new(name + "_mesh")
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    m = ob.modifiers.new("GN", "NODES")
    m.node_group = ng
    getattr(m.properties.inputs, item.identifier).value = other
    return ob, ng, other


def _src_sig(mod, ob, ng):
    return mod.queue._geo_source_sig(mod.sources._idref(ob), ng)


def test_modifier_input_object_follows_a_mesh_edit(mod):
    ob, ng, other = _modifier_setup("NPV_t143_h", "ORIGINAL")
    try:
        watch = _watch(mod)
        s0 = _src_sig(mod, ob, ng)
        _edit_mesh(mod, other)
        assert _src_sig(mod, ob, ng) != s0, \
            "modifier panel object: editing its mesh did not change the hash"
        s1 = _src_sig(mod, ob, ng)
        _move(other)
        assert _src_sig(mod, ob, ng) == s1, "Original mode, Geometry only: a move re-renders for nothing"
        assert other.name not in watch
    finally:
        _watch(mod)
        _remove(ob)
        bpy.data.node_groups.remove(ng)
        _remove(other)


def test_modifier_input_object_relative_follows_a_move(mod):
    ob, ng, other = _modifier_setup("NPV_t143_i", "RELATIVE")
    try:
        watch = _watch(mod)
        s0 = _src_sig(mod, ob, ng)
        assert other.name in watch, "the move of an object read in Relative mode is not watched"
        _move(other)
        assert _src_sig(mod, ob, ng) != s0, \
            "modifier panel object read in Relative mode: moving it did not change the hash"
    finally:
        _watch(mod)
        _remove(ob)
        bpy.data.node_groups.remove(ng)
        _remove(other)
