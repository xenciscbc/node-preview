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
import os
import sys

import bpy

# Split-out modules, in dependency order (a module only imports from those
# before it). Installing or updating through Blender's extension operators
# drops the add-on and all its modules from sys.modules, but when files change
# in place (development) and the add-on is re-enabled, or on Reload Scripts,
# Blender reloads only this file: reload them first so they don't keep
# running the old code. On the first load none of them is in sys.modules yet.
_SUBMODULES = ("common", "eligibility", "hashing", "i18n", "preview_scene",
               "sources", "renderers", "queue", "timer", "drawing")
for _name in _SUBMODULES:
    _mod = sys.modules.get("%s.%s" % (__name__, _name))
    if _mod is not None:
        importlib.reload(_mod)
del _name, _mod

# The code below uses their names as globals, imported here. A function that
# tests replace (mod.<module>.<name> = fake) is never imported by name: it is
# called through its module (queue._tag_node_editors()) so the
# replacement reaches every caller. tests/test_package_layout.py checks this.
from .common import (
    KIND_SHADER, KIND_GEO, KIND_COMP, KIND_WORLD, KINDS, space_kind, _state,
    MAX_TEXTURES, _key_ctx,
)
from .eligibility import (
    _previewable_outputs, _preview_targets, _socket_enum_cache,
    _npv_socket_items, _zone_cache, _in_zone,
)
from .i18n import TR, _t
from .preview_scene import _env_items, _cleanup_datablocks
from .sources import _idref, resolve_source
from .renderers import _instance_chain
from . import queue
from .queue import _reset_cache, _prune_cache
from .timer import (
    _timer, _ensure_timer, _on_depsgraph, _on_frame_change, _on_load_post,
    _on_save_pre,
)
from .drawing import _editor_hint, draw_callback

# --------------------------------------------------------------------------- #
#  Properties
# --------------------------------------------------------------------------- #
def _toggle_enabled(self, context):
    if self.enabled:
        _state["dirty"] = True
        _ensure_timer()
    queue._tag_node_editors()


def _mark_dirty(self, context):
    _state["dirty"] = True


def _toggle_auto_update(self, context):
    # Turned back on: catch up on whatever changed while it was off.
    if self.auto_update:
        _state["dirty"] = True
        _ensure_timer()


def _redraw(self, context):
    queue._tag_node_editors()


def _node_show_update(self, context):
    _state["dirty"] = True
    queue._tag_node_editors()


def _scope_update(self, context):
    _state["dirty"] = True
    _state["sel_sig"] = None
    queue._tag_node_editors()


class NPVProps(bpy.types.PropertyGroup):
    language: bpy.props.EnumProperty(
        name="Language",
        description="UI language. Auto follows Blender's language setting "
                    "(non-Chinese falls back to English)",
        items=[("AUTO", "Auto", "Follow Blender's language setting"),
               ("EN", "EN", "English"),
               ("ZH", "中文", "Chinese")],
        default="AUTO")
    enabled: bpy.props.BoolProperty(name="Show Previews", default=True, update=_toggle_enabled)
    auto_update: bpy.props.BoolProperty(
        name="Auto Update", default=True, update=_toggle_auto_update)
    only_tex_shader: bpy.props.BoolProperty(
        name="Only Texture / Shader Nodes", default=True, update=_mark_dirty)
    preview_scope: bpy.props.EnumProperty(
        name="Preview Scope",
        description="Which nodes get a preview thumbnail",
        items=[("ALL", "All", "Preview every eligible node"),
               ("SELECTED", "Selected", "Only preview nodes that are selected "
                "in the editor — select nodes to control what shows"),
               ("MARKED", "Marked", "Only preview nodes whose 'Show Preview' "
                "checkbox is on (right-click a node, or use the buttons below)")],
        default="ALL", update=_scope_update)
    show_all_outputs: bpy.props.BoolProperty(
        name="Show All Linked Outputs",
        description="Preview every linked output of a node side by side (in a "
                    "2-column grid), instead of a single Preview Socket. One "
                    "render per linked output",
        default=False, update=_node_show_update)
    resolution: bpy.props.EnumProperty(
        name="Quality",
        items=[("64", "Low (64px)", ""), ("128", "Medium (128px)", ""),
               ("256", "High (256px)", "")],
        default="128", update=_mark_dirty)
    batch_size: bpy.props.IntProperty(
        name="Nodes / Tick",
        description="Most previews rendered per step",
        default=2, min=1, max=8)
    time_budget: bpy.props.IntProperty(
        name="Time Budget",
        description="Stop a step's renders once this much time is spent (at "
                    "least one render per step). Lower = smoother UI, slower "
                    "refresh",
        default=250, min=20, max=2000)
    update_on_frame: bpy.props.BoolProperty(
        name="Update on Frame Change",
        description="Re-render previews when the frame changes (Scene Time, "
                    "image sequences, simulations) and keep rendering during "
                    "playback. Off: previews pause while animation plays",
        default=False, update=_mark_dirty)
    shader_shape: bpy.props.EnumProperty(
        name="Shader Shape",
        items=[("SPHERE", "Sphere", "Material-ball preview, lit"),
               ("CUBE", "Cube", "Lit cube, three faces visible"),
               ("PLANE", "Plane", "Flat lit swatch")],
        default="SPHERE", update=_mark_dirty)
    preview_env: bpy.props.EnumProperty(
        name="Environment",
        description="Lighting for shader balls: an even white world, or one of "
                    "Blender's bundled studio HDRIs",
        items=_env_items, update=_mark_dirty)
    world_strength: bpy.props.FloatProperty(
        name="World Light", default=1.0, min=0.0, max=10.0, update=_mark_dirty)
    sun_strength: bpy.props.FloatProperty(
        name="Key Light", default=2.0, min=0.0, max=20.0, update=_mark_dirty)
    preview_geometry: bpy.props.BoolProperty(
        name="Geometry Nodes",
        description="Preview geometry-output nodes as a small 3D render",
        default=False, update=_mark_dirty)
    geo_fields: bpy.props.BoolProperty(
        name="Texture / Math Nodes",
        description="Also preview texture / math / colour nodes in Geometry "
                    "Nodes as a flat swatch (isolated node, socket defaults)",
        default=True, update=_node_show_update)
    preview_compositor: bpy.props.BoolProperty(
        name="Compositor",
        description="Preview compositor nodes (each preview renders the scene "
                    "through the compositor)",
        default=False, update=_mark_dirty)
    comp_groups: bpy.props.BoolProperty(
        name="Inside Node Groups",
        description="Also preview the nodes inside a compositor node group when "
                    "you enter it (each node renders the scene once)",
        default=True, update=_node_show_update)
    preview_world: bpy.props.BoolProperty(
        name="World",
        description="Preview world / environment shader nodes",
        default=True, update=_mark_dirty)
    # -- Display (drawing only; nothing re-renders)
    thumb_scale: bpy.props.FloatProperty(
        name="Thumbnail Size",
        description="Thumbnail width relative to the node's width",
        default=1.0, min=0.25, max=3.0, update=_redraw)
    thumb_position: bpy.props.EnumProperty(
        name="Position",
        description="Where the thumbnail sits relative to its node",
        items=[("ABOVE", "Above", "Above the node"),
               ("BELOW", "Below", "Below the node"),
               ("LEFT", "Left", "Left of the node"),
               ("RIGHT", "Right", "Right of the node")],
        default="ABOVE", update=_redraw)
    zoom_active: bpy.props.BoolProperty(
        name="Enlarge Active Node",
        description="Draw the active node's thumbnail larger, on top "
                    "(Ctrl+Alt+Z in the node editor)",
        default=False, update=_redraw)
    zoom_factor: bpy.props.FloatProperty(
        name="Enlarge",
        description="Size of the active node's thumbnail when enlarged",
        default=2.5, min=1.25, max=6.0, update=_redraw)
    checker_bg: bpy.props.BoolProperty(
        name="Checkerboard",
        description="Draw a checkerboard behind thumbnails so transparency "
                    "shows",
        default=False, update=_redraw)
    show_status: bpy.props.BoolProperty(
        name="Status Markers",
        description="Orange outline: waiting to re-render. Red outline / '!': "
                    "the last render failed (see the system console)",
        default=True, update=_redraw)
    show_values: bpy.props.BoolProperty(
        name="Show Values",
        description="Write the number on a Value swatch when the whole swatch "
                    "is one value (e.g. a Math node with fixed inputs)",
        default=True, update=_mark_dirty)


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


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #
class NPV_OT_help(bpy.types.Operator):
    bl_idname = "node.npv_help"
    bl_label = "Node Preview — Help"
    bl_description = "Explain what each option and button does"
    bl_options = {"REGISTER"}

    # One page at a time: the whole help no longer fits a popup's height, and
    # popups do not scroll. The last page shown is remembered (operator prop).
    page: bpy.props.EnumProperty(
        name="Page",
        items=[(pid, label, "") for pid, label in TR["EN"]["help_tabs"]],
        default="GENERAL")

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=520)

    def draw(self, context):
        layout = self.layout
        props = context.scene.npv
        row = layout.row(align=True)
        row.prop(props, "language", expand=True)
        layout.label(text=_t(props, "help_title"), icon="INFO")
        row = layout.row(align=True)
        for pid, label in _t(props, "help_tabs"):
            row.prop_enum(self, "page", pid, text=label)
        layout.separator()
        for kind, text in _t(props, "help").get(self.page, ()):
            if kind == "sec":
                layout.separator()
            layout.label(text=text)


class NPV_PT_panel(bpy.types.Panel):
    bl_label = "Node Preview"
    bl_idname = "NPV_PT_panel"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Preview"

    @classmethod
    def poll(cls, context):
        sp = context.space_data
        return sp and sp.type == "NODE_EDITOR" and sp.tree_type in KINDS

    def draw(self, context):
        layout = self.layout
        props = context.scene.npv
        kind = space_kind(context.space_data)
        t = lambda k: _t(props, k)

        row = layout.row(align=True)
        row.prop(props, "enabled", toggle=True, text=t("show_previews"),
                 icon="HIDE_OFF" if props.enabled else "HIDE_ON")
        row.prop(props, "language", expand=True)
        row.operator("node.npv_help", text="", icon="QUESTION")

        body = layout.column()
        body.enabled = props.enabled
        col = body.column(align=True)
        col.prop(props, "auto_update", text=t("auto_update"))
        col.prop(props, "resolution", text=t("quality"))
        col.prop(props, "batch_size", text=t("nodes_tick"))
        col.prop(props, "time_budget", text=t("time_budget"))
        col.prop(props, "update_on_frame", text=t("update_on_frame"))
        col.label(text=t("engine_fmt")
                  % context.scene.render.engine.replace("BLENDER_", "").title())
        if kind in (KIND_SHADER, KIND_WORLD):
            col.prop(props, "only_tex_shader", text=t("only_tex"))

        mbox = body.box()
        mbox.label(text=t("scope"))
        mbox.prop(props, "preview_scope", expand=True)
        if props.preview_scope == "SELECTED":
            mbox.label(text=t("scope_sel_hint"), icon="RESTRICT_SELECT_OFF")
        elif props.preview_scope == "MARKED":
            an = context.active_node
            if an is not None:
                mbox.prop(an, "npv_show", text=t("preview_active"), toggle=True)
            r = mbox.row(align=True)
            r.operator("node.npv_mark_selected", text=t("mark_sel")).mark = True
            r.operator("node.npv_mark_selected", text=t("unmark_sel")).mark = False

        obox = body.box()
        obox.prop(props, "show_all_outputs", text=t("show_all_outputs"))
        an = context.active_node
        if an is not None and not props.show_all_outputs \
                and len(_previewable_outputs(an, kind)) > 1:
            obox.prop(an, "npv_socket", text=t("preview_socket"))

        if kind == KIND_SHADER:
            box = body.box()
            box.label(text=t("shader_box"), icon="SHADING_RENDERED")
            box.prop(props, "shader_shape", expand=True)
            box.prop(props, "preview_env", text=t("environment"))
            box.prop(props, "world_strength", slider=True, text=t("world_light"))
            sub = box.column(align=True)
            sub.enabled = (props.shader_shape != "PLANE")
            sub.prop(props, "sun_strength", slider=True, text=t("key_light"))

        dbox = body.box()
        dbox.label(text=t("display_box"), icon="IMAGE_BACKGROUND")
        dbox.prop(props, "thumb_scale", slider=True, text=t("thumb_scale"))
        dbox.prop(props, "thumb_position", text=t("thumb_position"))
        r = dbox.row(align=True)
        r.prop(props, "zoom_active", text=t("zoom_active"), toggle=True)
        sub = r.row(align=True)
        sub.enabled = props.zoom_active
        sub.prop(props, "zoom_factor", text="")
        r = dbox.row()
        r.prop(props, "checker_bg", text=t("checker_bg"))
        r.prop(props, "show_status", text=t("show_status"))
        dbox.prop(props, "show_values", text=t("show_values"))

        box = body.box()
        box.label(text=t("other_editors"), icon="NODETREE")
        box.prop(props, "preview_world", text=t("world"))
        box.prop(props, "preview_geometry", text=t("geometry"))
        sub = box.row()
        sub.enabled = props.preview_geometry
        sub.separator(factor=2.0)
        sub.prop(props, "geo_fields", text=t("geo_fields"))
        box.prop(props, "preview_compositor", text=t("compositor"))
        sub = box.row()
        sub.enabled = props.preview_compositor
        sub.separator(factor=2.0)
        sub.prop(props, "comp_groups", text=t("comp_groups"))
        if kind == KIND_COMP and props.preview_compositor:
            box.label(text=t("comp_note1"), icon="INFO")
            box.label(text=t("comp_note2"), icon="BLANK1")

        row = body.row(align=True)
        row.operator("node.npv_refresh", text=t("refresh"), icon="FILE_REFRESH")
        row.operator("node.npv_export", text="", icon="EXPORT")
        row.operator("node.npv_clear", text="", icon="TRASH")

        if _state["queue"]:
            body.label(text=t("rendering_fmt") % len(_state["queue"]),
                       icon="SORTTIME")
        nfail = 0
        et = getattr(context.space_data, "edit_tree", None)
        if et is not None:
            # Only this editor's view: failures of the same tree through
            # another source (another object ...) don't apply here.
            try:
                vc = _state["editors"].get(context.space_data.as_pointer(), {}).get("ctx")
            except Exception:
                vc = None
            pre = "%d:" % et.as_pointer()
            nfail = sum(1 for k in _state["failed"] if k.startswith(pre)
                        and (vc is None or _key_ctx(k) in (vc, "")))
        if nfail:
            body.label(text=t("failed_fmt") % nfail, icon="ERROR")
        body.label(text=t("cached_fmt") % (len(_state["textures"]), queue._max_textures()),
                   icon="IMAGE_DATA")


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
