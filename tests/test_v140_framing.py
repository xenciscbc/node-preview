"""GN previews of an object that is moved, scaled or parented stay framed
(complements test_v140_review::test_geometry_off_origin_is_framed). They also
pass with the earlier ``obj.matrix_world`` framing on Blender 5.2."""
import bpy

from npv_testutil import capture_renders, opaque_rgb


def _props():
    return bpy.context.scene.npv


def _geo_object(name):
    ng = bpy.data.node_groups.new(name + "_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube")
    cube.name = "Cube"
    ng.links.new(cube.outputs["Mesh"], go.inputs[0])
    me = bpy.data.meshes.new(name + "_mesh")
    ob = bpy.data.objects.new(name + "_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    return ob, ng


def _remove_geo(ob, ng):
    me = ob.data
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(me)
    bpy.data.node_groups.remove(ng)


def _coverage(px):
    return len(opaque_rgb(px)) / (len(px) / 4)


def _render_pair(mod, setup):
    """Coverage of a plain object and of one changed by ``setup(ob)``."""
    ob, ng = _geo_object("NPV_test_plain")
    ob2, ng2 = _geo_object("NPV_test_moved")
    extra = setup(ob2)
    bpy.context.view_layer.update()
    try:
        with capture_renders(mod) as shots:
            assert mod.renderers.render_geo(ob, "Cube", 48, _props())
            assert mod.renderers.render_geo(ob2, "Cube", 48, _props())
    finally:
        _remove_geo(ob, ng)
        _remove_geo(ob2, ng2)
        for d in extra or ():
            bpy.data.objects.remove(d)
    return _coverage(shots[0]), _coverage(shots[1])


def _assert_framed(plain, moved, what):
    assert plain > 0.1, "plain render is nearly empty (%.3f)" % plain
    assert 0.5 * plain < moved < 0.97, \
        "%s is badly framed: coverage %.3f vs %.3f" % (what, moved, plain)


def test_geo_preview_of_an_offset_object_is_framed(mod):
    def setup(o):
        o.location = (20.0, -7.0, 4.0)
        o.rotation_euler = (0.6, 0.0, 1.1)
    plain, moved = _render_pair(mod, setup)
    _assert_framed(plain, moved, "offset object")


def test_geo_preview_of_a_scaled_object_is_framed(mod):
    # The copy keeps the object's scale; framing must include it.
    def setup(o):
        o.scale = (6.0, 6.0, 6.0)
    plain, moved = _render_pair(mod, setup)
    _assert_framed(plain, moved, "scaled object")


def test_geo_preview_of_a_parented_object_is_framed(mod):
    # The copy keeps its parent and renders at the parent's transform;
    # framing must follow it.
    def setup(o):
        parent = bpy.data.objects.new("NPV_test_parent", None)
        bpy.context.scene.collection.objects.link(parent)
        parent.location = (15.0, 10.0, -5.0)
        o.parent = parent
        return [parent]
    plain, moved = _render_pair(mod, setup)
    _assert_framed(plain, moved, "parented object")
