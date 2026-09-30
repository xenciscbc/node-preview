# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators: refresh, mark / clear nodes, export a node's preview as PNG."""

import os

import bpy

from . import queue
from .common import KINDS, KIND_GEO, _state, space_kind
from .eligibility import _in_zone, _preview_targets, _zone_cache
from .sources import _idref, resolve_source
from .renderers import _instance_chain
from .queue import _reset_cache
from .timer import _ensure_timer
from .drawing import _editor_hint


# --------------------------------------------------------------------------- #
#  Operators
# --------------------------------------------------------------------------- #
def _panel_poll(context):
    sp = context.space_data
    return (sp and sp.type == "NODE_EDITOR" and sp.tree_type in KINDS
            and getattr(sp, "edit_tree", None) is not None)


class NPV_OT_refresh(bpy.types.Operator):
    bl_idname = "node.npv_refresh"
    bl_label = "Refresh Previews"
    bl_description = "Re-render all node previews in the current editor"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _panel_poll(context)

    def execute(self, context):
        props = context.scene.npv
        sp = context.space_data
        path = [p.node_tree for p in sp.path if p.node_tree is not None]
        _state["src_hint"] = _editor_hint(context, sp)
        queue.rebuild_queue(sp.edit_tree, space_kind(sp), props, force=True,
                      path=path or None)
        _ensure_timer()
        self.report({"INFO"}, "Queued %d node previews" % len(_state["queue"]))
        return {"FINISHED"}


class NPV_OT_mark(bpy.types.Operator):
    bl_idname = "node.npv_mark_selected"
    bl_label = "Mark Selected For Preview"
    bl_description = "Turn the preview checkbox on/off for the selected nodes"
    bl_options = {"REGISTER", "UNDO"}

    mark: bpy.props.BoolProperty(default=True)

    @classmethod
    def poll(cls, context):
        return _panel_poll(context)

    def execute(self, context):
        tree = context.space_data.edit_tree
        cnt = 0
        for n in tree.nodes:
            if n.select:
                n.npv_show = self.mark
                cnt += 1
        _state["dirty"] = True
        queue._tag_node_editors()
        self.report({"INFO"}, "%s %d node(s)" % ("Marked" if self.mark else "Unmarked", cnt))
        return {"FINISHED"}


class NPV_OT_clear(bpy.types.Operator):
    bl_idname = "node.npv_clear"
    bl_label = "Clear Cache"
    bl_description = "Remove all cached preview thumbnails"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _reset_cache()
        # With Auto Update on, the editor's previews render again from scratch.
        _state["dirty"] = True
        queue._tag_node_editors()
        self.report({"INFO"}, "Preview cache cleared")
        return {"FINISHED"}


def export_job(tree, kind, props, node, path=None):
    """Queue-style item that renders ``node`` of ``tree`` (entered through
    ``path``), or None when the node can't be previewed from here."""
    if kind == KIND_GEO:
        _zone_cache.clear()
        if _in_zone(node):
            return None           # inside a zone: can't be wired out
    path = list(path) if path else [tree]
    if path[-1].as_pointer() != tree.as_pointer():
        path = [tree]
    chain = _instance_chain(path)
    src = resolve_source(path[0], kind) if chain is not None else None
    if src is None:
        return None
    oid = _preview_targets(node, kind, props)[0]
    return {"kind": kind, "src": src[1], "src_type": src[0], "tree": _idref(tree),
            "root": _idref(path[0]), "chain": chain, "node": node.name, "out": oid,
            "key": None, "hash": None}


class NPV_OT_export(bpy.types.Operator):
    bl_idname = "node.npv_export"
    bl_label = "Export Preview"
    bl_description = ("Render the active node's preview at the chosen size and "
                      "save it as a PNG")
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.png", options={"HIDDEN"})
    size: bpy.props.EnumProperty(
        name="Size",
        items=[("256", "256 px", ""), ("512", "512 px", ""),
               ("1024", "1024 px", ""), ("2048", "2048 px", "")],
        default="512")
    load_image: bpy.props.BoolProperty(
        name="Open in Blender",
        description="Also load the saved PNG as an image data-block",
        default=True)

    @classmethod
    def poll(cls, context):
        return _panel_poll(context) and getattr(context, "active_node", None) is not None

    def invoke(self, context, event):
        node = context.active_node
        safe = bpy.path.clean_name(node.name) or "node"
        self.filepath = os.path.join(os.path.dirname(bpy.data.filepath) or
                                     os.path.expanduser("~"), safe + ".png")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        sp = context.space_data
        node = getattr(context, "active_node", None)
        if not _panel_poll(context) or node is None:
            self.report({"WARNING"}, "Run Export from a node editor with an active node")
            return {"CANCELLED"}
        props = context.scene.npv
        kind = space_kind(sp)
        fp = bpy.path.ensure_ext(bpy.path.abspath(self.filepath), ".png")
        path = [p.node_tree for p in sp.path if p.node_tree is not None]
        _state["src_hint"] = _editor_hint(context, sp)
        job = export_job(sp.edit_tree, kind, props, node, path or None)
        if job is None:
            self.report({"WARNING"}, "This node can't be previewed here")
            return {"CANCELLED"}
        _state["export_to"] = fp
        _state["rendering"] = True
        try:
            ok = queue._render_item(job, int(self.size), props)
        except Exception as exc:
            print("[NodePreview] export failed for %s: %r" % (node.name, exc))
            ok = None
        finally:
            _state["export_to"] = None
            _state["rendering"] = False
        if not ok or not os.path.isfile(fp):
            self.report({"ERROR"}, "Export failed (see the system console)")
            return {"CANCELLED"}
        if self.load_image:
            img = bpy.data.images.load(fp, check_existing=True)
            img.reload()        # an earlier export to the same file
        self.report({"INFO"}, "Saved %s" % fp)
        return {"FINISHED"}
