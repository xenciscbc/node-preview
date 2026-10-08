# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw the thumbnails over the node editor, and record which editors show
which trees."""

import bpy
import gpu
import blf
from gpu_extras.batch import batch_for_shader

from . import queue, sources, timer
from .common import KINDS, _key_ctx, _skey, _state, _view_ctx, space_kind
from .eligibility import _preview_targets, _zone_cache, node_eligible
from .sources import _idref, resolve_source
from .renderers import _instance_chain


# --------------------------------------------------------------------------- #
#  Drawing
# --------------------------------------------------------------------------- #
def _image_shader():
    sh = _state.get("shader_image")
    if sh is None:
        sh = gpu.shader.from_builtin("IMAGE")
        _state["shader_image"] = sh
    return sh


def _draw_tex(tex, x0, y0, x1, y1):
    sh = _image_shader()
    pos = ((x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1))
    uv = ((0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1))
    batch = batch_for_shader(sh, "TRIS", {"pos": pos, "texCoord": uv})
    sh.bind()
    sh.uniform_sampler("image", tex)
    batch.draw(sh)


def _color_shader():
    sh = _state.get("shader_color")
    if sh is None:
        sh = gpu.shader.from_builtin("UNIFORM_COLOR")
        _state["shader_color"] = sh
    return sh


def _draw_tris(color, pos):
    if not pos:
        return
    sh = _color_shader()
    batch = batch_for_shader(sh, "TRIS", {"pos": pos})
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)


def _draw_rect(color, x0, y0, x1, y1):
    _draw_tris(color, ((x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)))


def _draw_border(color, x0, y0, x1, y1, width=1.0):
    sh = _color_shader()
    pos = ((x0, y0), (x1, y0), (x1, y0), (x1, y1),
           (x1, y1), (x0, y1), (x0, y1), (x0, y0))
    batch = batch_for_shader(sh, "LINES", {"pos": pos})
    gpu.state.line_width_set(width)
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)
    gpu.state.line_width_set(1.0)


def _checker_tris(x0, y0, x1, y1, size):
    """Triangles of the light squares of a checkerboard filling the rect."""
    pos = []
    if size <= 0:
        return pos
    j, y = 0, y0
    while y < y1:
        ya = min(y + size, y1)
        i, x = 0, x0
        while x < x1:
            xa = min(x + size, x1)
            if (i + j) % 2 == 0:
                pos += ((x, y), (xa, y), (xa, ya), (x, y), (xa, ya), (x, ya))
            x += size
            i += 1
        y += size
        j += 1
    return pos


def _blf_size(font, size):
    try:
        blf.size(font, size)
    except TypeError:
        blf.size(font, size, 72)


def _fit_text(font, text, limit):
    if blf.dimensions(font, text)[0] > limit:
        while text and blf.dimensions(font, text + "…")[0] > limit:
            text = text[:-1]
        text = (text + "…") if text else ""
    return text


def _draw_text(text, x, y, ps, size=11, color=(1.0, 1.0, 1.0, 1.0)):
    font = 0
    _blf_size(font, round(size * ps))
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(font, round(1 * ps), round(-1 * ps))
    blf.position(font, x, y, 0.0)
    blf.color(font, *color)
    blf.draw(font, text)
    blf.disable(font, blf.SHADOW)


def _draw_label(text, x, y, maxw, ps=1.0):
    """Small socket name shown on a cell (side-by-side mode). Truncated with an
    ellipsis to fit the cell width. ``ps`` is the UI pixel size so the font and
    padding scale with HiDPI / UI resolution scale."""
    _blf_size(0, round(11 * ps))
    text = _fit_text(0, text, max(0.0, maxw - 6.0 * ps))
    if text:
        _draw_text(text, x, y, ps)


def _draw_centered(text, cx0, cy0, cx1, cy1, ps, size=11, color=(1, 1, 1, 1)):
    _blf_size(0, round(size * ps))
    text = _fit_text(0, text, max(0.0, (cx1 - cx0) - 6.0 * ps))
    if not text:
        return
    tw, th = blf.dimensions(0, text)
    _draw_text(text, (cx0 + cx1 - tw) / 2.0, (cy0 + cy1 - th) / 2.0, ps, size, color)


def format_value(v):
    """Number drawn on a Value swatch: short, no '-0'."""
    if abs(v) < 5e-7:
        v = 0.0
    txt = "%.4g" % v
    return txt


COL_QUEUED = (1.0, 0.62, 0.15, 1.0)   # stale: waiting to re-render
COL_FAILED = (0.95, 0.2, 0.2, 1.0)    # the last render failed


def _grid_origin(pos, x0, x1, y0, node_h, gw, gh, gap):
    """Bottom-left corner of a preview grid of size gw x gh placed at
    ``pos`` relative to a node whose region rect is x0..x1 wide, top y0."""
    if pos == "BELOW":
        return x0 + (x1 - x0 - gw) / 2.0, y0 - node_h - gap - gh
    if pos == "LEFT":
        return x0 - gap - gw, y0 - gh
    if pos == "RIGHT":
        return x1 + gap, y0 - gh
    return x0 + (x1 - x0 - gw) / 2.0, y0 + gap        # ABOVE


def _editor_hint(ctx, space):
    """Which material / light / object (compositor: scene) the editor shows
    (its id / id_from and, unless it is pinned, the active object), so the
    queue previews through it (see resolve_source)."""
    objs = [getattr(space, "id", None), getattr(space, "id_from", None)]
    if not getattr(space, "pin", False):
        objs.append(getattr(ctx, "active_object", None))
    hint = []
    for d in objs:
        # _idref: linked data keeps its library, so a linked material "Foo"
        # doesn't resolve to a local "Foo" (or to nothing).
        if isinstance(d, bpy.types.Material):
            hint.append(("MAT", _idref(d)))
        elif isinstance(d, bpy.types.Light):
            hint.append(("LIGHT", _idref(d)))
        elif isinstance(d, bpy.types.Object):
            hint.append(("OBJ", _idref(d)))
            if isinstance(d.data, bpy.types.Light):
                hint.append(("LIGHT", _idref(d.data)))
    # A compositor tree can be shared by several scenes (Scene.copy() shares
    # it): preview it through the window's scene, not the first one using it.
    if getattr(space, "tree_type", None) == "CompositorNodeTree":
        scene = getattr(ctx, "scene", None)
        if scene is not None:
            hint.append(("SCENE", _idref(scene)))
    return list(dict.fromkeys(hint))


def _record_editor(ctx, space, ptr, kind, path, props, tree):
    """Store this editor's view; mark previews dirty only when *this*
    editor's own view changed (so two editors redrawing in turn no longer
    re-queue each other on every redraw)."""
    try:
        sptr = space.as_pointer()
    except Exception:
        sptr = id(space)
    pinned = bool(getattr(space, "pin", False))
    hint = _editor_hint(ctx, space)
    sel = None
    if getattr(props, "preview_scope", "ALL") == "SELECTED":
        # A selection change has no depsgraph update: watch it here.
        sel = tuple(sorted(n.name for n in tree.nodes if n.select))
    eds = _state["editors"]
    old = eds.get(sptr)
    new_view = (ptr, kind, path, hint, pinned, sel)
    if old is None or old["view"] != new_view:
        _state["dirty"] = True
    left = dict(old) if old is not None and old.get("tree") != ptr else None
    ent = old or {"visible": set(), "priority": set()}
    ent.update({"view": new_view, "tree": ptr, "kind": kind, "path": path,
                "hint": hint, "pinned": pinned})
    eds[sptr] = ent
    if left is not None:
        # The editor switched to another tree (another material, object or
        # scene): the one it showed is gone from it, like a closed editor.
        _editors_gone([left])
    # The last drawn editor, for code / tests that look at one editor.
    _state["active_tree_ptr"] = ptr
    _state["active_kind"] = kind
    _state["active_path"] = path
    if not pinned:
        _state["src_hint"] = hint
    return ent


def _editor_view_ctx(space, tree, kind, hint):
    """The _view_ctx this editor's thumbnails are cached under -- the same
    one rebuild_queue computes for its view (source through this editor's
    hint, group-node chain through its path)."""
    trees = [p.node_tree for p in getattr(space, "path", ()) if p.node_tree is not None]
    if not trees or trees[-1] != tree:
        trees = [tree]
    chain = _instance_chain(trees)
    if chain is None:
        return ""
    saved = _state["src_hint"]
    _state["src_hint"] = hint
    try:
        src = resolve_source(trees[0], kind)
    finally:
        _state["src_hint"] = saved
    return _view_ctx(src, chain) if src is not None else ""


def _forget_editor(space):
    """This editor shows no previews (any more): stop rebuilding / protecting
    the tree it showed before."""
    try:
        ent = _state["editors"].pop(space.as_pointer(), None)
    except Exception:
        return
    if ent is not None:
        _editors_gone([ent])


def _editors_gone(gone):
    """Editors ``gone`` were forgotten: drop their pending renders that no
    remaining editor shows, and once no editor is left, the last drawn tree
    too -- the no-editor fallback (_editor_targets, _in_view) would otherwise
    keep rebuilding and protecting the tree they showed."""
    eds = _state["editors"]
    if not eds:
        _state["active_tree_ptr"] = None
        _state["active_kind"] = None
        _state["active_path"] = None
    trees = {e.get("tree") for e in gone}
    shown = {(e.get("tree"), e.get("ctx")) for e in eds.values()}

    def orphan(k):
        try:
            ptr = int(k.split(":", 1)[0])
        except ValueError:
            return False
        if ptr not in trees:
            return False
        kc = _key_ctx(k)
        return not any(t == ptr and c in (kc, "", None) for t, c in shown)

    q = _state["queue"]
    drop = [it for it in q if orphan(it["key"])]
    if drop:
        q[:] = [it for it in q if not orphan(it["key"])]
        _state["queued_keys"].difference_update(it["key"] for it in drop)


def draw_callback():
    ctx = bpy.context
    space = ctx.space_data
    if space is None or space.type != "NODE_EDITOR":
        return
    if space.tree_type not in KINDS:
        _forget_editor(space)
        return
    kind = space_kind(space)
    props = getattr(ctx.scene, "npv", None)
    if props is None or not props.enabled or not sources._kind_enabled(kind, props):
        _forget_editor(space)
        return
    tree = getattr(space, "edit_tree", None)
    if tree is None:
        _forget_editor(space)
        return
    _zone_cache.clear()

    ptr = tree.as_pointer()
    # Tree path of the editor (outermost first): entering a node group adds
    # the group's tree; the previews inside need the path back to the
    # material / world / modifier.
    path = [p.node_tree.as_pointer() for p in space.path if p.node_tree is not None]
    if not path or path[-1] != ptr:
        path = [ptr]
    # A new or changed view (other tree / editor type / path / source /
    # selection) re-queues once, so the editor auto-refreshes on switch.
    ent = _record_editor(ctx, space, ptr, kind, path, props, tree)
    timer._ensure_timer()
    vctx = _editor_view_ctx(space, tree, kind, ent["hint"])
    ent["ctx"] = vctx

    region = ctx.region
    v2d = region.view2d
    # UI-scale correction. The node editor draws every node at
    # ``location * ui_scale`` in view2d space (and ``node.dimensions`` is
    # already in those scaled view units), while ``node.location`` itself is in
    # unscaled node units. view_to_region() and this POST_PIXEL handler share
    # the same region-pixel space, so no framebuffer/pixel_size factor is
    # involved -- but node coordinates must be multiplied by ui_scale BEFORE
    # view_to_region(), or previews land at 1/ui_scale of the node position
    # (offset grows with distance from the view origin): the reported bug on
    # HiDPI / scaled-UI machines. NOTE: read ui_scale inside the draw callback;
    # outside a window draw context it can report stale values.
    ps = ctx.preferences.system.ui_scale
    # The same factor keeps fixed decorations (gap, padding, borders, label)
    # proportional to Blender's own UI at any scale.
    gap = 6.0 * ps
    pad = 2.0 * ps                 # backdrop / outer-border padding
    bw = 1.0                       # border line width: intentionally NOT
                                   # scaled -- a hairline border looks right at
                                   # any Resolution Scale; scaling it reads as
                                   # too thick.
    rw, rh = region.width, region.height
    scale = max(0.1, float(getattr(props, "thumb_scale", 1.0)))
    where = getattr(props, "thumb_position", "ABOVE")
    status = getattr(props, "show_status", True)
    checker = getattr(props, "checker_bg", False)
    show_values = getattr(props, "show_values", True)
    zoom = float(getattr(props, "zoom_factor", 2.5)) \
        if getattr(props, "zoom_active", False) else 1.0
    active = tree.nodes.active
    textures, queued = _state["textures"], _state["queued_keys"]
    failed, hashes, values = _state["failed"], _state["hashes"], _state["values"]
    visible, priority = set(), set()
    jobs = []
    for node in tree.nodes:
        if not node_eligible(node, kind, props):
            continue
        keys = [(oid, _skey(tree, node.name, oid, vctx))
                for oid in _preview_targets(node, kind, props)]
        if node.select or node == active:
            priority.update(k for _o, k in keys)
        loc = node.location_absolute
        # Scale node-space coords by ui_scale BEFORE view_to_region (see note).
        x0, y0 = v2d.view_to_region(loc.x * ps, loc.y * ps, clip=False)
        x1, _ = v2d.view_to_region((loc.x + node.width) * ps, loc.y * ps,
                                   clip=False)
        w = x1 - x0
        dims = node.dimensions
        node_h = w * dims.y / dims.x if dims.x > 0 else 0.0
        z = zoom if node == active else 1.0
        gw = w * scale * z             # grid width (== node width at 1x)

        def layout(n):
            # Single big swatch for one preview, otherwise 2 per row and wrap
            # to further rows (cell = half the grid width, stays legible).
            cols = 1 if n == 1 else 2
            cw = gw / cols
            gh = ((n + cols - 1) // cols) * cw
            gx0, gy0 = _grid_origin(where, x0, x1, y0, node_h, gw, gh, gap)
            return cols, cw, gh, gx0, gy0

        cols, cw, gh, gx0, gy0 = layout(len(keys))
        # Cull: skip nodes whose node and preview rects are both off screen.
        lo_x, hi_x = min(x0, gx0), max(x1, gx0 + gw)
        lo_y, hi_y = min(y0 - node_h, gy0), max(y0, gy0 + gh)
        if hi_x < 0 or lo_x > rw or hi_y < 0 or lo_y > rh:
            continue
        visible.update(k for _o, k in keys)
        if w < 10:
            continue
        cells = []
        for oid, k in keys:
            t = textures.get(k)
            st = None
            if status:
                if k in failed and (t is None or failed[k] != hashes.get(k)):
                    st = "FAILED"
                elif k in queued:
                    st = "QUEUED"
            if t is not None:
                queue._touch(k)
            if t is not None or st is not None:
                cells.append((oid, k, t, st))
        if cells:
            cols, cw, gh, gx0, gy0 = layout(len(cells))
            jobs.append((node == active and z != 1.0, node, cells, cols,
                         gx0, gy0, gw, gh, cw))
    # The queue serves every editor: render order uses all of their sets.
    ent["visible"], ent["priority"] = visible, priority
    vis, pri = set(), set()
    for e in _state["editors"].values():
        vis |= e["visible"]
        pri |= e["priority"]
    _state["visible"], _state["priority"] = vis, pri

    gpu.state.blend_set("ALPHA")
    # The enlarged active node last, so it sits on top of its neighbours.
    for _top, node, cells, cols, gx0, gy0, gw, gh, cw in sorted(
            jobs, key=lambda j: j[0]):
        n = len(cells)
        # One dark backdrop + outer border for the whole grid.
        _draw_rect((0.05, 0.05, 0.05, 0.85), gx0 - pad, gy0 - pad, gx0 + gw + pad, gy0 + gh + pad)
        oname = {s.identifier: (s.name or s.identifier) for s in node.outputs}
        for i, (oid, k, tex, st) in enumerate(cells):
            col = i % cols
            row_from_top = i // cols
            cx0 = gx0 + col * cw
            cx1 = cx0 + cw
            cy1 = gy0 + gh - row_from_top * cw     # top of this cell
            cy0 = cy1 - cw                         # bottom of this cell
            if tex is not None:
                if checker:
                    _draw_rect((0.22, 0.22, 0.22, 1.0), cx0, cy0, cx1, cy1)
                    _draw_tris((0.36, 0.36, 0.36, 1.0),
                               _checker_tris(cx0, cy0, cx1, cy1, 8.0 * ps))
                _draw_tex(tex, cx0, cy0, cx1, cy1)
                v = values.get(k) if show_values else None
                if v is not None and cw >= 28 * ps:
                    _draw_centered(format_value(v), cx0, cy0, cx1, cy1, ps,
                                   size=12 if cw >= 60 * ps else 10)
            elif st == "QUEUED":
                _draw_centered("…", cx0, cy0, cx1, cy1, ps, size=14,
                               color=COL_QUEUED)
            elif st == "FAILED":
                _draw_centered("!", cx0, cy0, cx1, cy1, ps, size=16,
                               color=COL_FAILED)
            if st is not None:
                _draw_border(COL_QUEUED if st == "QUEUED" else COL_FAILED,
                             cx0 + 0.5, cy0 + 0.5, cx1 - 0.5, cy1 - 0.5, bw)
            elif n > 1:
                _draw_border((0.0, 0.0, 0.0, 1.0), cx0, cy0, cx1, cy1, bw)
            if n > 1 and oid and cw >= 40 * ps:
                _draw_label(oname.get(oid, oid), cx0 + 3 * ps, cy0 + 3 * ps, cw, ps)
        _draw_border((0.0, 0.0, 0.0, 1.0), gx0 - pad, gy0 - pad, gx0 + gw + pad, gy0 + gh + pad, bw)
    gpu.state.blend_set("NONE")
