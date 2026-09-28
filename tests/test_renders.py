"""Render smoke tests per preview kind, plus "don't touch user data" checks."""
import bpy

from npv_testutil import (capture_renders, color_std, datablock_names,
                          mean_rgb, opaque_rgb)


def _props():
    return bpy.context.scene.npv


def test_shader_ball_renders(mod):
    mat = bpy.data.materials.new("NPV_test_ball")
    try:
        out = next(n for n in mat.node_tree.nodes
                   if n.bl_idname == "ShaderNodeOutputMaterial")
        with capture_renders(mod) as shots:
            assert mod.render_shader(mat, out.name, 48, _props())
        assert opaque_rgb(shots[0]), "material ball render is empty"
    finally:
        bpy.data.materials.remove(mat)
    assert bpy.data.materials.get(mod.PREVIEW_MAT_TMP) is None, "temp material leaked"


def test_world_swatch_renders_and_restores_scene(mod):
    world = bpy.data.worlds.new("NPV_test_world")
    nt = world.node_tree
    rgb = nt.nodes.new("ShaderNodeRGB")
    rgb.name = "RGB"
    rgb.outputs[0].default_value = (0.1, 0.8, 0.1, 1.0)
    try:
        scn, plane, sphere = mod.ensure_preview_scene(48)
        cam = scn.camera
        before = (cam.data.type, tuple(cam.location), scn.world.name,
                  scn.render.film_transparent, plane.hide_render, sphere.hide_render)
        with capture_renders(mod) as shots:
            assert mod.render_world(world, "RGB", 48, _props())
        r, g, b = mean_rgb(shots[0])
        assert g > r + 0.2 and g > b + 0.2, "world swatch not green: %r" % ((r, g, b),)
        after = (cam.data.type, tuple(cam.location), scn.world.name,
                 scn.render.film_transparent, plane.hide_render, sphere.hide_render)
        assert after == before, "preview scene not restored: %r -> %r" % (before, after)
        assert bpy.data.worlds.get(mod.PREVIEW_PREV_WORLD) is None, "temp world leaked"
    finally:
        bpy.data.worlds.remove(world)


def _geo_object():
    ng = bpy.data.node_groups.new("NPV_test_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube")
    cube.name = "Cube"
    noise = ng.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    ng.links.new(cube.outputs["Mesh"], go.inputs[0])
    me = bpy.data.meshes.new("NPV_test_gn_mesh")
    ob = bpy.data.objects.new("NPV_test_gn_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    return ob, ng


def _remove_geo(ob, ng):
    me = ob.data
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(me)
    bpy.data.node_groups.remove(ng)


def test_geometry_renders_without_leaks(mod):
    ob, ng = _geo_object()
    try:
        mod.ensure_preview_scene(48)
        snap = datablock_names()
        with capture_renders(mod) as shots:
            assert mod.render_geo(ob, "Cube", 48, _props())
        assert opaque_rgb(shots[0]), "geometry render is empty"
        after = datablock_names()
        # The clay material is a cached helper, created once.
        after["materials"] = [m for m in after["materials"] if m != mod.GEO_CLAY_MAT]
        leaked = {k: sorted(set(after[k]) - set(snap[k])) for k in after
                  if set(after[k]) - set(snap[k])}
        assert not leaked, "render_geometry leaked datablocks: %r" % leaked
    finally:
        _remove_geo(ob, ng)


def test_geometry_clay_render_is_shaded(mod):
    # Regression: generated geometry skipped the clay material and rendered
    # as a flat, fully clipped white silhouette.
    ob, ng = _geo_object()
    try:
        with capture_renders(mod) as shots:
            assert mod.render_geo(ob, "Cube", 64, _props())
    finally:
        _remove_geo(ob, ng)
    lum = sorted(sum(p) / 3 for p in opaque_rgb(shots[0]))
    n = len(lum)
    clipped = sum(1 for v in lum if v >= 0.99) / n
    spread = lum[9 * n // 10] - lum[n // 10]
    assert clipped < 0.05, "clay render is overexposed (%.0f%% clipped)" % (clipped * 100)
    assert spread > 0.15, "clay render has no shading (p90-p10 = %.3f)" % spread


def test_geometry_field_swatch_has_texture(mod):
    ob, ng = _geo_object()
    try:
        with capture_renders(mod) as shots:
            assert mod.render_geo(ob, "Noise", 48, _props())
        std = color_std(shots[0])
        assert std > 0.02, "noise field swatch is flat (std=%.4f)" % std
    finally:
        _remove_geo(ob, ng)


def _comp_tree(scene):
    tree = bpy.data.node_groups.new("NPV_test_comp", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    go = tree.nodes.new("NodeGroupOutput")
    rgb = tree.nodes.new("CompositorNodeRGB")
    rgb.name = "RGB"
    rgb.outputs[0].default_value = (0.9, 0.1, 0.1, 1.0)
    mix = tree.nodes.new("ShaderNodeMix")
    mix.name = "Mix"
    mix.data_type = "RGBA"
    tree.links.new(rgb.outputs[0], mix.inputs["A"])
    tree.links.new(mix.outputs["Result"], go.inputs[0])
    scene.compositing_node_group = tree
    return tree


def _tree_state(tree):
    return (sorted(n.name for n in tree.nodes),
            sorted((l.from_node.name, l.from_socket.identifier,
                    l.to_node.name, l.to_socket.identifier) for l in tree.links))


def test_compositor_renders_on_a_copy(mod):
    scene = bpy.context.scene
    tree = _comp_tree(scene)
    r = scene.render
    try:
        settings = (r.resolution_x, r.resolution_y, r.resolution_percentage,
                    r.engine, r.use_compositing, r.film_transparent)
        tstate = _tree_state(tree)
        snap = datablock_names()
        with capture_renders(mod) as shots:
            assert mod.render_compositor(scene, "RGB", 32, _props())
        cr, cg, cb = mean_rgb(shots[0])
        assert cr > cg + 0.3 and cr > cb + 0.3, "compositor swatch not red: %r" % ((cr, cg, cb),)

        assert scene.compositing_node_group == tree, "user compositor tree swapped"
        assert _tree_state(tree) == tstate, "user compositor tree modified"
        assert (r.resolution_x, r.resolution_y, r.resolution_percentage,
                r.engine, r.use_compositing, r.film_transparent) == settings, \
            "user render settings modified"
        assert datablock_names() == snap, "render_compositor leaked or added datablocks"
    finally:
        scene.compositing_node_group = None
        bpy.data.node_groups.remove(tree)


def test_cleanup_removes_all_preview_datablocks(mod):
    mod.ensure_preview_scene(32)
    mod._cleanup_datablocks()
    names = datablock_names()
    leftover = [n for coll in names.values() for n in coll if n.startswith("NPV_")]
    assert not leftover, "left after cleanup: %r" % leftover
