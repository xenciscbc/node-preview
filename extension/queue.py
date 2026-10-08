# SPDX-License-Identifier: GPL-3.0-or-later
"""The render queue: which nodes need a new preview (content hashes of the
node, its inputs and the data it shows), in what order, and the GPU
texture cache."""

import time
import hashlib

import bpy
import numpy as np

from . import sources, timer
from .common import (
    KIND_COMP, KIND_GEO, KIND_SHADER, KIND_WORLD, MAX_TEXTURES, VOLUME_NODES,
    _engine_id, _key_ctx, _skey, _state, _view_ctx,
)
from .eligibility import (
    _preview_targets, _zone_cache, node_eligible, renders_as_shader,
)
from .hashing import (
    _STRUCT_SKIP, _isolated_hash, _plain, _simple_props_sig, tree_signature,
    upstream_hash,
)
from .sources import _idget, _idref, resolve_source
from .renderers import (
    _context_sig, _geo_modifier_index, _instance_chain, reads_object_transform,
    render_compositor, render_geo, render_shader, render_world,
)


# --------------------------------------------------------------------------- #
#  Queue
# --------------------------------------------------------------------------- #
def _get_props():
    return getattr(bpy.context.scene, "npv", None)


def _light_sig(props):
    return "%s|%.4f|%.4f|%s" % (props.shader_shape, props.world_strength,
                                props.sun_strength,
                                getattr(props, "preview_env", "UNIFORM"))


def _dequeue(key):
    if key in _state["queued_keys"]:
        _state["queue"][:] = [it for it in _state["queue"] if it["key"] != key]
        _state["queued_keys"].discard(key)


def _enqueue(kind, src, tree, node_name, out_id, key, h, force, root=None,
             chain=None):
    pending = None
    if key in _state["queued_keys"]:
        pending = next((it for it in _state["queue"] if it["key"] == key), None)
    # A render Refresh queued is never dropped by a plain rebuild (one runs
    # whenever something sets dirty, e.g. each compositor render), only kept
    # current: it skips the two early returns below and gets the hash of the
    # state it will render.
    keep = force or (pending is not None and pending.get("force"))
    if not keep and _state["hashes"].get(key) == h and key in _state["textures"]:
        # Back to what the thumbnail shows (e.g. undo after a failing edit):
        # a failure recorded for another hash no longer applies, and a render
        # still queued for the undone state is not needed.
        _state["failed"].pop(key, None)
        _dequeue(key)
        return
    # A render that failed is not retried until something it depends on
    # changes (or Refresh forces it): otherwise every edit anywhere in the
    # tree would re-run it -- a whole scene render for a compositor node.
    if not keep and _state["failed"].get(key) == h:
        _dequeue(key)
        return
    if pending is not None:
        # Still waiting: render it with what the hash now describes (the
        # source may have changed, e.g. another object made active).
        pending.update({"hash": h, "src": src[1], "src_type": src[0],
                        "root": _idref(root or tree),
                        "chain": list(chain or ())})
        if force:
            pending["force"] = True
        return
    _state["queue"].append({"kind": kind, "src": src[1], "src_type": src[0],
                            "tree": _idref(tree),
                            "root": _idref(root or tree),
                            "chain": list(chain or ()),
                            "node": node_name, "out": out_id, "key": key,
                            "hash": h, "force": bool(force)})
    _state["queued_keys"].add(key)


_MOD_UI_PROPS = {"name", "is_active", "is_override_data", "use_pin_to_last",
                 "show_expanded", "show_in_editmode", "show_on_cage"}


def _is_ui_prop(pid):
    return (pid in _MOD_UI_PROPS or pid.startswith("open_") or pid == "is_open"
            or pid == "panels"
            or (pid.startswith("show_") and pid not in ("show_viewport", "show_render")))


def _nested_sig(struct, depth=2):
    """Plain settings of the non-ID structs and collection items hanging off
    ``struct`` (``depth`` levels), minus UI state. Blender 5.2 no longer keeps
    Geometry Nodes modifier inputs as ID properties; walking the RNA catches
    them wherever the running version exposes them."""
    out = []
    for p in struct.bl_rna.properties:
        pid = p.identifier
        if pid in _STRUCT_SKIP or _is_ui_prop(pid) or p.type not in {"POINTER", "COLLECTION"}:
            continue
        try:
            v = getattr(struct, pid)
        except Exception:
            continue
        if p.type == "POINTER":
            if v is None or isinstance(v, bpy.types.ID):
                continue            # ID pointers: by name in _simple_props_sig
            items = [v]
        else:
            try:
                items = list(v)
            except Exception:
                continue
            if len(items) > 512:
                continue
        sig = []
        for it in items:
            if it is None or isinstance(it, bpy.types.ID):
                sig.append(_plain(it))
                continue
            flat = tuple(kv for kv in _simple_props_sig(it) if not _is_ui_prop(kv[0]))
            sig.append((flat, _nested_sig(it, depth - 1) if depth > 1 else ()))
        out.append((pid, tuple(sig)))
    return tuple(out)


def _modifier_sig(m):
    """A modifier's settings and its inputs, minus panel / UI state. Up to
    Blender 5.1 a Geometry Nodes modifier keeps its input values as ID
    properties; from 5.2 they are RNA data (walked by _nested_sig)."""
    vals = [(pid, v) for pid, v in _simple_props_sig(m) if not _is_ui_prop(pid)]
    try:
        idp = tuple(sorted((k, _plain(m[k])) for k in m.keys()))
    except Exception:           # 5.2+: "id properties not supported"
        idp = ()
    try:
        nested = _nested_sig(m)
    except Exception:
        nested = ()
    return (m.type, tuple(vals), idp, nested, _gn_inputs_sig(m))


def _gn_inputs_sig(m):
    """Blender 5.2+: a Geometry Nodes modifier's input values live at
    ``m.properties.inputs.<socket identifier>`` (value / attribute name /
    input type), one struct per group input."""
    inputs = getattr(getattr(m, "properties", None), "inputs", None)
    ng = getattr(m, "node_group", None)
    if inputs is None or ng is None:
        return ()
    out = []
    for item in ng.interface.items_tree:
        if getattr(item, "item_type", "") != "SOCKET" or item.in_out != "INPUT":
            continue
        v = getattr(inputs, item.identifier, None)
        if v is None:
            continue
        try:
            out.append((item.identifier, _simple_props_sig(v),
                        _plain(getattr(v, "value", None))))
        except Exception:
            pass
    return tuple(out)


def _geo_source_sig(obj_ref, root):
    """What a Geometry Nodes preview depends on outside its node tree: the
    modifier's input values, the modifiers below it in the stack, and the
    object's own data (counted by _on_depsgraph while it is edited)."""
    obj = _idget(bpy.data.objects, obj_ref)
    if obj is None:
        return ""
    idx = _geo_modifier_index(obj, root)
    if idx is None:
        return ""
    mods = list(obj.modifiers)
    parts = [_modifier_sig(m) for m in mods[:idx + 1]]
    # An earlier Geometry Nodes modifier's tree shapes the geometry every
    # node of this one receives: its contents, not just its name.
    for m in mods[:idx]:
        if m.type == 'NODES' and m.node_group is not None:
            parts.append(("tree", m.name, tree_signature(m.node_group)))
    # Vertex groups: the names live on the object (a node reads a group by
    # name), so they are read here, not cached with the mesh.
    parts.append(tuple(g.name for g in obj.vertex_groups))
    parts.append(_data_sig(obj.data, obj))
    # Previews keep the object's placement only for a tree that reads it
    # (render_geometry): then it is part of the hash, and moving the object
    # re-hashes (timer._on_depsgraph).
    if reads_object_transform(root):
        _state["xform_watch"].add(obj.name)
        parts.append(tuple(round(x, 5) for row in obj.matrix_world for x in row))
    return hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()


def _coords_digest(coll, attr, width, dtype=np.float32):
    """md5 of one attribute of every item of an RNA collection."""
    n = len(coll)
    arr = np.empty(n * width, dtype=dtype)
    if n:
        coll.foreach_get(attr, arr)
    return hashlib.md5(arr.tobytes()).hexdigest()


# Attribute data type -> (field read by foreach_get, width, numpy dtype).
_ATTR_FIELDS = {
    "FLOAT": ("value", 1, np.float32), "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int32), "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT_VECTOR": ("vector", 3, np.float32), "FLOAT2": ("vector", 2, np.float32),
    "INT32_2D": ("value", 2, np.int32), "INT16_2D": ("value", 2, np.int32),
    "FLOAT_COLOR": ("color", 4, np.float32), "BYTE_COLOR": ("color", 4, np.float32),
    "QUATERNION": ("value", 4, np.float32), "FLOAT4X4": ("value", 16, np.float32),
}
VGROUP_WEIGHT_LIMIT = 100000   # vertices; above it weights aren't hashed


_UI_ATTR_PREFIXES = (".select", ".hide", ".uv_select", ".vs.", ".es.", ".pn.",
                     ".sculpt_mask")


def _attrs_sig(attrs):
    """Every attribute's name, domain, type and values: positions, UVs,
    colours, custom attributes, and the internal topology arrays
    (.edge_verts, .corner_vert ...: Flip Normals / Rotate Edge change only
    these). Selection, hiding and UV-editor state are skipped: they don't
    change what a preview shows."""
    out = []
    for a in attrs:
        if a.name.startswith(_UI_ATTR_PREFIXES):
            continue
        f = _ATTR_FIELDS.get(a.data_type)
        try:
            dig = _coords_digest(a.data, f[0], f[1], f[2]) if f else len(a.data)
        except Exception:
            dig = len(a.data)
        out.append((a.name, a.domain, a.data_type, dig))
    return tuple(out)


def _vgroups_sig(data, obj):
    """Vertex-group weights (weight paint). They live in the mesh but are not
    attributes, so this is a Python loop: skipped above VGROUP_WEIGHT_LIMIT
    vertices, and deferred while weight painting (see _data_sig). The group
    names are on the object (_geo_source_sig). Only read when the object has
    vertex groups: vertex.groups is not touched otherwise."""
    if obj is None or not len(getattr(obj, "vertex_groups", ())):
        return None
    verts = getattr(data, "vertices", None)
    if verts is None:
        return None
    if len(verts) > VGROUP_WEIGHT_LIMIT:
        return "unhashed"
    h = hashlib.md5()
    for v in verts:
        for g in v.groups:
            h.update(b"%d:%d:%.5f;" % (v.index, g.group, g.weight))
    return h.hexdigest()


def _splines_sig(data):
    out = []
    for sp in data.splines:
        out.append((_simple_props_sig(sp),
                    _coords_digest(sp.bezier_points, "co", 3),
                    _coords_digest(sp.bezier_points, "handle_left", 3),
                    _coords_digest(sp.bezier_points, "handle_right", 3),
                    _coords_digest(sp.bezier_points, "radius", 1),
                    _coords_digest(sp.bezier_points, "tilt", 1),
                    _coords_digest(sp.points, "co", 4),
                    _coords_digest(sp.points, "radius", 1),
                    _coords_digest(sp.points, "tilt", 1)))
    return tuple(out)


def _gpencil_sig(data):
    """Grease Pencil (v3): every layer's settings and every drawing's
    attributes (stroke points live there)."""
    out = []
    for layer in data.layers:
        frames = []
        for fr in getattr(layer, "frames", ()):
            dr = getattr(fr, "drawing", None)
            attrs = getattr(dr, "attributes", None)
            frames.append((fr.frame_number,
                           _attrs_sig(attrs) if attrs is not None else None))
        out.append((layer.name, _simple_props_sig(layer), tuple(frames)))
    return tuple(out)


def _shape_keys_sig(data):
    """Shape keys: each key's value, range, mute, relative key, vertex group
    and point positions (a slider drag or editing a non-Basis key changes
    the shape the preview renders)."""
    sk = getattr(data, "shape_keys", None)
    if sk is None:
        return None
    out = [sk.use_relative, round(getattr(sk, "eval_time", 0.0), 5)]
    for kb in sk.key_blocks:
        out.append((kb.name, round(kb.value, 5), round(kb.slider_min, 5),
                    round(kb.slider_max, 5), kb.mute, kb.interpolation,
                    kb.relative_key.name if kb.relative_key else None,
                    kb.vertex_group, _coords_digest(kb.data, "co", 3)))
    return tuple(out)


def _compute_data_sig(data, obj=None):
    parts = [_idref(data), _simple_props_sig(data)]   # bevel, text size, ...
    try:
        parts.append(_shape_keys_sig(data))
        attrs = getattr(data, "attributes", None)
        if attrs is not None:
            parts.append(_attrs_sig(attrs))
            for dom in ("vertices", "edges", "polygons", "loops", "points", "curves"):
                c = getattr(data, dom, None)
                if c is not None:
                    parts.append((dom, len(c)))
            parts.append(_vgroups_sig(data, obj))
        elif hasattr(data, "splines"):                  # Curve / Text
            parts.append(_splines_sig(data))
            parts.append(getattr(data, "body", None))
        elif hasattr(data, "points") and hasattr(data, "points_u"):   # Lattice
            parts.append(_coords_digest(data.points, "co_deform", 3))
        if hasattr(data, "layers") and not hasattr(data, "splines"):  # GP v3
            parts.append(_gpencil_sig(data))
    except Exception as exc:
        parts.append(repr(exc))
    return tuple(parts)


def _data_sig(data, obj=None):
    """Fingerprint of an object's own data *content* (positions, attribute
    values, vertex-group weights, the data's settings; curve points, radius
    and tilt; Grease Pencil drawings), part of its Geometry Nodes previews'
    hash. Content, not update events: a preview render shares the user's
    mesh and makes Blender report it as updated, so counting events
    re-rendered every GN preview after each render, forever. Computed again
    only after an update event for that data (_on_depsgraph). In Edit Mode
    the mesh keeps its pre-edit data until you leave it, which is also what
    the previews render."""
    if data is None:
        return None
    ref = _idref(data)
    # Keyed by whether weights are included: a mesh shared by an object with
    # vertex groups and one without has two fingerprints, each noticing a
    # change on its own (the update count is per data-block).
    ck = (ref, bool(obj is not None and len(getattr(obj, "vertex_groups", ()))))
    gen = _state["data_gen"].get(ref, 0)
    hit = _state["data_sigs"].get(ck)
    if hit is not None and (getattr(data, "is_editmode", False) or (
            obj is not None and obj.mode == "WEIGHT_PAINT")):
        # Edit Mode: the mesh keeps its pre-edit data until you leave, so a
        # fingerprint now would only re-render the same stale image.
        # Weight Paint: every brush dab updates the mesh; hashing the weights
        # each time would stall painting. Either way the update count stays
        # ahead: leaving the mode updates the object and the next rebuild
        # fingerprints once.
        return hit[1]
    if hit is None or hit[0] != gen:
        hit = (gen, _compute_data_sig(data, obj))
        _state["data_sigs"][ck] = hit
    return hit[1]


def _mark_data_changed(ref):
    """Invalidate the cached fingerprints of one object data-block."""
    _state["data_gen"][ref] = _state["data_gen"].get(ref, 0) + 1


def rebuild_queue(tree, kind, props, force=False, path=None):
    """Queue the eligible nodes of ``tree`` whose hash changed. ``path`` is the
    editor's tree path (outermost first, ending with ``tree``); when it is
    longer than one, ``tree`` is a node group entered from path[0]."""
    _state["tree_sig_memo"] = {}
    try:
        _rebuild_queue(tree, kind, props, force, path)
    finally:
        _state["tree_sig_memo"] = None


def _rebuild_queue(tree, kind, props, force, path):
    _zone_cache.clear()
    path = list(path) if path else [tree]
    if path[-1].as_pointer() != tree.as_pointer():
        path = [tree]
    chain = _instance_chain(path)
    if chain is None:
        return
    # Compositor group previews can be switched off (each one renders the
    # scene); with them off nothing is live, so the loop below is skipped and
    # the group's old thumbnails are dropped.
    skip = bool(chain) and kind == KIND_COMP and not getattr(props, "comp_groups", True)
    root = path[0]
    src = resolve_source(root, kind)
    if src is None:
        return
    ctx = _view_ctx(src, chain)
    # Resolution is part of the signature so a Quality change re-renders;
    # the source too (a GN tree shared by several objects previews the one
    # the editor shows).
    esig = "%s|%s|%s|%d" % (_engine_id(props), props.resolution, src,
                            int(getattr(props, "show_values", True)))
    if getattr(props, "update_on_frame", False):
        esig += "|f%d" % bpy.context.scene.frame_current
    if chain:
        esig += "|" + _context_sig(src, path, chain)
    # The object / modifier side of a GN preview only matters to 3D renders of
    # geometry: field swatches (Noise, Math ...) render the node in isolation,
    # so a mesh edit or modifier input change doesn't re-render them.
    gsig = "|" + _geo_source_sig(src[1], root) if kind == KIND_GEO else ""
    lsig = _light_sig(props)
    memo = {}
    live = set()
    for node in tree.nodes:
        if skip or not node_eligible(node, kind, props):
            continue
        try:
            if kind == KIND_GEO and not any(s.type == "GEOMETRY" for s in node.outputs):
                # A field swatch renders the node alone (upstream fields are
                # not evaluated), so upstream edits can't change it.
                h = _isolated_hash(node)
            else:
                h = upstream_hash(node, memo)
        except Exception:
            continue
        if gsig and any(s.type == "GEOMETRY" for s in node.outputs):
            base = h + esig + gsig
        else:
            base = h + esig
        for out_id in _preview_targets(node, kind, props):
            # The preview lights only reach lit previews: a shader output
            # (sphere / cube), or a world volume (fog on a sphere lit by the
            # key light).
            if kind == KIND_SHADER and renders_as_shader(node, out_id):
                extra = "|" + lsig
            elif kind == KIND_WORLD and node.bl_idname in VOLUME_NODES:
                extra = "|%.4f" % props.sun_strength
            else:
                extra = ""
            ho = hashlib.md5((base + extra).encode("utf-8", "replace")).hexdigest()
            key = _skey(tree, node.name, out_id, ctx)
            live.add(key)
            _enqueue(kind, src, tree, node.name, out_id, key, ho, force,
                     root, chain)
    # Thumbnails of this tree that are no longer shown (node deleted or
    # renamed, output switched, filtered out) only hold GPU memory.
    # Only this view's (same context): another editor may show the same tree
    # through another source, and its thumbnails are still in use. Those of
    # a context nobody shows any more age out of the cache (_prune_cache).
    prefix = "%d:" % tree.as_pointer()

    def stale(k):
        return k.startswith(prefix) and k not in live and _key_ctx(k) in ("", ctx)

    for key in [k for k in set(_state["textures"]) | set(_state["failed"]) if stale(k)]:
        _drop_texture(key)
    # Their pending renders too (e.g. Scope switched to Selected while a
    # compositor tree was queued: each one would render the whole scene), and
    # those of another context no open editor shows (the source switched
    # before they rendered).
    shown = {e.get("ctx") for e in _state["editors"].values()
             if e.get("tree") == tree.as_pointer()}

    def unwanted(k):
        return stale(k) or (k.startswith(prefix) and k not in live
                            and _key_ctx(k) not in shown)

    q = _state["queue"]
    gone = [it for it in q if unwanted(it["key"])]
    if gone:
        q[:] = [it for it in q if not unwanted(it["key"])]
        _state["queued_keys"].difference_update(it["key"] for it in gone)


def _touch(key):
    _state["tick"] += 1
    _state["tex_tick"][key] = _state["tick"]


def _drop_texture(key):
    _state["textures"].pop(key, None)
    _state["hashes"].pop(key, None)
    _state["tex_tick"].pop(key, None)
    _state["failed"].pop(key, None)
    _state["values"].pop(key, None)


def _reset_cache():
    """Forget every thumbnail, pending render and failure."""
    for k in ("textures", "tex_tick", "img_gen", "data_sigs", "data_gen", "hashes", "failed",
              "values"):
        _state[k].clear()
    _state["queue"].clear()
    _state["queued_keys"].clear()
    _state["xform_watch"].clear()
    _state["visible"] = set()
    _state["priority"] = set()


def _live_tree_pointers():
    ptrs = set()
    for m in bpy.data.materials:
        if m.node_tree is not None:
            ptrs.add(m.node_tree.as_pointer())
    for w in bpy.data.worlds:
        if w.node_tree is not None:
            ptrs.add(w.node_tree.as_pointer())
    for lt in bpy.data.lights:
        if getattr(lt, "node_tree", None) is not None:
            ptrs.add(lt.node_tree.as_pointer())
    for ng in bpy.data.node_groups:
        ptrs.add(ng.as_pointer())
    return ptrs


def _max_textures():
    """The user's 'Max Cached Thumbnails' preference (MAX_TEXTURES when the
    add-on's preferences aren't available)."""
    try:
        return int(bpy.context.preferences.addons[__package__].preferences.max_textures)
    except Exception:
        return MAX_TEXTURES


def _prune_cache():
    """Drop thumbnails whose node tree no longer exists, then evict the least
    recently used ones above the cache limit."""
    live = _live_tree_pointers()
    for key in set(_state["textures"]) | set(_state["failed"]):
        try:
            ptr = int(key.split(":", 1)[0])
        except ValueError:
            ptr = None
        if ptr not in live:
            _drop_texture(key)
    extra = len(_state["textures"]) - _max_textures()
    if extra > 0:
        # Never evict the editor's own thumbnails: they'd go blank, re-render
        # on the next edit and get evicted again. If they alone exceed the
        # limit, the cache stays above it until the user moves on.
        tick = _state["tex_tick"]
        victims = sorted((k for k in _state["textures"] if not timer._in_view(k)),
                         key=lambda k: tick.get(k, 0))
        for key in victims[:extra]:
            _drop_texture(key)


def _pop_next():
    """Next queue item: the active / selected nodes first, then the ones on
    screen, then the rest (each group in queue order)."""
    q = _state["queue"]
    pri, vis = _state["priority"], _state["visible"]
    best, best_rank = 0, 3
    for i, it in enumerate(q):
        k = it.get("key")
        rank = 0 if k in pri else 1 if k in vis else 2
        if rank < best_rank:
            best, best_rank = i, rank
            if rank == 0:
                break
    it = q.pop(best)
    _state["queued_keys"].discard(it.get("key"))
    return it


def _render_item(item, res, props):
    """Render one queue item; returns what _finish() returned, or None."""
    k = item["kind"]
    oid = item.get("out")
    chain = item.get("chain") or None
    if k == KIND_SHADER:
        if item.get("src_type") == "LIGHT":
            m = _idget(bpy.data.lights, item["src"])
        else:
            m = _idget(bpy.data.materials, item["src"])
        return render_shader(m, item["node"], res, props, oid, chain) if m else None
    if k == KIND_WORLD:
        w = _idget(bpy.data.worlds, item["src"])
        return render_world(w, item["node"], res, props, oid, chain) if w else None
    if k == KIND_GEO:
        o = _idget(bpy.data.objects, item["src"])
        t = _idget(bpy.data.node_groups, item.get("tree"))
        r = _idget(bpy.data.node_groups, item.get("root")) or t
        return render_geo(o, item["node"], res, props, oid, t, chain, r) \
            if o and t else None
    if k == KIND_COMP:
        s = _idget(bpy.data.scenes, item["src"])
        return render_compositor(s, item["node"], res, props, oid, chain) if s else None
    return None


def _queue_allowed(item, props):
    """False for a pending render whose preview type was switched off since
    it was queued (Compositor, Geometry Nodes, World, compositor groups)."""
    kind = item.get("kind")
    if kind is not None and not sources._kind_enabled(kind, props):
        return False
    if kind == KIND_COMP and item.get("chain") \
            and not getattr(props, "comp_groups", True):
        return False
    return True


def _drop_disallowed(props):
    q = _state["queue"]
    drop = [it for it in q if not _queue_allowed(it, props)]
    if drop:
        q[:] = [it for it in q if _queue_allowed(it, props)]
        _state["queued_keys"].difference_update(it["key"] for it in drop)


def process_queue(props):
    """Render queued previews: at most 'Nodes / Tick', and stop early once the
    'Time Budget' is spent (at least one render per call), so a slow engine
    or a high Quality doesn't freeze the UI for several renders in a row."""
    if not _state["queue"]:
        return False
    n = max(1, int(props.batch_size))
    budget = max(0.0, float(getattr(props, "time_budget", 0))) / 1000.0
    res = int(props.resolution)
    did = False
    start = time.perf_counter()
    _state["rendering"] = True
    try:
        for i in range(n):
            if not _state["queue"]:
                break
            if i and budget and time.perf_counter() - start >= budget:
                break
            item = _pop_next()
            _state["last_value"] = None
            try:
                tex = _render_item(item, res, props)
            except Exception as exc:
                print("[NodePreview] render failed for %s: %r" % (item["node"], exc))
                tex = None
            key = item["key"]
            if tex is not None:
                _state["textures"][key] = tex
                _state["hashes"][key] = item["hash"]
                _state["failed"].pop(key, None)
                if _state["last_value"] is None:
                    _state["values"].pop(key, None)
                else:
                    _state["values"][key] = _state["last_value"]
                _touch(key)
            else:
                _state["failed"][key] = item["hash"]
            # A failure changes what is drawn too (the error marker).
            did = True
    finally:
        _state["rendering"] = False
        _state["last_value"] = None
    return did


def _tag_node_editors():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "NODE_EDITOR":
                area.tag_redraw()
