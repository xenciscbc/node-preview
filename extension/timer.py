# SPDX-License-Identifier: GPL-3.0-or-later
"""The timer that works through the queue, and the handlers (depsgraph,
frame change, file load / save) that mark previews out of date."""

import bpy
from bpy.app.handlers import persistent

from . import drawing, queue, sources
from .common import KINDS, _key_ctx, _state, space_kind
from .eligibility import _socket_enum_cache
from .preview_scene import _cleanup_datablocks
from .sources import _idref, _tree_by_pointer


# --------------------------------------------------------------------------- #
#  Timer / depsgraph
# --------------------------------------------------------------------------- #
def _live_space_ptrs():
    """Pointers of every node-editor space in every open window, or None
    when there are no windows (background mode)."""
    wm = bpy.context.window_manager
    if wm is None or not wm.windows:
        return None
    ptrs = set()
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "NODE_EDITOR":
                for sp in area.spaces:
                    if sp.type == "NODE_EDITOR":
                        ptrs.add(sp.as_pointer())
    return ptrs


def _live_space_kinds():
    """Space pointer -> the preview kind it shows now (None for a tree type
    we don't preview), or None when there are no windows."""
    wm = bpy.context.window_manager
    if wm is None or not wm.windows:
        return None
    kinds = {}
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "NODE_EDITOR":
                for sp in area.spaces:
                    if sp.type == "NODE_EDITOR":
                        kinds[sp.as_pointer()] = (space_kind(sp)
                                                  if sp.tree_type in KINDS else None)
    return kinds


def _prune_editors():
    """Forget editors that were closed, switched to another editor type, or
    now show a tree type / preview kind other than the one recorded (the
    space pointer stays the same when only its tree type changes, and the
    draw callback that would forget it may run after the next timer tick)."""
    live = _live_space_ptrs()
    if live is None:
        return
    kinds = _live_space_kinds() or {}
    eds = _state["editors"]
    stale = [k for k, e in eds.items() if k not in live
             or (k in kinds and kinds[k] != e.get("kind"))]
    gone = [eds.pop(k) for k in stale]
    if gone:
        drawing._editors_gone(gone)


def _in_view(key):
    """Is this thumbnail one an open editor is showing right now (its tree
    in the context that editor shows)? Those are never evicted. Thumbnails of
    the same tree for another source (another object sharing a GN tree ...)
    age out like any other. With no editor recorded, the last drawn tree's."""
    try:
        ptr = int(key.split(":", 1)[0])
    except ValueError:
        return False
    eds = _state["editors"]
    if not eds:
        return ptr == _state["active_tree_ptr"]
    kc = _key_ctx(key)
    return any(e.get("tree") == ptr and e.get("ctx", kc) in (kc, "")
               for e in eds.values())


def _editor_targets():
    """(tree, kind, path, source hint) for every editor showing previews.
    With no editor recorded (e.g. background mode) the last drawn one --
    cleared once the last recorded editor is forgotten (_editors_gone)."""
    _prune_editors()
    props = queue._get_props()
    eds = [e for e in _state["editors"].values()
           if props is None or sources._kind_enabled(e["kind"], props)]
    # Editors on the same tree through different sources (pinned to another
    # object, a group entered from another material) each get their own
    # thumbnails: the source is part of the cache key (_view_ctx).
    out, seen = [], set()
    for e in eds:
        sig = (e["kind"], tuple(e["path"]), repr(e["hint"]))
        if sig in seen:
            continue
        path = [_tree_by_pointer(p) for p in e["path"]]
        if not path or any(t is None for t in path):
            continue
        seen.add(sig)
        out.append((path[-1], e["kind"], path, e["hint"]))
    if not out:
        tree, kind, path = sources._resolve_active()
        out.append((tree, kind, path, _state["src_hint"]))
    return out


def _animation_playing():
    try:
        return any(w.screen is not None and w.screen.is_animation_playing
                   for w in bpy.context.window_manager.windows)
    except Exception:
        return False


def _timer():
    props = queue._get_props()
    if props is None or not props.enabled:
        _state["timer_running"] = False
        return None
    # Renders run on the UI thread and would stall playback; pending work
    # (dirty flag, queue) simply waits until it stops -- unless the user
    # asked for previews to follow the frame.
    if not getattr(props, "update_on_frame", False) and _animation_playing():
        return 0.25
    shown = len(_state["textures"])
    if _state["dirty"] and props.auto_update:
        _state["dirty"] = False
        saved_hint = _state["src_hint"]
        try:
            for tree, kind, path, hint in _editor_targets():
                if tree is not None and sources._kind_enabled(kind, props):
                    _state["src_hint"] = hint
                    queue.rebuild_queue(tree, kind, props, force=False, path=path)
        finally:
            _state["src_hint"] = saved_hint
    # Every tick, before rendering: an editor switched away from previews
    # must not get one more render before its area redraws.
    _prune_editors()
    queue._drop_disallowed(props)
    rendered = queue.process_queue(props)
    _state["prune_in"] -= 1
    # Prune on the ~3 s cadence, and right away once new renders push the
    # cache over its limit (switching quickly between objects would overshoot
    # it for seconds otherwise).
    if _state["prune_in"] <= 0 or (rendered and len(_state["textures"]) > queue._max_textures()):
        _state["prune_in"] = 20          # ~every 3 s
        queue._prune_cache()
    # Redraw after new renders, and after thumbnails were dropped (filtered
    # out, node deleted, cache pruned) so they don't linger on screen.
    if rendered or len(_state["textures"]) < shown:
        queue._tag_node_editors()
    return 0.15


def _ensure_timer():
    # Trust Blender's own registry, not our cached flag: a non-persistent
    # timer is silently dropped on every file load, which would otherwise
    # leave `timer_running` stuck True forever with no timer actually running.
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.1, persistent=True)
    _state["timer_running"] = True


_DATA_ID_TYPES = {"MESH", "CURVE", "CURVES", "POINTCLOUD", "VOLUME", "LATTICE",
                  "META", "FONT", "GREASEPENCIL", "GREASEPENCIL_V3"}


@persistent
def _on_depsgraph(scene, depsgraph):
    if _state["rendering"]:
        return
    # Which object data changed is recorded even while previews are off or
    # Auto Update is off: the fingerprints are cached (_data_sig), and an
    # edit made meanwhile must not be missed once previews come back.
    for upd in depsgraph.updates:
        idt = getattr(upd.id, "id_type", "")
        try:
            if idt in _DATA_ID_TYPES:
                queue._mark_data_changed(_idref(getattr(upd.id, "original", upd.id)))
            elif idt == "KEY":
                # A shape key slider: the Key's user is the mesh / curve.
                user = getattr(getattr(upd.id, "original", upd.id), "user", None)
                if user is not None:
                    queue._mark_data_changed(_idref(user))
            elif idt == "OBJECT" and upd.is_updated_geometry:
                # Sculpting, or a script's foreach_set + update_tag(), may tag
                # only the object: its data may have changed too.
                data = getattr(getattr(upd.id, "original", upd.id), "data", None)
                if data is not None:
                    queue._mark_data_changed(_idref(data))
        except Exception:
            if idt in _DATA_ID_TYPES:
                queue._mark_data_changed(getattr(upd.id, "name", ""))
    props = getattr(scene, "npv", None)
    if props is None or not props.enabled or not props.auto_update:
        return
    for upd in depsgraph.updates:
        idt = getattr(upd.id, "id_type", "")
        if idt == "IMAGE":
            # Texture paint sends Image updates; the counter is part of the
            # image's hash so textures using it re-render.
            name = upd.id.name
            _state["img_gen"][name] = _state["img_gen"].get(name, 0) + 1
            _state["dirty"] = True
        elif idt in _DATA_ID_TYPES or idt == "KEY":
            # An object's own data may have changed (recorded above): re-hash;
            # its content is part of its GN previews' hash (_data_sig).
            _state["dirty"] = True
        elif idt == "OBJECT":
            # Moving / rotating an object changes nothing a preview shows
            # (geometry previews render a copy at the origin), and would
            # otherwise re-hash the whole tree every tick during a drag.
            if upd.is_updated_transform and not upd.is_updated_geometry \
                    and not upd.is_updated_shading:
                continue
            _state["dirty"] = True
        elif idt in {"MATERIAL", "NODETREE", "WORLD", "SCENE", "LIGHT"}:
            # SCENE: e.g. a render engine switch (part of the hash).
            _state["dirty"] = True


@persistent
def _on_frame_change(scene, depsgraph=None):
    """With 'Update on Frame Change' the frame is part of every hash, so
    time-dependent nodes (Scene Time, image sequences, ...) re-render."""
    props = getattr(scene, "npv", None)
    if props is not None and props.enabled and props.auto_update \
            and getattr(props, "update_on_frame", False):
        _state["dirty"] = True


@persistent
def _on_load_post(_filepath):
    """Reset after a .blend load: every cached tree/node pointer and GPU
    texture belongs to the old file. The preview timer itself is persistent
    and survives the load; _ensure_timer() here is just a safety net in case
    it is gone for any other reason."""
    queue._reset_cache()
    _socket_enum_cache.clear()
    _state["sel_sig"] = None
    _state["src_hint"] = []
    _state["editors"].clear()
    _state["active_tree_ptr"] = None
    _state["active_kind"] = None
    _state["active_path"] = None
    _state["rendering"] = False
    _state["timer_running"] = False
    _state["dirty"] = True
    # Files saved by versions before 1.1.7 carry the preview scene; drop it so
    # it doesn't show up in the scene list (rebuilt on demand).
    _cleanup_datablocks()
    _ensure_timer()


@persistent
def _on_save_pre(_filepath):
    """Keep the preview scene / objects out of the user's .blend. They are
    rebuilt on the next preview render."""
    _cleanup_datablocks()
