# SPDX-License-Identifier: GPL-3.0-or-later
"""The add-on's scene settings (Scene.npv) and their update callbacks."""

import bpy

from . import preview_scene, queue
from .common import _state
from .preview_scene import _env_items
from .timer import _ensure_timer


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


def _comp_cache_mode_update(self, context):
    # Part of the compositor previews' hash: they all render again. Off
    # never reads the cache, so it goes.
    if self.comp_cache_mode == "OFF":
        preview_scene.drop_comp_cache()
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
        description="Preview compositor nodes (rendered through the "
                    "compositor; Compositor Scene Cache sets how the scene is "
                    "rendered for them)",
        default=False, update=_mark_dirty)
    comp_groups: bpy.props.BoolProperty(
        name="Inside Node Groups",
        description="Also preview the nodes inside a compositor node group when "
                    "you enter it",
        default=True, update=_node_show_update)
    comp_cache_mode: bpy.props.EnumProperty(
        name="Compositor Scene Cache",
        description="How compositor previews get the scene they composite",
        items=[("OFF", "Off", "Render the scene for every compositor node: "
                "always current, slowest"),
               ("SIMPLE", "Simple", "Render the scene once and re-composite "
                "each node from it. Follows the edits Blender reports, and "
                "the content of objects hidden in the viewport or in "
                "excluded collections (see the help for what it can miss)"),
               ("ADVANCED", "Advanced", "Like Simple, with a checklist of "
                "what is checked on hidden objects: tick more to cover more "
                "kinds of edits, untick to make the check cheaper. Its "
                "defaults equal Simple")],
        default="SIMPLE", update=_comp_cache_mode_update)
    # What Advanced checks on objects no window evaluates (hidden in the
    # viewport, or in excluded collections). No update callback: a change is
    # picked up by the next compositor preview (the set is part of the scene
    # cache's key) and re-renders no thumbnail.
    comp_sig_xform: bpy.props.BoolProperty(
        name="Transform and Visibility",
        description="Hidden objects' location, rotation, scale, parent and "
                    "visibility. Very low cost. If unticked, press Refresh "
                    "after changing this",
        default=True)
    comp_sig_modifiers: bpy.props.BoolProperty(
        name="Modifiers",
        description="Hidden objects' modifier settings, including Geometry "
                    "Nodes trees. Low cost. If unticked, press Refresh after "
                    "changing this",
        default=True)
    comp_sig_constraints: bpy.props.BoolProperty(
        name="Constraints",
        description="Hidden objects' constraint settings and target names. "
                    "Very low cost. If unticked, press Refresh after "
                    "changing this",
        default=True)
    comp_sig_data: bpy.props.BoolProperty(
        name="Object Data",
        description="Hidden objects' mesh / curve data (vertex positions, "
                    "attributes ...). Cost grows with the vertex count. If "
                    "unticked, press Refresh after changing this",
        default=True)
    comp_sig_materials: bpy.props.BoolProperty(
        name="Materials and Light Nodes",
        description="Hidden objects' materials and light node trees "
                    "(structure and node settings, not image content). "
                    "Medium cost. If unticked, press Refresh after changing "
                    "this",
        default=True)
    comp_sig_images: bpy.props.BoolProperty(
        name="Image Settings",
        description="The images those node trees use (generated settings, "
                    "file changes, painting). Very low cost. If unticked, "
                    "press Refresh after changing this",
        default=True)
    comp_sig_refs: bpy.props.BoolProperty(
        name="Referenced Objects",
        description="Objects hidden objects reach (parent, constraint "
                    "targets, Boolean / Array objects, Object Info sources, "
                    "Texture Coordinate objects ...), transitively, even "
                    "when disabled in renders. Medium to high cost: a large "
                    "referenced collection can make it expensive. If "
                    "unticked, press Refresh after changing this",
        default=False)
    comp_sig_particles: bpy.props.BoolProperty(
        name="Particles and Textures",
        description="Hidden objects' particle settings, and the textures "
                    "their modifiers and particles use. Medium cost. If "
                    "unticked, press Refresh after changing this",
        default=False)
    comp_sig_drivers: bpy.props.BoolProperty(
        name="Drivers",
        description="Drivers on hidden objects, their data, materials and "
                    "node trees: definitions and target values (expressions "
                    "are never run). Medium cost. If unticked, press Refresh "
                    "after changing this",
        default=False)
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
