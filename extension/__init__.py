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

Tested on Blender 5.2 (EEVEE + Cycles, Vulkan). Legacy add-on: install via
Preferences > Add-ons > (v) Install from Disk...
"""


# NOTE: This is the Blender Extension build. Metadata lives in
# blender_manifest.toml (no bl_info needed for extensions).

import os
import time
import shutil
import hashlib
import zlib

import bpy
import bmesh
from bpy.app.handlers import persistent
import gpu
import blf
from mathutils import Vector
import numpy as np
from gpu.types import GPUTexture, Buffer
from gpu_extras.batch import batch_for_shader

# --------------------------------------------------------------------------- #
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
    "tex_tick": {},       # texture key -> last-used tick (cache eviction)
    "tick": 0, "prune_in": 0,
    "failed": {},         # texture key -> hash whose render failed (no retry)
    "values": {},         # texture key -> number shown on a uniform Value swatch
    "last_value": None,   # set by the loader for the render in progress
    "visible": set(),     # keys of nodes on screen (rendered first)
    "priority": set(),    # keys of the active / selected nodes (rendered first)
    "src_hint": [],       # (kind, name) of the data-blocks the editor shows
    "export_to": None,    # file path: the next render is copied there instead
    "data_gen": {},       # object data name -> update counter (mesh edits)
    "tree_sig_memo": None,  # tree pointer -> tree_signature, during a rebuild
}
MAX_TEXTURES = 256        # cached thumbnails kept before evicting least-used


def _key(tree, node_name):
    return "%d:%s" % (tree.as_pointer(), node_name)


def _skey(tree, node_name, out_id):
    """Cache key including the previewed output socket ('' for socketless)."""
    return "%d:%s|%s" % (tree.as_pointer(), node_name, out_id or "")


def _engine_id(props=None):
    """Follow the scene's own render engine (EEVEE / Cycles). Falls back to
    EEVEE for engines we don't render previews with (e.g. Workbench)."""
    e = bpy.context.scene.render.engine
    if e in {"CYCLES", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}:
        return e
    return "BLENDER_EEVEE"


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
        if not _previewable_outputs(node, kind):
            return False
        # Field-swatch nodes (no geometry output) are gated by a checkbox.
        if not any(s.type == "GEOMETRY" for s in node.outputs):
            return getattr(props, "geo_fields", True)
        return True
    if kind == KIND_COMP:
        return first_enabled_output(node) is not None
    return False


def renders_as_shader(node):
    if node.bl_idname in SHADER_OUTPUT_NODES:
        return True
    o = first_enabled_output(node)
    return o is not None and o.type == "SHADER"


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


# --------------------------------------------------------------------------- #
#  Preview scene
# --------------------------------------------------------------------------- #
def _new_uv_mesh(name, build):
    """Mesh built by ``build(bm)`` with a UV layer. bmesh ``calc_uvs`` only
    fills an existing UV layer; it never creates one."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")
    build(bm)
    bm.to_mesh(me)
    bm.free()
    return me


def _drop_if_no_uv(obj):
    """Remove a preview object left by an older version without UVs (image
    textures would sample a single texel); returns None so it gets rebuilt."""
    if obj is None or getattr(obj.data, "uv_layers", None):
        return obj
    me = obj.data
    bpy.data.objects.remove(obj)
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)
    return None


_env_enum_cache = []


def _env_dir():
    try:
        return bpy.utils.system_resource("DATAFILES", path="studiolights/world") or ""
    except Exception:
        return ""


def _env_items(self, context):
    """'Uniform' plus Blender's bundled studio-light HDRIs."""
    items = [("UNIFORM", "Uniform", "Even white environment (World Light sets "
              "its strength)", 0)]
    d = _env_dir()
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.lower().endswith((".exr", ".hdr")))
    except OSError:
        files = []
    used = {0}
    for f in files:
        label = os.path.splitext(f)[0].replace("_", " ").title()
        items.append((f, label, "Light the preview with Blender's '%s' HDRI" % label,
                      _enum_num(f, used)))
    _env_enum_cache[:] = items
    return items


def _env_image(filename):
    """The bundled HDRI ``filename`` as an image (loaded once), or None."""
    name = ENV_IMAGE_PREFIX + filename
    img = bpy.data.images.get(name)
    if img is not None:
        return img
    path = os.path.join(_env_dir(), filename)
    if not os.path.isfile(path):
        return None
    try:
        img = bpy.data.images.load(path, check_existing=False)
    except RuntimeError:
        return None
    img.name = name
    return img


def _setup_env(wnt, bg, env, world_strength):
    """Feed the preview world's Background from a bundled HDRI, or white."""
    tex = wnt.nodes.get("NPV_env")
    img = _env_image(env) if env and env != "UNIFORM" else None
    if img is None:
        if tex is not None:
            wnt.nodes.remove(tex)
        bg.inputs[0].default_value = (1, 1, 1, 1)
    else:
        if tex is None:
            tex = wnt.nodes.new("ShaderNodeTexEnvironment")
            tex.name = "NPV_env"
        tex.image = img
        if not bg.inputs[0].is_linked:
            wnt.links.new(tex.outputs["Color"], bg.inputs[0])
    bg.inputs[1].default_value = world_strength


def ensure_preview_scene(res, world_strength=1.0, sun_strength=2.0,
                         engine="BLENDER_EEVEE", env="UNIFORM"):
    scn = bpy.data.scenes.get(PREVIEW_SCENE)
    if scn is None:
        scn = bpy.data.scenes.new(PREVIEW_SCENE)
    # Render at the user's current frame (animated geometry, drivers, keyed
    # material values), not the preview scene's own frame 1.
    user_scene = bpy.context.scene
    if user_scene is not None and user_scene != scn:
        scn.frame_current = user_scene.frame_current
    try:
        scn.render.engine = engine
    except TypeError:
        scn.render.engine = "BLENDER_EEVEE"
    if scn.render.engine == "CYCLES":
        try:
            scn.cycles.samples = 16
            scn.cycles.use_denoising = True
        except Exception:
            pass
    r = scn.render
    r.resolution_x = res
    r.resolution_y = res
    r.resolution_percentage = 100
    r.film_transparent = True
    r.use_compositing = False
    r.use_sequencer = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    r.image_settings.color_depth = "8"
    try:
        scn.view_settings.view_transform = "Standard"
        scn.display_settings.display_device = "sRGB"
    except Exception:
        pass

    if scn.world is None:
        scn.world = bpy.data.worlds.get("NPV_world") or bpy.data.worlds.new("NPV_world")
    wnt = scn.world.node_tree
    bg = next((n for n in wnt.nodes if n.bl_idname == "ShaderNodeBackground"), None)
    if bg is None:
        bg = wnt.nodes.new("ShaderNodeBackground")
        wout = next((n for n in wnt.nodes if n.bl_idname == "ShaderNodeOutputWorld"), None) \
            or wnt.nodes.new("ShaderNodeOutputWorld")
        wnt.links.new(bg.outputs[0], wout.inputs["Surface"])
    _setup_env(wnt, bg, env, world_strength)

    plane = _drop_if_no_uv(bpy.data.objects.get(PREVIEW_PLANE))
    if plane is None:
        me = _new_uv_mesh(PREVIEW_PLANE + "_mesh", lambda bm: bmesh.ops.create_grid(
            bm, x_segments=1, y_segments=1, size=1.0, calc_uvs=True))
        plane = bpy.data.objects.new(PREVIEW_PLANE, me)
    if plane.name not in scn.collection.objects:
        scn.collection.objects.link(plane)
    plane.location = (0, 0, 0)
    plane.rotation_euler = (0, 0, 0)
    plane.scale = (1.04, 1.04, 1.0)

    sphere = _drop_if_no_uv(bpy.data.objects.get(PREVIEW_SPHERE))
    if sphere is None:
        me = _new_uv_mesh(PREVIEW_SPHERE + "_mesh", lambda bm: bmesh.ops.create_uvsphere(
            bm, u_segments=48, v_segments=24, radius=0.92, calc_uvs=True))
        for poly in me.polygons:
            poly.use_smooth = True
        sphere = bpy.data.objects.new(PREVIEW_SPHERE, me)
    if sphere.name not in scn.collection.objects:
        scn.collection.objects.link(sphere)
    sphere.location = (0, 0, 0)

    cube = _drop_if_no_uv(bpy.data.objects.get(PREVIEW_CUBE))
    if cube is None:
        me = _new_uv_mesh(PREVIEW_CUBE + "_mesh", lambda bm: bmesh.ops.create_cube(
            bm, size=1.0, calc_uvs=True))
        cube = bpy.data.objects.new(PREVIEW_CUBE, me)
    if cube.name not in scn.collection.objects:
        scn.collection.objects.link(cube)
    cube.location = (0, 0, 0)
    # Tilted so three faces show (a cube seen face-on reads as a square).
    cube.rotation_euler = (0.6155, 0.0, 0.7854)
    cube.scale = (0.98, 0.98, 0.98)
    cube.hide_render = True      # only render_shader's Cube shape shows it

    cam = bpy.data.objects.get(PREVIEW_CAM)
    if cam is None:
        cd = bpy.data.cameras.new(PREVIEW_CAM + "_data")
        cam = bpy.data.objects.new(PREVIEW_CAM, cd)
    if cam.name not in scn.collection.objects:
        scn.collection.objects.link(cam)
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = 2.0
    cam.location = (0, 0, 2)
    cam.rotation_euler = (0, 0, 0)
    scn.camera = cam

    sun = bpy.data.objects.get(PREVIEW_SUN)
    if sun is None:
        sd = bpy.data.lights.new(PREVIEW_SUN + "_data", type="SUN")
        sun = bpy.data.objects.new(PREVIEW_SUN, sd)
    if sun.name not in scn.collection.objects:
        scn.collection.objects.link(sun)
    sun.data.type = "SUN"
    sun.data.energy = sun_strength
    sun.rotation_euler = (0.9, 0.15, 0.5)
    return scn, plane, sphere


def _linear_to_srgb(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92,
                    1.055 * np.power(v, 1.0 / 2.4) - 0.055)


def _load_render(path):
    """Read a preview render back: (width, height, float32 RGBA pixels ready
    to display, number or None). ``foreach_get`` into a numpy array is far
    faster than ``img.pixels[:]`` (a Python list of w*h*4 floats).

    A '.exr' render is a Value swatch encoded by _value_emission(): scene-
    linear R = max(v, 0), G = max(-v, 0). It is shown as the grey the PNG path
    would give (Standard view = sRGB, clipped) and, when every pixel holds the
    same number, that number is returned for drawing on the thumbnail."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            return None
        px = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
    finally:
        bpy.data.images.remove(img)
    value = None
    if path.lower().endswith(".exr"):
        px = px.reshape(-1, 4)
        a = px[:, 3]
        solid = a > 0.5
        r = np.where(a > 1e-6, px[:, 0] / np.maximum(a, 1e-6), 0.0)
        g = np.where(a > 1e-6, px[:, 1] / np.maximum(a, 1e-6), 0.0)
        if solid.any():
            v = (r - g)[solid]
            lo, hi = float(v.min()), float(v.max())
            if hi - lo <= 1e-3 * max(1.0, abs(hi), abs(lo)):
                value = float(v.mean())
        grey = _linear_to_srgb(r).astype(np.float32)
        px = np.stack([grey, grey, grey, a], axis=1).reshape(-1)
    return w, h, np.ascontiguousarray(px, dtype=np.float32), value


def _png_to_texture(path):
    got = _load_render(path)
    if got is None:
        return None
    w, h, px, value = got
    _state["last_value"] = value
    buf = Buffer("FLOAT", w * h * 4, px)
    return GPUTexture((w, h), format="RGBA16F", data=buf)


def _finish(path):
    """Hand a finished render on: to the thumbnail cache, or -- while the
    Export operator runs -- copied to the chosen file."""
    dst = _state.get("export_to")
    if dst:
        shutil.copyfile(path, dst)
        return True
    return _png_to_texture(path)


def _render_scene(scn):
    ext = ".exr" if scn.render.image_settings.file_format == "OPEN_EXR" else ".png"
    path = os.path.join(bpy.app.tempdir, "npv_render" + ext)
    # The previous preview's file must not pass for this one if the render
    # writes nothing.
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    scn.render.filepath = path
    # Override only the scene: adding a window makes render report FINISHED
    # without writing the file.
    with bpy.context.temp_override(scene=scn):
        bpy.ops.render.render(write_still=True)
    if not os.path.isfile(path):
        raise RuntimeError("render finished without writing %s" % path)
    return path


# --------------------------------------------------------------------------- #
#  Renderers
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  Node groups
# --------------------------------------------------------------------------- #
def _instance_chain(path):
    """Names of the group nodes leading through ``path`` (the editor's tree
    path, outermost first): for each parent tree, the group node that uses the
    next tree -- the active one if it qualifies (Tab enters the active group),
    else the first. [] for a top-level tree, None if the path is broken."""
    names = []
    for parent, child in zip(path, path[1:]):
        cands = [n for n in parent.nodes
                 if n.type == "GROUP" and getattr(n, "node_tree", None) == child]
        if not cands:
            return None
        act = parent.nodes.active
        names.append(act.name if act in cands else cands[0].name)
    return names


def _inputs_sig(node, memo):
    """Fingerprint of what flows *into* ``node`` (not its own settings)."""
    parts = []
    for inp in node.inputs:
        if inp.is_linked:
            parts.append((inp.identifier, tuple(
                (l.from_socket.identifier, upstream_hash(l.from_node, memo))
                for l in inp.links)))
        else:
            parts.append((inp.identifier, _socket_default(inp)))
    return tuple(parts)


def _context_sig(src, path, chain):
    """For a node inside a group: the source data-block plus the inputs of
    every enclosing group node, so entering the same group from another
    material / with other inputs re-renders."""
    parts = [src]
    for parent, name in zip(path, chain):
        inst = parent.nodes.get(name)
        parts.append((name, _inputs_sig(inst, {}) if inst is not None else None))
    return hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()


def _geometry_out(node, out_id):
    if node is None:
        return None
    s = None
    if out_id:
        s = next((o for o in node.outputs
                  if o.identifier == out_id and o.type == "GEOMETRY"), None)
    return s or next((o for o in node.outputs if o.type == "GEOMETRY"), None)


def _route_out(top_tree, chain, node_name, pick):
    """Expose a node inside nested groups at the top level of ``top_tree``
    (which must already be a throwaway copy). Each group along ``chain`` is
    copied and re-assigned, gets an extra output socket, and the node's output
    (chosen by ``pick(node)``) is wired out level by level. Returns
    (node, socket in top_tree or None, [copied groups to remove])."""
    copies, insts = [], []
    tree = top_tree
    for name in chain:
        inst = tree.nodes.get(name)
        if inst is None or getattr(inst, "node_tree", None) is None:
            return None, None, copies
        g = inst.node_tree.copy()
        copies.append(g)
        inst.node_tree = g
        insts.append(inst)
        tree = g
    node = tree.nodes.get(node_name)
    sock = pick(node) if node is not None else None
    if sock is None:
        return node, None, copies
    for g, inst in zip(reversed(copies), reversed(insts)):
        stype = {"SHADER": "NodeSocketShader", "VALUE": "NodeSocketFloat",
                 "GEOMETRY": "NodeSocketGeometry"}.get(sock.type, "NodeSocketColor")
        item = g.interface.new_socket("NPV Preview", in_out="OUTPUT", socket_type=stype)
        gout = _active_output(g.nodes, "NodeGroupOutput") \
            or g.nodes.new("NodeGroupOutput")
        gin = next((i for i in gout.inputs if i.identifier == item.identifier), None)
        nxt = next((o for o in inst.outputs if o.identifier == item.identifier), None)
        if gin is None or nxt is None:
            return node, None, copies
        g.links.new(sock, gin)
        sock = nxt
    return node, sock, copies


def _active_output(nodes, idname):
    """The output node of type ``idname`` Blender uses (the active one), else
    the first, else None."""
    outs = [n for n in nodes if n.bl_idname == idname]
    return next((n for n in outs if getattr(n, "is_active_output", False)),
                outs[0] if outs else None)


def _sole_output(nt, idname, keep=None):
    """Make ``keep`` (default: the active one, created if missing) the only
    ``idname`` output of the throwaway tree ``nt``, set to render for every
    engine. With several outputs (e.g. one per engine) the render would use
    whichever one Blender picks, not necessarily the one wired here."""
    keep = keep or _active_output(nt.nodes, idname) or nt.nodes.new(idname)
    for n in [n for n in nt.nodes if n.bl_idname == idname and n != keep]:
        nt.nodes.remove(n)
    if hasattr(keep, "target"):
        try:
            keep.target = "ALL"
        except Exception:
            pass
    return keep


def _unlink(nt, sock):
    if sock is not None:
        for l in list(sock.links):
            nt.links.remove(l)


def _remove_groups(groups):
    for g in reversed(groups):
        try:
            bpy.data.node_groups.remove(g)
        except Exception:
            pass


def _want_value(props):
    """Render Value swatches to EXR so the number can be read back (not while
    exporting: the export is a PNG)."""
    return getattr(props, "show_values", True) and not _state.get("export_to")


def _value_emission(nt, osock, surf):
    """Wire a Value socket to ``surf`` through an Emission whose colour
    encodes the number losslessly for an EXR render: R = max(v, 0),
    G = max(-v, 0) (a negative emission would be clamped to black)."""
    def math(op, a, b):
        m = nt.nodes.new("ShaderNodeMath")
        m.operation = op
        m.use_clamp = False
        nt.links.new(a, m.inputs[0])
        m.inputs[1].default_value = b
        return m.outputs[0]
    pos = math("MAXIMUM", osock, 0.0)
    neg = math("MAXIMUM", math("MULTIPLY", osock, -1.0), 0.0)
    comb = nt.nodes.new("ShaderNodeCombineColor")
    comb.mode = "RGB"
    nt.links.new(pos, comb.inputs[0])
    nt.links.new(neg, comb.inputs[1])
    comb.inputs[2].default_value = 0.0
    emit = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(comb.outputs[0], emit.inputs["Color"])
    nt.links.new(emit.outputs[0], surf)


def _use_exr(scn):
    r = scn.render.image_settings
    r.file_format = "OPEN_EXR"
    r.color_depth = "32"
    r.color_mode = "RGBA"


def _material_from_tree(src_tree):
    """A throwaway material rebuilt from a non-material shader tree (a light's
    node tree): same node names and links; the Light Output becomes a
    Material Output so the tree renders on the preview objects."""
    m = bpy.data.materials.new(PREVIEW_MAT_TMP)
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    made = {}
    for sn in src_tree.nodes:
        if sn.bl_idname == "ShaderNodeOutputLight":
            dn = nt.nodes.new("ShaderNodeOutputMaterial")
        else:
            try:
                dn = _clone_shader_node(nt, sn)
            except RuntimeError:
                continue
        dn.name = sn.name
        made[sn.name] = dn
    for l in src_tree.links:
        a, b = made.get(l.from_node.name), made.get(l.to_node.name)
        if a is None or b is None:
            continue
        fs = next((o for o in a.outputs if o.identifier == l.from_socket.identifier), None)
        ts = next((i for i in b.inputs if i.identifier == l.to_socket.identifier), None)
        if fs is not None and ts is not None:
            nt.links.new(fs, ts)
    return m


def render_shader(src_mat, node_name, res, props, out_id=None, chain=None):
    """``src_mat`` is a Material, or a Light whose node tree is previewed."""
    scn, plane, sphere = ensure_preview_scene(
        res, props.world_strength, props.sun_strength, _engine_id(props),
        getattr(props, "preview_env", "UNIFORM"))
    if isinstance(src_mat, bpy.types.Light):
        if src_mat.node_tree is None:
            return None
        prev = _material_from_tree(src_mat.node_tree)
    else:
        prev = src_mat.copy()
        prev.name = PREVIEW_MAT_TMP
    copies = []
    try:
        nt = prev.node_tree
        if chain:
            node, osock, copies = _route_out(
                nt, chain, node_name, lambda n: _out_by_id(n, out_id))
            if osock is None:
                return None
        else:
            node = nt.nodes.get(node_name)
            if node is None:
                return None
            osock = _out_by_id(node, out_id)
        is_output = node.bl_idname in SHADER_OUTPUT_NODES and not chain
        is_shader = is_output or (osock is not None and osock.type == "SHADER")
        if is_output:
            # The output itself: the whole material, as that output renders it.
            _sole_output(nt, "ShaderNodeOutputMaterial",
                         node if node.bl_idname == "ShaderNodeOutputMaterial" else None)
        else:
            out = _sole_output(nt, "ShaderNodeOutputMaterial")
            surf = out.inputs["Surface"]
            # Only the previewed node: the material's own volume and
            # displacement would fog / deform it.
            for sk in (surf, out.inputs.get("Volume"), out.inputs.get("Displacement")):
                _unlink(nt, sk)
            if osock is None:
                return None
            if osock.type == "SHADER" and node.bl_idname in VOLUME_NODES \
                    and out.inputs.get("Volume") is not None:
                nt.links.new(osock, out.inputs["Volume"])
            elif osock.type == "SHADER":
                nt.links.new(osock, surf)
            elif osock.type == "VALUE" and _want_value(props):
                _value_emission(nt, osock, surf)
                _use_exr(scn)
            else:
                emit = nt.nodes.new("ShaderNodeEmission")
                nt.links.new(osock, emit.inputs["Color"])
                nt.links.new(emit.outputs[0], surf)
        cube = bpy.data.objects.get(PREVIEW_CUBE)
        shape = props.shader_shape if is_shader else "PLANE"
        obj = {"SPHERE": sphere, "CUBE": cube}.get(shape) or plane
        for o in (plane, sphere, cube):
            if o is not None:
                o.hide_render = o is not obj
        obj.data.materials.clear()
        obj.data.materials.append(prev)
        return _finish(_render_scene(scn))
    finally:
        try:
            bpy.data.materials.remove(prev)
        except Exception:
            pass
        cube = bpy.data.objects.get(PREVIEW_CUBE)
        if cube is not None:
            cube.hide_render = True
            try:
                cube.data.materials.clear()
            except Exception:
                pass
        _remove_groups(copies)


def _frame_object(scn, cam, obj):
    cam.data.type = "ORTHO"
    center = Vector((0, 0, 0))
    radius = 1.0
    try:
        with bpy.context.temp_override(scene=scn):
            dg = bpy.context.evaluated_depsgraph_get()
            dg.update()
            ev = obj.evaluated_get(dg)
            # The evaluated matrix: this depsgraph isn't the active one, so
            # ``obj.matrix_world`` still holds the user's object placement.
            corners = [ev.matrix_world @ Vector(c[:]) for c in ev.bound_box]
        center = sum(corners, Vector()) / 8.0
        radius = max((c - center).length for c in corners) or 1.0
    except Exception:
        pass
    view_dir = Vector((0.55, -0.8, 0.5)).normalized()
    cam.location = center + view_dir * (radius * 4.0 + 2.0)
    fwd = center - cam.location
    cam.rotation_euler = fwd.to_track_quat('-Z', 'Y').to_euler()
    cam.data.ortho_scale = max(radius * 2.3, 0.2)


def _geo_modifier_index(obj, tree=None):
    """Index of the Geometry Nodes modifier using ``tree`` (the first GN
    modifier when ``tree`` is None), or None."""
    for i, m in enumerate(obj.modifiers):
        if m.type == 'NODES' and m.node_group is not None \
                and (tree is None or m.node_group == tree):
            return i
    return None


def render_geometry(obj, node_name, res, props, out_id=None, tree=None, chain=None):
    """``tree`` is the modifier's tree; with ``chain`` (group-node names) the
    node lives inside those nested groups."""
    idx = _geo_modifier_index(obj, tree)
    if idx is None:
        return None
    tree = obj.modifiers[idx].node_group
    if not chain:
        node = tree.nodes.get(node_name)
        if node is None or not any(s.type == "GEOMETRY" for s in node.outputs):
            return None

    # Fixed clay lighting (the World / Key Light sliders are for shader balls):
    # a dim uniform world plus a key light hitting the three camera-facing
    # faces at clearly different angles, so shapes read in 3D instead of as a
    # flat, near-white silhouette.
    scn, plane, sphere = ensure_preview_scene(
        res, GEO_WORLD_STRENGTH, GEO_SUN_STRENGTH, _engine_id(props))
    sun = bpy.data.objects.get(PREVIEW_SUN)
    if sun is not None:
        sun.rotation_euler = GEO_SUN_DIR.to_track_quat('Z', 'Y').to_euler()
    plane.hide_render = True
    sphere.hide_render = True

    ng2 = tree.copy()
    # obj.copy() shares the object data (mesh / curve / ...). It is never
    # modified here -- the clay material goes in through the tree below -- so
    # a (possibly huge) mesh isn't duplicated for every preview.
    obj2 = obj.copy()
    mat = bpy.data.materials.get(GEO_CLAY_MAT)
    if mat is None:
        mat = bpy.data.materials.new(GEO_CLAY_MAT)
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (0.6, 0.6, 0.62, 1.0)
    copies = []
    try:
        m2 = obj2.modifiers[idx]
        m2.node_group = ng2
        # Show the geometry at the previewed node: later modifiers (another GN
        # modifier could replace the geometry entirely) must not run on top.
        for later in list(obj2.modifiers)[idx + 1:]:
            later.show_viewport = False
            later.show_render = False
        if chain:
            _n, gos, copies = _route_out(
                ng2, chain, node_name, lambda n: _geometry_out(n, out_id))
        else:
            gos = _geometry_out(ng2.nodes.get(node_name), out_id)
        go = _active_output(ng2.nodes, "NodeGroupOutput")
        goin = next((i for i in go.inputs if i.type == "GEOMETRY"), None) if go else None
        if gos is None or goin is None:
            return None
        for l in list(goin.links):
            ng2.links.remove(l)
        # Apply the clay material in the tree itself: it covers both the
        # object's own mesh and geometry created inside the tree (Mesh Cube,
        # ...), whose own empty material list would otherwise render with
        # Blender's default surface.
        setm = ng2.nodes.new("GeometryNodeSetMaterial")
        setm.inputs["Material"].default_value = mat
        ng2.links.new(gos, setm.inputs["Geometry"])
        ng2.links.new(setm.outputs["Geometry"], goin)

        scn.collection.objects.link(obj2)
        obj2.location = (0, 0, 0)
        obj2.rotation_euler = (0, 0, 0)
        # Object-linked material slots would override the clay material; they
        # belong to obj2 only, so this doesn't touch the user's object.
        for slot in obj2.material_slots:
            if slot.link == "OBJECT":
                slot.material = mat
        obj2.hide_render = False
        _frame_object(scn, scn.camera, obj2)
        return _finish(_render_scene(scn))
    finally:
        try:
            if obj2.name in scn.collection.objects:
                scn.collection.objects.unlink(obj2)
        except Exception:
            pass
        for db, d in ((bpy.data.objects, obj2), (bpy.data.node_groups, ng2)):
            try:
                db.remove(d)
            except Exception:
                pass
        _remove_groups(copies)


def _geo_tree_of(obj):
    m = next((mo for mo in obj.modifiers
              if mo.type == 'NODES' and mo.node_group is not None), None)
    return m.node_group if m else None


def _clone_shader_node(dst_tree, src):
    """Best-effort clone of a ShaderNode into another node tree: copies writable
    settings, colour-ramp / curve data and input default values. Lets us preview
    texture / math / colour nodes that live in a Geometry node tree."""
    dst = dst_tree.nodes.new(src.bl_idname)
    for p in src.bl_rna.properties:
        pid = p.identifier
        if pid in _SKIP_PROPS or p.is_readonly:
            continue
        try:
            setattr(dst, pid, getattr(src, pid))
        except Exception:
            pass
    cr = getattr(src, "color_ramp", None)
    dcr = getattr(dst, "color_ramp", None)
    if cr is not None and dcr is not None:
        try:
            while len(dcr.elements) > len(cr.elements):
                dcr.elements.remove(dcr.elements[-1])
            for i, e in enumerate(cr.elements):
                el = dcr.elements[i] if i < len(dcr.elements) \
                    else dcr.elements.new(e.position)
                el.position = e.position
                el.color = e.color
            dcr.color_mode = cr.color_mode
            dcr.interpolation = cr.interpolation
        except Exception:
            pass
    sm = getattr(src, "mapping", None)
    dm = getattr(dst, "mapping", None)
    if sm is not None and dm is not None and hasattr(sm, "curves"):
        try:
            for ci, c in enumerate(sm.curves):
                dc = dm.curves[ci]
                for pi, pt in enumerate(c.points):
                    dp = dc.points[pi] if pi < len(dc.points) \
                        else dc.points.new(pt.location[0], pt.location[1])
                    dp.location = pt.location
            dm.update()
        except Exception:
            pass
    # Outputs too: RGB / Value nodes keep their value on the output socket.
    for si, di in list(zip(src.inputs, dst.inputs)) + list(zip(src.outputs, dst.outputs)):
        if hasattr(si, "default_value") and hasattr(di, "default_value"):
            try:
                di.default_value = si.default_value
            except Exception:
                pass
    return dst


def render_geo_swatch(obj, node_name, res, props, out_id=None, tree=None):
    """Flat swatch for a field-producing ShaderNode (texture / math / colour)
    inside a Geometry node tree: rebuild the node in a temporary material, feed
    Generated coordinates to any Vector input, and render it like a shader
    swatch. Upstream fields are not evaluated — socket defaults stand in."""
    if tree is None:
        tree = _geo_tree_of(obj)
    if tree is None:
        return None
    src = tree.nodes.get(node_name)
    if src is None:
        return None
    scn, plane, sphere = ensure_preview_scene(
        res, props.world_strength, props.sun_strength, _engine_id(props))
    m = bpy.data.materials.new(PREVIEW_MAT_TMP)
    try:
        nt = m.node_tree
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        node = _clone_shader_node(nt, src)
        osock = _out_by_id(node, out_id)
        if osock is None:
            return None
        vin = node.inputs.get("Vector")
        if vin is not None and not vin.is_linked:
            tc = nt.nodes.new("ShaderNodeTexCoord")
            nt.links.new(tc.outputs["Generated"], vin)
        if osock.type == "SHADER":
            nt.links.new(osock, out.inputs["Surface"])
        elif osock.type == "VALUE" and _want_value(props):
            _value_emission(nt, osock, out.inputs["Surface"])
            _use_exr(scn)
        else:
            emit = nt.nodes.new("ShaderNodeEmission")
            nt.links.new(osock, emit.inputs["Color"])
            nt.links.new(emit.outputs[0], out.inputs["Surface"])
        sphere.hide_render = True
        plane.hide_render = False
        plane.data.materials.clear()
        plane.data.materials.append(m)
        return _finish(_render_scene(scn))
    finally:
        try:
            bpy.data.materials.remove(m)
        except Exception:
            pass


def render_geo(obj, node_name, res, props, out_id=None, tree=None, chain=None,
               root=None):
    """Dispatch a Geometry-node preview: a 3D render for geometry-output nodes,
    a flat swatch for field-producing ShaderNodes. ``tree`` is the edited node
    tree (an object can carry several GN modifiers); defaults to the first.
    Inside a node group, ``root`` is the modifier's tree and ``chain`` the
    group nodes leading from it to ``tree``."""
    if tree is None:
        tree = _geo_tree_of(obj)
    if tree is None:
        return None
    node = tree.nodes.get(node_name)
    if node is None:
        return None
    if any(s.type == "GEOMETRY" for s in node.outputs):
        return render_geometry(obj, node_name, res, props, out_id,
                               root or tree, chain)
    return render_geo_swatch(obj, node_name, res, props, out_id, tree)


def render_compositor(scene, node_name, res, props, out_id=None, chain=None):
    # Blender 5.2's new compositor evaluates only its designated output during
    # a render (the Viewer image comes from the realtime GPU compositor, which
    # a headless render does not drive). So to preview a node we temporarily
    # route its output to the Group Output and render through the compositor
    # to a file, then read it back.
    # The render runs on a throwaway copy of the scene and its compositor tree:
    # rendering the user's own scene overwrites its Render Result, and a Viewer
    # node in the rendered tree overwrites the shared "Viewer Node" image, both
    # at thumbnail size. Scene.copy() links objects/collections (cheap) but
    # shares the compositor tree, so the tree is copied separately.
    # Inside a node group, ``chain`` names the group nodes leading from the
    # scene's tree to the node; _route_out wires it out through copies of
    # those groups.
    src_tree = getattr(scene, "compositing_node_group", None)
    if src_tree is None or (not chain and src_tree.nodes.get(node_name) is None):
        return None
    tmp = scene.copy()
    tree = src_tree.copy()
    copies = []
    try:
        tmp.compositing_node_group = tree
        if chain:
            node, out, copies = _route_out(
                tree, chain, node_name, lambda n: _out_by_id(n, out_id))
        else:
            node = tree.nodes.get(node_name)
            out = _out_by_id(node, out_id) if node is not None else None
        # (A Viewer / File Output node has no outputs, so ``out`` survives.)
        for t in [tree] + copies:
            for n in list(t.nodes):
                if n.bl_idname in ("CompositorNodeViewer", "CompositorNodeOutputFile"):
                    t.nodes.remove(n)
                elif (n.bl_idname == "CompositorNodeRLayers"
                      and getattr(n, "scene", None) == scene):
                    n.scene = tmp
        if out is None:
            return None
        go = _active_output(tree.nodes, "NodeGroupOutput")
        if go is None:
            go = tree.nodes.new("NodeGroupOutput")
        goin = next((i for i in go.inputs if i.type == "RGBA"), None)
        if goin is None:
            try:
                tree.interface.new_socket("Image", in_out='OUTPUT',
                                          socket_type='NodeSocketColor')
            except Exception:
                pass
            goin = next((i for i in go.inputs if i.type == "RGBA"),
                        go.inputs[0] if go.inputs else None)
        if goin is None:
            return None
        for l in list(goin.links):
            tree.links.remove(l)
        tree.links.new(out, goin)
        r = tmp.render
        r.resolution_x = res
        r.resolution_y = res
        r.resolution_percentage = 100
        try:
            r.engine = _engine_id(props)
        except Exception:
            pass
        r.use_compositing = True
        r.film_transparent = True
        # The copy inherits the user's output format (JPEG, EXR, ...); the
        # loader needs an 8-bit RGBA PNG.
        r.image_settings.file_format = "PNG"
        r.image_settings.color_mode = "RGBA"
        r.image_settings.color_depth = "8"
        # The copy inherits the user's final-render quality (e.g. 4096 Cycles
        # samples); every compositor node renders the scene once, on the UI
        # thread, so keep each preview render cheap.
        r.use_motion_blur = False
        # Nor the user's output extras: a VSE edit would replace the
        # composite, a render region would crop it, stamps burn in text.
        for attr in ("use_sequencer", "use_border", "use_crop_to_border", "use_stamp"):
            try:
                setattr(r, attr, False)
            except Exception:
                pass
        try:
            if r.engine == "CYCLES":
                tmp.cycles.samples = min(tmp.cycles.samples, 16)
            else:
                tmp.eevee.taa_render_samples = min(tmp.eevee.taa_render_samples, 16)
        except Exception:
            pass
        return _finish(_render_scene(tmp))
    finally:
        try:
            bpy.data.scenes.remove(tmp)
        except Exception:
            pass
        try:
            bpy.data.node_groups.remove(tree)
        except Exception:
            pass
        _remove_groups(copies)


def render_world(world, node_name, res, props, out_id=None, chain=None):
    scn, plane, sphere = ensure_preview_scene(
        res, props.world_strength, props.sun_strength, _engine_id(props))
    prevw = world.copy()
    prevw.name = PREVIEW_PREV_WORLD
    saved_world = scn.world
    cam = scn.camera
    cd = cam.data
    saved = (cd.type, getattr(cd, "lens", 50.0), cam.location.copy(),
             cam.rotation_euler.copy(), scn.render.film_transparent,
             getattr(cd, "panorama_type", None),
             plane.hide_render, sphere.hide_render)
    helper = None
    copies = []
    routed = None
    try:
        wnt = prevw.node_tree
        if chain:
            node, routed, copies = _route_out(
                wnt, chain, node_name, lambda n: _out_by_id(n, out_id))
            if routed is None:
                return None
        else:
            node = wnt.nodes.get(node_name)
        if node is None:
            return None
        out = _sole_output(wnt, "ShaderNodeOutputWorld",
                           node if node.bl_idname == "ShaderNodeOutputWorld"
                           and not chain else None)
        is_vol = node.bl_idname in VOLUME_NODES
        scn.world = prevw
        scn.render.film_transparent = False

        if is_vol:
            # A volume node: show the fog on a lit sphere at finite distance
            # (a global volume viewed as a plain 360 just absorbs to black).
            volin = out.inputs.get("Volume")
            surfin = out.inputs.get("Surface")
            for sk in (volin, surfin):
                if sk is not None:
                    for l in list(sk.links):
                        wnt.links.remove(l)
            osock = routed or _out_by_id(node, out_id)
            if osock is None or volin is None:
                return None
            wnt.links.new(osock, volin)
            plane.hide_render = True
            sphere.hide_render = False
            sphere.location = (0, 0, 0)
            clay = bpy.data.materials.get(GEO_CLAY_MAT)
            if clay is None:
                clay = bpy.data.materials.new(GEO_CLAY_MAT)
                b = clay.node_tree.nodes.get("Principled BSDF")
                if b:
                    b.inputs["Base Color"].default_value = (0.6, 0.6, 0.62, 1.0)
            sphere.data.materials.clear()
            sphere.data.materials.append(clay)
            helper = bpy.data.objects.get("NPV_vol_light")
            if helper is None:
                ld = bpy.data.lights.new("NPV_vol_light_data", type="POINT")
                helper = bpy.data.objects.new("NPV_vol_light", ld)
            if helper.name not in scn.collection.objects:
                scn.collection.objects.link(helper)
            helper.data.energy = 1500.0
            helper.location = (2.0, -2.0, 2.0)
            cd.type = "PERSP"
            cd.lens = 45.0
            cam.location = (0.0, -4.0, 0.0)
            cam.rotation_euler = (1.5708, 0.0, 0.0)
        else:
            # Surface / texture / color node -> flat environment swatch.
            if node.bl_idname != "ShaderNodeOutputWorld" or chain:
                surf = out.inputs["Surface"]
                for l in list(surf.links):
                    wnt.links.remove(l)
                osock = routed or _out_by_id(node, out_id)
                if osock is None:
                    return None
                if osock.type == "SHADER":
                    wnt.links.new(osock, surf)
                else:
                    bg = wnt.nodes.new("ShaderNodeBackground")
                    wnt.links.new(osock, bg.inputs["Color"])
                    wnt.links.new(bg.outputs[0], surf)
            # Drop the volume so it doesn't blacken the 360 environment.
            vol = out.inputs.get("Volume")
            if vol is not None:
                for l in list(vol.links):
                    wnt.links.remove(l)
            plane.hide_render = True
            sphere.hide_render = True
            cam.location = (0, 0, 0)
            if _engine_id(props) == "CYCLES":
                cd.type = "PANO"
                try:
                    cd.panorama_type = "EQUIRECTANGULAR"
                except Exception:
                    pass
                cam.rotation_euler = (1.5708, 0.0, 0.0)
            else:
                cd.type = "PERSP"
                cd.lens = 12.0
                cam.rotation_euler = (1.3, 0.0, 0.0)
        return _finish(_render_scene(scn))
    finally:
        scn.world = saved_world
        cd.type, cd.lens, cam.location, cam.rotation_euler, \
            scn.render.film_transparent, ptype, \
            plane.hide_render, sphere.hide_render = saved
        if ptype is not None:
            try:
                cd.panorama_type = ptype
            except Exception:
                pass
        if helper is not None:
            try:
                if helper.name in scn.collection.objects:
                    scn.collection.objects.unlink(helper)
            except Exception:
                pass
        try:
            sphere.data.materials.clear()
        except Exception:
            pass
        try:
            bpy.data.worlds.remove(prevw)
        except Exception:
            pass
        _remove_groups(copies)


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
        for s in bpy.data.scenes:
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


# --------------------------------------------------------------------------- #
#  Queue
# --------------------------------------------------------------------------- #
def _get_props():
    return getattr(bpy.context.scene, "npv", None)


def _light_sig(props):
    return "%s|%.4f|%.4f|%s" % (props.shader_shape, props.world_strength,
                                props.sun_strength,
                                getattr(props, "preview_env", "UNIFORM"))


def _enqueue(kind, src, tree, node_name, out_id, key, h, force, root=None,
             chain=None):
    if not force and _state["hashes"].get(key) == h and key in _state["textures"]:
        # Back to what the thumbnail shows (e.g. undo after a failing edit):
        # a failure recorded for another hash no longer applies.
        _state["failed"].pop(key, None)
        return
    # A render that failed is not retried until something it depends on
    # changes (or Refresh forces it): otherwise every edit anywhere in the
    # tree would re-run it -- a whole scene render for a compositor node.
    if not force and _state["failed"].get(key) == h:
        return
    if key in _state["queued_keys"]:
        # Still waiting: render it with what the hash now describes (the
        # source may have changed, e.g. another object made active).
        for it in _state["queue"]:
            if it["key"] == key:
                it.update({"hash": h, "src": src[1], "src_type": src[0],
                           "root": _idref(root or tree),
                           "chain": list(chain or ())})
                break
        return
    _state["queue"].append({"kind": kind, "src": src[1], "src_type": src[0],
                            "tree": _idref(tree),
                            "root": _idref(root or tree),
                            "chain": list(chain or ()),
                            "node": node_name, "out": out_id, "key": key,
                            "hash": h})
    _state["queued_keys"].add(key)


_MOD_UI_PROPS = {"name", "is_active", "is_override_data", "use_pin_to_last",
                 "show_expanded", "show_in_editmode", "show_on_cage"}


def _modifier_sig(m):
    """A modifier's settings and its ID-property inputs (a Geometry Nodes
    modifier keeps its input values there), minus panel / UI state."""
    vals = [(pid, v) for pid, v in _simple_props_sig(m)
            if pid not in _MOD_UI_PROPS and not pid.startswith("open_")
            and not (pid.startswith("show_")
                     and pid not in ("show_viewport", "show_render"))]
    try:
        idp = tuple(sorted((k, _plain(m[k])) for k in m.keys()))
    except Exception:
        idp = ()
    return (m.type, tuple(vals), idp)


def _geo_source_sig(obj_ref, root):
    """What a Geometry Nodes preview depends on outside its node tree: the
    modifier's input values, the modifiers below it in the stack, and the
    object's own data (counted by _on_depsgraph while it is edited)."""
    obj = _idget(bpy.data.objects, obj_ref)
    if obj is None:
        return ""
    idx = _geo_modifier_index(obj, root)
    if idx is None:
        return ""
    parts = [_modifier_sig(m) for m in list(obj.modifiers)[:idx + 1]]
    data = obj.data
    if data is not None:
        parts.append((data.name, _state["data_gen"].get(data.name, 0)))
    return hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()


def rebuild_queue(tree, kind, props, force=False, path=None):
    """Queue the eligible nodes of ``tree`` whose hash changed. ``path`` is the
    editor's tree path (outermost first, ending with ``tree``); when it is
    longer than one, ``tree`` is a node group entered from path[0]."""
    _state["tree_sig_memo"] = {}
    try:
        _rebuild_queue(tree, kind, props, force, path)
    finally:
        _state["tree_sig_memo"] = None


def _rebuild_queue(tree, kind, props, force, path):
    path = list(path) if path else [tree]
    if path[-1].as_pointer() != tree.as_pointer():
        path = [tree]
    chain = _instance_chain(path)
    if chain is None:
        return
    # Compositor group previews can be switched off (each one renders the
    # scene); with them off nothing is live, so the loop below is skipped and
    # the group's old thumbnails are dropped.
    skip = bool(chain) and kind == KIND_COMP and not getattr(props, "comp_groups", True)
    root = path[0]
    src = resolve_source(root, kind)
    if src is None:
        return
    # Resolution is part of the signature so a Quality change re-renders;
    # the source too (a GN tree shared by several objects previews the one
    # the editor shows).
    esig = "%s|%s|%s|%d" % (_engine_id(props), props.resolution, src,
                            int(getattr(props, "show_values", True)))
    if getattr(props, "update_on_frame", False):
        esig += "|f%d" % bpy.context.scene.frame_current
    if chain:
        esig += "|" + _context_sig(src, path, chain)
    if kind == KIND_GEO:
        esig += "|" + _geo_source_sig(src[1], root)
    lsig = _light_sig(props)
    memo = {}
    live = set()
    for node in tree.nodes:
        if skip or not node_eligible(node, kind, props):
            continue
        try:
            h = upstream_hash(node, memo)
        except Exception:
            continue
        if kind == KIND_SHADER and renders_as_shader(node):
            extra = esig + "|" + lsig
        else:
            extra = esig
        h = hashlib.md5((h + extra).encode("utf-8", "replace")).hexdigest()
        for out_id in _preview_targets(node, kind, props):
            key = _skey(tree, node.name, out_id)
            live.add(key)
            _enqueue(kind, src, tree, node.name, out_id, key, h, force,
                     root, chain)
    # Thumbnails of this tree that are no longer shown (node deleted or
    # renamed, output switched, filtered out) only hold GPU memory.
    prefix = "%d:" % tree.as_pointer()
    for key in [k for k in set(_state["textures"]) | set(_state["failed"])
                if k.startswith(prefix) and k not in live]:
        _drop_texture(key)
    # Their pending renders too (e.g. Scope switched to Selected while a
    # compositor tree was queued: each one would render the whole scene).
    q = _state["queue"]
    stale = [it for it in q if it["key"].startswith(prefix) and it["key"] not in live]
    if stale:
        q[:] = [it for it in q if not (it["key"].startswith(prefix)
                                        and it["key"] not in live)]
        _state["queued_keys"].difference_update(it["key"] for it in stale)


def _touch(key):
    _state["tick"] += 1
    _state["tex_tick"][key] = _state["tick"]


def _drop_texture(key):
    _state["textures"].pop(key, None)
    _state["hashes"].pop(key, None)
    _state["tex_tick"].pop(key, None)
    _state["failed"].pop(key, None)
    _state["values"].pop(key, None)


def _reset_cache():
    """Forget every thumbnail, pending render and failure."""
    for k in ("textures", "tex_tick", "img_gen", "data_gen", "hashes", "failed",
              "values"):
        _state[k].clear()
    _state["queue"].clear()
    _state["queued_keys"].clear()
    _state["visible"] = set()
    _state["priority"] = set()


def _live_tree_pointers():
    ptrs = set()
    for m in bpy.data.materials:
        if m.node_tree is not None:
            ptrs.add(m.node_tree.as_pointer())
    for w in bpy.data.worlds:
        if w.node_tree is not None:
            ptrs.add(w.node_tree.as_pointer())
    for lt in bpy.data.lights:
        if getattr(lt, "node_tree", None) is not None:
            ptrs.add(lt.node_tree.as_pointer())
    for ng in bpy.data.node_groups:
        ptrs.add(ng.as_pointer())
    return ptrs


def _max_textures():
    """The user's 'Max Cached Thumbnails' preference (MAX_TEXTURES when the
    add-on's preferences aren't available)."""
    try:
        return int(bpy.context.preferences.addons[__name__].preferences.max_textures)
    except Exception:
        return MAX_TEXTURES


def _prune_cache():
    """Drop thumbnails whose node tree no longer exists, then evict the least
    recently used ones above the cache limit."""
    live = _live_tree_pointers()
    for key in set(_state["textures"]) | set(_state["failed"]):
        try:
            ptr = int(key.split(":", 1)[0])
        except ValueError:
            ptr = None
        if ptr not in live:
            _drop_texture(key)
    extra = len(_state["textures"]) - _max_textures()
    if extra > 0:
        # Never evict the editor's own thumbnails: they'd go blank, re-render
        # on the next edit and get evicted again. If they alone exceed the
        # limit, the cache stays above it until the user moves on.
        active = _state["active_tree_ptr"]
        keep = "%d:" % active if active else None
        tick = _state["tex_tick"]
        victims = sorted((k for k in _state["textures"]
                          if keep is None or not k.startswith(keep)),
                         key=lambda k: tick.get(k, 0))
        for key in victims[:extra]:
            _drop_texture(key)


def _pop_next():
    """Next queue item: the active / selected nodes first, then the ones on
    screen, then the rest (each group in queue order)."""
    q = _state["queue"]
    pri, vis = _state["priority"], _state["visible"]
    best, best_rank = 0, 3
    for i, it in enumerate(q):
        k = it.get("key")
        rank = 0 if k in pri else 1 if k in vis else 2
        if rank < best_rank:
            best, best_rank = i, rank
            if rank == 0:
                break
    it = q.pop(best)
    _state["queued_keys"].discard(it.get("key"))
    return it


def _render_item(item, res, props):
    """Render one queue item; returns what _finish() returned, or None."""
    k = item["kind"]
    oid = item.get("out")
    chain = item.get("chain") or None
    if k == KIND_SHADER:
        if item.get("src_type") == "LIGHT":
            m = _idget(bpy.data.lights, item["src"])
        else:
            m = _idget(bpy.data.materials, item["src"])
        return render_shader(m, item["node"], res, props, oid, chain) if m else None
    if k == KIND_WORLD:
        w = _idget(bpy.data.worlds, item["src"])
        return render_world(w, item["node"], res, props, oid, chain) if w else None
    if k == KIND_GEO:
        o = _idget(bpy.data.objects, item["src"])
        t = _idget(bpy.data.node_groups, item.get("tree"))
        r = _idget(bpy.data.node_groups, item.get("root")) or t
        return render_geo(o, item["node"], res, props, oid, t, chain, r) \
            if o and t else None
    if k == KIND_COMP:
        s = _idget(bpy.data.scenes, item["src"])
        return render_compositor(s, item["node"], res, props, oid, chain) if s else None
    return None


def _queue_allowed(item, props):
    """False for a pending render whose preview type was switched off since
    it was queued (Compositor, Geometry Nodes, World, compositor groups)."""
    kind = item.get("kind")
    if kind is not None and not _kind_enabled(kind, props):
        return False
    if kind == KIND_COMP and item.get("chain") \
            and not getattr(props, "comp_groups", True):
        return False
    return True


def _drop_disallowed(props):
    q = _state["queue"]
    drop = [it for it in q if not _queue_allowed(it, props)]
    if drop:
        q[:] = [it for it in q if _queue_allowed(it, props)]
        _state["queued_keys"].difference_update(it["key"] for it in drop)


def process_queue(props):
    """Render queued previews: at most 'Nodes / Tick', and stop early once the
    'Time Budget' is spent (at least one render per call), so a slow engine
    or a high Quality doesn't freeze the UI for several renders in a row."""
    if not _state["queue"]:
        return False
    n = max(1, int(props.batch_size))
    budget = max(0.0, float(getattr(props, "time_budget", 0))) / 1000.0
    res = int(props.resolution)
    did = False
    start = time.perf_counter()
    _state["rendering"] = True
    try:
        for i in range(n):
            if not _state["queue"]:
                break
            if i and budget and time.perf_counter() - start >= budget:
                break
            item = _pop_next()
            _state["last_value"] = None
            try:
                tex = _render_item(item, res, props)
            except Exception as exc:
                print("[NodePreview] render failed for %s: %r" % (item["node"], exc))
                tex = None
            key = item["key"]
            if tex is not None:
                _state["textures"][key] = tex
                _state["hashes"][key] = item["hash"]
                _state["failed"].pop(key, None)
                if _state["last_value"] is None:
                    _state["values"].pop(key, None)
                else:
                    _state["values"][key] = _state["last_value"]
                _touch(key)
            else:
                _state["failed"][key] = item["hash"]
            # A failure changes what is drawn too (the error marker).
            did = True
    finally:
        _state["rendering"] = False
        _state["last_value"] = None
    return did


def _tag_node_editors():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "NODE_EDITOR":
                area.tag_redraw()


# --------------------------------------------------------------------------- #
#  Timer / depsgraph
# --------------------------------------------------------------------------- #
def _animation_playing():
    try:
        return any(w.screen is not None and w.screen.is_animation_playing
                   for w in bpy.context.window_manager.windows)
    except Exception:
        return False


def _timer():
    props = _get_props()
    if props is None or not props.enabled:
        _state["timer_running"] = False
        return None
    # Renders run on the UI thread and would stall playback; pending work
    # (dirty flag, queue) simply waits until it stops -- unless the user
    # asked for previews to follow the frame.
    if not getattr(props, "update_on_frame", False) and _animation_playing():
        return 0.25
    shown = len(_state["textures"])
    if _state["dirty"] and props.auto_update:
        _state["dirty"] = False
        tree, kind, path = _resolve_active()
        if tree is not None and _kind_enabled(kind, props):
            rebuild_queue(tree, kind, props, force=False, path=path)
    _drop_disallowed(props)
    rendered = process_queue(props)
    _state["prune_in"] -= 1
    if _state["prune_in"] <= 0:
        _state["prune_in"] = 20          # ~every 3 s
        _prune_cache()
    # Redraw after new renders, and after thumbnails were dropped (filtered
    # out, node deleted, cache pruned) so they don't linger on screen.
    if rendered or len(_state["textures"]) < shown:
        _tag_node_editors()
    return 0.15


def _ensure_timer():
    # Trust Blender's own registry, not our cached flag: a non-persistent
    # timer is silently dropped on every file load, which would otherwise
    # leave `timer_running` stuck True forever with no timer actually running.
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.1, persistent=True)
    _state["timer_running"] = True


_DATA_ID_TYPES = {"MESH", "CURVE", "CURVES", "POINTCLOUD", "VOLUME", "LATTICE",
                  "META", "FONT", "GREASEPENCIL", "GREASEPENCIL_V3"}


@persistent
def _on_depsgraph(scene, depsgraph):
    if _state["rendering"]:
        return
    props = getattr(scene, "npv", None)
    if props is None or not props.enabled or not props.auto_update:
        return
    for upd in depsgraph.updates:
        idt = getattr(upd.id, "id_type", "")
        if idt == "IMAGE":
            # Texture paint sends Image updates; the counter is part of the
            # image's hash so textures using it re-render.
            name = upd.id.name
            _state["img_gen"][name] = _state["img_gen"].get(name, 0) + 1
            _state["dirty"] = True
        elif idt in _DATA_ID_TYPES:
            # Editing an object's own data (edit-mode mesh edits, ...): the
            # counter is part of its Geometry Nodes previews' hash.
            name = upd.id.name
            _state["data_gen"][name] = _state["data_gen"].get(name, 0) + 1
            _state["dirty"] = True
        elif idt == "OBJECT":
            # Moving / rotating an object changes nothing a preview shows
            # (geometry previews render a copy at the origin), and would
            # otherwise re-hash the whole tree every tick during a drag.
            if upd.is_updated_transform and not upd.is_updated_geometry \
                    and not upd.is_updated_shading:
                continue
            _state["dirty"] = True
        elif idt in {"MATERIAL", "NODETREE", "WORLD", "SCENE", "LIGHT"}:
            # SCENE: e.g. a render engine switch (part of the hash).
            _state["dirty"] = True


@persistent
def _on_frame_change(scene, depsgraph=None):
    """With 'Update on Frame Change' the frame is part of every hash, so
    time-dependent nodes (Scene Time, image sequences, ...) re-render."""
    props = getattr(scene, "npv", None)
    if props is not None and props.enabled and props.auto_update \
            and getattr(props, "update_on_frame", False):
        _state["dirty"] = True


@persistent
def _on_load_post(_filepath):
    """Reset after a .blend load: every cached tree/node pointer and GPU
    texture belongs to the old file. The preview timer itself is persistent
    and survives the load; _ensure_timer() here is just a safety net in case
    it is gone for any other reason."""
    _reset_cache()
    _socket_enum_cache.clear()
    _state["sel_sig"] = None
    _state["src_hint"] = []
    _state["active_tree_ptr"] = None
    _state["active_kind"] = None
    _state["active_path"] = None
    _state["rendering"] = False
    _state["timer_running"] = False
    _state["dirty"] = True
    # Files saved by versions before 1.1.7 carry the preview scene; drop it so
    # it doesn't show up in the scene list (rebuilt on demand).
    _cleanup_datablocks()
    _ensure_timer()


@persistent
def _on_save_pre(_filepath):
    """Keep the preview scene / objects out of the user's .blend. They are
    rebuilt on the next preview render."""
    _cleanup_datablocks()


# --------------------------------------------------------------------------- #
#  Drawing
# --------------------------------------------------------------------------- #
def _image_shader():
    sh = _state.get("shader_image")
    if sh is None:
        sh = gpu.shader.from_builtin("IMAGE")
        _state["shader_image"] = sh
    return sh


def _draw_tex(tex, x0, y0, x1, y1):
    sh = _image_shader()
    pos = ((x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1))
    uv = ((0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1))
    batch = batch_for_shader(sh, "TRIS", {"pos": pos, "texCoord": uv})
    sh.bind()
    sh.uniform_sampler("image", tex)
    batch.draw(sh)


def _color_shader():
    sh = _state.get("shader_color")
    if sh is None:
        sh = gpu.shader.from_builtin("UNIFORM_COLOR")
        _state["shader_color"] = sh
    return sh


def _draw_tris(color, pos):
    if not pos:
        return
    sh = _color_shader()
    batch = batch_for_shader(sh, "TRIS", {"pos": pos})
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)


def _draw_rect(color, x0, y0, x1, y1):
    _draw_tris(color, ((x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)))


def _draw_border(color, x0, y0, x1, y1, width=1.0):
    sh = _color_shader()
    pos = ((x0, y0), (x1, y0), (x1, y0), (x1, y1),
           (x1, y1), (x0, y1), (x0, y1), (x0, y0))
    batch = batch_for_shader(sh, "LINES", {"pos": pos})
    gpu.state.line_width_set(width)
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)
    gpu.state.line_width_set(1.0)


def _checker_tris(x0, y0, x1, y1, size):
    """Triangles of the light squares of a checkerboard filling the rect."""
    pos = []
    if size <= 0:
        return pos
    j, y = 0, y0
    while y < y1:
        ya = min(y + size, y1)
        i, x = 0, x0
        while x < x1:
            xa = min(x + size, x1)
            if (i + j) % 2 == 0:
                pos += ((x, y), (xa, y), (xa, ya), (x, y), (xa, ya), (x, ya))
            x += size
            i += 1
        y += size
        j += 1
    return pos


def _blf_size(font, size):
    try:
        blf.size(font, size)
    except TypeError:
        blf.size(font, size, 72)


def _fit_text(font, text, limit):
    if blf.dimensions(font, text)[0] > limit:
        while text and blf.dimensions(font, text + "…")[0] > limit:
            text = text[:-1]
        text = (text + "…") if text else ""
    return text


def _draw_text(text, x, y, ps, size=11, color=(1.0, 1.0, 1.0, 1.0)):
    font = 0
    _blf_size(font, round(size * ps))
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(font, round(1 * ps), round(-1 * ps))
    blf.position(font, x, y, 0.0)
    blf.color(font, *color)
    blf.draw(font, text)
    blf.disable(font, blf.SHADOW)


def _draw_label(text, x, y, maxw, ps=1.0):
    """Small socket name shown on a cell (side-by-side mode). Truncated with an
    ellipsis to fit the cell width. ``ps`` is the UI pixel size so the font and
    padding scale with HiDPI / UI resolution scale."""
    _blf_size(0, round(11 * ps))
    text = _fit_text(0, text, max(0.0, maxw - 6.0 * ps))
    if text:
        _draw_text(text, x, y, ps)


def _draw_centered(text, cx0, cy0, cx1, cy1, ps, size=11, color=(1, 1, 1, 1)):
    _blf_size(0, round(size * ps))
    text = _fit_text(0, text, max(0.0, (cx1 - cx0) - 6.0 * ps))
    if not text:
        return
    tw, th = blf.dimensions(0, text)
    _draw_text(text, (cx0 + cx1 - tw) / 2.0, (cy0 + cy1 - th) / 2.0, ps, size, color)


def format_value(v):
    """Number drawn on a Value swatch: short, no '-0'."""
    if abs(v) < 5e-7:
        v = 0.0
    txt = "%.4g" % v
    return txt


COL_QUEUED = (1.0, 0.62, 0.15, 1.0)   # stale: waiting to re-render
COL_FAILED = (0.95, 0.2, 0.2, 1.0)    # the last render failed


def _grid_origin(pos, x0, x1, y0, node_h, gw, gh, gap):
    """Bottom-left corner of a preview grid of size gw x gh placed at
    ``pos`` relative to a node whose region rect is x0..x1 wide, top y0."""
    if pos == "BELOW":
        return x0 + (x1 - x0 - gw) / 2.0, y0 - node_h - gap - gh
    if pos == "LEFT":
        return x0 - gap - gw, y0 - gh
    if pos == "RIGHT":
        return x1 + gap, y0 - gh
    return x0 + (x1 - x0 - gw) / 2.0, y0 + gap        # ABOVE


def _record_hint(ctx, space):
    """Remember which material / light / object the editor shows so the
    queue previews through it (see resolve_source).

    A pinned editor is skipped: it shows its own data-block, and letting it
    write the (single, global) hint would make it fight an unpinned editor on
    the same shared tree -- each redraw would flip the source, change every
    hash and re-render the tree forever. Unpinned editors all follow the
    active object / material, so they agree."""
    if getattr(space, "pin", False):
        return
    hint = []
    for d in (getattr(space, "id", None), getattr(space, "id_from", None),
              getattr(ctx, "active_object", None)):
        if isinstance(d, bpy.types.Material):
            hint.append(("MAT", _idref(d)))
        elif isinstance(d, bpy.types.Light):
            hint.append(("LIGHT", _idref(d)))
        elif isinstance(d, bpy.types.Object):
            hint.append(("OBJ", _idref(d)))
            if isinstance(d.data, bpy.types.Light):
                hint.append(("LIGHT", _idref(d.data)))
    hint = list(dict.fromkeys(hint))
    if hint != _state.get("src_hint"):
        _state["src_hint"] = hint
        _state["dirty"] = True


def draw_callback():
    ctx = bpy.context
    space = ctx.space_data
    if space is None or space.type != "NODE_EDITOR":
        return
    if space.tree_type not in KINDS:
        return
    kind = space_kind(space)
    props = getattr(ctx.scene, "npv", None)
    if props is None or not props.enabled or not _kind_enabled(kind, props):
        return
    tree = getattr(space, "edit_tree", None)
    if tree is None:
        return

    ptr = tree.as_pointer()
    # Tree path of the editor (outermost first): entering a node group adds
    # the group's tree; the previews inside need the path back to the
    # material / world / modifier.
    path = [p.node_tree.as_pointer() for p in space.path if p.node_tree is not None]
    if not path or path[-1] != ptr:
        path = [ptr]
    if (_state["active_tree_ptr"] != ptr or _state["active_kind"] != kind
            or _state["active_path"] != path):
        # Switched to a different node tree / editor type: re-queue so an
        # enabled editor auto-refreshes once on switch (when Auto Update is on),
        # instead of waiting for a depsgraph update or a manual Refresh.
        _state["dirty"] = True
    _state["active_tree_ptr"] = ptr
    _state["active_kind"] = kind
    _state["active_path"] = path
    _record_hint(ctx, space)
    _ensure_timer()

    # In 'Selected' scope, a selection change has no depsgraph update, so watch
    # it here and mark dirty when the set of selected nodes changes.
    if getattr(props, "preview_scope", "ALL") == "SELECTED":
        sig = (tree.as_pointer(),
               tuple(sorted(n.name for n in tree.nodes if n.select)))
        if _state.get("sel_sig") != sig:
            _state["sel_sig"] = sig
            _state["dirty"] = True

    region = ctx.region
    v2d = region.view2d
    # UI-scale correction. The node editor draws every node at
    # ``location * ui_scale`` in view2d space (and ``node.dimensions`` is
    # already in those scaled view units), while ``node.location`` itself is in
    # unscaled node units. view_to_region() and this POST_PIXEL handler share
    # the same region-pixel space, so no framebuffer/pixel_size factor is
    # involved -- but node coordinates must be multiplied by ui_scale BEFORE
    # view_to_region(), or previews land at 1/ui_scale of the node position
    # (offset grows with distance from the view origin): the reported bug on
    # HiDPI / scaled-UI machines. NOTE: read ui_scale inside the draw callback;
    # outside a window draw context it can report stale values.
    ps = ctx.preferences.system.ui_scale
    # The same factor keeps fixed decorations (gap, padding, borders, label)
    # proportional to Blender's own UI at any scale.
    gap = 6.0 * ps
    pad = 2.0 * ps                 # backdrop / outer-border padding
    bw = 1.0                       # border line width: intentionally NOT
                                   # scaled -- a hairline border looks right at
                                   # any Resolution Scale; scaling it reads as
                                   # too thick.
    rw, rh = region.width, region.height
    scale = max(0.1, float(getattr(props, "thumb_scale", 1.0)))
    where = getattr(props, "thumb_position", "ABOVE")
    status = getattr(props, "show_status", True)
    checker = getattr(props, "checker_bg", False)
    show_values = getattr(props, "show_values", True)
    zoom = float(getattr(props, "zoom_factor", 2.5)) \
        if getattr(props, "zoom_active", False) else 1.0
    active = tree.nodes.active
    textures, queued = _state["textures"], _state["queued_keys"]
    failed, hashes, values = _state["failed"], _state["hashes"], _state["values"]
    visible, priority = set(), set()
    jobs = []
    for node in tree.nodes:
        if not node_eligible(node, kind, props):
            continue
        keys = [(oid, _skey(tree, node.name, oid))
                for oid in _preview_targets(node, kind, props)]
        if node.select or node == active:
            priority.update(k for _o, k in keys)
        loc = node.location_absolute
        # Scale node-space coords by ui_scale BEFORE view_to_region (see note).
        x0, y0 = v2d.view_to_region(loc.x * ps, loc.y * ps, clip=False)
        x1, _ = v2d.view_to_region((loc.x + node.width) * ps, loc.y * ps,
                                   clip=False)
        w = x1 - x0
        dims = node.dimensions
        node_h = w * dims.y / dims.x if dims.x > 0 else 0.0
        z = zoom if node == active else 1.0
        gw = w * scale * z             # grid width (== node width at 1x)

        def layout(n):
            # Single big swatch for one preview, otherwise 2 per row and wrap
            # to further rows (cell = half the grid width, stays legible).
            cols = 1 if n == 1 else 2
            cw = gw / cols
            gh = ((n + cols - 1) // cols) * cw
            gx0, gy0 = _grid_origin(where, x0, x1, y0, node_h, gw, gh, gap)
            return cols, cw, gh, gx0, gy0

        cols, cw, gh, gx0, gy0 = layout(len(keys))
        # Cull: skip nodes whose node and preview rects are both off screen.
        lo_x, hi_x = min(x0, gx0), max(x1, gx0 + gw)
        lo_y, hi_y = min(y0 - node_h, gy0), max(y0, gy0 + gh)
        if hi_x < 0 or lo_x > rw or hi_y < 0 or lo_y > rh:
            continue
        visible.update(k for _o, k in keys)
        if w < 10:
            continue
        cells = []
        for oid, k in keys:
            t = textures.get(k)
            st = None
            if status:
                if k in failed and (t is None or failed[k] != hashes.get(k)):
                    st = "FAILED"
                elif k in queued:
                    st = "QUEUED"
            if t is not None:
                _touch(k)
            if t is not None or st is not None:
                cells.append((oid, k, t, st))
        if cells:
            cols, cw, gh, gx0, gy0 = layout(len(cells))
            jobs.append((node == active and z != 1.0, node, cells, cols,
                         gx0, gy0, gw, gh, cw))
    _state["visible"] = visible
    _state["priority"] = priority

    gpu.state.blend_set("ALPHA")
    # The enlarged active node last, so it sits on top of its neighbours.
    for _top, node, cells, cols, gx0, gy0, gw, gh, cw in sorted(
            jobs, key=lambda j: j[0]):
        n = len(cells)
        # One dark backdrop + outer border for the whole grid.
        _draw_rect((0.05, 0.05, 0.05, 0.85), gx0 - pad, gy0 - pad, gx0 + gw + pad, gy0 + gh + pad)
        oname = {s.identifier: (s.name or s.identifier) for s in node.outputs}
        for i, (oid, k, tex, st) in enumerate(cells):
            col = i % cols
            row_from_top = i // cols
            cx0 = gx0 + col * cw
            cx1 = cx0 + cw
            cy1 = gy0 + gh - row_from_top * cw     # top of this cell
            cy0 = cy1 - cw                         # bottom of this cell
            if tex is not None:
                if checker:
                    _draw_rect((0.22, 0.22, 0.22, 1.0), cx0, cy0, cx1, cy1)
                    _draw_tris((0.36, 0.36, 0.36, 1.0),
                               _checker_tris(cx0, cy0, cx1, cy1, 8.0 * ps))
                _draw_tex(tex, cx0, cy0, cx1, cy1)
                v = values.get(k) if show_values else None
                if v is not None and cw >= 28 * ps:
                    _draw_centered(format_value(v), cx0, cy0, cx1, cy1, ps,
                                   size=12 if cw >= 60 * ps else 10)
            elif st == "QUEUED":
                _draw_centered("…", cx0, cy0, cx1, cy1, ps, size=14,
                               color=COL_QUEUED)
            elif st == "FAILED":
                _draw_centered("!", cx0, cy0, cx1, cy1, ps, size=16,
                               color=COL_FAILED)
            if st is not None:
                _draw_border(COL_QUEUED if st == "QUEUED" else COL_FAILED,
                             cx0 + 0.5, cy0 + 0.5, cx1 - 0.5, cy1 - 0.5, bw)
            elif n > 1:
                _draw_border((0.0, 0.0, 0.0, 1.0), cx0, cy0, cx1, cy1, bw)
            if n > 1 and oid and cw >= 40 * ps:
                _draw_label(oname.get(oid, oid), cx0 + 3 * ps, cy0 + 3 * ps, cw, ps)
        _draw_border((0.0, 0.0, 0.0, 1.0), gx0 - pad, gy0 - pad, gx0 + gw + pad, gy0 + gh + pad, bw)
    gpu.state.blend_set("NONE")


# --------------------------------------------------------------------------- #
#  Properties
# --------------------------------------------------------------------------- #
def _toggle_enabled(self, context):
    if self.enabled:
        _state["dirty"] = True
        _ensure_timer()
    _tag_node_editors()


def _mark_dirty(self, context):
    _state["dirty"] = True


def _redraw(self, context):
    _tag_node_editors()


def _node_show_update(self, context):
    _state["dirty"] = True
    _tag_node_editors()


def _scope_update(self, context):
    _state["dirty"] = True
    _state["sel_sig"] = None
    _tag_node_editors()


# --------------------------------------------------------------------------- #
#  Localisation (English / Chinese, default English)
# --------------------------------------------------------------------------- #
TR = {
    "EN": {
        "show_previews": "Show Previews",
        "auto_update": "Auto Update",
        "quality": "Quality",
        "nodes_tick": "Nodes / Tick",
        "engine_fmt": "Engine: %s (from render settings)",
        "only_tex": "Only Texture / Shader Nodes",
        "only_marked": "Only Marked Nodes",
        "scope": "Preview Scope",
        "scope_sel_hint": "Select nodes to preview them",
        "show_all_outputs": "Show All Linked Outputs",
        "preview_socket": "Preview Socket",
        "preview_active": "Preview Active Node",
        "mark_sel": "Mark Sel",
        "unmark_sel": "Unmark Sel",
        "shader_box": "Shader Nodes (BSDF / Output)",
        "world_light": "World Light",
        "key_light": "Key Light",
        "other_editors": "Other Editors",
        "world": "World",
        "geometry": "Geometry Nodes",
        "geo_fields": "Texture / Math Nodes",
        "compositor": "Compositor",
        "comp_note1": "Auto-updates on node edits.",
        "comp_note2": "Refresh to reflect 3D scene changes.",
        "refresh": "Refresh Previews",
        "rendering_fmt": "Rendering... %d left",
        "cached_fmt": "Cached: %d / %d",
        "ctx_show": "Show Node Preview",
        "comp_groups": "Inside Node Groups",
        "pref_limit_fmt": "At the limit: ~%d MB (Low) / ~%d MB (Medium) / ~%d MB (High)",
        "time_budget": "Time Budget (ms)",
        "update_on_frame": "Update on Frame Change",
        "environment": "Environment",
        "display_box": "Display",
        "thumb_scale": "Thumbnail Size",
        "thumb_position": "Position",
        "zoom_active": "Enlarge Active Node",
        "checker_bg": "Checkerboard",
        "show_status": "Status Markers",
        "show_values": "Show Values",
        "failed_fmt": "%d preview(s) failed - see the system console",
        "keys_title": "Shortcuts (Node Editor, Ctrl+Alt):",
        "export": "Export Node Preview...",
        "help_tip": "Explain what each option and button does",
        "help_title": "Node Preview - what each control does",
        "help_tabs": [
            ("GENERAL", "General"), ("FILTER", "Filter"), ("OUTPUTS", "Outputs"),
            ("SHADER", "Shader"), ("EDITORS", "Editors"),
            ("GROUPS", "Groups"), ("DISPLAY", "Display"), ("CACHE", "Cache"),
        ],
        "help": {
            "GENERAL": [
                ("line", "Show Previews:  master on/off for all thumbnails."),
                ("line", "Auto Update:  re-render a node when its inputs change,"),
                ("line", "        also after texture painting an image or"),
                ("line", "        editing inside a node group."),
                ("line", "Quality:  thumbnail resolution (64 / 128 / 256 px)."),
                ("line", "Nodes / Tick:  most previews rendered per step;"),
                ("line", "        Time Budget (ms) ends a step early. Selected"),
                ("line", "        and on-screen nodes render first."),
                ("line", "Update on Frame Change:  re-render when the frame"),
                ("line", "        changes. Off: previews pause during playback."),
                ("line", "Engine:  follows the scene's Render Engine."),
                ("sec", "Buttons"),
                ("line", "Refresh:  re-render every node in the current editor."),
                ("line", "Export:  save the active node's preview as a PNG."),
                ("line", "Trash:  clear all cached thumbnails (they render again)."),
            ],
            "FILTER": [
                ("line", "Only Texture / Shader Nodes:  skip Value / Math nodes."),
                ("line", "Preview Scope:"),
                ("line", "        All:  preview every eligible node."),
                ("line", "        Selected:  only the nodes you select."),
                ("line", "        Marked:  only nodes you switch on (right-click"),
                ("line", "        > Show Node Preview, or Mark Sel / Unmark Sel)."),
            ],
            "OUTPUTS": [
                ("line", "For nodes with several outputs (e.g. Texture Coordinate):"),
                ("line", "Preview Socket:  which output the node previews"),
                ("line", "        (Auto = first linked). Also on right-click menu."),
                ("line", "Show All Linked Outputs:  preview every linked output"),
                ("line", "        side by side in a 2-column grid."),
            ],
            "SHADER": [
                ("line", "Shader nodes (BSDF / Output):"),
                ("line", "Sphere / Cube / Plane:  lit ball, cube, or flat swatch."),
                ("line", "Environment:  Uniform (white), or one of Blender's"),
                ("line", "        studio HDRIs (Forest, City, ...)."),
                ("line", "World Light:  environment brightness on the ball."),
                ("line", "Key Light:  sun strength (Sphere / Cube)."),
                ("line", "Texture / color nodes always show a flat swatch;"),
                ("line", "        Value outputs also show their number."),
                ("line", "Lights:  a light's node tree previews like a material."),
            ],
            "EDITORS": [
                ("line", "Turn these on to preview the other editors:"),
                ("line", "World:  environment swatch; a volume node (fog) is"),
                ("line", "        shown on a lit sphere instead."),
                ("line", "Geometry Nodes:  a small shaded (clay) 3D render"),
                ("line", "        of the geometry."),
                ("line", "        Texture / Math Nodes (checkbox): also show a"),
                ("line", "        flat swatch for texture / math / colour nodes."),
                ("line", "        A tree on several objects previews the active one."),
                ("line", "Compositor:  each node's image. Renders the scene per"),
                ("line", "        node, so it is heavier. Updates on node edits;"),
                ("line", "        press Refresh after changing the 3D scene."),
                ("line", "        Inside Node Groups (checkbox): see Groups."),
            ],
            "GROUPS": [
                ("line", "Group nodes get a thumbnail of their own output."),
                ("line", "Inside a group (Tab):  its nodes are previewed with"),
                ("line", "        the values the outer group node passes in."),
                ("line", "A group used in several places follows the group"),
                ("line", "        node you entered it from."),
                ("line", "Compositor:  previews inside groups can be switched"),
                ("line", "        off with Inside Node Groups (under Compositor);"),
                ("line", "        each node renders the scene once."),
            ],
            "DISPLAY": [
                ("line", "Thumbnail Size:  width relative to the node."),
                ("line", "Position:  above, below, left or right of the node."),
                ("line", "Enlarge Active Node:  draw it bigger, on top."),
                ("line", "Checkerboard:  shows transparency behind thumbnails."),
                ("line", "Status Markers:  orange = waiting to re-render;"),
                ("line", "        red / '!' = render failed (system console)."),
                ("line", "        A failed node retries once it changes."),
                ("line", "Show Values:  a Value swatch that is one number"),
                ("line", "        shows it (e.g. Math with fixed inputs)."),
                ("sec", "Shortcuts (Node Editor)"),
                ("line", "Ctrl+Alt+P  show / hide previews"),
                ("line", "Ctrl+Alt+R  refresh      Ctrl+Alt+Z  enlarge active"),
            ],
            "CACHE": [
                ("line", "Cached: n / max  (panel bottom):  thumbnails kept"),
                ("line", "        in GPU memory."),
                ("line", "Max Cached Thumbnails:  set it in Edit > Preferences"),
                ("line", "        > Add-ons > Node Preview Thumbnails. Above it,"),
                ("line", "        the least recently shown are released; the"),
                ("line", "        editor you are looking at keeps its own."),
                ("line", "Per 100 thumbnails:  ~3 MB (64px), ~13 MB (128px),"),
                ("line", "        ~50 MB (256px)."),
                ("line", "Thumbnails of deleted nodes are released automatically."),
                ("line", "Trash button:  clear all cached thumbnails now (the"),
                ("line", "        editor's previews then render again)."),
            ],
        },
    },
    "ZH": {
        "show_previews": "顯示預覽",
        "auto_update": "自動更新",
        "quality": "畫質",
        "nodes_tick": "每次算幾個",
        "engine_fmt": "引擎：%s（來自算圖設定）",
        "only_tex": "只有貼圖 / 著色器節點",
        "only_marked": "只顯示已勾選節點",
        "scope": "預覽範圍",
        "scope_sel_hint": "選取節點即可預覽",
        "show_all_outputs": "並排顯示所有連線輸出",
        "preview_socket": "預覽插槽",
        "preview_active": "預覽作用中節點",
        "mark_sel": "勾選所選",
        "unmark_sel": "取消所選",
        "shader_box": "著色器節點（BSDF / 輸出）",
        "world_light": "世界光",
        "key_light": "主光",
        "other_editors": "其他編輯器",
        "world": "世界",
        "geometry": "幾何節點",
        "geo_fields": "貼圖 / 數學節點",
        "compositor": "合成器",
        "comp_note1": "編輯節點時自動更新。",
        "comp_note2": "按刷新以反映 3D 場景變動。",
        "refresh": "刷新預覽",
        "rendering_fmt": "算圖中… 剩 %d",
        "cached_fmt": "快取：%d / %d",
        "ctx_show": "顯示節點預覽",
        "comp_groups": "群組內節點",
        "pref_limit_fmt": "達上限時約：%d MB（低）/ %d MB（中）/ %d MB（高）",
        "time_budget": "時間預算（毫秒）",
        "update_on_frame": "換影格時更新",
        "environment": "環境光",
        "display_box": "顯示",
        "thumb_scale": "縮圖大小",
        "thumb_position": "位置",
        "zoom_active": "放大作用中節點",
        "checker_bg": "棋盤格背景",
        "show_status": "狀態標記",
        "show_values": "顯示數值",
        "failed_fmt": "%d 張預覽失敗 — 詳見系統主控台",
        "keys_title": "快捷鍵（節點編輯器，Ctrl+Alt）：",
        "export": "匯出節點預覽…",
        "help_tip": "說明各選項與按鈕的作用",
        "help_title": "節點預覽 — 各控制項的作用",
        "help_tabs": [
            ("GENERAL", "一般"), ("FILTER", "過濾"), ("OUTPUTS", "多輸出"),
            ("SHADER", "著色器"), ("EDITORS", "其他編輯器"),
            ("GROUPS", "節點群組"), ("DISPLAY", "顯示"), ("CACHE", "快取"),
        ],
        "help": {
            "GENERAL": [
                ("line", "顯示預覽：所有縮圖的總開關。"),
                ("line", "自動更新：節點輸入改變時自動重算；在圖片上"),
                ("line", "        用 Texture Paint 繪製、或修改節點群組內容"),
                ("line", "        後也會更新。"),
                ("line", "畫質：縮圖解析度（64 / 128 / 256 px）。"),
                ("line", "每次算幾個：每次最多算幾張；超過時間預算"),
                ("line", "        （毫秒）就提早結束。選取的節點與畫面上"),
                ("line", "        看得到的節點優先算。"),
                ("line", "換影格時更新：換影格時重算。關閉時播放動畫"),
                ("line", "        期間暫停預覽。"),
                ("line", "引擎：跟隨場景的算圖引擎（EEVEE / Cycles）。"),
                ("sec", "按鈕"),
                ("line", "刷新：重算目前編輯器中所有節點。"),
                ("line", "匯出：把作用中節點的預覽存成 PNG。"),
                ("line", "垃圾桶：清除所有快取縮圖（之後重新算圖）。"),
            ],
            "FILTER": [
                ("line", "只有貼圖 / 著色器節點：略過純 Value / Math 節點。"),
                ("line", "預覽範圍："),
                ("line", "        All：預覽所有符合的節點。"),
                ("line", "        Selected：只預覽你選取的節點。"),
                ("line", "        Marked：只預覽你開啟的節點（右鍵 > 顯示"),
                ("line", "        節點預覽，或用 勾選所選 / 取消所選）。"),
            ],
            "OUTPUTS": [
                ("line", "適用有多個輸出的節點（如 Texture Coordinate）："),
                ("line", "預覽插槽：節點要預覽哪個輸出（自動 = 第一個連線）。"),
                ("line", "        也可在右鍵選單設定。"),
                ("line", "並排顯示所有連線輸出：有連線的輸出以 2 欄格狀並排"),
                ("line", "        （每個各算一張圖）。"),
            ],
            "SHADER": [
                ("line", "著色器節點（BSDF / 輸出）："),
                ("line", "球體 / 方塊 / 平面：打光材質球、方塊或平面色板。"),
                ("line", "環境光：均勻白光，或 Blender 內建的攝影棚"),
                ("line", "        HDRI（Forest、City…）。"),
                ("line", "世界光：材質球的環境亮度。"),
                ("line", "主光：塑形的主光強度（球體 / 方塊）。"),
                ("line", "貼圖 / 顏色節點一律顯示平面色板；"),
                ("line", "        Value 輸出另外顯示數字。"),
                ("line", "燈光：燈光的節點樹會像材質一樣預覽。"),
            ],
            "EDITORS": [
                ("line", "開啟後即可預覽其他編輯器："),
                ("line", "世界：環境色板；體積節點（霧）改用打光球顯示。"),
                ("line", "幾何節點：幾何輸出以有明暗的灰色（clay）小張"),
                ("line", "        3D 算圖顯示。"),
                ("line", "        貼圖 / 數學節點（勾選框）：另外把貼圖 /"),
                ("line", "        數學 / 顏色節點顯示為平面色板。"),
                ("line", "        多個物件共用同一棵樹時，以作用中物件為準。"),
                ("line", "合成器：各節點的影像結果。每個節點會算一次"),
                ("line", "        場景，較重。編輯節點時自動更新；3D 場景"),
                ("line", "        變動後請按刷新。"),
                ("line", "        群組內節點（勾選框）：見「節點群組」頁。"),
            ],
            "GROUPS": [
                ("line", "群組節點本身也會顯示其輸出的縮圖。"),
                ("line", "進入群組（Tab）後：群組內的節點會依外層群組"),
                ("line", "        節點實際傳入的值來預覽。"),
                ("line", "同一個群組用在多處時，依你進入時所用的那個"),
                ("line", "        群組節點計算。"),
                ("line", "合成器：群組內的預覽可用「合成器」底下的"),
                ("line", "        「群組內節點」關閉；每個節點各算一次場景。"),
            ],
            "DISPLAY": [
                ("line", "縮圖大小：相對於節點寬度。"),
                ("line", "位置：節點的上、下、左或右。"),
                ("line", "放大作用中節點：畫得更大，並蓋在最上層。"),
                ("line", "棋盤格背景：讓縮圖的透明部分看得出來。"),
                ("line", "狀態標記：橘框 = 等待重算；"),
                ("line", "        紅框 / 「!」= 算圖失敗（見系統主控台）。"),
                ("line", "        失敗的節點有變動時才會重試。"),
                ("line", "顯示數值：整張都是同一個數字的 Value 色板"),
                ("line", "        會顯示該數字（例如輸入固定的 Math）。"),
                ("sec", "快捷鍵（節點編輯器）"),
                ("line", "Ctrl+Alt+P  顯示 / 隱藏預覽"),
                ("line", "Ctrl+Alt+R  刷新      Ctrl+Alt+Z  放大作用中節點"),
            ],
            "CACHE": [
                ("line", "快取：n / max（面板底部）：目前保留在 GPU 記憶體"),
                ("line", "        的縮圖數量。"),
                ("line", "上限（Max Cached Thumbnails）：在 Edit >"),
                ("line", "        Preferences > Add-ons > Node Preview"),
                ("line", "        Thumbnails 設定。超過時釋放最久沒顯示的"),
                ("line", "        縮圖；目前正在看的編輯器一定保留。"),
                ("line", "每 100 張約：3 MB（64px）、13 MB（128px）、"),
                ("line", "        50 MB（256px）。"),
                ("line", "已刪除節點的縮圖會自動釋放。"),
                ("line", "垃圾桶按鈕：立即清除所有快取縮圖（目前編輯器"),
                ("line", "        的預覽會重新算圖）。"),
            ],
        },
    },
}


def _effective_lang(props):
    """Resolve the UI language. 'AUTO' follows Blender's own language setting
    (Chinese -> ZH, anything else -> EN); 'EN' / 'ZH' force it."""
    lang = getattr(props, "language", "AUTO")
    if lang in ("EN", "ZH"):
        return lang
    try:
        loc = (bpy.app.translations.locale or "").lower()
    except Exception:
        loc = ""
    return "ZH" if loc.startswith("zh") else "EN"


def _t(props, key):
    lang = _effective_lang(props)
    return TR.get(lang, TR["EN"]).get(key, TR["EN"].get(key, key))


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
    auto_update: bpy.props.BoolProperty(name="Auto Update", default=True)
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
        rebuild_queue(sp.edit_tree, space_kind(sp), props, force=True,
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
        _tag_node_editors()
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
        _tag_node_editors()
        self.report({"INFO"}, "Preview cache cleared")
        return {"FINISHED"}


def export_job(tree, kind, props, node, path=None):
    """Queue-style item that renders ``node`` of ``tree`` (entered through
    ``path``), or None when the node can't be previewed from here."""
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
        job = export_job(sp.edit_tree, kind, props, node, path or None)
        if job is None:
            self.report({"WARNING"}, "This node can't be previewed here")
            return {"CANCELLED"}
        _state["export_to"] = fp
        _state["rendering"] = True
        try:
            ok = _render_item(job, int(self.size), props)
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
        nfail = sum(1 for k in _state["failed"]
                    if k.startswith("%d:" % context.space_data.edit_tree.as_pointer()))\
            if getattr(context.space_data, "edit_tree", None) is not None else 0
        if nfail:
            body.label(text=t("failed_fmt") % nfail, icon="ERROR")
        body.label(text=t("cached_fmt") % (len(_state["textures"]), _max_textures()),
                   icon="IMAGE_DATA")


# --------------------------------------------------------------------------- #
#  Register
# --------------------------------------------------------------------------- #
def _prefs_update(self, context):
    _prune_cache()
    _tag_node_editors()


class NPVAddonPrefs(bpy.types.AddonPreferences):
    # __name__ is the add-on's module: "node_preview_thumbnails" (legacy) or
    # "bl_ext.<repo>.node_preview" (extension package).
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


def _cleanup_datablocks():
    for name in (PREVIEW_MAT_TMP, GEO_CLAY_MAT):
        m = bpy.data.materials.get(name)
        if m is not None:
            try:
                bpy.data.materials.remove(m)
            except Exception:
                pass
    scn = bpy.data.scenes.get(PREVIEW_SCENE)
    if scn is not None:
        try:
            bpy.data.scenes.remove(scn)
        except Exception:
            pass
    for name in (PREVIEW_PLANE, PREVIEW_SPHERE, PREVIEW_CUBE, PREVIEW_CAM,
                 PREVIEW_SUN, "NPV_vol_light"):
        o = bpy.data.objects.get(name)
        if o is not None:
            data = o.data
            try:
                bpy.data.objects.remove(o)
                if data is not None and data.users == 0:
                    bpy.data.batch_remove([data])
            except Exception:
                pass
    for name in (PREVIEW_PREV_WORLD, "NPV_world"):
        w = bpy.data.worlds.get(name)
        if w is not None:
            try:
                bpy.data.worlds.remove(w)
            except Exception:
                pass
    for img in [i for i in bpy.data.images if i.name.startswith(ENV_IMAGE_PREFIX)]:
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass


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
    _state["shader_image"] = None
    _state["shader_color"] = None
    _cleanup_datablocks()
    del bpy.types.Scene.npv
    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
