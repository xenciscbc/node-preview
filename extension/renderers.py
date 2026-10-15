# SPDX-License-Identifier: GPL-3.0-or-later
"""Render one node's preview: shader / world swatches and material balls,
geometry, compositor results, and node groups previewed with the values
the outer group node passes in."""

import hashlib
import os
import time

import bpy
from mathutils import Vector

from . import preview_scene
from .common import (
    GEO_CLAY_MAT, GEO_SUN_DIR, GEO_SUN_STRENGTH, GEO_WORLD_STRENGTH,
    PREVIEW_CUBE, PREVIEW_MAT_TMP, PREVIEW_PREV_WORLD, PREVIEW_SUN,
    SHADER_OUTPUT_NODES, VOLUME_NODES, _engine_id, _state,
)
from .eligibility import _out_by_id
from .hashing import (
    _SKIP_PROPS, _image_sig, _interface_sig, _plain, _simple_props_sig,
    _socket_default, tree_signature, upstream_hash,
)
from .preview_scene import _finish, ensure_preview_scene


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
    # The groups' interfaces: a Default Input or a min / max changes what the
    # Group Input inside passes on.
    parts.extend(_interface_sig(t) for t in path[1:])
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


# Nodes whose result depends on the object's own placement.
_SELF_XFORM_NODES = {"GeometryNodeSelfObject"}
_RELATIVE_NODES = {"GeometryNodeObjectInfo", "GeometryNodeCollectionInfo"}


def reads_object_transform(tree, _seen=frozenset()):
    """Does a Geometry Nodes tree (or a group in it) read the evaluated
    object's transform: Self Object, or Object / Collection Info in Relative
    mode? Then its previews keep the object's placement (and hash it)."""
    if tree is None or tree.as_pointer() in _seen:
        return False
    seen = _seen | {tree.as_pointer()}
    for n in tree.nodes:
        if n.bl_idname in _SELF_XFORM_NODES:
            return True
        if n.bl_idname in _RELATIVE_NODES \
                and getattr(n, "transform_space", "") == "RELATIVE":
            return True
        if getattr(n, "node_tree", None) is not None \
                and reads_object_transform(n.node_tree, seen):
            return True
    return False


def _neutral_transform(ob):
    """No placement at all on the preview copy: the geometry the nodes make,
    in object space, whatever the object's rotation mode, scale, deltas,
    parent or constraints (the frame is fitted to it afterwards)."""
    ob.parent = None
    for c in ob.constraints:
        c.mute = True
    ob.location = ob.delta_location = (0, 0, 0)
    ob.rotation_euler = ob.delta_rotation_euler = (0, 0, 0)
    ob.rotation_quaternion = ob.delta_rotation_quaternion = (1, 0, 0, 0)
    ob.rotation_axis_angle = (0, 0, 1, 0)
    ob.scale = ob.delta_scale = (1, 1, 1)


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
        # The preview renders, but should show what the viewport shows: the
        # modifiers' viewport toggles and subdivision levels.
        for m in list(obj2.modifiers)[:idx + 1]:
            m.show_render = m.show_viewport
            if hasattr(m, "render_levels") and hasattr(m, "levels"):
                m.render_levels = m.levels
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
        if not reads_object_transform(tree):
            _neutral_transform(obj2)
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


# --------------------------------------------------------------------------- #
#  Compositor: one scene render, composited per node
# --------------------------------------------------------------------------- #
# Every compositor preview is a render of the tree, and a tree reading the
# scene (Render Layers, Cryptomatte) makes that render run the engine first:
# a full scene render per previewed node, on the UI thread. Without such a
# node the render pipeline only composites. So the scene is rendered once, to
# a multilayer EXR holding every view layer and pass, and each preview reads
# it instead: in the throwaway tree copy, every Render Layers node becomes an
# Image node on that EXR and every Cryptomatte node (source Render) reads it
# as an Image. The result is the same pixels for a fraction of the time.
# Cases where the swap would change the result render as before
# (_comp_cache_eligible), and so does anything the swap can't map.
_NO_SWAP = object()

# The 'Compositor Scene Cache' modes (NPVProps.comp_cache_mode) that preview
# from the cached render; OFF renders the scene for every node, as before
# the cache. What SIMPLE / ADVANCED check of hidden objects: _components.
_CACHE_MODES = {"SIMPLE", "ADVANCED"}


def _cache_mode(props):
    return getattr(props, "comp_cache_mode", "SIMPLE")


def _reads_render(node):
    """Does this compositor node read a scene's render (and so make the
    compositor render the scene)?"""
    idn = node.bl_idname
    return idn == "CompositorNodeRLayers" or (
        idn == "CompositorNodeCryptomatteV2"
        and getattr(node, "source", "RENDER") == "RENDER")


def _comp_cache_eligible(tree, scene):
    """True when the previews of ``tree`` can composite from the cached render
    of ``scene``: some node reads the render, all of them at the top level of
    the tree, unmuted, reading ``scene`` itself. A Render Layers node inside a
    node group renders nothing (as on F12), one reading another scene renders
    that scene, a muted one passes nothing through: the cache would change
    those, so they keep rendering the scene per preview."""
    found = False
    groups = []
    for n in tree.nodes:
        if _reads_render(n):
            if n.mute or getattr(n, "scene", None) != scene:
                return False
            found = True
        sub = getattr(n, "node_tree", None)
        if sub is not None:
            groups.append(sub)
    seen = set()
    while groups:
        g = groups.pop()
        if g.as_pointer() in seen:
            continue
        seen.add(g.as_pointer())
        for n in g.nodes:
            if _reads_render(n):
                return False
            sub = getattr(n, "node_tree", None)
            if sub is not None:
                groups.append(sub)
    return found


def _comp_render_settings(scn, res, props):
    """Render settings of a compositor preview on ``scn`` (a copy of the
    user's scene): thumbnail size, the preview engine, transparent film, and
    none of the user's final-render quality or output extras."""
    r = scn.render
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
    # samples); the scene renders on the UI thread, so keep it cheap.
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
            scn.cycles.samples = min(scn.cycles.samples, 16)
        else:
            scn.eevee.taa_render_samples = min(scn.eevee.taa_render_samples, 16)
    except Exception:
        pass


def _layer_collections_sig(lc):
    return (lc.name, lc.exclude, lc.holdout, lc.indirect_only,
            tuple(_layer_collections_sig(c) for c in lc.children))


def _settings_sig(struct):
    # A view layer's depsgraph reads None until it is first evaluated (by a
    # render, say): not a setting.
    return tuple(v for v in _simple_props_sig(struct) if v[0] != "depsgraph")


def _scene_render_sig(scene):
    """The scene-level settings its render depends on: render / engine
    settings, camera, world, view layers and their passes. Selecting an
    object sends the same depsgraph update for the scene as changing these
    does, so they are compared instead (_comp_cache_key)."""
    parts = [_settings_sig(scene), _settings_sig(scene.render)]
    for attr in ("cycles", "eevee"):
        sub = getattr(scene, attr, None)
        if sub is not None:
            parts.append(_settings_sig(sub))
    for vl in scene.view_layers:
        parts.append(_settings_sig(vl))
        for attr in ("cycles", "eevee"):
            sub = getattr(vl, attr, None)
            if sub is not None:
                parts.append(_settings_sig(sub))
        parts.append(tuple(_settings_sig(a) for a in getattr(vl, "aovs", ())))
        parts.append(tuple(lg.name for lg in getattr(vl, "lightgroups", ())))
        parts.append(_layer_collections_sig(vl.layer_collection))
    return hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()


def _window_view_layers(scene):
    """The view layers of ``scene`` whose depsgraphs the open windows
    evaluate (the context's in background mode): the depsgraph handler only
    hears of edits to what those show."""
    out = []
    try:
        wm = bpy.context.window_manager
        for w in (wm.windows if wm is not None else ()):
            if w.scene == scene and w.view_layer is not None:
                out.append(w.view_layer)
        if not out and bpy.context.scene == scene and bpy.context.view_layer:
            out.append(bpy.context.view_layer)
    except Exception:
        pass
    return out


def _window_seen(scene):
    """Pointers of the objects a window's depsgraph evaluates (visible in a
    window's view layer of ``scene``): edits to those reach the depsgraph
    handler's updates."""
    seen = set()
    for wl in _window_view_layers(scene):
        for ob in wl.objects:
            try:
                if ob.visible_get(view_layer=wl):
                    seen.add(ob.as_pointer())
            except Exception:
                pass
    return seen


def _unseen_objects(scene, seen=None):
    """Objects the scene renders that no window's depsgraph evaluates: hidden
    in the viewport, or in a collection a window's view layer excludes (but
    another view layer renders), plus what such objects instance. Editing
    them reaches the depsgraph handler with nothing in its updates (or not
    at all), so the cached render follows their content instead
    (_unseen_content_sig)."""
    if seen is None:
        seen = _window_seen(scene)
    out, done = [], set()
    stack = [ob for vl in scene.view_layers if vl.use for ob in vl.objects]
    while stack:
        ob = stack.pop()
        ptr = ob.as_pointer()
        if ptr in done or ob.hide_render:
            continue
        done.add(ptr)
        if ptr in seen:
            continue
        out.append(ob)
        inst = getattr(ob, "instance_collection", None)
        if inst is not None and ob.instance_type == "COLLECTION":
            stack.extend(inst.all_objects)
    return out


# --------------------------------------------------------------------------- #
#  What the cache checks of unseen objects (the signature's components)
# --------------------------------------------------------------------------- #
# Component ids, in the order the Advanced checklist shows them (NPVProps
# comp_sig_<id>). Simple checks the first six; Advanced the ticked ones, so
# Advanced with its defaults gives exactly Simple's signature.
SIG_ITEMS = ("xform", "modifiers", "constraints", "data", "materials",
             "images", "refs", "particles", "drivers")
SIMPLE_COMPONENTS = frozenset(SIG_ITEMS[:6])


def _components(props):
    """The components the cache's signature covers in the current mode."""
    if _cache_mode(props) == "ADVANCED":
        return frozenset(i for i in SIG_ITEMS if getattr(
            props, "comp_sig_" + i, i in SIMPLE_COMPONENTS))
    return SIMPLE_COMPONENTS


# An object's settings that place or show it: component 'xform'. The rest
# of _settings_sig(ob) is always part of the signature.
_XFORM_FIELDS = {"location", "scale", "parent", "parent_type", "parent_bone",
                 "parent_vertices"}
_XFORM_PREFIXES = ("rotation_", "delta_", "hide_", "visible_", "matrix_")


def _is_xform_field(pid):
    return pid in _XFORM_FIELDS or pid.startswith(_XFORM_PREFIXES)


def _id_pointers(struct, out):
    """Append the objects ``struct``'s pointer settings reach (a collection
    as all its objects)."""
    for p in struct.bl_rna.properties:
        if p.type != "POINTER" or p.identifier == "rna_type":
            continue
        try:
            v = getattr(struct, p.identifier)
        except Exception:
            continue
        if isinstance(v, bpy.types.Object):
            out.append(v)
        elif isinstance(v, bpy.types.Collection):
            out.extend(v.all_objects)


def _walk_tree(tree, fn, _seen=None):
    """Call fn(node) for every node of ``tree`` and of the groups it nests."""
    if _seen is None:
        _seen = set()
    if tree is None or tree.as_pointer() in _seen:
        return
    _seen.add(tree.as_pointer())
    for n in tree.nodes:
        fn(n)
        sub = getattr(n, "node_tree", None)
        if isinstance(sub, bpy.types.NodeTree):
            _walk_tree(sub, fn, _seen)


def _shading_trees(ob):
    """The node trees of ``ob``'s materials, and a light's own."""
    out = [sl.material.node_tree for sl in ob.material_slots
           if sl.material is not None and sl.material.node_tree is not None]
    dnt = getattr(ob.data, "node_tree", None)
    if dnt is not None:
        out.append(dnt)
    return out


def _tree_images(trees):
    """_image_sig of every image the trees (and their nested groups) point
    to: component 'images'."""
    found = {}

    def visit(n):
        for p in n.bl_rna.properties:
            if p.type == "POINTER":
                try:
                    v = getattr(n, p.identifier)
                except Exception:
                    continue
                if isinstance(v, bpy.types.Image):
                    found[v.name] = v
        for sk in n.inputs:
            v = getattr(sk, "default_value", None) if sk.type == "IMAGE" else None
            if isinstance(v, bpy.types.Image):
                found[v.name] = v

    for t in trees:
        _walk_tree(t, visit)
    return tuple(_image_sig(found[k]) for k in sorted(found))


def _texture_sig(tex):
    img = getattr(tex, "image", None)
    nt = getattr(tex, "node_tree", None)
    return (tex.name, _settings_sig(tex),
            _image_sig(img) if img is not None else None,
            tree_signature(nt) if nt is not None else None)


def _particles_sig(ob):
    """Component 'particles': each particle system's settings and the
    textures of its slots, and the textures modifiers use (Displace, Wave,
    Warp ...)."""
    out = []
    for ps in ob.particle_systems:
        st = ps.settings
        texs = []
        if st is not None:
            for ts in getattr(st, "texture_slots", ()):
                if ts is not None and ts.texture is not None:
                    texs.append(_texture_sig(ts.texture))
        out.append((_settings_sig(ps), _settings_sig(st) if st is not None else None,
                    tuple(texs)))
    for m in ob.modifiers:
        tex = getattr(m, "texture", None)
        if isinstance(tex, bpy.types.Texture):
            out.append((m.name, _texture_sig(tex)))
    return tuple(out)


def _driver_owners(ob):
    """The data-blocks whose drivers component 'drivers' reads: the object,
    its data and shape keys, its materials and their node trees, a light's
    node tree, its Geometry Nodes modifiers' trees. (Drivers on node values
    live on the node tree, not the material.)"""
    data = ob.data
    out = [ob, data, getattr(data, "shape_keys", None),
           getattr(data, "node_tree", None)]
    for sl in ob.material_slots:
        if sl.material is not None:
            out += [sl.material, sl.material.node_tree]
    out += [m.node_group for m in ob.modifiers if m.type == "NODES"]
    return [o for o in out if o is not None]


_TRANSFORM_VARS = {"TRANSFORMS", "LOC_DIFF", "ROTATION_DIFF"}


def _driver_targets(d):
    for v in d.variables:
        n = 2 if v.type in ("LOC_DIFF", "ROTATION_DIFF") else 1
        for t in list(v.targets)[:n]:
            yield v, t


def _drivers_sig(ob):
    """Component 'drivers': each driver's type, expression, use_self and
    variables (type; each target's settings), and the target values -- a
    property read with path_resolve, a transform as the target object's
    matrix. Expressions are never evaluated and nothing is run."""
    out = []
    for owner in _driver_owners(ob):
        ad = getattr(owner, "animation_data", None)
        for fc in (ad.drivers if ad is not None else ()):
            d = fc.driver
            vs = []
            for v, t in _driver_targets(d):
                val = None
                idb = t.id
                if v.type == "SINGLE_PROP":
                    try:
                        r = idb.path_resolve(t.data_path)
                        if isinstance(r, bpy.types.ID):
                            val = ("ID", r.name)
                        elif isinstance(r, bpy.types.bpy_struct):
                            val = ("STRUCT", type(r).__name__)
                        else:
                            val = _plain(r)
                    except Exception:
                        val = "<missing>"
                elif v.type in _TRANSFORM_VARS and isinstance(idb, bpy.types.Object):
                    val = tuple(round(x, 5) for row in idb.matrix_world for x in row)
                vs.append((v.name, v.type, _settings_sig(t), val))
            out.append((owner.name, fc.data_path, fc.array_index, d.type,
                        d.expression, d.use_self, tuple(vs)))
    return tuple(out)


def _object_refs(ob, comps):
    """Component 'refs': the objects ``ob`` reaches -- its parent,
    constraint targets, modifiers' object / collection settings and
    Geometry Nodes inputs, Object / Collection pointers and socket values in
    its Geometry Nodes, material and light node trees (nested groups too);
    with 'particles' the particle instance objects, with 'drivers' the
    driver targets."""
    out = []
    if ob.parent is not None:
        out.append(ob.parent)
    for c in ob.constraints:
        _id_pointers(c, out)
        for t in getattr(c, "targets", ()):       # Armature constraint
            _id_pointers(t, out)
    trees = _shading_trees(ob)
    for m in ob.modifiers:
        _id_pointers(m, out)
        if m.type == "NODES" and m.node_group is not None:
            trees.append(m.node_group)
            inputs = getattr(getattr(m, "properties", None), "inputs", None)
            for item in m.node_group.interface.items_tree:
                if getattr(item, "item_type", "") != "SOCKET" or item.in_out != "INPUT":
                    continue
                v = getattr(getattr(inputs, item.identifier, None), "value", None)
                if isinstance(v, bpy.types.Object):
                    out.append(v)
                elif isinstance(v, bpy.types.Collection):
                    out.extend(v.all_objects)

    def visit(n):
        _id_pointers(n, out)
        for sk in n.inputs:
            if sk.type in ("OBJECT", "COLLECTION"):
                v = getattr(sk, "default_value", None)
                if isinstance(v, bpy.types.Object):
                    out.append(v)
                elif isinstance(v, bpy.types.Collection):
                    out.extend(v.all_objects)

    for t in trees:
        _walk_tree(t, visit)
    if "particles" in comps:
        for ps in ob.particle_systems:
            if ps.settings is not None:
                _id_pointers(ps.settings, out)
    if "drivers" in comps:
        for owner in _driver_owners(ob):
            ad = getattr(owner, "animation_data", None)
            for fc in (ad.drivers if ad is not None else ()):
                for _v, t in _driver_targets(fc.driver):
                    if isinstance(t.id, bpy.types.Object):
                        out.append(t.id)
    return out


def _object_sig_parts(ob, comps, queue):
    """One object's part of the signature, per component in ``comps``."""
    settings = _settings_sig(ob)
    parts = [ob.name, ob.type,
             tuple(v for v in settings if not _is_xform_field(v[0]))]
    if "xform" in comps:
        parts.append(("xform", tuple(v for v in settings if _is_xform_field(v[0])),
                      tuple(round(x, 5) for row in ob.matrix_world for x in row)))
    if "modifiers" in comps:
        parts.append(("modifiers", tuple(
            (queue._modifier_sig(m), tree_signature(m.node_group)
             if m.type == "NODES" and m.node_group is not None else None)
            for m in ob.modifiers)))
    if "constraints" in comps:
        # A constraint's settings, its target and subtarget by name.
        parts.append(("constraints",
                      tuple((c.type, _settings_sig(c)) for c in ob.constraints)))
    if "data" in comps:
        # Uncached: no depsgraph update reaches unseen data to invalidate
        # the cached fingerprint (queue._data_sig).
        try:
            dsig = queue._compute_data_sig(ob.data, ob) if ob.data is not None else None
        except Exception as exc:
            dsig = repr(exc)
        parts.append(("data", dsig))
    if "materials" in comps:
        mats = []
        for sl in ob.material_slots:
            mat = sl.material
            nt = getattr(mat, "node_tree", None)
            mats.append(None if mat is None else (
                mat.name, _settings_sig(mat),
                tree_signature(nt, images=False) if nt is not None else None))
        dnt = getattr(ob.data, "node_tree", None)          # a light's nodes
        parts.append(("materials", tuple(mats),
                      tree_signature(dnt, images=False) if dnt is not None else None))
    if "images" in comps:
        parts.append(("images", _tree_images(_shading_trees(ob))))
    if "particles" in comps:
        parts.append(("particles", _particles_sig(ob)))
    if "drivers" in comps:
        parts.append(("drivers", _drivers_sig(ob)))
    return tuple(parts)


def _unseen_content_sig(scene, comps=SIMPLE_COMPONENTS):
    """Fingerprint of the render-relevant content no window's depsgraph
    reports edits to: the unseen objects (_unseen_objects) -- per component
    in ``comps`` (SIG_ITEMS): transform and visibility, modifiers (and a
    Geometry Nodes modifier's tree), constraints, data content, materials,
    the images their node trees read; with 'refs' the objects they reach
    (_object_refs), transitively, except those a window evaluates; with
    'particles' / 'drivers' those -- and the view layers' material / world
    overrides. Content: an IMAGE-only edit, or one evaluated along with
    another update (selecting an object), can't be told from the depsgraph
    update alone (a compositor edit flags the same ID types as a material
    edit). Computed once per batch of previews (_comp_cache_key); how long it
    took and what it covered is kept for the panel (comp_sig_stats)."""
    # queue imports this module: imported here (data fingerprints).
    from . import queue
    start = time.perf_counter()
    seen = _window_seen(scene)
    unseen = sorted(_unseen_objects(scene, seen), key=lambda o: o.name)
    refs = []
    if "refs" in comps:
        visited = {o.as_pointer() for o in unseen}
        stack = [r for o in unseen for r in _object_refs(o, comps)]
        while stack:
            o = stack.pop()
            ptr = o.as_pointer()
            if ptr in visited:
                continue
            visited.add(ptr)
            if ptr in seen:
                continue            # its edits reach the depsgraph updates
            refs.append(o)
            stack.extend(_object_refs(o, comps))
        refs.sort(key=lambda o: o.name)
    parts = [_object_sig_parts(ob, comps, queue) for ob in unseen]
    parts += [("ref", _object_sig_parts(ob, comps, queue)) for ob in refs]
    for vl in scene.view_layers:
        parts.append((vl.name, _material_sig(getattr(vl, "material_override", None)),
                      _material_sig(getattr(vl, "world_override", None))))
    sig = hashlib.md5(repr(parts).encode("utf-8", "replace")).hexdigest()
    _state["comp_sig_stats"][scene.as_pointer()] = {
        "ms": (time.perf_counter() - start) * 1000.0,
        "unseen": len(unseen), "refs": len(refs)}
    return sig


def _material_sig(mat):
    if mat is None:
        return None
    nt = getattr(mat, "node_tree", None)
    return (mat.name, _settings_sig(mat),
            tree_signature(nt) if nt is not None else None)


def _comp_cache_key(scene, res, props):
    """What the cached render depends on besides the scene's contents (those
    drop the cache from the depsgraph handler, timer._on_depsgraph). The frame
    always: previews follow the frame only with 'Update on Frame Change', but
    one re-rendered after scrubbing (a node edit) must show the current one,
    as it did when every preview rendered the scene. What no window's
    depsgraph reports (hidden / excluded objects) by content, as far as the
    components checked (_components) go; those too. The working colour
    space of the blend file: switching it sends a depsgraph update with no
    updates in it, and the EXR holds pixels in the working space it was
    rendered in."""
    working = getattr(getattr(bpy.data, "colorspace", None), "working_space", None)
    comps = _components(props)
    # The unseen content once per batch of previews (queue.process_queue
    # sets the memo for its tick): nothing is edited in between, and
    # fingerprinting a heavy hidden mesh for every node adds up.
    memo = _state.get("unseen_sig_memo")
    ptr = scene.as_pointer()
    mkey = (ptr, _cache_mode(props), comps)
    if memo is not None and mkey in memo:
        unseen = memo[mkey]
    else:
        unseen = _unseen_content_sig(scene, comps)
        if memo is not None:
            memo[mkey] = unseen
    return (ptr, scene.name, _engine_id(props), int(res), scene.frame_current,
            working, _scene_render_sig(scene), tuple(sorted(comps)), unseen)


def _comp_cache(scene, res, props):
    """Path of the multilayer EXR of ``scene`` rendered with the preview
    settings, rendering it unless the cached one is still current."""
    key = _comp_cache_key(scene, res, props)
    path = preview_scene.comp_cache_path()
    c = _state.get("comp_cache")
    if c is not None and c["key"] == key and os.path.isfile(c["path"]):
        return c["path"]
    preview_scene.drop_comp_cache()
    # A copy, like the previews themselves: rendering the user's scene would
    # overwrite its Render Result. Without the compositor tree (shared with
    # the user's scene by Scene.copy(), whose Render Layers nodes read the
    # user's scene) and without compositing: the engine's passes only.
    tmp = scene.copy()
    try:
        tmp.compositing_node_group = None
        _comp_render_settings(tmp, res, props)
        r = tmp.render
        r.use_compositing = False
        # Every view layer and pass, in full float. The format only accepts
        # the multilayer EXR once the media type is set.
        r.image_settings.media_type = "MULTI_LAYER_IMAGE"
        r.image_settings.file_format = "OPEN_EXR_MULTILAYER"
        r.image_settings.color_depth = "32"
        preview_scene._render_scene(tmp, path)
    finally:
        try:
            bpy.data.scenes.remove(tmp)
        except Exception:
            pass
    _state["comp_cache"] = {"key": key, "path": path}
    return path


def _swap_in_cache(tree, img, out):
    """Make the top level of ``tree`` read the cached render ``img`` instead
    of the scene: each Render Layers node is replaced by an Image node on the
    same view layer, with the same links; each Cryptomatte node (source
    Render) reads the image. Returns ``out``, or the Image node output that
    takes its place when it belonged to a replaced node. Raises when the
    cache lacks a view layer or a pass a link (or ``out``) needs."""
    new_out = out
    for n in list(tree.nodes):
        if n.bl_idname == "CompositorNodeRLayers":
            layer = n.layer
            im = tree.nodes.new("CompositorNodeImage")
            im.image = img
            im.layer = layer
            if im.layer != layer:
                raise LookupError("no view layer %r in the cache" % layer)
            im.location = n.location
            for o in n.outputs:
                is_out = out is not None and o == out
                if not (o.is_linked or is_out):
                    continue
                # The Image node calls the combined pass Combined.
                name = "Combined" if o.name == "Image" else o.name
                no = next((x for x in im.outputs if x.name == name and x.enabled), None)
                if no is None:
                    raise LookupError("no pass %r in the cache" % name)
                for l in list(o.links):
                    muted = l.is_muted
                    nl = tree.links.new(no, l.to_socket)
                    nl.is_muted = muted
                if is_out:
                    new_out = no
            tree.nodes.remove(n)
        elif _reads_render(n):
            # The layer name ("<view layer>.<CryptoObject>" ...) and the
            # picked mattes stay; only where they are read from changes.
            layer, matte = n.layer_name, n.matte_id
            n.source = "IMAGE"
            n.image = img
            n.layer_name = layer
            n.matte_id = matte
            if n.layer_name != layer:
                raise LookupError("no Cryptomatte layer %r in the cache" % layer)
    return new_out


def _flush_depsgraph():
    """Evaluate the depsgraph updates the preview's scene copies tagged on the
    user's scene (its objects and collections were linked into the copy) now,
    while _on_depsgraph ignores them (_state["rendering"]): run later, they
    would look like scene edits and drop the cached render after every
    preview."""
    try:
        wm = bpy.context.window_manager
        layers = [w.view_layer for w in wm.windows] if wm is not None else []
        for vl in layers or [bpy.context.view_layer]:
            if vl is not None:
                vl.update()
    except Exception:
        pass


def render_compositor(scene, node_name, res, props, out_id=None, chain=None):
    """Render the preview of a compositor node. Composited from the cached
    render of the scene (_comp_cache) when the mode and the tree allow it;
    otherwise, or when the swap fails, the tree renders the scene itself.
    Export renders (at their own size) don't replace the thumbnails' cached
    render."""
    src_tree = getattr(scene, "compositing_node_group", None)
    if src_tree is None or (not chain and src_tree.nodes.get(node_name) is None):
        return None
    # The depsgraph updates of these renders (scene copies, loaded images)
    # are not scene edits: _on_depsgraph ignores them while this is set, as
    # it is during the queue and Export, but not for a direct call.
    was = _state["rendering"]
    _state["rendering"] = True
    try:
        if not _state.get("export_to") and _cache_mode(props) in _CACHE_MODES \
                and _comp_cache_eligible(src_tree, scene):
            try:
                cache = _comp_cache(scene, res, props)
            except Exception as exc:
                print("[NodePreview] compositor scene render failed: %r" % (exc,))
                cache = None
            if cache is not None:
                got = _render_compositor(scene, node_name, res, props, out_id,
                                         chain, cache)
                if got is not _NO_SWAP:
                    return got
        return _render_compositor(scene, node_name, res, props, out_id, chain)
    finally:
        _flush_depsgraph()
        _state["rendering"] = was


def _render_compositor(scene, node_name, res, props, out_id, chain, cache=None):
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
    # With ``cache`` (the path of the scene's cached render) the copy reads
    # it instead of rendering the scene; _NO_SWAP when it can't.
    src_tree = scene.compositing_node_group
    tmp = scene.copy()
    tree = src_tree.copy()
    copies = []
    img = None
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
        # (A node reading a third scene, neither this one nor a copy of it,
        # still renders that scene: it can't be redirected without changing
        # what the node shows.)
        for t in [tree] + copies:
            for n in list(t.nodes):
                if n.bl_idname in ("CompositorNodeViewer", "CompositorNodeOutputFile"):
                    t.nodes.remove(n)
                elif getattr(n, "scene", None) == scene:
                    n.scene = tmp
        if out is None:
            return None
        if cache is not None:
            # Loaded afresh each time: the file is rewritten when the cache
            # is rebuilt, and a reused image could keep the old pixels.
            try:
                img = bpy.data.images.load(cache, check_existing=False)
                out = _swap_in_cache(tree, img, out)
            except Exception as exc:
                # The cache lacking a view layer / pass (an unknown enum name
                # is a TypeError) is expected; the preview then renders the
                # scene as before. Anything else is worth a line.
                if not isinstance(exc, (LookupError, TypeError)):
                    print("[NodePreview] compositor cache not usable for %s: %r"
                          % (node_name, exc))
                return _NO_SWAP
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
        _comp_render_settings(tmp, res, props)
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
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass


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
