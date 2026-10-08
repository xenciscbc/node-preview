# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared constants, the add-on's runtime state (_state) and cache-key helpers."""

import hashlib

import bpy
from mathutils import Vector


KIND_SHADER = "ShaderNodeTree"
KIND_GEO = "GeometryNodeTree"
KIND_COMP = "CompositorNodeTree"
KIND_WORLD = "World"  # a Shader Editor in World mode (not a tree_type)
KINDS = {KIND_SHADER, KIND_GEO, KIND_COMP}  # valid space.tree_type values
PREVIEW_PREV_WORLD = "NPV_prev_world"


def space_kind(space):
    """Map a node-editor space to our preview kind, distinguishing a Shader
    Editor showing the World from one showing a material."""
    k = space.tree_type
    if k == KIND_SHADER and getattr(space, "shader_type", "OBJECT") == "WORLD":
        return KIND_WORLD
    return k

PREVIEW_SCENE = "NPV_preview_scene"
PREVIEW_PLANE = "NPV_preview_plane"
PREVIEW_SPHERE = "NPV_preview_sphere"
PREVIEW_CUBE = "NPV_preview_cube"
ENV_IMAGE_PREFIX = "NPV_env_"
PREVIEW_CAM = "NPV_preview_cam"
PREVIEW_SUN = "NPV_preview_sun"
PREVIEW_MAT_TMP = "NPV_preview_tmp_mat"
GEO_CLAY_MAT = "NPV_geo_clay"
GEO_WORLD_STRENGTH = 0.25
GEO_SUN_STRENGTH = 4.0
GEO_SUN_DIR = Vector((0.3, -0.6, 0.9)).normalized()  # towards the light

SHADER_OUTPUT_NODES = {"ShaderNodeOutputMaterial", "ShaderNodeOutputWorld",
                       "ShaderNodeOutputLight"}
VOLUME_NODES = {"ShaderNodeVolumePrincipled", "ShaderNodeVolumeScatter",
                "ShaderNodeVolumeAbsorption"}
SKIP_TYPES = {"FRAME", "REROUTE"}
SKIP_IDN = {"NodeGroupInput", "NodeGroupOutput"}

COLOR_VECTOR_NODES = {
    "ShaderNodeValToRGB", "ShaderNodeMixRGB", "ShaderNodeMix", "ShaderNodeRGB",
    "ShaderNodeRGBCurve", "ShaderNodeMapping", "ShaderNodeTexCoord",
    "ShaderNodeNormalMap", "ShaderNodeBump", "ShaderNodeBrightContrast",
    "ShaderNodeGamma", "ShaderNodeHueSaturation", "ShaderNodeInvert",
    "ShaderNodeCombineColor", "ShaderNodeSeparateColor", "ShaderNodeBlackbody",
    "ShaderNodeWavelength", "ShaderNodeVertexColor", "ShaderNodeAttribute",
}

_state = {
    "draw_handle": None, "textures": {}, "hashes": {}, "queue": [],
    "queued_keys": set(), "dirty": True, "rendering": False,
    "timer_running": False, "active_tree_ptr": None, "active_kind": None,
    "active_path": None,  # editor tree path (pointers, outermost first)
    "shader_image": None, "sel_sig": None,
    "img_gen": {},        # image name -> update counter (texture paint)
    "xform_watch": set(), # objects whose transform a preview's hash holds
    "tex_tick": {},       # texture key -> last-used tick (cache eviction)
    "tick": 0, "prune_in": 0,
    "failed": {},         # texture key -> hash whose render failed (no retry)
    "values": {},         # texture key -> number shown on a uniform Value swatch
    "last_value": None,   # set by the loader for the render in progress
    "visible": set(),     # keys of nodes on screen (rendered first)
    "priority": set(),    # keys of the active / selected nodes (rendered first)
    "src_hint": [],       # (kind, name) of the data-blocks the editor shows
    "data_sigs": {},      # object data ref -> content fingerprint (_data_sig)
    "data_gen": {},       # object data ref -> update count (cache invalidation
                          # only; never part of a hash, see _data_sig)
    # Every node editor showing previews, by space pointer: its tree, kind,
    # path, source hint, selection signature and on-screen / priority keys.
    # One global "active editor" made two open editors overwrite each other.
    "editors": {},
    "export_to": None,    # file path: the next render is copied there instead
    "tree_sig_memo": None,  # tree pointer -> tree_signature, during a rebuild
}
MAX_TEXTURES = 256        # cached thumbnails kept before evicting least-used


def _key(tree, node_name):
    return "%d:%s" % (tree.as_pointer(), node_name)


def _skey(tree, node_name, out_id, ctx=""):
    """Cache key: tree, node, previewed output socket ('' for socketless) and
    the view context (see _view_ctx) after '#'. The context keeps apart the
    thumbnails of one tree seen through different sources -- a GN tree on two
    objects, a group entered from two materials -- so two editors showing
    them don't overwrite each other's thumbnails."""
    key = "%d:%s|%s" % (tree.as_pointer(), node_name, out_id or "")
    return key + "#" + ctx if ctx else key


def _view_ctx(src, chain):
    """Short id of the source data-block and the group-node chain a tree is
    previewed through."""
    return hashlib.md5(repr((src, list(chain or ()))).encode(
        "utf-8", "replace")).hexdigest()[:10]


def _key_ctx(key):
    return key.rsplit("#", 1)[1] if "#" in key else ""


def _engine_id(props=None):
    """Follow the scene's own render engine (EEVEE / Cycles). Falls back to
    EEVEE for engines we don't render previews with (e.g. Workbench)."""
    e = bpy.context.scene.render.engine
    if e in {"CYCLES", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}:
        return e
    return "BLENDER_EEVEE"
