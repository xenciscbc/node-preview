# SPDX-License-Identifier: GPL-3.0-or-later
"""
Node Preview Thumbnails
=======================

Live-rendered thumbnails above nodes in the Shader, Geometry Nodes and
Compositor editors.

Shader Editor:
  - Texture / color nodes -> flat emission swatch (lighting-independent).
  - Shader-output nodes (BSDF / Output) -> lit material ball (sphere) or flat
    lit plane, using a self-contained preview environment (world + key light).
Geometry Nodes:
  - Nodes with a Geometry output -> a small 3D render of the geometry at that
    node.
  - Texture / math / colour ShaderNodes (field outputs) -> a flat swatch, built
    by rebuilding the node in a temporary material (upstream fields are not
    evaluated; socket defaults stand in).
Compositor:
  - Every node with an image output -> a flat swatch of that node's result.
    (Heavier: each preview renders the scene through the compositor.)
Lights:
  - A light's node tree (Cycles) previews like a material.

Engine: EEVEE or Cycles (per preview). Updates: automatic (only the changed
nodes re-render, visible / active nodes first), plus a manual Refresh button.

Tested on Blender 5.2 (EEVEE + Cycles, Vulkan). Blender Extension: metadata
and version live in blender_manifest.toml next to this file.
"""

import importlib
import sys

import bpy

# Split-out modules, in dependency order (a module only imports from those
# before it). Installing or updating through Blender's extension operators
# drops the add-on and all its modules from sys.modules, but when files change
# in place (development) and the add-on is re-enabled, or on Reload Scripts,
# Blender reloads only this file: reload them first so they don't keep
# running the old code. On the first load none of them is in sys.modules yet.
_SUBMODULES = ("common", "eligibility", "hashing", "i18n", "preview_scene",
               "sources", "renderers", "queue", "timer", "drawing", "props",
               "operators", "ui")
for _name in _SUBMODULES:
    _mod = sys.modules.get("%s.%s" % (__name__, _name))
    if _mod is not None:
        importlib.reload(_mod)
del _name, _mod

# The code below uses their names as globals, imported here. A function that
# tests replace (mod.<module>.<name> = fake) is never imported by name: it is
# called through its module (queue._tag_node_editors()) so the
# replacement reaches every caller. tests/test_package_layout.py checks this.
from .common import KINDS, space_kind, _state, MAX_TEXTURES
from .eligibility import _previewable_outputs, _socket_enum_cache, _npv_socket_items
from .i18n import TR, _t
from .preview_scene import _cleanup_datablocks
from . import queue
from .queue import _reset_cache, _prune_cache
from .timer import (
    _timer, _ensure_timer, _on_depsgraph, _on_frame_change, _on_load_post,
    _on_save_pre,
)
from .drawing import draw_callback
from .props import _node_show_update, NPVProps
from .operators import NPV_OT_refresh, NPV_OT_mark, NPV_OT_clear, NPV_OT_export
from .ui import NPV_OT_help, NPV_PT_panel

# --------------------------------------------------------------------------- #
#  Register
# --------------------------------------------------------------------------- #
def _prefs_update(self, context):
    _prune_cache()
    queue._tag_node_editors()


class NPVAddonPrefs(bpy.types.AddonPreferences):
    # __name__ is the extension package: "bl_ext.<repo>.node_preview".
    bl_idname = __name__

    max_textures: bpy.props.IntProperty(
        name="Max Cached Thumbnails",
        description="Thumbnails kept in GPU memory; the least recently shown are "
                    "released above this (the node editor you are looking at always "
                    "keeps its own). Approx. per 100 thumbnails: 3 MB at "
                    "Low (64px), 13 MB at Medium (128px), 50 MB at High (256px)",
        default=MAX_TEXTURES, min=16, max=4096, update=_prefs_update)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "max_textures")
        mb = {"64": 0.03125, "128": 0.125, "256": 0.5}
        npv = getattr(context.scene, "npv", None)
        fmt = _t(npv, "pref_limit_fmt") if npv is not None else TR["EN"]["pref_limit_fmt"]
        col.label(text=fmt % tuple(
            max(1, round(self.max_textures * mb[r])) for r in ("64", "128", "256")),
            icon="INFO")
        col.separator()
        col.label(text=(_t(npv, "keys_title") if npv is not None
                        else TR["EN"]["keys_title"]), icon="KEYINGSET")
        for km, kmi in _addon_keymaps:
            r = col.row()
            r.label(text=kmi.name or kmi.idname)
            r.prop(kmi, "type", text="", full_event=True)


_classes = (NPVProps, NPVAddonPrefs, NPV_OT_refresh, NPV_OT_mark, NPV_OT_clear,
            NPV_OT_export, NPV_OT_help, NPV_PT_panel)

# (keymap, item) pairs added to the add-on keyconfig; removed on unregister.
_addon_keymaps = []

# Node editor shortcuts (remappable in Preferences > Keymap > Node Editor).
KEYMAP_ITEMS = (
    ("wm.context_toggle", "P", {"data_path": "scene.npv.enabled"}),
    ("node.npv_refresh", "R", {}),
    ("wm.context_toggle", "Z", {"data_path": "scene.npv.zoom_active"}),
)


def _register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon if wm is not None else None
    if kc is None:        # background mode has no key configuration
        return
    km = kc.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
    for idname, key, attrs in KEYMAP_ITEMS:
        kmi = km.keymap_items.new(idname, key, "PRESS", ctrl=True, alt=True)
        for k, v in attrs.items():
            setattr(kmi.properties, k, v)
        _addon_keymaps.append((km, kmi))


def _unregister_keymaps():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()


def _node_context_menu(self, context):
    sp = context.space_data
    node = getattr(context, "active_node", None)
    if sp and sp.type == "NODE_EDITOR" and sp.tree_type in KINDS and node is not None:
        self.layout.separator()
        self.layout.prop(node, "npv_show", text=_t(context.scene.npv, "ctx_show"))
        if len(_previewable_outputs(node, space_kind(sp))) > 1:
            self.layout.prop(node, "npv_socket",
                             text=_t(context.scene.npv, "preview_socket"))
        self.layout.operator("node.npv_export", icon="EXPORT",
                             text=_t(context.scene.npv, "export"))


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.npv = bpy.props.PointerProperty(type=NPVProps)
    bpy.types.Node.npv_show = bpy.props.BoolProperty(
        name="Show Preview",
        description="Show this node's preview thumbnail "
                    "(applies when Preview Scope is Marked)",
        default=True, update=_node_show_update)
    bpy.types.Node.npv_socket = bpy.props.EnumProperty(
        name="Preview Socket",
        description="Which output of this node to preview "
                    "(Auto = the first linked output)",
        items=_npv_socket_items, update=_node_show_update)
    try:
        bpy.types.NODE_MT_context_menu.append(_node_context_menu)
    except Exception:
        pass
    if _state["draw_handle"] is None:
        _state["draw_handle"] = bpy.types.SpaceNodeEditor.draw_handler_add(
            draw_callback, (), "WINDOW", "POST_PIXEL")
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    if _on_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_on_save_pre)
    if _on_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_on_frame_change)
    try:
        _register_keymaps()
    except Exception as exc:
        print("[NodePreview] keymap registration failed: %r" % (exc,))
    _state["dirty"] = True
    _ensure_timer()


def unregister():
    _unregister_keymaps()
    try:
        bpy.types.NODE_MT_context_menu.remove(_node_context_menu)
    except Exception:
        pass
    try:
        del bpy.types.Node.npv_show
    except Exception:
        pass
    try:
        del bpy.types.Node.npv_socket
    except Exception:
        pass
    _socket_enum_cache.clear()
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_on_save_pre)
    if _on_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    if bpy.app.timers.is_registered(_timer):
        try:
            bpy.app.timers.unregister(_timer)
        except Exception:
            pass
    _state["timer_running"] = False
    if _state["draw_handle"] is not None:
        try:
            bpy.types.SpaceNodeEditor.draw_handler_remove(_state["draw_handle"], "WINDOW")
        except Exception:
            pass
        _state["draw_handle"] = None
    _reset_cache()
    _state["src_hint"] = []
    _state["editors"].clear()
    _state["shader_image"] = None
    _state["shader_color"] = None
    _cleanup_datablocks()
    del bpy.types.Scene.npv
    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
