# SPDX-License-Identifier: GPL-3.0-or-later
"""Render one node's preview: shader / world swatches and material balls,
geometry, compositor results, and node groups previewed with the values
the outer group node passes in."""

import hashlib

import bpy
from mathutils import Vector

from . import preview_scene
from .common import (
    GEO_CLAY_MAT, GEO_SUN_DIR, GEO_SUN_STRENGTH, GEO_WORLD_STRENGTH,
    PREVIEW_CUBE, PREVIEW_MAT_TMP, PREVIEW_PREV_WORLD, PREVIEW_SUN,
    SHADER_OUTPUT_NODES, VOLUME_NODES, _engine_id, _state,
)
from .eligibility import _out_by_id
from .hashing import _SKIP_PROPS, _socket_default, upstream_hash
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
