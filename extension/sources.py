# SPDX-License-Identifier: GPL-3.0-or-later
"""Find the data-block (material, world, light, object, scene) a node tree
is previewed through, and look trees up by pointer."""

import bpy

from .common import KIND_COMP, KIND_GEO, KIND_SHADER, KIND_WORLD, _state


# --------------------------------------------------------------------------- #
#  Source resolution
# --------------------------------------------------------------------------- #
def find_material_for_tree(tree):
    for m in bpy.data.materials:
        if m.node_tree is not None and m.node_tree == tree:
            return m
    return None


def _idref(d):
    """How a queue item / hint refers to a data-block: its name, or (name,
    library path) for linked data, which can share a local data-block's name."""
    if d.library is None:
        return d.name
    return (d.name, d.library.filepath)


def _idget(collection, ref):
    """The data-block ``ref`` (from _idref) names in ``collection``, or None.
    A plain name only matches local data."""
    if not ref:
        return None
    name, lib = (ref, None) if isinstance(ref, str) else ref
    d = collection.get(name)
    if d is not None and (d.library.filepath if d.library else None) == lib:
        return d
    return next((d for d in collection if d.name == name
                 and (d.library.filepath if d.library else None) == lib), None)


def _hinted(cls, collection):
    """Data-blocks of ``cls`` the editor was last seen showing (its id /
    id_from and the active object), most specific first."""
    out = []
    for c, name in _state.get("src_hint") or ():
        if c == cls:
            d = _idget(collection, name)
            if d is not None and d not in out:
                out.append(d)
    return out


def _uses_geo_tree(obj, tree):
    return any(mo.type == 'NODES' and mo.node_group == tree for mo in obj.modifiers)


def resolve_source(tree, kind):
    """The data-block a tree is previewed through. Several can share a tree
    (a material on many objects is fine; a GN tree on several objects is
    not): prefer the one the editor shows, then fall back to the first."""
    if kind == KIND_SHADER:
        for m in _hinted("MAT", bpy.data.materials):
            if m.node_tree is not None and m.node_tree == tree:
                return ("MAT", _idref(m))
        m = find_material_for_tree(tree)
        if m:
            return ("MAT", _idref(m))
        for lt in _hinted("LIGHT", bpy.data.lights) + list(bpy.data.lights):
            if getattr(lt, "node_tree", None) is not None and lt.node_tree == tree:
                return ("LIGHT", _idref(lt))
        return None
    if kind == KIND_GEO:
        for obj in _hinted("OBJ", bpy.data.objects) + list(bpy.data.objects):
            if _uses_geo_tree(obj, tree):
                return ("OBJ", _idref(obj))
        return None
    if kind == KIND_COMP:
        for s in _hinted("SCENE", bpy.data.scenes) + list(bpy.data.scenes):
            if getattr(s, "compositing_node_group", None) == tree:
                return ("SCENE", _idref(s))
        return None
    if kind == KIND_WORLD:
        for w in bpy.data.worlds:
            if w.node_tree is not None and w.node_tree == tree:
                return ("WORLD", _idref(w))
        return None
    return None


def _tree_by_pointer(ptr):
    """A live node tree (material / world tree or node group) by pointer."""
    for m in bpy.data.materials:
        if m.node_tree is not None and m.node_tree.as_pointer() == ptr:
            return m.node_tree
    for w in bpy.data.worlds:
        if w.node_tree is not None and w.node_tree.as_pointer() == ptr:
            return w.node_tree
    for lt in bpy.data.lights:
        nt = getattr(lt, "node_tree", None)
        if nt is not None and nt.as_pointer() == ptr:
            return nt
    for ng in bpy.data.node_groups:
        if ng.as_pointer() == ptr:
            return ng
    return None


def _resolve_active():
    """(edited tree, kind, editor path) recorded by the last draw."""
    kind = _state["active_kind"]
    ptrs = _state["active_path"] or (
        [_state["active_tree_ptr"]] if _state["active_tree_ptr"] else [])
    if not ptrs or kind is None:
        return None, None, None
    path = [_tree_by_pointer(p) for p in ptrs]
    if any(t is None for t in path):
        return None, None, None
    return path[-1], kind, path


def _kind_enabled(kind, props):
    if kind == KIND_SHADER:
        return True
    if kind == KIND_WORLD:
        return props.preview_world
    if kind == KIND_GEO:
        return props.preview_geometry
    if kind == KIND_COMP:
        return props.preview_compositor
    return False
