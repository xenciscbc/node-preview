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
    if hasattr(v, "__len__"):
        try:
            return tuple(round(float(x), 6) for x in v)
        except Exception:
            return tuple(v)
    try:
        return round(float(v), 6)
    except Exception:
        return str(v)


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
        if inp.is_linked:
            srcs = [(l.from_socket.identifier, upstream_hash(l.from_node, memo))
                    for l in inp.links]
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
            if not inp.is_linked:
                parts.append((n.name, inp.identifier, _socket_default(inp)))
        for out in n.outputs:  # RGB / Value nodes
            parts.append((n.name, "O", out.identifier, _socket_default(out)))
    for l in tree.links:
        parts.append((l.from_node.name, l.from_socket.identifier,
                      l.to_node.name, l.to_socket.identifier))
    hv = hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()
    if memo is not None:
        memo[ptr] = hv
    return hv
