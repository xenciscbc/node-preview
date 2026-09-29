"""v1.4.0 review fixes: change detection (curves, image user, Geometry Nodes
modifier inputs / object data), output-node choice, stale render files,
off-origin geometry framing, compositor output extras, queue pruning, stable
enum numbers, linked data with local names, Clear Cache."""
import os
import tempfile

import bpy

from npv_testutil import capture_renders, load_pixels, mean_rgb


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    mod._reset_cache()
    mod._state["src_hint"] = []


def _mark_rendered(mod):
    st = mod._state
    for it in st["queue"]:
        st["textures"][it["key"]] = object()
        st["hashes"][it["key"]] = it["hash"]
    st["queue"].clear()
    st["queued_keys"].clear()


def _bare_material(name):
    mat = bpy.data.materials.new(name)
    for n in list(mat.node_tree.nodes):
        mat.node_tree.nodes.remove(n)
    return mat


# --------------------------------------------------------------------------- #
#  Change detection
# --------------------------------------------------------------------------- #
def test_curve_edit_changes_the_hash(mod):
    mat = _bare_material("NPV_trv_curve")
    try:
        node = mat.node_tree.nodes.new("ShaderNodeRGBCurve")
        h1 = mod.upstream_hash(node, {})
        assert mod.upstream_hash(node, {}) == h1, "curve hash is unstable"
        node.mapping.curves[3].points[0].location = (0.0, 0.4)
        node.mapping.update()
        assert mod.upstream_hash(node, {}) != h1, "curve edit not detected"
    finally:
        bpy.data.materials.remove(mat)


def test_image_user_changes_the_hash_but_not_its_frame(mod):
    mat = _bare_material("NPV_trv_iu")
    img = bpy.data.images.new("NPV_trv_iu_img", 8, 8)
    try:
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        h1 = mod.upstream_hash(node, {})
        try:
            node.image_user.frame_current = 7
        except (AttributeError, TypeError):
            pass
        assert mod.upstream_hash(node, {}) == h1, \
            "the sequence frame changed the hash without Update on Frame Change"
        node.image_user.frame_offset = 5
        assert mod.upstream_hash(node, {}) != h1, "Image User edit not detected"
    finally:
        bpy.data.materials.remove(mat)
        bpy.data.images.remove(img)


def test_hashes_are_stable_for_common_nodes(mod):
    # Anything unstable in a node's settings would re-render it forever.
    mat = _bare_material("NPV_trv_stable")
    img = bpy.data.images.new("NPV_trv_stable_img", 8, 8)
    try:
        nt = mat.node_tree
        for idn in ("ShaderNodeTexImage", "ShaderNodeRGBCurve", "ShaderNodeValToRGB",
                    "ShaderNodeMapping", "ShaderNodeTexNoise", "ShaderNodeMath",
                    "ShaderNodeMix", "ShaderNodeFloatCurve", "ShaderNodeVectorCurve",
                    "ShaderNodeTexEnvironment", "ShaderNodeTexCoord"):
            try:
                n = nt.nodes.new(idn)
            except RuntimeError:
                continue
            if hasattr(n, "image"):
                n.image = img
        for n in nt.nodes:
            a = mod.upstream_hash(n, {})
            b = mod.upstream_hash(n, {})
            assert a == b, "hash of %s is unstable" % n.bl_idname
    finally:
        bpy.data.materials.remove(mat)
        bpy.data.images.remove(img)


def _gn_object():
    ng = bpy.data.node_groups.new("NPV_trv_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    amount = ng.interface.new_socket("Amount", in_out="INPUT",
                                     socket_type="NodeSocketFloat")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "Xform"
    ng.links.new(gi.outputs[0], tr.inputs[0])
    ng.links.new(tr.outputs[0], go.inputs[0])
    me = bpy.data.meshes.new("NPV_trv_gn_mesh")
    ob = bpy.data.objects.new("NPV_trv_gn_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    m = ob.modifiers.new("GN", "NODES")
    m.node_group = ng
    m[amount.identifier] = 1.0
    return ob, ng, m, amount.identifier


class _Upd:
    def __init__(self, idt, name):
        self.id = type("ID", (), {"id_type": idt, "name": name})()
        self.is_updated_transform = False
        self.is_updated_geometry = False
        self.is_updated_shading = False


class _DG:
    def __init__(self, *updates):
        self.updates = updates


def test_geo_modifier_inputs_and_object_data_requeue(mod):
    ob, ng, m, ident = _gn_object()
    props = _props()
    props.preview_geometry = True
    try:
        _clear(mod)
        mod._state["src_hint"] = [("OBJ", ob.name)]
        mod.rebuild_queue(ng, mod.KIND_GEO, props)
        assert any(it["node"] == "Xform" for it in mod._state["queue"])
        _mark_rendered(mod)
        mod.rebuild_queue(ng, mod.KIND_GEO, props)
        assert not mod._state["queue"], "GN previews re-queued with no change"

        m.show_expanded = not m.show_expanded
        mod.rebuild_queue(ng, mod.KIND_GEO, props)
        assert not mod._state["queue"], "a modifier panel toggle re-rendered"

        m[ident] = 2.0
        mod.rebuild_queue(ng, mod.KIND_GEO, props)
        assert mod._state["queue"], "modifier input change not detected"
        _mark_rendered(mod)

        mod._on_depsgraph(bpy.context.scene, _DG(_Upd("MESH", ob.data.name)))
        assert mod._state["dirty"]
        mod.rebuild_queue(ng, mod.KIND_GEO, props)
        assert mod._state["queue"], "object data edit not detected"
    finally:
        props.preview_geometry = False
        _clear(mod)
        me = ob.data
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


def test_tree_signature_memo_is_scoped_to_a_rebuild(mod):
    mat = _bare_material("NPV_trv_memo")
    try:
        mat.node_tree.nodes.new("ShaderNodeTexNoise")
        _clear(mod)
        mod.rebuild_queue(mat.node_tree, mod.KIND_SHADER, _props())
        assert mod._state["tree_sig_memo"] is None, "memo left active after rebuild"
    finally:
        _clear(mod)
        bpy.data.materials.remove(mat)


# --------------------------------------------------------------------------- #
#  Output node choice
# --------------------------------------------------------------------------- #
def test_preview_uses_the_output_the_engine_renders(mod):
    # Two engine-specific outputs; the first one is for the engine that is not
    # rendering. The preview of 'Src' must not show the material's own result.
    mat = _bare_material("NPV_trv_outs")
    nt = mat.node_tree
    out_other = nt.nodes.new("ShaderNodeOutputMaterial")
    out_this = nt.nodes.new("ShaderNodeOutputMaterial")
    engine = mod._engine_id(_props())
    out_other.target = "EEVEE" if engine == "CYCLES" else "CYCLES"
    out_this.target = "CYCLES" if engine == "CYCLES" else "EEVEE"
    green = nt.nodes.new("ShaderNodeEmission")
    green.inputs["Color"].default_value = (0.0, 1.0, 0.0, 1.0)
    nt.links.new(green.outputs[0], out_other.inputs["Surface"])
    nt.links.new(green.outputs[0], out_this.inputs["Surface"])
    src = nt.nodes.new("ShaderNodeRGB")
    src.name = "Src"
    src.outputs[0].default_value = (1.0, 0.0, 0.0, 1.0)
    links = len(nt.links)
    try:
        with capture_renders(mod) as shots:
            assert mod.render_shader(mat, "Src", 32, _props())
        r, g, b = mean_rgb(shots[0])
        assert r > g + 0.3, "preview shows the material, not the node: %r" % ((r, g, b),)
        assert len(nt.links) == links and len(nt.nodes) == 4, "user material modified"
    finally:
        bpy.data.materials.remove(mat)


# --------------------------------------------------------------------------- #
#  Renders
# --------------------------------------------------------------------------- #
def test_stale_render_file_is_never_returned(mod):
    scn, _plane, _sphere = mod.ensure_preview_scene(16)
    path = os.path.join(bpy.app.tempdir, "npv_render.png")
    with open(path, "wb") as f:
        f.write(b"stale")
    try:
        got = mod._render_scene(scn)
    except RuntimeError:
        got = None
    if got is not None:
        with open(got, "rb") as f:
            assert f.read() != b"stale", "the previous render file was reused"


def test_geometry_off_origin_is_framed(mod):
    ng = bpy.data.node_groups.new("NPV_trv_far", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube")
    cube.name = "Cube"
    ng.links.new(cube.outputs["Mesh"], go.inputs[0])
    me = bpy.data.meshes.new("NPV_trv_far_mesh")
    ob = bpy.data.objects.new("NPV_trv_far_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    ob.location = (30.0, -12.0, 5.0)
    ob.rotation_euler = (0.3, 0.0, 0.8)
    res = 48
    shots = []
    orig = mod._png_to_texture

    def spy(p):
        shots.append(load_pixels(p))
        return True

    mod._png_to_texture = spy
    try:
        assert mod.render_geo(ob, "Cube", res, _props())
    finally:
        mod._png_to_texture = orig
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)
    px = shots[0]
    pts = [(i % res, i // res) for i in range(res * res) if px[i * 4 + 3] > 0.5]
    assert len(pts) > res * res * 0.1, "off-origin geometry out of frame (%d px)" % len(pts)
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    assert abs(cx - res / 2) < res / 4 and abs(cy - res / 2) < res / 4, \
        "geometry not centred: (%.1f, %.1f)" % (cx, cy)


def test_compositor_preview_ignores_output_extras(mod):
    scene = bpy.context.scene
    tree = bpy.data.node_groups.new("NPV_trv_c", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    go = tree.nodes.new("NodeGroupOutput")
    rgb = tree.nodes.new("CompositorNodeRGB")
    rgb.name = "RGB"
    tree.links.new(rgb.outputs[0], go.inputs[0])
    scene.compositing_node_group = tree
    r = scene.render
    attrs = ("use_sequencer", "use_border", "use_stamp")
    saved = {a: getattr(r, a) for a in attrs}
    seen = []
    orig = mod._render_scene

    def spy(s):
        seen.append({a: getattr(s.render, a) for a in attrs})
        raise RuntimeError("stop")

    for a in attrs:
        setattr(r, a, True)
    mod._render_scene = spy
    try:
        try:
            mod.render_compositor(scene, "RGB", 32, scene.npv)
        except RuntimeError:
            pass
        assert seen and not any(seen[0].values()), "preview inherited %r" % seen
        assert all(getattr(r, a) for a in attrs), "user render settings modified"
    finally:
        mod._render_scene = orig
        for a, v in saved.items():
            setattr(r, a, v)
        scene.compositing_node_group = None
        bpy.data.node_groups.remove(tree)


# --------------------------------------------------------------------------- #
#  Queue
# --------------------------------------------------------------------------- #
def test_rebuild_drops_pending_renders_no_longer_shown(mod):
    mat = _bare_material("NPV_trv_scope")
    props = _props()
    try:
        nt = mat.node_tree
        for idn in ("ShaderNodeTexNoise", "ShaderNodeTexWave"):
            nt.nodes.new(idn).select = False
        _clear(mod)
        mod.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert len(mod._state["queue"]) == 2
        props.preview_scope = "SELECTED"
        mod.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert not mod._state["queue"], "renders of filtered-out nodes kept queued"
        assert not mod._state["queued_keys"]
    finally:
        props.preview_scope = "ALL"
        _clear(mod)
        bpy.data.materials.remove(mat)


def test_timer_drops_pending_renders_of_disabled_kinds(mod):
    props = _props()
    st = mod._state
    _clear(mod)
    try:
        props.preview_compositor = False
        st["queue"][:] = [{"key": "1:a|", "kind": mod.KIND_COMP, "chain": []},
                          {"key": "1:b|", "kind": mod.KIND_SHADER, "chain": []}]
        st["queued_keys"] = {"1:a|", "1:b|"}
        mod._drop_disallowed(props)
        assert [it["key"] for it in st["queue"]] == ["1:b|"], st["queue"]
        assert st["queued_keys"] == {"1:b|"}

        props.preview_compositor = True
        props.comp_groups = False
        st["queue"][:] = [{"key": "1:c|", "kind": mod.KIND_COMP, "chain": ["G"]},
                          {"key": "1:d|", "kind": mod.KIND_COMP, "chain": []}]
        st["queued_keys"] = {"1:c|", "1:d|"}
        mod._drop_disallowed(props)
        assert [it["key"] for it in st["queue"]] == ["1:d|"], st["queue"]
    finally:
        props.preview_compositor = False
        props.comp_groups = True
        _clear(mod)


# --------------------------------------------------------------------------- #
#  Enums, linked data, Clear Cache
# --------------------------------------------------------------------------- #
def test_socket_enum_numbers_follow_the_identifier(mod):
    mat = _bare_material("NPV_trv_enum")
    try:
        a = mat.node_tree.nodes.new("ShaderNodeTexCoord")
        b = mat.node_tree.nodes.new("ShaderNodeTexCoord")
        items = mod._npv_socket_items(a, None)
        nums = [it[3] for it in items]
        assert nums[0] == 0 and len(set(nums)) == len(nums), nums
        by_id = {it[0]: it[3] for it in items}
        uv = next(s for s in a.outputs if s.identifier == "UV")
        try:
            uv.enabled = False
        except (AttributeError, TypeError):
            print("  (socket 'enabled' is read-only here; part skipped)")
        else:
            items2 = mod._npv_socket_items(a, None)
            assert len(items2) == len(items) - 1, items2
            assert all(by_id[it[0]] == it[3] for it in items2), \
                "a socket's number changed when another was disabled"
            uv.enabled = True
        n = len(mod._socket_enum_cache)
        assert mod._npv_socket_items(b, None) is mod._npv_socket_items(a, None)
        assert len(mod._socket_enum_cache) == n, "cache grows per node"
    finally:
        bpy.data.materials.remove(mat)


def test_env_enum_numbers_are_unique(mod):
    items = mod._env_items(None, None)
    nums = [it[3] for it in items]
    assert items[0][0] == "UNIFORM" and nums[0] == 0
    assert len(set(nums)) == len(nums), nums


def test_linked_data_with_a_local_name_resolves_separately(mod):
    name = "NPV_trv_lib_mat"
    mat = bpy.data.materials.new(name)
    path = os.path.join(tempfile.mkdtemp(), "npv_trv_lib.blend")
    bpy.data.libraries.write(path, {mat})
    with bpy.data.libraries.load(path, link=True) as (_src, dst):
        dst.materials = [name]
    linked = next(m for m in bpy.data.materials if m.name == name and m.library)
    try:
        assert mod._idref(mat) == name
        assert mod._idget(bpy.data.materials, name) == mat, "plain name hit linked data"
        ref = mod._idref(linked)
        assert ref != name
        assert mod._idget(bpy.data.materials, ref) == linked
        mod._state["src_hint"] = [("MAT", ref)]
        assert mod.resolve_source(linked.node_tree, mod.KIND_SHADER) == ("MAT", ref)
    finally:
        mod._state["src_hint"] = []
        bpy.data.libraries.remove(linked.library)
        bpy.data.materials.remove(mat)


def test_clear_cache_marks_previews_dirty(mod):
    mod._state["textures"]["1:x|"] = object()
    mod._state["dirty"] = False
    bpy.ops.node.npv_clear()
    assert not mod._state["textures"]
    assert mod._state["dirty"], "Clear Cache left the editor blank"
