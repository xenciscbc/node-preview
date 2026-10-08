# SPDX-License-Identifier: GPL-3.0-or-later
"""Which nodes and output sockets get a preview."""

import zlib

from .common import (
    COLOR_VECTOR_NODES, KIND_COMP, KIND_GEO, KIND_SHADER, KIND_WORLD,
    SHADER_OUTPUT_NODES, SKIP_IDN, SKIP_TYPES,
)

# --------------------------------------------------------------------------- #
#  Eligibility
# --------------------------------------------------------------------------- #
def first_enabled_output(node):
    for s in node.outputs:
        if s.enabled and not s.hide:
            return s
    for s in node.outputs:
        if s.enabled:
            return s
    return None


def _tree_kind(tree):
    """Best-effort preview kind from a node tree's bl_idname (Shader and World
    share ShaderNodeTree; their previewable output set is the same)."""
    tt = getattr(tree, "bl_idname", "")
    if tt == KIND_GEO:
        return KIND_GEO
    if tt == KIND_COMP:
        return KIND_COMP
    return KIND_SHADER


def _previewable_outputs(node, kind):
    """Output sockets we can render a preview for, in socket order. Shader
    output nodes (BSDF/Output) are drawn as the node itself, so they expose no
    per-socket choice and return an empty list."""
    if kind in (KIND_SHADER, KIND_WORLD):
        if node.bl_idname in SHADER_OUTPUT_NODES:
            return []
        return [s for s in node.outputs
                if s.enabled and s.type in {"SHADER", "RGBA", "VECTOR", "VALUE"}]
    if kind == KIND_GEO:
        geo = [s for s in node.outputs if s.enabled and s.type == "GEOMETRY"]
        if geo:
            return geo
        # Field-producing ShaderNodes (texture / math / colour) -> flat swatch.
        if node.bl_idname.startswith("ShaderNode"):
            return [s for s in node.outputs
                    if s.enabled and s.type in {"RGBA", "VECTOR", "VALUE"}]
        return []
    if kind == KIND_COMP:
        return [s for s in node.outputs if s.enabled]
    return []


def _out_by_id(node, out_id):
    """Resolve an output socket by identifier; fall back to first enabled."""
    if out_id:
        s = next((o for o in node.outputs if o.identifier == out_id), None)
        if s is not None:
            return s
    return first_enabled_output(node)


def _preview_targets(node, kind, props):
    """List of output-socket identifiers to preview for this node.

    - Shader output nodes -> [None]  (socketless, rendered as the node).
    - 'Show All Linked Outputs' on + node has >=1 linked previewable output ->
      every linked output, drawn side by side.
    - Otherwise the node's chosen 'Preview Socket' (npv_socket); 'AUTO' means the
      first linked output, or the first previewable output if none is linked.
    """
    if node.bl_idname in SHADER_OUTPUT_NODES:
        return [None]
    outs = _previewable_outputs(node, kind)
    if not outs:
        return [None]
    linked = [s for s in outs if s.is_linked]
    if getattr(props, "show_all_outputs", False) and linked:
        return [s.identifier for s in linked]
    pick = getattr(node, "npv_socket", "AUTO")
    if pick not in ("", "AUTO") and any(s.identifier == pick for s in outs):
        return [pick]
    if linked:
        return [linked[0].identifier]
    return [outs[0].identifier]


# Keep a reference to dynamically-built enum item lists so Blender does not
# free the underlying strings (a well-known dynamic-EnumProperty pitfall).
# Keyed by the items themselves: one entry per distinct socket list, not one
# per node ever drawn.
_socket_enum_cache = {}


def _enum_num(ident, used):
    """Stable, non-zero item number for ``ident``. Blender stores a dynamic
    enum's value as this number, so numbering by position would make a saved
    choice point at another item once the list changes (a socket enabled or
    disabled, another Blender's set of HDRIs)."""
    n = (zlib.crc32(ident.encode("utf-8")) & 0x3FFFFFFF) or 1
    while n in used:
        n = n % 0x3FFFFFFF + 1
    used.add(n)
    return n


def _npv_socket_items(self, context):
    node = self
    kind = _tree_kind(node.id_data) if node.id_data is not None else KIND_SHADER
    items = [("AUTO", "Auto (first linked)",
              "Preview the first linked output, or the first output if none is linked", 0)]
    used = {0}
    for s in _previewable_outputs(node, kind):
        label = s.name or s.identifier
        items.append((s.identifier, label, "Preview the '%s' output" % label,
                      _enum_num(s.identifier, used)))
    return _socket_enum_cache.setdefault(tuple(items), items)


def _shader_eligible(node, only_tex_shader):
    if node.mute:
        return False
    idn = node.bl_idname
    if idn in SHADER_OUTPUT_NODES:
        return True
    if not only_tex_shader:
        return any(s.type in {"SHADER", "RGBA", "VECTOR", "VALUE"} for s in node.outputs)
    if idn.startswith("ShaderNodeTex"):
        return True
    if any(s.type == "SHADER" for s in node.outputs):
        return True
    if node.type == "GROUP":
        # A node group is usually a texture / shading building block.
        return any(s.type in {"RGBA", "VECTOR", "VALUE"} for s in node.outputs)
    return idn in COLOR_VECTOR_NODES


_zone_cache = {}     # tree pointer -> names of nodes inside a zone (per pass)


def _zone_members(tree):
    """Names of the nodes inside Repeat / Simulation / For Each (any paired
    input / output) zones of ``tree``: the zone input and everything
    downstream of it up to the zone output -- what Blender itself puts in the
    zone. A node that only feeds into the zone (a Mesh Cube joined in there,
    a field sampled there) stays outside, like in Blender: it can be wired to
    the Group Output too and previews fine. A node inside can't be wired out
    of the zone, so its preview would only be black. The zone output itself
    is outside and previews fine."""
    zins = [n for n in tree.nodes if getattr(n, "paired_output", None) is not None]
    if not zins:
        return frozenset()
    down = {}
    for l in tree.links:
        down.setdefault(l.from_node.name, []).append(l.to_node.name)

    def reach(start, stop):
        seen, todo = set(), list(down.get(start, ()))
        while todo:
            n = todo.pop()
            if n in seen or n == stop:
                continue
            seen.add(n)
            todo.extend(down.get(n, ()))
        return seen

    members = set()
    for zi in zins:
        members |= reach(zi.name, zi.paired_output.name)
        members.add(zi.name)
    return frozenset(members)


def _in_zone(node):
    tree = node.id_data
    key = tree.as_pointer()
    got = _zone_cache.get(key)
    if got is None:
        got = _zone_cache[key] = _zone_members(tree)
    return node.name in got


def node_eligible(node, kind, props):
    if node.type in SKIP_TYPES or node.bl_idname in SKIP_IDN:
        return False
    scope = getattr(props, "preview_scope", "ALL")
    if scope == "MARKED" and not getattr(node, "npv_show", True):
        return False
    if scope == "SELECTED" and not node.select:
        return False
    if kind == KIND_SHADER or kind == KIND_WORLD:
        return _shader_eligible(node, props.only_tex_shader)
    if kind == KIND_GEO:
        if not _previewable_outputs(node, kind) or _in_zone(node):
            return False
        # Field-swatch nodes (no geometry output) are gated by a checkbox.
        if not any(s.type == "GEOMETRY" for s in node.outputs):
            return getattr(props, "geo_fields", True)
        return True
    if kind == KIND_COMP:
        return first_enabled_output(node) is not None
    return False


def renders_as_shader(node, out_id=None):
    """Does the preview of this output render lit (sphere / cube, preview
    lights)? Decided by the previewed output, as render_shader does: a group
    whose first output is a colour still previews its BSDF output lit."""
    if node.bl_idname in SHADER_OUTPUT_NODES:
        return True
    o = _out_by_id(node, out_id)
    return o is not None and o.type == "SHADER"
