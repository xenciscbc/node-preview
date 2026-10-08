# SPDX-License-Identifier: GPL-3.0-or-later
"""Content hashes that decide when a node's preview must re-render."""

import os
import hashlib

import bpy

from .common import _state

# --------------------------------------------------------------------------- #
#  Hashing
# --------------------------------------------------------------------------- #
def _socket_default(sock):
    try:
        v = sock.default_value
    except Exception:
        return None
    if isinstance(v, bpy.types.ID) or v is None and sock.type in _ID_SOCKETS:
        # Whether the object's transform counts depends on who reads it: the
        # node itself for an input, the nodes it feeds for an output.
        node = getattr(sock, "node", None)
        if getattr(sock, "is_output", False):
            xform = output_reads_transform(sock)
        else:
            xform = reads_transform(node) if node is not None else True
        return _id_value_sig(v, xform)
    if hasattr(v, "__len__"):
        try:
            return tuple(round(float(x), 6) for x in v)
        except Exception:
            return tuple(v)
    try:
        return round(float(v), 6)
    except Exception:
        return str(v)


_ID_SOCKETS = {"IMAGE", "OBJECT", "COLLECTION", "MATERIAL", "TEXTURE"}


# Object Info outputs that carry the object's placement.
_XFORM_OUTPUTS = {"Transform", "Location", "Rotation", "Scale"}


def reads_transform(node, _depth=0):
    """Does ``node``, reading an object through one of its inputs, use that
    object's transform? Object Info only in Relative mode or through its
    Transform / Location / Rotation / Scale outputs (Original mode's Geometry
    and As Instance are in the object's own space); Collection Info always
    (it places the members within the collection). A reroute: whatever it
    feeds. Any other node (a group node ...): assumed to."""
    idn = getattr(node, "bl_idname", "")
    if idn == "GeometryNodeObjectInfo":
        if getattr(node, "transform_space", "") == "RELATIVE":
            return True
        return any(o.identifier in _XFORM_OUTPUTS and _feeds(o)
                   for o in node.outputs)
    if idn == "NodeReroute" and _depth < 32:
        return any(reads_transform(l.to_node, _depth + 1)
                   for o in node.outputs for l in o.links if not l.is_muted)
    return True


def _feeds(out):
    return any(not l.is_muted for l in out.links)


def output_reads_transform(out):
    """Does any node fed by output socket ``out`` (an Object input node's,
    a Group Input's) use the transform of the object it passes on?"""
    return any(reads_transform(l.to_node) for l in out.links if not l.is_muted)


def _object_sig(ob, xform=True):
    """What a node reading another object gets from it: its data's content
    and, with ``xform``, its transform. Then its name also goes into
    ``_state["xform_watch"]`` so moving it re-hashes (moves are otherwise
    ignored, see timer._on_depsgraph)."""
    # queue imports this module: imported here, when hashing (it holds the
    # cached object data fingerprints, _data_sig).
    from . import queue
    sig = ("OB", ob.name, getattr(ob.library, "filepath", None),
           queue._data_sig(ob.data, ob))
    if not xform:
        return sig
    _state["xform_watch"].add(ob.name)
    return sig + (tuple(round(x, 5) for row in ob.matrix_world for x in row),)


def _id_value_sig(v, xform=True):
    """The value of an ID socket or setting (Image, Object, Collection,
    Material ...): for an image or an object what its pixels / data (and,
    with ``xform``, an object's transform) are, not just its name -- painting
    or editing it must re-render. A collection's members always count with
    their transforms. Never ``str(v)``: that holds a memory address."""
    if v is None:
        return None
    try:
        if isinstance(v, bpy.types.Image):
            return ("IMG",) + _image_sig(v)
        if isinstance(v, bpy.types.Object):
            return _object_sig(v, xform)
        if isinstance(v, bpy.types.Collection):
            return ("CO", v.name, tuple(_object_sig(o) for o in v.all_objects))
    except Exception:
        pass
    return ("ID", v.name, getattr(v.library, "filepath", None))


# Input nodes whose ID setting is passed on to other nodes (hashed by content
# like an ID socket, not by name).
_ID_INPUT_NODES = {"GeometryNodeInputObject", "GeometryNodeInputCollection"}


_SKIP_PROPS = {
    "location", "location_absolute", "width", "width_hidden", "height",
    "dimensions", "select", "name", "label", "use_custom_color", "color",
    "hide", "show_options", "show_preview", "show_texture", "parent",
    "bl_idname", "rna_type", "inputs", "outputs", "internal_links", "type",
    "bl_label", "bl_description", "bl_icon", "bl_static_type",
    "bl_width_default", "bl_width_min", "bl_width_max", "bl_height_default",
    "bl_height_min", "bl_height_max", "warning_propagation",
}


def _image_sig(img):
    """What makes an Image's pixels differ: a counter bumped by depsgraph
    Image updates (texture paint), the unsaved-edits flag, the file's mtime
    (reload after an external edit) and the generated-image settings."""
    sig = [img.name, img.source, _state["img_gen"].get(img.name, 0),
           bool(img.is_dirty)]
    if img.source == "GENERATED":
        sig += [img.generated_type, tuple(round(c, 5) for c in img.generated_color),
                tuple(img.size)]
    elif img.packed_file is None and img.filepath:
        try:
            sig.append(os.path.getmtime(
                bpy.path.abspath(img.filepath, library=img.library)))
        except (OSError, ValueError):
            pass
    return tuple(sig)


def _plain(v):
    """A property value as a hashable, repr-stable Python value."""
    if isinstance(v, bpy.types.ID):
        return ("ID", v.name)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (bool, int, str)) or v is None:
        return v
    if isinstance(v, (set, frozenset)):
        return tuple(sorted(v))
    if hasattr(v, "to_dict"):          # IDPropertyGroup
        return tuple(sorted((k, _plain(x)) for k, x in v.to_dict().items()))
    if hasattr(v, "to_list"):          # IDPropertyArray
        return tuple(_plain(x) for x in v.to_list())
    if isinstance(v, dict):
        return tuple(sorted((k, _plain(x)) for k, x in v.items()))
    try:
        return tuple(_plain(x) for x in v)
    except TypeError:
        return str(v)


# The sequence frame an Image User shows follows the scene frame; the frame is
# only part of the hash with 'Update on Frame Change' (see rebuild_queue).
_STRUCT_SKIP = {"rna_type", "frame_current"}


def _simple_props_sig(struct):
    """Writable plain settings (bool / int / float / string / enum) of an RNA
    struct, plus the names of the data-blocks it points to. Only these types,
    so nothing whose text holds a memory address can make the hash unstable."""
    vals = []
    for p in struct.bl_rna.properties:
        pid = p.identifier
        if pid in _STRUCT_SKIP or p.is_readonly and p.type != "POINTER":
            continue
        try:
            if p.type == "POINTER":
                ref = getattr(struct, pid)
                if ref is None or isinstance(ref, bpy.types.ID):
                    vals.append((pid, ref.name if ref is not None else None))
            elif p.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
                vals.append((pid, _plain(getattr(struct, pid))))
        except Exception:
            pass
    return tuple(vals)


def _curve_sig(cm):
    """A CurveMapping (RGB / Vector / Float Curve, compositor Curves ...):
    its settings and every curve point."""
    return (_simple_props_sig(cm), tuple(
        tuple((round(pt.location[0], 5), round(pt.location[1], 5), pt.handle_type)
              for pt in c.points)
        for c in cm.curves))


def _node_settings(node, _seen=frozenset()):
    vals = []
    for p in node.bl_rna.properties:
        pid = p.identifier
        if pid in _SKIP_PROPS:
            continue
        if p.type == "POINTER":
            try:
                ref = getattr(node, pid)
                if isinstance(ref, bpy.types.Image):
                    vals.append((pid, _image_sig(ref)))
                elif isinstance(ref, bpy.types.NodeTree):
                    # Group node: its contents are part of its result.
                    vals.append((pid, ref.name, tree_signature(ref, _seen)))
                elif node.bl_idname in _ID_INPUT_NODES \
                        and isinstance(ref, (bpy.types.Object, bpy.types.Collection)):
                    xform = any(output_reads_transform(o) for o in node.outputs)
                    vals.append((pid, _id_value_sig(ref, xform)))
                elif ref is None or isinstance(ref, bpy.types.ID):
                    vals.append((pid, ref.name if ref is not None else None))
                elif isinstance(ref, (bpy.types.Node, bpy.types.NodeSocket)):
                    # A zone input's paired_output: which node, not its
                    # location / selection / label (links carry the data).
                    vals.append((pid, ref.name))
                elif isinstance(ref, bpy.types.CurveMapping):
                    vals.append((pid, _curve_sig(ref)))
                else:
                    # Other settings structs (Image User: sequence frames,
                    # Texture / Color Mapping ...). The color ramp is below.
                    vals.append((pid, _simple_props_sig(ref)))
            except Exception:
                pass
            continue
        if p.is_readonly:
            continue
        try:
            vals.append((pid, str(getattr(node, pid))))
        except Exception:
            pass
    cr = getattr(node, "color_ramp", None)
    if cr is not None:
        try:
            vals.append(("__ramp__", tuple(
                (round(e.position, 5), tuple(round(c, 5) for c in e.color))
                for e in cr.elements)))
        except Exception:
            pass
    return tuple(vals)


def upstream_hash(node, memo):
    ptr = node.as_pointer()
    if ptr in memo:
        return memo[ptr]
    memo[ptr] = "0"
    parts = [node.bl_idname, _node_settings(node)]
    for inp in node.inputs:
        # A muted link still shows in inp.links, but the input then reads its
        # own value: hashed as if unlinked.
        links = [l for l in inp.links if not l.is_muted]
        if links:
            srcs = [(l.from_socket.identifier, upstream_hash(l.from_node, memo))
                    for l in links]
            parts.append(("L", inp.identifier, tuple(srcs)))
        else:
            parts.append(("D", inp.identifier, _socket_default(inp)))
    # RGB / Value nodes keep their value in an output socket.
    for out in node.outputs:
        parts.append(("O", out.identifier, _socket_default(out)))
    hv = hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()
    memo[ptr] = hv
    return hv


def _isolated_hash(node):
    """Hash of what render_geo_swatch uses: the node's own settings and the
    stored value of every input (linked or not -- the swatch rebuilds the
    node alone) and output (Value / RGB nodes)."""
    parts = [node.bl_idname, _node_settings(node)]
    for inp in node.inputs:
        parts.append(("D", inp.identifier, _socket_default(inp)))
    for out in node.outputs:
        parts.append(("O", out.identifier, _socket_default(out)))
    return hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()


def tree_signature(tree, _seen=frozenset()):
    """Whole-tree fingerprint, nested groups included. ``_seen`` holds the
    trees already being walked so a malformed self-nesting can't recurse."""
    ptr = tree.as_pointer()
    if ptr in _seen:
        return "cycle"
    # Within one rebuild_queue pass a group used by many group nodes is
    # walked once.
    memo = _state.get("tree_sig_memo")
    if memo is not None and ptr in memo:
        return memo[ptr]
    seen = _seen | {ptr}
    parts = []
    for n in tree.nodes:
        parts.append((n.name, n.bl_idname, n.mute, _node_settings(n, seen)))
        for inp in n.inputs:
            if not any(not l.is_muted for l in inp.links):
                parts.append((n.name, inp.identifier, _socket_default(inp)))
        for out in n.outputs:  # RGB / Value nodes
            parts.append((n.name, "O", out.identifier, _socket_default(out)))
    for l in tree.links:
        parts.append((l.from_node.name, l.from_socket.identifier,
                      l.to_node.name, l.to_socket.identifier, l.is_muted))
    parts.append(_interface_sig(tree))
    hv = hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()
    if memo is not None:
        memo[ptr] = hv
    return hv


# Interface settings that don't change what a group computes.
_INTERFACE_SKIP = {"name", "description", "default_closed", "hide_in_modifier",
                   "force_non_field", "panel_toggle"}


def _interface_sig(tree):
    """A group's interface settings that change its result: a socket's
    Default Input (Position, Normal, Index ... used when the group node's
    input is unlinked), min / max (clamp the value passed in), type. Not its
    name or tooltip."""
    out = []
    iface = getattr(tree, "interface", None)
    for item in getattr(iface, "items_tree", ()):
        if getattr(item, "item_type", "") != "SOCKET":
            continue
        try:
            out.append((item.identifier, tuple(
                (pid, v) for pid, v in _simple_props_sig(item)
                if pid not in _INTERFACE_SKIP)))
        except Exception:
            pass
    return tuple(out)
