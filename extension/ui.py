# SPDX-License-Identifier: GPL-3.0-or-later
"""The sidebar panel and the help popup."""

import bpy

from . import queue
from .common import (
    KINDS, KIND_COMP, KIND_SHADER, KIND_WORLD, _key_ctx, _state, space_kind,
)
from .eligibility import _previewable_outputs
from .i18n import TR, _t


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
