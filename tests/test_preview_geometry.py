"""Preview scene geometry and flat-swatch rendering."""
import bmesh
import bpy


def _uv_names(obj):
    return [l.name for l in obj.data.uv_layers]


def _pixel_stats(path):
    """Mean per-channel std-dev of the opaque pixels of a rendered PNG."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        px = img.pixels[:]
    finally:
        bpy.data.images.remove(img)
    rgb = [px[i:i + 3] for i in range(0, len(px), 4) if px[i + 3] > 0.5]
    assert rgb, "render has no opaque pixels"
    n = len(rgb)
    devs = []
    for c in range(3):
        mean = sum(p[c] for p in rgb) / n
        devs.append((sum((p[c] - mean) ** 2 for p in rgb) / n) ** 0.5)
    return sum(devs) / 3


def test_preview_meshes_have_uv(mod):
    _scn, plane, sphere = mod.ensure_preview_scene(32)
    assert _uv_names(plane), "preview plane has no UV layer"
    assert _uv_names(sphere), "preview sphere has no UV layer"


def test_stale_preview_mesh_without_uv_is_rebuilt(mod):
    # A plane left over from an older add-on version (saved in a .blend) has
    # no UV layer; ensure_preview_scene must not keep reusing it.
    me = bpy.data.meshes.new(mod.PREVIEW_PLANE + "_mesh")
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0)
    bm.to_mesh(me)
    bm.free()
    bpy.data.objects.new(mod.PREVIEW_PLANE, me)
    _scn, plane, _sphere = mod.ensure_preview_scene(32)
    assert _uv_names(plane), "stale preview plane without UVs was reused"


def test_image_texture_thumbnail_is_not_flat(mod):
    img = bpy.data.images.new("NPV_test_grid", 256, 256)
    img.generated_type = "COLOR_GRID"
    mat = bpy.data.materials.new("NPV_test_mat")
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    captured = {}
    orig = mod._png_to_texture
    mod._png_to_texture = lambda path: captured.setdefault("std", _pixel_stats(path))
    try:
        mod.render_shader(mat, tex.name, 64, bpy.context.scene.npv)
    finally:
        mod._png_to_texture = orig
        bpy.data.materials.remove(mat)
        bpy.data.images.remove(img)
    assert "std" in captured, "render_shader did not render"
    assert captured["std"] > 0.05, (
        "image texture thumbnail is a flat colour (std=%.4f)" % captured["std"])
