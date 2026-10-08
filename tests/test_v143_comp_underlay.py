"""The dark backdrop under a thumbnail is opaque: Blender draws its own node
preview (Render Layers' eye toggle, on by default) at the same place first,
and a see-through backdrop let it show through the transparent edges of a
compositor thumbnail as a second, smaller image. GPU stubbed: background
mode can't draw."""
import bpy


class _Rec:
    """Swallows any call / attribute access; blf.dimensions() gives a size."""
    def __init__(self, name=""):
        self._name = name

    def __getattr__(self, attr):
        return _Rec(attr)

    def __call__(self, *a, **k):
        return (10.0, 10.0) if self._name == "dimensions" else _Rec(self._name + "()")


def _draw_once(mod, nt, mat, checker):
    """Run draw_callback for an editor showing ``nt``; returns the colours of
    every filled rect and every triangle batch drawn."""
    rects, tris = [], []

    class V2D:
        def view_to_region(self, x, y, clip=False):
            return x, y

    class Region:
        width, height, view2d = 4000, 4000, V2D()

    class Space:
        type, tree_type, shader_type = "NODE_EDITOR", mod.common.KIND_SHADER, "OBJECT"
        edit_tree, path, id, id_from = nt, [], mat, None

    class Ctx:
        space_data, region, scene, active_object = Space(), Region(), bpy.context.scene, None
        preferences = type("P", (), {"system": type("S", (), {"ui_scale": 1.0})()})()

    class Bpy:
        types, data, app, path, props, utils = (bpy.types, bpy.data, bpy.app,
                                                bpy.path, bpy.props, bpy.utils)
        context = Ctx()

    names = ("bpy", "gpu", "blf", "batch_for_shader", "_draw_rect", "_draw_tris")
    saved = {k: getattr(mod.drawing, k) for k in names}
    props = bpy.context.scene.npv
    props.checker_bg = checker
    try:
        mod.drawing.bpy, mod.drawing.gpu, mod.drawing.blf = Bpy, _Rec(), _Rec()
        mod.drawing.batch_for_shader = _Rec()
        mod.drawing._draw_rect = lambda color, *a: rects.append(tuple(color))
        mod.drawing._draw_tris = lambda color, *a: tris.append(tuple(color))
        mod.draw_callback()
    finally:
        for k, v in saved.items():
            setattr(mod.drawing, k, v)
        props.checker_bg = False
    return rects, tris


def test_thumbnail_backdrop_is_opaque(mod):
    mat = bpy.data.materials.new("NPV_t143_under")
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    noise.location = (0.0, 200.0)
    props = bpy.context.scene.npv
    st = mod._state
    try:
        for k in ("textures", "hashes", "failed", "queue", "queued_keys", "values"):
            st[k].clear()
        mod.queue.rebuild_queue(nt, mod.common.KIND_SHADER, props)
        key = next(it["key"] for it in st["queue"] if it["node"] == "Noise")
        st["textures"][key] = object()          # "rendered"
        st["hashes"][key] = "h"
        for checker in (False, True):
            rects, tris = _draw_once(mod, nt, mat, checker)
            assert rects, "no backdrop drawn"
            see_through = [c for c in rects if c[3] < 1.0]
            assert not see_through, \
                "backdrop not opaque (checker %s): %r -- Blender's own node " \
                "preview shows through" % (checker, see_through)
            if checker:
                assert tris, "checkerboard squares not drawn over the backdrop"
    finally:
        for k in ("textures", "hashes", "failed", "queue", "queued_keys", "values"):
            st[k].clear()
        st["editors"].clear()
        bpy.data.materials.remove(mat)
