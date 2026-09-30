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
import time
import hashlib

import bpy
from bpy.app.handlers import persistent
import gpu
import blf
from mathutils import Vector
import numpy as np
from gpu_extras.batch import batch_for_shader

# Split-out modules, in dependency order (a module only imports from those
# before it). Installing or updating through Blender's extension operators
# drops the add-on and all its modules from sys.modules, but when files change
# in place (development) and the add-on is re-enabled, or on Reload Scripts,
# Blender reloads only this file: reload them first so they don't keep
# running the old code. On the first load none of them is in sys.modules yet.
_SUBMODULES = ("common", "eligibility", "hashing", "i18n", "preview_scene",
               "sources")
for _name in _SUBMODULES:
    _mod = sys.modules.get("%s.%s" % (__name__, _name))
    if _mod is not None:
        importlib.reload(_mod)
del _name, _mod

# The code below uses their names as globals, imported here. A function that
# tests replace (mod.<module>.<name> = fake) is never imported by name: it is
# called through its module (preview_scene._render_scene(...)) so the
# replacement reaches every caller. tests/test_package_layout.py checks this.
from .common import (
    KIND_SHADER, KIND_GEO, KIND_COMP, KIND_WORLD, KINDS, PREVIEW_PREV_WORLD,
    space_kind, PREVIEW_SCENE, PREVIEW_PLANE, PREVIEW_SPHERE, PREVIEW_CUBE,
    ENV_IMAGE_PREFIX, PREVIEW_CAM, PREVIEW_SUN, PREVIEW_MAT_TMP, GEO_CLAY_MAT,
    GEO_WORLD_STRENGTH, GEO_SUN_STRENGTH, GEO_SUN_DIR, SHADER_OUTPUT_NODES,
    VOLUME_NODES, _state, MAX_TEXTURES, _skey, _view_ctx, _key_ctx, _engine_id,
)
from .eligibility import (
    _previewable_outputs, _out_by_id, _preview_targets, _socket_enum_cache,
    _npv_socket_items, _zone_cache, _in_zone, node_eligible, renders_as_shader,
)
from .hashing import (
    _socket_default, _SKIP_PROPS, _plain, _STRUCT_SKIP, _simple_props_sig,
    upstream_hash, _isolated_hash, tree_signature,
)
from .i18n import TR, _t
from . import preview_scene, sources
from .preview_scene import _env_items, ensure_preview_scene, _finish
from .sources import _idref, _idget, resolve_source, _tree_by_pointer

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
        return _finish(preview_scene._render_scene(scn))
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
        return _finish(preview_scene._render_scene(scn))
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
        return _finish(preview_scene._render_scene(scn))
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
        # Every node reading the user's scene (Render Layers, Cryptomatte)
        # must read the copy instead: the render pipeline also renders each
        # other scene the tree reads, at the copy's thumbnail size, which
        # overwrote the user's Render Result and so the Viewer's input.
        for t in [tree] + copies:
            for n in list(t.nodes):
                if n.bl_idname in ("CompositorNodeViewer", "CompositorNodeOutputFile"):
                    t.nodes.remove(n)
                elif getattr(n, "scene", None) == scene:
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
        # loader needs an 8-bit RGBA PNG. A Video / Multilayer EXR output
        # only accepts its own formats until the media type is Image again.
        if "media_type" in r.image_settings.bl_rna.properties:
            r.image_settings.media_type = "IMAGE"
        r.image_settings.file_format = "PNG"
        # Stereoscopy ("Individual" views) writes npv_render_L/_R.png and
        # never the file the loader reads.
        r.use_multiview = False
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
        return _finish(preview_scene._render_scene(tmp))
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
        return _finish(preview_scene._render_scene(scn))
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


def _is_ui_prop(pid):
    return (pid in _MOD_UI_PROPS or pid.startswith("open_") or pid == "is_open"
            or pid == "panels"
            or (pid.startswith("show_") and pid not in ("show_viewport", "show_render")))


def _nested_sig(struct, depth=2):
    """Plain settings of the non-ID structs and collection items hanging off
    ``struct`` (``depth`` levels), minus UI state. Blender 5.2 no longer keeps
    Geometry Nodes modifier inputs as ID properties; walking the RNA catches
    them wherever the running version exposes them."""
    out = []
    for p in struct.bl_rna.properties:
        pid = p.identifier
        if pid in _STRUCT_SKIP or _is_ui_prop(pid) or p.type not in {"POINTER", "COLLECTION"}:
            continue
        try:
            v = getattr(struct, pid)
        except Exception:
            continue
        if p.type == "POINTER":
            if v is None or isinstance(v, bpy.types.ID):
                continue            # ID pointers: by name in _simple_props_sig
            items = [v]
        else:
            try:
                items = list(v)
            except Exception:
                continue
            if len(items) > 512:
                continue
        sig = []
        for it in items:
            if it is None or isinstance(it, bpy.types.ID):
                sig.append(_plain(it))
                continue
            flat = tuple(kv for kv in _simple_props_sig(it) if not _is_ui_prop(kv[0]))
            sig.append((flat, _nested_sig(it, depth - 1) if depth > 1 else ()))
        out.append((pid, tuple(sig)))
    return tuple(out)


def _modifier_sig(m):
    """A modifier's settings and its inputs, minus panel / UI state. Up to
    Blender 5.1 a Geometry Nodes modifier keeps its input values as ID
    properties; from 5.2 they are RNA data (walked by _nested_sig)."""
    vals = [(pid, v) for pid, v in _simple_props_sig(m) if not _is_ui_prop(pid)]
    try:
        idp = tuple(sorted((k, _plain(m[k])) for k in m.keys()))
    except Exception:           # 5.2+: "id properties not supported"
        idp = ()
    try:
        nested = _nested_sig(m)
    except Exception:
        nested = ()
    return (m.type, tuple(vals), idp, nested, _gn_inputs_sig(m))


def _gn_inputs_sig(m):
    """Blender 5.2+: a Geometry Nodes modifier's input values live at
    ``m.properties.inputs.<socket identifier>`` (value / attribute name /
    input type), one struct per group input."""
    inputs = getattr(getattr(m, "properties", None), "inputs", None)
    ng = getattr(m, "node_group", None)
    if inputs is None or ng is None:
        return ()
    out = []
    for item in ng.interface.items_tree:
        if getattr(item, "item_type", "") != "SOCKET" or item.in_out != "INPUT":
            continue
        v = getattr(inputs, item.identifier, None)
        if v is None:
            continue
        try:
            out.append((item.identifier, _simple_props_sig(v),
                        _plain(getattr(v, "value", None))))
        except Exception:
            pass
    return tuple(out)


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
    mods = list(obj.modifiers)
    parts = [_modifier_sig(m) for m in mods[:idx + 1]]
    # An earlier Geometry Nodes modifier's tree shapes the geometry every
    # node of this one receives: its contents, not just its name.
    for m in mods[:idx]:
        if m.type == 'NODES' and m.node_group is not None:
            parts.append(("tree", m.name, tree_signature(m.node_group)))
    # Vertex groups: the names live on the object (a node reads a group by
    # name), so they are read here, not cached with the mesh.
    parts.append(tuple(g.name for g in obj.vertex_groups))
    parts.append(_data_sig(obj.data, obj))
    return hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()


def _coords_digest(coll, attr, width, dtype=np.float32):
    """md5 of one attribute of every item of an RNA collection."""
    n = len(coll)
    arr = np.empty(n * width, dtype=dtype)
    if n:
        coll.foreach_get(attr, arr)
    return hashlib.md5(arr.tobytes()).hexdigest()


# Attribute data type -> (field read by foreach_get, width, numpy dtype).
_ATTR_FIELDS = {
    "FLOAT": ("value", 1, np.float32), "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int32), "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT_VECTOR": ("vector", 3, np.float32), "FLOAT2": ("vector", 2, np.float32),
    "INT32_2D": ("value", 2, np.int32), "INT16_2D": ("value", 2, np.int32),
    "FLOAT_COLOR": ("color", 4, np.float32), "BYTE_COLOR": ("color", 4, np.float32),
    "QUATERNION": ("value", 4, np.float32), "FLOAT4X4": ("value", 16, np.float32),
}
VGROUP_WEIGHT_LIMIT = 100000   # vertices; above it weights aren't hashed


_UI_ATTR_PREFIXES = (".select", ".hide", ".uv_select", ".vs.", ".es.", ".pn.",
                     ".sculpt_mask")


def _attrs_sig(attrs):
    """Every attribute's name, domain, type and values: positions, UVs,
    colours, custom attributes, and the internal topology arrays
    (.edge_verts, .corner_vert ...: Flip Normals / Rotate Edge change only
    these). Selection, hiding and UV-editor state are skipped: they don't
    change what a preview shows."""
    out = []
    for a in attrs:
        if a.name.startswith(_UI_ATTR_PREFIXES):
            continue
        f = _ATTR_FIELDS.get(a.data_type)
        try:
            dig = _coords_digest(a.data, f[0], f[1], f[2]) if f else len(a.data)
        except Exception:
            dig = len(a.data)
        out.append((a.name, a.domain, a.data_type, dig))
    return tuple(out)


def _vgroups_sig(data, obj):
    """Vertex-group weights (weight paint). They live in the mesh but are not
    attributes, so this is a Python loop: skipped above VGROUP_WEIGHT_LIMIT
    vertices, and deferred while weight painting (see _data_sig). The group
    names are on the object (_geo_source_sig). Only read when the object has
    vertex groups: vertex.groups is not touched otherwise."""
    if obj is None or not len(getattr(obj, "vertex_groups", ())):
        return None
    verts = getattr(data, "vertices", None)
    if verts is None:
        return None
    if len(verts) > VGROUP_WEIGHT_LIMIT:
        return "unhashed"
    h = hashlib.md5()
    for v in verts:
        for g in v.groups:
            h.update(b"%d:%d:%.5f;" % (v.index, g.group, g.weight))
    return h.hexdigest()


def _splines_sig(data):
    out = []
    for sp in data.splines:
        out.append((_simple_props_sig(sp),
                    _coords_digest(sp.bezier_points, "co", 3),
                    _coords_digest(sp.bezier_points, "handle_left", 3),
                    _coords_digest(sp.bezier_points, "handle_right", 3),
                    _coords_digest(sp.bezier_points, "radius", 1),
                    _coords_digest(sp.bezier_points, "tilt", 1),
                    _coords_digest(sp.points, "co", 4),
                    _coords_digest(sp.points, "radius", 1),
                    _coords_digest(sp.points, "tilt", 1)))
    return tuple(out)


def _gpencil_sig(data):
    """Grease Pencil (v3): every layer's settings and every drawing's
    attributes (stroke points live there)."""
    out = []
    for layer in data.layers:
        frames = []
        for fr in getattr(layer, "frames", ()):
            dr = getattr(fr, "drawing", None)
            attrs = getattr(dr, "attributes", None)
            frames.append((fr.frame_number,
                           _attrs_sig(attrs) if attrs is not None else None))
        out.append((layer.name, _simple_props_sig(layer), tuple(frames)))
    return tuple(out)


def _shape_keys_sig(data):
    """Shape keys: each key's value, range, mute, relative key, vertex group
    and point positions (a slider drag or editing a non-Basis key changes
    the shape the preview renders)."""
    sk = getattr(data, "shape_keys", None)
    if sk is None:
        return None
    out = [sk.use_relative, round(getattr(sk, "eval_time", 0.0), 5)]
    for kb in sk.key_blocks:
        out.append((kb.name, round(kb.value, 5), round(kb.slider_min, 5),
                    round(kb.slider_max, 5), kb.mute, kb.interpolation,
                    kb.relative_key.name if kb.relative_key else None,
                    kb.vertex_group, _coords_digest(kb.data, "co", 3)))
    return tuple(out)


def _compute_data_sig(data, obj=None):
    parts = [_idref(data), _simple_props_sig(data)]   # bevel, text size, ...
    try:
        parts.append(_shape_keys_sig(data))
        attrs = getattr(data, "attributes", None)
        if attrs is not None:
            parts.append(_attrs_sig(attrs))
            for dom in ("vertices", "edges", "polygons", "loops", "points", "curves"):
                c = getattr(data, dom, None)
                if c is not None:
                    parts.append((dom, len(c)))
            parts.append(_vgroups_sig(data, obj))
        elif hasattr(data, "splines"):                  # Curve / Text
            parts.append(_splines_sig(data))
            parts.append(getattr(data, "body", None))
        elif hasattr(data, "points") and hasattr(data, "points_u"):   # Lattice
            parts.append(_coords_digest(data.points, "co_deform", 3))
        if hasattr(data, "layers") and not hasattr(data, "splines"):  # GP v3
            parts.append(_gpencil_sig(data))
    except Exception as exc:
        parts.append(repr(exc))
    return tuple(parts)


def _data_sig(data, obj=None):
    """Fingerprint of an object's own data *content* (positions, attribute
    values, vertex-group weights, the data's settings; curve points, radius
    and tilt; Grease Pencil drawings), part of its Geometry Nodes previews'
    hash. Content, not update events: a preview render shares the user's
    mesh and makes Blender report it as updated, so counting events
    re-rendered every GN preview after each render, forever. Computed again
    only after an update event for that data (_on_depsgraph). In Edit Mode
    the mesh keeps its pre-edit data until you leave it, which is also what
    the previews render."""
    if data is None:
        return None
    ref = _idref(data)
    # Keyed by whether weights are included: a mesh shared by an object with
    # vertex groups and one without has two fingerprints, each noticing a
    # change on its own (the update count is per data-block).
    ck = (ref, bool(obj is not None and len(getattr(obj, "vertex_groups", ()))))
    gen = _state["data_gen"].get(ref, 0)
    hit = _state["data_sigs"].get(ck)
    if hit is not None and (getattr(data, "is_editmode", False) or (
            obj is not None and obj.mode == "WEIGHT_PAINT")):
        # Edit Mode: the mesh keeps its pre-edit data until you leave, so a
        # fingerprint now would only re-render the same stale image.
        # Weight Paint: every brush dab updates the mesh; hashing the weights
        # each time would stall painting. Either way the update count stays
        # ahead: leaving the mode updates the object and the next rebuild
        # fingerprints once.
        return hit[1]
    if hit is None or hit[0] != gen:
        hit = (gen, _compute_data_sig(data, obj))
        _state["data_sigs"][ck] = hit
    return hit[1]


def _mark_data_changed(ref):
    """Invalidate the cached fingerprints of one object data-block."""
    _state["data_gen"][ref] = _state["data_gen"].get(ref, 0) + 1


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
    _zone_cache.clear()
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
    ctx = _view_ctx(src, chain)
    # Resolution is part of the signature so a Quality change re-renders;
    # the source too (a GN tree shared by several objects previews the one
    # the editor shows).
    esig = "%s|%s|%s|%d" % (_engine_id(props), props.resolution, src,
                            int(getattr(props, "show_values", True)))
    if getattr(props, "update_on_frame", False):
        esig += "|f%d" % bpy.context.scene.frame_current
    if chain:
        esig += "|" + _context_sig(src, path, chain)
    # The object / modifier side of a GN preview only matters to 3D renders of
    # geometry: field swatches (Noise, Math ...) render the node in isolation,
    # so a mesh edit or modifier input change doesn't re-render them.
    gsig = "|" + _geo_source_sig(src[1], root) if kind == KIND_GEO else ""
    lsig = _light_sig(props)
    memo = {}
    live = set()
    for node in tree.nodes:
        if skip or not node_eligible(node, kind, props):
            continue
        try:
            if kind == KIND_GEO and not any(s.type == "GEOMETRY" for s in node.outputs):
                # A field swatch renders the node alone (upstream fields are
                # not evaluated), so upstream edits can't change it.
                h = _isolated_hash(node)
            else:
                h = upstream_hash(node, memo)
        except Exception:
            continue
        if kind == KIND_SHADER and renders_as_shader(node):
            extra = esig + "|" + lsig
        elif gsig and any(s.type == "GEOMETRY" for s in node.outputs):
            extra = esig + gsig
        else:
            extra = esig
        h = hashlib.md5((h + extra).encode("utf-8", "replace")).hexdigest()
        for out_id in _preview_targets(node, kind, props):
            key = _skey(tree, node.name, out_id, ctx)
            live.add(key)
            _enqueue(kind, src, tree, node.name, out_id, key, h, force,
                     root, chain)
    # Thumbnails of this tree that are no longer shown (node deleted or
    # renamed, output switched, filtered out) only hold GPU memory.
    # Only this view's (same context): another editor may show the same tree
    # through another source, and its thumbnails are still in use. Those of
    # a context nobody shows any more age out of the cache (_prune_cache).
    prefix = "%d:" % tree.as_pointer()

    def stale(k):
        return k.startswith(prefix) and k not in live and _key_ctx(k) in ("", ctx)

    for key in [k for k in set(_state["textures"]) | set(_state["failed"]) if stale(k)]:
        _drop_texture(key)
    # Their pending renders too (e.g. Scope switched to Selected while a
    # compositor tree was queued: each one would render the whole scene), and
    # those of another context no open editor shows (the source switched
    # before they rendered).
    shown = {e.get("ctx") for e in _state["editors"].values()
             if e.get("tree") == tree.as_pointer()}

    def unwanted(k):
        return stale(k) or (k.startswith(prefix) and k not in live
                            and _key_ctx(k) not in shown)

    q = _state["queue"]
    gone = [it for it in q if unwanted(it["key"])]
    if gone:
        q[:] = [it for it in q if not unwanted(it["key"])]
        _state["queued_keys"].difference_update(it["key"] for it in gone)


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
    for k in ("textures", "tex_tick", "img_gen", "data_sigs", "data_gen", "hashes", "failed",
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
        tick = _state["tex_tick"]
        victims = sorted((k for k in _state["textures"] if not _in_view(k)),
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
    if kind is not None and not sources._kind_enabled(kind, props):
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
def _live_space_ptrs():
    """Pointers of every node-editor space in every open window, or None
    when there are no windows (background mode)."""
    wm = bpy.context.window_manager
    if wm is None or not wm.windows:
        return None
    ptrs = set()
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "NODE_EDITOR":
                for sp in area.spaces:
                    if sp.type == "NODE_EDITOR":
                        ptrs.add(sp.as_pointer())
    return ptrs


def _live_space_kinds():
    """Space pointer -> the preview kind it shows now (None for a tree type
    we don't preview), or None when there are no windows."""
    wm = bpy.context.window_manager
    if wm is None or not wm.windows:
        return None
    kinds = {}
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "NODE_EDITOR":
                for sp in area.spaces:
                    if sp.type == "NODE_EDITOR":
                        kinds[sp.as_pointer()] = (space_kind(sp)
                                                  if sp.tree_type in KINDS else None)
    return kinds


def _prune_editors():
    """Forget editors that were closed, switched to another editor type, or
    now show a tree type / preview kind other than the one recorded (the
    space pointer stays the same when only its tree type changes, and the
    draw callback that would forget it may run after the next timer tick)."""
    live = _live_space_ptrs()
    if live is None:
        return
    kinds = _live_space_kinds() or {}
    eds = _state["editors"]
    stale = [k for k, e in eds.items() if k not in live
             or (k in kinds and kinds[k] != e.get("kind"))]
    gone = [eds.pop(k) for k in stale]
    if gone:
        _editors_gone(gone)


def _in_view(key):
    """Is this thumbnail one an open editor is showing right now (its tree
    in the context that editor shows)? Those are never evicted. Thumbnails of
    the same tree for another source (another object sharing a GN tree ...)
    age out like any other. With no editor recorded, the last drawn tree's."""
    try:
        ptr = int(key.split(":", 1)[0])
    except ValueError:
        return False
    eds = _state["editors"]
    if not eds:
        return ptr == _state["active_tree_ptr"]
    kc = _key_ctx(key)
    return any(e.get("tree") == ptr and e.get("ctx", kc) in (kc, "")
               for e in eds.values())


def _editor_targets():
    """(tree, kind, path, source hint) for every editor showing previews.
    With no editor recorded (e.g. background mode) the last drawn one --
    cleared once the last recorded editor is forgotten (_editors_gone)."""
    _prune_editors()
    props = _get_props()
    eds = [e for e in _state["editors"].values()
           if props is None or sources._kind_enabled(e["kind"], props)]
    # Editors on the same tree through different sources (pinned to another
    # object, a group entered from another material) each get their own
    # thumbnails: the source is part of the cache key (_view_ctx).
    out, seen = [], set()
    for e in eds:
        sig = (e["kind"], tuple(e["path"]), repr(e["hint"]))
        if sig in seen:
            continue
        path = [_tree_by_pointer(p) for p in e["path"]]
        if not path or any(t is None for t in path):
            continue
        seen.add(sig)
        out.append((path[-1], e["kind"], path, e["hint"]))
    if not out:
        tree, kind, path = sources._resolve_active()
        out.append((tree, kind, path, _state["src_hint"]))
    return out


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
        saved_hint = _state["src_hint"]
        try:
            for tree, kind, path, hint in _editor_targets():
                if tree is not None and sources._kind_enabled(kind, props):
                    _state["src_hint"] = hint
                    rebuild_queue(tree, kind, props, force=False, path=path)
        finally:
            _state["src_hint"] = saved_hint
    # Every tick, before rendering: an editor switched away from previews
    # must not get one more render before its area redraws.
    _prune_editors()
    _drop_disallowed(props)
    rendered = process_queue(props)
    _state["prune_in"] -= 1
    # Prune on the ~3 s cadence, and right away once new renders push the
    # cache over its limit (switching quickly between objects would overshoot
    # it for seconds otherwise).
    if _state["prune_in"] <= 0 or (rendered and len(_state["textures"]) > _max_textures()):
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
    # Which object data changed is recorded even while previews are off or
    # Auto Update is off: the fingerprints are cached (_data_sig), and an
    # edit made meanwhile must not be missed once previews come back.
    for upd in depsgraph.updates:
        idt = getattr(upd.id, "id_type", "")
        try:
            if idt in _DATA_ID_TYPES:
                _mark_data_changed(_idref(getattr(upd.id, "original", upd.id)))
            elif idt == "KEY":
                # A shape key slider: the Key's user is the mesh / curve.
                user = getattr(getattr(upd.id, "original", upd.id), "user", None)
                if user is not None:
                    _mark_data_changed(_idref(user))
            elif idt == "OBJECT" and upd.is_updated_geometry:
                # Sculpting, or a script's foreach_set + update_tag(), may tag
                # only the object: its data may have changed too.
                data = getattr(getattr(upd.id, "original", upd.id), "data", None)
                if data is not None:
                    _mark_data_changed(_idref(data))
        except Exception:
            if idt in _DATA_ID_TYPES:
                _mark_data_changed(getattr(upd.id, "name", ""))
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
        elif idt in _DATA_ID_TYPES or idt == "KEY":
            # An object's own data may have changed (recorded above): re-hash;
            # its content is part of its GN previews' hash (_data_sig).
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
    _state["editors"].clear()
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


def _editor_hint(ctx, space):
    """Which material / light / object the editor shows (its id / id_from
    and, unless it is pinned, the active object), so the queue previews
    through it (see resolve_source)."""
    objs = [getattr(space, "id", None), getattr(space, "id_from", None)]
    if not getattr(space, "pin", False):
        objs.append(getattr(ctx, "active_object", None))
    hint = []
    for d in objs:
        # _idref: linked data keeps its library, so a linked material "Foo"
        # doesn't resolve to a local "Foo" (or to nothing).
        if isinstance(d, bpy.types.Material):
            hint.append(("MAT", _idref(d)))
        elif isinstance(d, bpy.types.Light):
            hint.append(("LIGHT", _idref(d)))
        elif isinstance(d, bpy.types.Object):
            hint.append(("OBJ", _idref(d)))
            if isinstance(d.data, bpy.types.Light):
                hint.append(("LIGHT", _idref(d.data)))
    return list(dict.fromkeys(hint))


def _record_editor(ctx, space, ptr, kind, path, props, tree):
    """Store this editor's view; mark previews dirty only when *this*
    editor's own view changed (so two editors redrawing in turn no longer
    re-queue each other on every redraw)."""
    try:
        sptr = space.as_pointer()
    except Exception:
        sptr = id(space)
    pinned = bool(getattr(space, "pin", False))
    hint = _editor_hint(ctx, space)
    sel = None
    if getattr(props, "preview_scope", "ALL") == "SELECTED":
        # A selection change has no depsgraph update: watch it here.
        sel = tuple(sorted(n.name for n in tree.nodes if n.select))
    eds = _state["editors"]
    old = eds.get(sptr)
    new_view = (ptr, kind, path, hint, pinned, sel)
    if old is None or old["view"] != new_view:
        _state["dirty"] = True
    ent = old or {"visible": set(), "priority": set()}
    ent.update({"view": new_view, "tree": ptr, "kind": kind, "path": path,
                "hint": hint, "pinned": pinned})
    eds[sptr] = ent
    # The last drawn editor, for code / tests that look at one editor.
    _state["active_tree_ptr"] = ptr
    _state["active_kind"] = kind
    _state["active_path"] = path
    if not pinned:
        _state["src_hint"] = hint
    return ent


def _editor_view_ctx(space, tree, kind, hint):
    """The _view_ctx this editor's thumbnails are cached under -- the same
    one rebuild_queue computes for its view (source through this editor's
    hint, group-node chain through its path)."""
    trees = [p.node_tree for p in getattr(space, "path", ()) if p.node_tree is not None]
    if not trees or trees[-1] != tree:
        trees = [tree]
    chain = _instance_chain(trees)
    if chain is None:
        return ""
    saved = _state["src_hint"]
    _state["src_hint"] = hint
    try:
        src = resolve_source(trees[0], kind)
    finally:
        _state["src_hint"] = saved
    return _view_ctx(src, chain) if src is not None else ""


def _forget_editor(space):
    """This editor shows no previews (any more): stop rebuilding / protecting
    the tree it showed before."""
    try:
        ent = _state["editors"].pop(space.as_pointer(), None)
    except Exception:
        return
    if ent is not None:
        _editors_gone([ent])


def _editors_gone(gone):
    """Editors ``gone`` were forgotten: drop their pending renders that no
    remaining editor shows, and once no editor is left, the last drawn tree
    too -- the no-editor fallback (_editor_targets, _in_view) would otherwise
    keep rebuilding and protecting the tree they showed."""
    eds = _state["editors"]
    if not eds:
        _state["active_tree_ptr"] = None
        _state["active_kind"] = None
        _state["active_path"] = None
    trees = {e.get("tree") for e in gone}
    shown = {(e.get("tree"), e.get("ctx")) for e in eds.values()}

    def orphan(k):
        try:
            ptr = int(k.split(":", 1)[0])
        except ValueError:
            return False
        if ptr not in trees:
            return False
        kc = _key_ctx(k)
        return not any(t == ptr and c in (kc, "", None) for t, c in shown)

    q = _state["queue"]
    drop = [it for it in q if orphan(it["key"])]
    if drop:
        q[:] = [it for it in q if not orphan(it["key"])]
        _state["queued_keys"].difference_update(it["key"] for it in drop)


def draw_callback():
    ctx = bpy.context
    space = ctx.space_data
    if space is None or space.type != "NODE_EDITOR":
        return
    if space.tree_type not in KINDS:
        _forget_editor(space)
        return
    kind = space_kind(space)
    props = getattr(ctx.scene, "npv", None)
    if props is None or not props.enabled or not sources._kind_enabled(kind, props):
        _forget_editor(space)
        return
    tree = getattr(space, "edit_tree", None)
    if tree is None:
        _forget_editor(space)
        return
    _zone_cache.clear()

    ptr = tree.as_pointer()
    # Tree path of the editor (outermost first): entering a node group adds
    # the group's tree; the previews inside need the path back to the
    # material / world / modifier.
    path = [p.node_tree.as_pointer() for p in space.path if p.node_tree is not None]
    if not path or path[-1] != ptr:
        path = [ptr]
    # A new or changed view (other tree / editor type / path / source /
    # selection) re-queues once, so the editor auto-refreshes on switch.
    ent = _record_editor(ctx, space, ptr, kind, path, props, tree)
    _ensure_timer()
    vctx = _editor_view_ctx(space, tree, kind, ent["hint"])
    ent["ctx"] = vctx

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
        keys = [(oid, _skey(tree, node.name, oid, vctx))
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
    # The queue serves every editor: render order uses all of their sets.
    ent["visible"], ent["priority"] = visible, priority
    vis, pri = set(), set()
    for e in _state["editors"].values():
        vis |= e["visible"]
        pri |= e["priority"]
    _state["visible"], _state["priority"] = vis, pri

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


def _toggle_auto_update(self, context):
    # Turned back on: catch up on whatever changed while it was off.
    if self.auto_update:
        _state["dirty"] = True
        _ensure_timer()


def _redraw(self, context):
    _tag_node_editors()


def _node_show_update(self, context):
    _state["dirty"] = True
    _tag_node_editors()


def _scope_update(self, context):
    _state["dirty"] = True
    _state["sel_sig"] = None
    _tag_node_editors()


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
        body.label(text=t("cached_fmt") % (len(_state["textures"]), _max_textures()),
                   icon="IMAGE_DATA")


# --------------------------------------------------------------------------- #
#  Register
# --------------------------------------------------------------------------- #
def _prefs_update(self, context):
    _prune_cache()
    _tag_node_editors()


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
