"""Third v1.4.0 review: compositor previews with a Video / Multilayer output
or stereoscopy, the isometric cube, and zone nodes' paired_output."""
import bpy
from mathutils import Vector

from npv_testutil import capture_renders, opaque_rgb


def _props():
    return bpy.context.scene.npv


def _comp_scene(name):
    scn = bpy.data.scenes.new(name)
    tree = bpy.data.node_groups.new(name + "_tree", "CompositorNodeTree")
    scn.compositing_node_group = tree
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    rgb = tree.nodes.new("CompositorNodeRGB")
    rgb.name = "RGB"
    go = tree.nodes.new("NodeGroupOutput")
    tree.links.new(rgb.outputs[0], go.inputs[0])
    return scn, tree


def _render_comp(mod, scn):
    paths = []
    orig = mod._png_to_texture
    mod._png_to_texture = lambda p: paths.append(p) or True
    try:
        ok = mod.render_compositor(scn, "RGB", 16, _props())
    finally:
        mod._png_to_texture = orig
    return ok, paths


def test_compositor_preview_with_video_or_multilayer_output(mod):
    im = bpy.types.ImageFormatSettings
    if "media_type" not in im.bl_rna.properties:
        print("  (skipped: no media_type in this Blender)")
        return
    for media in ("VIDEO", "MULTI_LAYER_IMAGE"):
        scn, tree = _comp_scene("NPV_tr3_" + media)
        try:
            scn.render.image_settings.media_type = media
            fmt = scn.render.image_settings.file_format
            ok, paths = _render_comp(mod, scn)
            assert ok and paths and paths[0].endswith(".png"), \
                "compositor preview failed with a %s output" % media
            assert scn.render.image_settings.media_type == media and \
                scn.render.image_settings.file_format == fmt, "user output changed"
        finally:
            bpy.data.scenes.remove(scn)
            bpy.data.node_groups.remove(tree)


def test_compositor_preview_with_stereoscopy(mod):
    scn, tree = _comp_scene("NPV_tr3_stereo")
    try:
        scn.render.use_multiview = True
        scn.render.views_format = "STEREO_3D"
        scn.render.image_settings.views_format = "INDIVIDUAL"
        ok, paths = _render_comp(mod, scn)
        assert ok and paths, "stereoscopy made the compositor preview fail"
        assert scn.render.use_multiview, "user's stereoscopy setting changed"
    finally:
        bpy.data.scenes.remove(scn)
        bpy.data.node_groups.remove(tree)


def test_cube_shows_three_faces(mod):
    mod.ensure_preview_scene(32)
    cube = bpy.data.objects[mod.PREVIEW_CUBE]
    m = cube.rotation_euler.to_matrix()
    facing = [round((m @ Vector(n)).z, 3) for n in
              ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))]
    facing = [z for z in facing if z > 0.05]     # the camera looks down -Z
    assert len(facing) == 3, "cube shows %d faces: %r" % (len(facing), facing)
    assert max(facing) - min(facing) < 0.05, "faces not evenly visible: %r" % facing
    # The render itself: not empty (two faces can take the key light at
    # nearly the same angle, so shades don't count faces reliably).
    mat = bpy.data.materials.new("NPV_tr3_cube")
    props = _props()
    props.shader_shape = "CUBE"
    try:
        out = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial")
        with capture_renders(mod) as shots:
            assert mod.render_shader(mat, out.name, 32, props)
        assert opaque_rgb(shots[0]), "cube render is empty"
    finally:
        props.shader_shape = "SPHERE"
        bpy.data.materials.remove(mat)


def test_zone_output_edits_do_not_rehash_the_input(mod):
    ng = bpy.data.node_groups.new("NPV_tr3_zone", "GeometryNodeTree")
    try:
        zi = ng.nodes.new("GeometryNodeRepeatInput")
        zo = ng.nodes.new("GeometryNodeRepeatOutput")
        zi.pair_with_output(zo)
        assert getattr(zi, "paired_output", None) == zo
        before = mod.upstream_hash(zi, {})
        zo.location.x += 250
        zo.select = not zo.select
        zo.label = "moved"
        zo.width += 40
        assert mod.upstream_hash(zi, {}) == before, \
            "moving / selecting the zone output re-hashed the zone input"
    finally:
        bpy.data.node_groups.remove(ng)
