"""Preview scene geometry and flat-swatch rendering."""
import bmesh
import bpy

from npv_testutil import capture_renders, color_std


def _uv_names(obj):
    return [l.name for l in obj.data.uv_layers]


def test_preview_meshes_have_uv(mod):
    _scn, plane, sphere = mod.preview_scene.ensure_preview_scene(32)
    assert _uv_names(plane), "preview plane has no UV layer"
    assert _uv_names(sphere), "preview sphere has no UV layer"


def test_stale_preview_mesh_without_uv_is_rebuilt(mod):
    # A plane left over from an older add-on version (saved in a .blend) has
    # no UV layer; ensure_preview_scene must not keep reusing it.
    me = bpy.data.meshes.new(mod.common.PREVIEW_PLANE + "_mesh")
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0)
    bm.to_mesh(me)
    bm.free()
    bpy.data.objects.new(mod.common.PREVIEW_PLANE, me)
    _scn, plane, _sphere = mod.preview_scene.ensure_preview_scene(32)
    assert _uv_names(plane), "stale preview plane without UVs was reused"


def test_image_texture_thumbnail_is_not_flat(mod):
    img = bpy.data.images.new("NPV_test_grid", 256, 256)
    img.generated_type = "COLOR_GRID"
    mat = bpy.data.materials.new("NPV_test_mat")
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    try:
        with capture_renders(mod) as shots:
            mod.renderers.render_shader(mat, tex.name, 64, bpy.context.scene.npv)
    finally:
        bpy.data.materials.remove(mat)
        bpy.data.images.remove(img)
    assert shots, "render_shader did not render"
    std = color_std(shots[0])
    assert std > 0.05, "image texture thumbnail is a flat colour (std=%.4f)" % std
