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
    (Heavier: each preview renders the scene through the compositor, so it is
    manual-refresh only.)

Engine: EEVEE or Cycles (per preview). Updates: automatic for Shader / Geometry
(only the changed nodes re-render), plus a manual Refresh button; Compositor is
manual only.

Tested on Blender 5.2 (EEVEE + Cycles, Vulkan). Legacy add-on: install via
Preferences > Add-ons > (v) Install from Disk...
"""



import os
import hashlib

import bpy
import bmesh
import gpu
import blf
from mathutils import Vector
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
PREVIEW_CAM = "NPV_preview_cam"
PREVIEW_SUN = "NPV_preview_sun"
PREVIEW_MAT_TMP = "NPV_preview_tmp_mat"
GEO_CLAY_MAT = "NPV_geo_clay"

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
    "shader_image": None, "sel_sig": None,
}


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
_socket_enum_cache = {}


def _npv_socket_items(self, context):
    node = self
    kind = _tree_kind(node.id_data) if node.id_data is not None else KIND_SHADER
    items = [("AUTO", "Auto (first linked)",
              "Preview the first linked output, or the first output if none is linked", 0)]
    for i, s in enumerate(_previewable_outputs(node, kind)):
        label = s.name or s.identifier
        items.append((s.identifier, label, "Preview the '%s' output" % label, i + 1))
    _socket_enum_cache[node.as_pointer()] = items
    return items


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


def _node_settings(node):
    vals = []
    for p in node.bl_rna.properties:
        pid = p.identifier
        if pid in _SKIP_PROPS:
            continue
        if p.type == "POINTER":
            try:
                ref = getattr(node, pid)
                vals.append((pid, ref.name if ref is not None else None))
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
    hv = hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()
    memo[ptr] = hv
    return hv


def tree_signature(tree):
    """Whole-tree fingerprint for geometry / compositor change detection."""
    parts = []
    for n in tree.nodes:
        parts.append((n.name, n.bl_idname, n.mute, _node_settings(n)))
        for inp in n.inputs:
            if not inp.is_linked:
                parts.append((n.name, inp.identifier, _socket_default(inp)))
    for l in tree.links:
        parts.append((l.from_node.name, l.from_socket.identifier,
                      l.to_node.name, l.to_socket.identifier))
    return hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------- #
#  Preview scene
# --------------------------------------------------------------------------- #
def ensure_preview_scene(res, world_strength=1.0, sun_strength=2.0,
                         engine="BLENDER_EEVEE"):
    scn = bpy.data.scenes.get(PREVIEW_SCENE)
    if scn is None:
        scn = bpy.data.scenes.new(PREVIEW_SCENE)
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
    try:
        scn.view_settings.view_transform = "Standard"
        scn.display_settings.display_device = "sRGB"
    except Exception:
        pass

    if scn.world is None:
        scn.world = bpy.data.worlds.get("NPV_world") or bpy.data.worlds.new("NPV_world")
    scn.world.use_nodes = True
    wnt = scn.world.node_tree
    bg = next((n for n in wnt.nodes if n.bl_idname == "ShaderNodeBackground"), None)
    if bg is None:
        bg = wnt.nodes.new("ShaderNodeBackground")
        wout = next((n for n in wnt.nodes if n.bl_idname == "ShaderNodeOutputWorld"), None) \
            or wnt.nodes.new("ShaderNodeOutputWorld")
        wnt.links.new(bg.outputs[0], wout.inputs["Surface"])
    bg.inputs[0].default_value = (1, 1, 1, 1)
    bg.inputs[1].default_value = world_strength

    plane = bpy.data.objects.get(PREVIEW_PLANE)
    if plane is None:
        me = bpy.data.meshes.new(PREVIEW_PLANE + "_mesh")
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0, calc_uvs=True)
        bm.to_mesh(me)
        bm.free()
        plane = bpy.data.objects.new(PREVIEW_PLANE, me)
    if plane.name not in scn.collection.objects:
        scn.collection.objects.link(plane)
    plane.location = (0, 0, 0)
    plane.rotation_euler = (0, 0, 0)
    plane.scale = (1.04, 1.04, 1.0)

    sphere = bpy.data.objects.get(PREVIEW_SPHERE)
    if sphere is None:
        me = bpy.data.meshes.new(PREVIEW_SPHERE + "_mesh")
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=0.92, calc_uvs=True)
        bm.to_mesh(me)
        bm.free()
        for poly in me.polygons:
            poly.use_smooth = True
        sphere = bpy.data.objects.new(PREVIEW_SPHERE, me)
    if sphere.name not in scn.collection.objects:
        scn.collection.objects.link(sphere)
    sphere.location = (0, 0, 0)

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


def _png_to_texture(path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            return None
        buf = Buffer("FLOAT", w * h * 4, img.pixels[:])
        return GPUTexture((w, h), format="RGBA16F", data=buf)
    finally:
        bpy.data.images.remove(img)


def _render_scene(scn):
    path = os.path.join(bpy.app.tempdir, "npv_render.png")
    scn.render.filepath = path
    # Override only the scene: adding a window makes render report FINISHED
    # without writing the file.
    with bpy.context.temp_override(scene=scn):
        bpy.ops.render.render(write_still=True)
    return path


# --------------------------------------------------------------------------- #
#  Renderers
# --------------------------------------------------------------------------- #
def render_shader(src_mat, node_name, res, props, out_id=None):
    scn, plane, sphere = ensure_preview_scene(
        res, props.world_strength, props.sun_strength, _engine_id(props))
    prev = src_mat.copy()
    prev.name = PREVIEW_MAT_TMP
    try:
        nt = prev.node_tree
        node = nt.nodes.get(node_name)
        if node is None:
            return None
        osock = _out_by_id(node, out_id)
        is_shader = (node.bl_idname in SHADER_OUTPUT_NODES
                     or (osock is not None and osock.type == "SHADER"))
        if node.bl_idname not in SHADER_OUTPUT_NODES:
            out = next((n for n in nt.nodes
                        if n.bl_idname == "ShaderNodeOutputMaterial"), None) \
                or nt.nodes.new("ShaderNodeOutputMaterial")
            surf = out.inputs["Surface"]
            for l in list(surf.links):
                nt.links.remove(l)
            if osock is None:
                return None
            if osock.type == "SHADER":
                nt.links.new(osock, surf)
            else:
                emit = nt.nodes.new("ShaderNodeEmission")
                nt.links.new(osock, emit.inputs["Color"])
                nt.links.new(emit.outputs[0], surf)
        if is_shader and props.shader_shape == "SPHERE":
            obj, other = sphere, plane
        else:
            obj, other = plane, sphere
        other.hide_render = True
        obj.hide_render = False
        obj.data.materials.clear()
        obj.data.materials.append(prev)
        return _png_to_texture(_render_scene(scn))
    finally:
        try:
            bpy.data.materials.remove(prev)
        except Exception:
            pass


def _frame_object(scn, cam, obj):
    cam.data.type = "ORTHO"
    center = Vector((0, 0, 0))
    radius = 1.0
    try:
        with bpy.context.temp_override(scene=scn):
            dg = bpy.context.evaluated_depsgraph_get()
            dg.update()
            ev = obj.evaluated_get(dg)
            corners = [obj.matrix_world @ Vector(c[:]) for c in ev.bound_box]
        center = sum(corners, Vector()) / 8.0
        radius = max((c - center).length for c in corners) or 1.0
    except Exception:
        pass
    view_dir = Vector((0.55, -0.8, 0.5)).normalized()
    cam.location = center + view_dir * (radius * 4.0 + 2.0)
    fwd = center - cam.location
    cam.rotation_euler = fwd.to_track_quat('-Z', 'Y').to_euler()
    cam.data.ortho_scale = max(radius * 2.3, 0.2)


def render_geometry(obj, node_name, res, props, out_id=None):
    mod = next((m for m in obj.modifiers
                if m.type == 'NODES' and m.node_group is not None), None)
    if mod is None:
        return None
    tree = mod.node_group
    node = tree.nodes.get(node_name)
    if node is None or not any(s.type == "GEOMETRY" for s in node.outputs):
        return None

    scn, plane, sphere = ensure_preview_scene(
        res, props.world_strength, props.sun_strength, _engine_id(props))
    plane.hide_render = True
    sphere.hide_render = True

    ng2 = tree.copy()
    obj2 = obj.copy()
    obj2.data = obj.data.copy()
    mat = bpy.data.materials.get(GEO_CLAY_MAT)
    if mat is None:
        mat = bpy.data.materials.new(GEO_CLAY_MAT)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (0.6, 0.6, 0.62, 1.0)
    try:
        m2 = next(m for m in obj2.modifiers if m.type == 'NODES')
        m2.node_group = ng2
        tgt = ng2.nodes.get(node_name)
        go = next((n for n in ng2.nodes if n.bl_idname == "NodeGroupOutput"), None)
        gos = None
        if out_id:
            gos = next((s for s in tgt.outputs
                        if s.identifier == out_id and s.type == "GEOMETRY"), None)
        if gos is None:
            gos = next((s for s in tgt.outputs if s.type == "GEOMETRY"), None)
        goin = next((i for i in go.inputs if i.type == "GEOMETRY"), None) if go else None
        if gos is None or goin is None:
            return None
        for l in list(goin.links):
            ng2.links.remove(l)
        ng2.links.new(gos, goin)

        scn.collection.objects.link(obj2)
        obj2.location = (0, 0, 0)
        obj2.rotation_euler = (0, 0, 0)
        obj2.data.materials.clear()
        obj2.data.materials.append(mat)
        obj2.hide_render = False
        _frame_object(scn, scn.camera, obj2)
        return _png_to_texture(_render_scene(scn))
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
    for si, di in zip(src.inputs, dst.inputs):
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
    m.use_nodes = True
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
        else:
            emit = nt.nodes.new("ShaderNodeEmission")
            nt.links.new(osock, emit.inputs["Color"])
            nt.links.new(emit.outputs[0], out.inputs["Surface"])
        sphere.hide_render = True
        plane.hide_render = False
        plane.data.materials.clear()
        plane.data.materials.append(m)
        return _png_to_texture(_render_scene(scn))
    finally:
        try:
            bpy.data.materials.remove(m)
        except Exception:
            pass


def render_geo(obj, node_name, res, props, out_id=None):
    """Dispatch a Geometry-node preview: a 3D render for geometry-output nodes,
    a flat swatch for field-producing ShaderNodes."""
    tree = _geo_tree_of(obj)
    if tree is None:
        return None
    node = tree.nodes.get(node_name)
    if node is None:
        return None
    if any(s.type == "GEOMETRY" for s in node.outputs):
        return render_geometry(obj, node_name, res, props, out_id)
    return render_geo_swatch(obj, node_name, res, props, out_id, tree)


def render_compositor(scene, node_name, res, props, out_id=None):
    # Blender 5.2's new compositor evaluates only its designated output during
    # a render (the Viewer image comes from the realtime GPU compositor, which
    # a headless render does not drive). So to preview a node we temporarily
    # route its output to the Group Output, render the scene through the
    # compositor to a file, read it back, then restore the original wiring.
    tree = getattr(scene, "compositing_node_group", None)
    if tree is None:
        return None
    node = tree.nodes.get(node_name)
    if node is None:
        return None
    out = _out_by_id(node, out_id)
    if out is None:
        return None
    go = next((n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"), None)
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
    saved = [(l.from_node.name, l.from_socket.identifier) for l in goin.links]
    r = scene.render
    rsaved = (r.resolution_x, r.resolution_y, r.resolution_percentage,
              r.engine, r.use_compositing, r.filepath, r.film_transparent)
    try:
        for l in list(goin.links):
            tree.links.remove(l)
        tree.links.new(out, goin)
        r.resolution_x = res
        r.resolution_y = res
        r.resolution_percentage = 100
        try:
            r.engine = _engine_id(props)
        except Exception:
            pass
        r.use_compositing = True
        r.film_transparent = True
        return _png_to_texture(_render_scene(scene))
    finally:
        for l in list(goin.links):
            tree.links.remove(l)
        for fn, fs in saved:
            src = tree.nodes.get(fn)
            if src is not None:
                so = next((s for s in src.outputs if s.identifier == fs), None)
                if so is not None:
                    tree.links.new(so, goin)
        (r.resolution_x, r.resolution_y, r.resolution_percentage,
         r.engine, r.use_compositing, r.filepath, r.film_transparent) = rsaved


def render_world(world, node_name, res, props, out_id=None):
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
    try:
        wnt = prevw.node_tree
        node = wnt.nodes.get(node_name)
        if node is None:
            return None
        out = node if node.bl_idname == "ShaderNodeOutputWorld" else \
            (next((n for n in wnt.nodes
                   if n.bl_idname == "ShaderNodeOutputWorld"), None)
             or wnt.nodes.new("ShaderNodeOutputWorld"))
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
            osock = _out_by_id(node, out_id)
            if osock is None or volin is None:
                return None
            wnt.links.new(osock, volin)
            plane.hide_render = True
            sphere.hide_render = False
            sphere.location = (0, 0, 0)
            clay = bpy.data.materials.get(GEO_CLAY_MAT)
            if clay is None:
                clay = bpy.data.materials.new(GEO_CLAY_MAT)
                clay.use_nodes = True
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
            if node.bl_idname != "ShaderNodeOutputWorld":
                surf = out.inputs["Surface"]
                for l in list(surf.links):
                    wnt.links.remove(l)
                osock = _out_by_id(node, out_id)
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
        return _png_to_texture(_render_scene(scn))
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


# --------------------------------------------------------------------------- #
#  Source resolution
# --------------------------------------------------------------------------- #
def find_material_for_tree(tree):
    for m in bpy.data.materials:
        if m.use_nodes and m.node_tree is not None and m.node_tree == tree:
            return m
    return None


def resolve_source(tree, kind):
    if kind == KIND_SHADER:
        m = find_material_for_tree(tree)
        return ("MAT", m.name) if m else None
    if kind == KIND_GEO:
        for obj in bpy.data.objects:
            for mo in obj.modifiers:
                if mo.type == 'NODES' and mo.node_group == tree:
                    return ("OBJ", obj.name)
        return None
    if kind == KIND_COMP:
        for s in bpy.data.scenes:
            if getattr(s, "compositing_node_group", None) == tree:
                return ("SCENE", s.name)
        return None
    if kind == KIND_WORLD:
        for w in bpy.data.worlds:
            if w.use_nodes and w.node_tree is not None and w.node_tree == tree:
                return ("WORLD", w.name)
        return None
    return None


def _resolve_active():
    ptr = _state["active_tree_ptr"]
    kind = _state["active_kind"]
    if ptr is None or kind is None:
        return None, None
    if kind == KIND_SHADER:
        for m in bpy.data.materials:
            if m.use_nodes and m.node_tree is not None and m.node_tree.as_pointer() == ptr:
                return m.node_tree, kind
    elif kind == KIND_GEO:
        for ng in bpy.data.node_groups:
            if ng.bl_idname == KIND_GEO and ng.as_pointer() == ptr:
                return ng, kind
    elif kind == KIND_COMP:
        for s in bpy.data.scenes:
            g = getattr(s, "compositing_node_group", None)
            if g is not None and g.as_pointer() == ptr:
                return g, kind
    elif kind == KIND_WORLD:
        for w in bpy.data.worlds:
            if w.use_nodes and w.node_tree is not None and w.node_tree.as_pointer() == ptr:
                return w.node_tree, kind
    return None, None


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
    return "%s|%.4f|%.4f" % (props.shader_shape, props.world_strength, props.sun_strength)


def _enqueue(kind, src, tree, node_name, out_id, key, h, force):
    if not force and _state["hashes"].get(key) == h and key in _state["textures"]:
        return
    if key in _state["queued_keys"]:
        for it in _state["queue"]:
            if it["key"] == key:
                it["hash"] = h
                break
        return
    _state["queue"].append({"kind": kind, "src": src[1], "node": node_name,
                            "out": out_id, "key": key, "hash": h})
    _state["queued_keys"].add(key)


def rebuild_queue(tree, kind, props, force=False):
    src = resolve_source(tree, kind)
    if src is None:
        return
    esig = _engine_id(props)
    lsig = _light_sig(props)
    memo = {}
    for node in tree.nodes:
        if not node_eligible(node, kind, props):
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
            _enqueue(kind, src, tree, node.name, out_id, key, h, force)


def process_queue(props):
    if not _state["queue"]:
        return False
    n = max(1, int(props.batch_size))
    res = int(props.resolution)
    did = False
    _state["rendering"] = True
    try:
        for _ in range(n):
            if not _state["queue"]:
                break
            item = _state["queue"].pop(0)
            _state["queued_keys"].discard(item["key"])
            k = item["kind"]
            oid = item.get("out")
            try:
                if k == KIND_SHADER:
                    m = bpy.data.materials.get(item["src"])
                    tex = render_shader(m, item["node"], res, props, oid) if m else None
                elif k == KIND_WORLD:
                    w = bpy.data.worlds.get(item["src"])
                    tex = render_world(w, item["node"], res, props, oid) if w else None
                elif k == KIND_GEO:
                    o = bpy.data.objects.get(item["src"])
                    tex = render_geo(o, item["node"], res, props, oid) if o else None
                elif k == KIND_COMP:
                    s = bpy.data.scenes.get(item["src"])
                    tex = render_compositor(s, item["node"], res, props, oid) if s else None
                else:
                    tex = None
            except Exception as exc:
                print("[NodePreview] render failed for %s: %r" % (item["node"], exc))
                tex = None
            if tex is not None:
                _state["textures"][item["key"]] = tex
                _state["hashes"][item["key"]] = item["hash"]
                did = True
    finally:
        _state["rendering"] = False
    return did


def _tag_node_editors():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "NODE_EDITOR":
                area.tag_redraw()


# --------------------------------------------------------------------------- #
#  Timer / depsgraph
# --------------------------------------------------------------------------- #
def _timer():
    props = _get_props()
    if props is None or not props.enabled:
        _state["timer_running"] = False
        return None
    if _state["dirty"] and props.auto_update:
        _state["dirty"] = False
        tree, kind = _resolve_active()
        if tree is not None and _kind_enabled(kind, props):
            rebuild_queue(tree, kind, props, force=False)
    if process_queue(props):
        _tag_node_editors()
    return 0.15


def _ensure_timer():
    if not _state["timer_running"]:
        _state["timer_running"] = True
        if not bpy.app.timers.is_registered(_timer):
            bpy.app.timers.register(_timer, first_interval=0.1)


def _on_depsgraph(scene, depsgraph):
    if _state["rendering"]:
        return
    props = getattr(scene, "npv", None)
    if props is None or not props.enabled or not props.auto_update:
        return
    for upd in depsgraph.updates:
        if getattr(upd.id, "id_type", "") in {"MATERIAL", "NODETREE", "OBJECT"}:
            _state["dirty"] = True
            break


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


def _draw_rect(color, x0, y0, x1, y1):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    pos = ((x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1))
    batch = batch_for_shader(sh, "TRIS", {"pos": pos})
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)


def _draw_border(color, x0, y0, x1, y1, width=1.0):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    pos = ((x0, y0), (x1, y0), (x1, y0), (x1, y1),
           (x1, y1), (x0, y1), (x0, y1), (x0, y0))
    batch = batch_for_shader(sh, "LINES", {"pos": pos})
    gpu.state.line_width_set(width)
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)
    gpu.state.line_width_set(1.0)


def _blf_size(font, size):
    try:
        blf.size(font, size)
    except TypeError:
        blf.size(font, size, 72)


def _draw_label(text, x, y, maxw, ps=1.0):
    """Small socket name shown on a cell (side-by-side mode). Truncated with an
    ellipsis to fit the cell width. ``ps`` is the UI pixel size so the font and
    padding scale with HiDPI / UI resolution scale."""
    font = 0
    _blf_size(font, round(11 * ps))
    limit = max(0.0, maxw - 6.0 * ps)
    if blf.dimensions(font, text)[0] > limit:
        while text and blf.dimensions(font, text + "…")[0] > limit:
            text = text[:-1]
        text = (text + "…") if text else ""
    if not text:
        return
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(font, round(1 * ps), round(-1 * ps))
    blf.position(font, x, y, 0.0)
    blf.color(font, 1.0, 1.0, 1.0, 1.0)
    blf.draw(font, text)
    blf.disable(font, blf.SHADOW)


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
    if _state["active_tree_ptr"] != ptr or _state["active_kind"] != kind:
        # Switched to a different node tree / editor type: re-queue so an
        # enabled editor auto-refreshes once on switch (when Auto Update is on),
        # instead of waiting for a depsgraph update or a manual Refresh.
        _state["dirty"] = True
    _state["active_tree_ptr"] = ptr
    _state["active_kind"] = kind
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
    gpu.state.blend_set("ALPHA")
    for node in tree.nodes:
        if not node_eligible(node, kind, props):
            continue
        cells = []
        for oid in _preview_targets(node, kind, props):
            t = _state["textures"].get(_skey(tree, node.name, oid))
            if t is not None:
                cells.append((oid, t))
        if not cells:
            continue
        loc = node.location_absolute
        # Scale node-space coords by ui_scale BEFORE view_to_region (see note).
        x0, y0 = v2d.view_to_region(loc.x * ps, loc.y * ps, clip=False)
        x1, _ = v2d.view_to_region((loc.x + node.width) * ps, loc.y * ps,
                                   clip=False)
        w = x1 - x0
        if w < 10:
            continue
        # Grid: single big swatch for one preview, otherwise 2 per row and wrap
        # to further rows (cell = half node width, so cells stay legible).
        n = len(cells)
        cols = 1 if n == 1 else 2
        rows = (n + cols - 1) // cols
        cw = w / cols
        by0 = y0 + gap                 # bottom edge of the whole grid
        gw = cols * cw                 # grid width (== node width)
        gh = rows * cw                 # grid height
        # One dark backdrop + outer border for the whole grid.
        _draw_rect((0.05, 0.05, 0.05, 0.85), x0 - pad, by0 - pad, x0 + gw + pad, by0 + gh + pad)
        oname = {s.identifier: (s.name or s.identifier) for s in node.outputs}
        for i, (oid, tex) in enumerate(cells):
            col = i % cols
            row_from_top = i // cols
            cx0 = x0 + col * cw
            cx1 = cx0 + cw
            cy1 = by0 + gh - row_from_top * cw     # top of this cell
            cy0 = cy1 - cw                         # bottom of this cell
            _draw_tex(tex, cx0, cy0, cx1, cy1)
            if n > 1:
                _draw_border((0.0, 0.0, 0.0, 1.0), cx0, cy0, cx1, cy1, bw)
                if oid and cw >= 40 * ps:
                    _draw_label(oname.get(oid, oid), cx0 + 3 * ps, cy0 + 3 * ps, cw, ps)
        _draw_border((0.0, 0.0, 0.0, 1.0), x0 - pad, by0 - pad, x0 + gw + pad, by0 + gh + pad, bw)
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
        "ctx_show": "Show Node Preview",
        "help_tip": "Explain what each option and button does",
        "help": [
            ("title", "Node Preview - what each control does"),
            ("sec", "General"),
            ("line", "Show Previews:  master on/off for all thumbnails."),
            ("line", "Auto Update:  re-render a node when its inputs change."),
            ("line", "Quality:  thumbnail resolution (64 / 128 / 256 px)."),
            ("line", "Nodes / Tick:  previews rendered per step. Higher ="),
            ("line", "        faster refresh but more stutter."),
            ("line", "Engine:  follows the scene's Render Engine."),
            ("sec", "Filtering (save resources)"),
            ("line", "Only Texture / Shader Nodes:  skip Value / Math nodes."),
            ("line", "Preview Scope:"),
            ("line", "        All:  preview every eligible node."),
            ("line", "        Selected:  only the nodes you select."),
            ("line", "        Marked:  only nodes you switch on (right-click"),
            ("line", "        > Show Node Preview, or Mark Sel / Unmark Sel)."),
            ("sec", "Multi-output nodes (e.g. Texture Coordinate)"),
            ("line", "Preview Socket:  which output the node previews"),
            ("line", "        (Auto = first linked). Also on right-click menu."),
            ("line", "Show All Linked Outputs:  preview every linked output"),
            ("line", "        side by side in a 2-column grid."),
            ("sec", "Shader Nodes (BSDF / Output)"),
            ("line", "Sphere / Plane:  lit material ball, or a flat swatch."),
            ("line", "World Light:  even environment brightness on the ball."),
            ("line", "Key Light:  sun strength (Sphere only)."),
            ("line", "Texture / color nodes always show a flat swatch."),
            ("sec", "Other Editors (turn on to preview)"),
            ("line", "World:  environment swatch; a volume node (fog) is"),
            ("line", "        shown on a lit sphere instead."),
            ("line", "Geometry Nodes:  a small 3D render of the geometry."),
            ("line", "        Texture / Math Nodes (checkbox): also show a"),
            ("line", "        flat swatch for texture / math / colour nodes."),
            ("line", "Compositor:  each node's image. Renders the scene per"),
            ("line", "        node, so it is heavier."),
            ("sec", "Buttons"),
            ("line", "Refresh:  re-render every node in the current editor."),
            ("line", "Trash:  clear all cached thumbnails."),
        ],
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
        "ctx_show": "顯示節點預覽",
        "help_tip": "說明各選項與按鈕的作用",
        "help": [
            ("title", "節點預覽 — 各控制項的作用"),
            ("sec", "一般"),
            ("line", "顯示預覽：所有縮圖的總開關。"),
            ("line", "自動更新：節點輸入改變時自動重算。"),
            ("line", "畫質：縮圖解析度（64 / 128 / 256 px）。"),
            ("line", "每次算幾個：每次更新算幾張。越高越快，"),
            ("line", "        但算圖時較卡。"),
            ("line", "引擎：跟隨場景的算圖引擎（EEVEE / Cycles）。"),
            ("sec", "過濾（節省資源）"),
            ("line", "只有貼圖 / 著色器節點：略過純 Value / Math 節點。"),
            ("line", "預覽範圍："),
            ("line", "        All：預覽所有符合的節點。"),
            ("line", "        Selected：只預覽你選取的節點。"),
            ("line", "        Marked：只預覽你開啟的節點（右鍵 > 顯示"),
            ("line", "        節點預覽，或用 勾選所選 / 取消所選）。"),
            ("sec", "多輸出節點（如 Texture Coordinate）"),
            ("line", "預覽插槽：節點要預覽哪個輸出（自動 = 第一個連線）。"),
            ("line", "        也可在右鍵選單設定。"),
            ("line", "並排顯示所有連線輸出：有連線的輸出以 2 欄格狀並排"),
            ("line", "        （每個各算一張圖）。"),
            ("sec", "著色器節點（BSDF / 輸出）"),
            ("line", "球體 / 平面：打光材質球，或平面色板。"),
            ("line", "世界光：材質球的均勻環境亮度。"),
            ("line", "主光：塑形的主光強度（僅球體）。"),
            ("line", "貼圖 / 顏色節點一律顯示平面色板。"),
            ("sec", "其他編輯器（開啟以預覽）"),
            ("line", "世界：環境色板；體積節點（霧）改用打光球顯示。"),
            ("line", "幾何節點：幾何輸出用小張 3D 算圖。"),
            ("line", "        貼圖 / 數學節點（勾選框）：另外把貼圖 /"),
            ("line", "        數學 / 顏色節點顯示為平面色板。"),
            ("line", "合成器：各節點的影像結果。每個節點會算一次"),
            ("line", "        場景，較重。"),
            ("sec", "按鈕"),
            ("line", "刷新：重算目前編輯器中所有節點。"),
            ("line", "垃圾桶：清除所有快取縮圖。"),
        ],
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
        default="128")
    batch_size: bpy.props.IntProperty(name="Nodes / Tick", default=2, min=1, max=8)
    shader_shape: bpy.props.EnumProperty(
        name="Shader Shape",
        items=[("SPHERE", "Sphere", "Material-ball preview, lit"),
               ("PLANE", "Plane", "Flat lit swatch")],
        default="SPHERE", update=_mark_dirty)
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
    preview_world: bpy.props.BoolProperty(
        name="World",
        description="Preview world / environment shader nodes",
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
        rebuild_queue(sp.edit_tree, space_kind(sp), props, force=True)
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
        _state["textures"].clear()
        _state["hashes"].clear()
        _state["queue"].clear()
        _state["queued_keys"].clear()
        _tag_node_editors()
        self.report({"INFO"}, "Preview cache cleared")
        return {"FINISHED"}


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #
class NPV_OT_help(bpy.types.Operator):
    bl_idname = "node.npv_help"
    bl_label = "Node Preview — Help"
    bl_description = "Explain what each option and button does"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=460)

    def draw(self, context):
        layout = self.layout
        props = context.scene.npv
        row = layout.row(align=True)
        row.prop(props, "language", expand=True)
        for kind, text in _t(props, "help"):
            if kind == "title":
                layout.label(text=text, icon="INFO")
            elif kind == "sec":
                layout.separator()
                layout.label(text=text)
            else:
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
            box.prop(props, "world_strength", slider=True, text=t("world_light"))
            sub = box.column(align=True)
            sub.enabled = (props.shader_shape == "SPHERE")
            sub.prop(props, "sun_strength", slider=True, text=t("key_light"))

        box = body.box()
        box.label(text=t("other_editors"), icon="NODETREE")
        box.prop(props, "preview_world", text=t("world"))
        box.prop(props, "preview_geometry", text=t("geometry"))
        sub = box.row()
        sub.enabled = props.preview_geometry
        sub.separator(factor=2.0)
        sub.prop(props, "geo_fields", text=t("geo_fields"))
        box.prop(props, "preview_compositor", text=t("compositor"))
        if kind == KIND_COMP and props.preview_compositor:
            box.label(text=t("comp_note1"), icon="INFO")
            box.label(text=t("comp_note2"), icon="BLANK1")

        row = body.row(align=True)
        row.operator("node.npv_refresh", text=t("refresh"), icon="FILE_REFRESH")
        row.operator("node.npv_clear", text="", icon="TRASH")

        if _state["queue"]:
            body.label(text=t("rendering_fmt") % len(_state["queue"]),
                       icon="SORTTIME")


# --------------------------------------------------------------------------- #
#  Register
# --------------------------------------------------------------------------- #
_classes = (NPVProps, NPV_OT_refresh, NPV_OT_mark, NPV_OT_clear, NPV_OT_help,
            NPV_PT_panel)


def _node_context_menu(self, context):
    sp = context.space_data
    node = getattr(context, "active_node", None)
    if sp and sp.type == "NODE_EDITOR" and sp.tree_type in KINDS and node is not None:
        self.layout.separator()
        self.layout.prop(node, "npv_show", text=_t(context.scene.npv, "ctx_show"))
        if len(_previewable_outputs(node, space_kind(sp))) > 1:
            self.layout.prop(node, "npv_socket",
                             text=_t(context.scene.npv, "preview_socket"))


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
    for name in (PREVIEW_PLANE, PREVIEW_SPHERE, PREVIEW_CAM, PREVIEW_SUN,
                 "NPV_vol_light"):
        o = bpy.data.objects.get(name)
        if o is not None:
            try:
                bpy.data.objects.remove(o)
            except Exception:
                pass
    for name in (PREVIEW_PREV_WORLD, "NPV_world"):
        w = bpy.data.worlds.get(name)
        if w is not None:
            try:
                bpy.data.worlds.remove(w)
            except Exception:
                pass


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.npv = bpy.props.PointerProperty(type=NPVProps)
    bpy.types.Node.npv_show = bpy.props.BoolProperty(
        name="Show Preview",
        description="Show this node's preview thumbnail "
                    "(applies when 'Only Marked Nodes' is on)",
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
    _state["dirty"] = True
    _ensure_timer()


def unregister():
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
    _state["textures"].clear()
    _state["hashes"].clear()
    _state["queue"].clear()
    _state["queued_keys"].clear()
    _cleanup_datablocks()
    del bpy.types.Scene.npv
    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


if __name__ == "__main__":
    register()
