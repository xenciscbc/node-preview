"""Regressions fixed in v1.1.9."""
import os
import tempfile

import bpy


class _Upd:
    def __init__(self, id_):
        self.id = id_


class _DG:
    def __init__(self, *ids):
        self.updates = [_Upd(i) for i in ids]


def _clear_state(mod):
    for k in ("textures", "hashes", "queue", "queued_keys"):
        mod._state[k].clear()


def _mark_rendered(mod):
    st = mod._state
    for it in st["queue"]:
        st["textures"][it["key"]] = object()
        st["hashes"][it["key"]] = it["hash"]
    st["queue"].clear()
    st["queued_keys"].clear()


# --- 10: Material/World.use_nodes is deprecated in 5.x --------------------- #
def test_source_does_not_use_deprecated_use_nodes(mod):
    from npv_testutil import addon_sources
    for path, src in addon_sources(mod):
        assert "use_nodes" not in src, "deprecated use_nodes still referenced in %s" % path


# --- 7: geometry previews must not copy the user's mesh -------------------- #
def test_geometry_preview_shares_the_mesh(mod):
    ng = bpy.data.node_groups.new("NPV_test_share", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "T"
    ng.links.new(gi.outputs[0], tr.inputs["Geometry"])
    ng.links.new(tr.outputs[0], go.inputs[0])
    import bmesh
    me = bpy.data.meshes.new("NPV_test_share_mesh")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(me)
    bm.free()
    user_mat = bpy.data.materials.new("NPV_test_user_mat")
    me.materials.append(user_mat)
    ob = bpy.data.objects.new("NPV_test_share_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    seen = {}
    orig = mod.preview_scene._render_scene

    def spy(scn):
        seen["meshes"] = sorted(m.name for m in bpy.data.meshes)
        seen["user_mats"] = [m.name if m else None for m in me.materials]
        raise RuntimeError("stop")

    mod.ensure_preview_scene(32)
    before = sorted(m.name for m in bpy.data.meshes)
    mod.preview_scene._render_scene = spy
    try:
        mod.render_geo(ob, "T", 32, bpy.context.scene.npv, tree=ng)
    except RuntimeError:
        pass
    finally:
        mod.preview_scene._render_scene = orig
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.materials.remove(user_mat)
        bpy.data.node_groups.remove(ng)
    assert seen, "render did not run"
    assert seen["meshes"] == before, "mesh copied for preview: %r" % (
        sorted(set(seen["meshes"]) - set(before)),)
    assert seen["user_mats"] == ["NPV_test_user_mat"], \
        "user's mesh materials changed during preview: %r" % seen["user_mats"]


# --- 5b: edits inside a node group ---------------------------------------- #
def test_group_internal_edit_changes_hash(mod):
    grp = bpy.data.node_groups.new("NPV_test_grp", "ShaderNodeTree")
    grp.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    inner = grp.nodes.new("ShaderNodeTexNoise")
    inner.name = "Inner"
    gout = grp.nodes.new("NodeGroupOutput")
    grp.links.new(inner.outputs["Color"], gout.inputs[0])
    mat = bpy.data.materials.new("NPV_test_grpmat")
    nt = mat.node_tree
    g = nt.nodes.new("ShaderNodeGroup")
    g.node_tree = grp
    g.name = "G"
    inv = nt.nodes.new("ShaderNodeInvert")
    inv.name = "Inv"
    nt.links.new(g.outputs[0], inv.inputs["Color"])
    try:
        memo = {}
        before = (mod.upstream_hash(g, memo), mod.upstream_hash(inv, memo))
        sig = mod.tree_signature(nt)
        inner.inputs["Scale"].default_value += 2.0
        memo = {}
        after = (mod.upstream_hash(g, memo), mod.upstream_hash(inv, memo))
        assert after[0] != before[0], "group node hash ignores the group's contents"
        assert after[1] != before[1], "downstream hash ignores the group's contents"
        assert mod.tree_signature(nt) != sig, "tree_signature ignores group contents"
    finally:
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(grp)


def test_recursive_group_does_not_loop(mod):
    # A group can't really contain itself, but nested trees are walked with
    # a guard; make sure nesting two levels deep works.
    inner = bpy.data.node_groups.new("NPV_test_in", "ShaderNodeTree")
    inner.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    outer = bpy.data.node_groups.new("NPV_test_out", "ShaderNodeTree")
    outer.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    gi = outer.nodes.new("ShaderNodeGroup")
    gi.node_tree = inner
    try:
        assert mod.tree_signature(outer)
        memo = {}
        assert mod.upstream_hash(gi, memo)
    finally:
        bpy.data.node_groups.remove(outer)
        bpy.data.node_groups.remove(inner)


# --- 5c: render engine switch --------------------------------------------- #
def test_scene_update_marks_dirty(mod):
    scene = bpy.context.scene
    mod._state["dirty"] = False
    mod._on_depsgraph(scene, _DG(scene))
    assert mod._state["dirty"], "a Scene update (e.g. engine switch) is ignored"


# --- 5a: image paint / reload --------------------------------------------- #
def _image_material(img):
    mat = bpy.data.materials.new("NPV_test_imgmat")
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.name = "Tex"
    tex.image = img
    return mat, tex


def test_image_paint_update_changes_hash(mod):
    img = bpy.data.images.new("NPV_test_paint", 16, 16)
    mat, tex = _image_material(img)
    try:
        h0 = mod.upstream_hash(tex, {})
        mod._on_depsgraph(bpy.context.scene, _DG(img))  # what a paint stroke sends
        assert mod.upstream_hash(tex, {}) != h0, "image update does not change the hash"
    finally:
        bpy.data.materials.remove(mat)
        bpy.data.images.remove(img)


def test_image_file_change_changes_hash(mod):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "npv_tex.png")
    src = bpy.data.images.new("NPV_test_src", 8, 8)
    src.filepath_raw = path
    src.file_format = "PNG"
    src.save()
    bpy.data.images.remove(src)
    img = bpy.data.images.load(path)
    mat, tex = _image_material(img)
    try:
        h0 = mod.upstream_hash(tex, {})
        st = os.stat(path)
        os.utime(path, (st.st_atime, st.st_mtime + 10))  # "edited externally"
        img.reload()
        assert mod.upstream_hash(tex, {}) != h0, "reloaded image file does not change the hash"
    finally:
        bpy.data.materials.remove(mat)
        bpy.data.images.remove(img)


# --- 8: texture cache pruning --------------------------------------------- #
def test_rebuild_drops_textures_of_removed_nodes(mod):
    mat = bpy.data.materials.new("NPV_test_prune")
    nt = mat.node_tree
    n1 = nt.nodes.new("ShaderNodeTexNoise")
    n1.name = "Keep"
    n2 = nt.nodes.new("ShaderNodeTexWave")
    n2.name = "Gone"
    props = bpy.context.scene.npv
    try:
        mod.rebuild_queue(nt, mod.KIND_SHADER, props)
        _mark_rendered(mod)
        gone = [k for k in mod._state["textures"] if ":Gone|" in k]
        assert gone, "setup: no texture for 'Gone'"
        nt.nodes.remove(n2)
        mod.rebuild_queue(nt, mod.KIND_SHADER, props)
        left = [k for k in mod._state["textures"] if ":Gone|" in k]
        assert not left, "texture of a deleted node kept: %r" % left
        assert any(":Keep|" in k for k in mod._state["textures"]), "live texture dropped"
    finally:
        _clear_state(mod)
        bpy.data.materials.remove(mat)


def test_prune_drops_textures_of_deleted_trees(mod):
    mat = bpy.data.materials.new("NPV_test_prune2")
    mat.node_tree.nodes.new("ShaderNodeTexNoise")
    props = bpy.context.scene.npv
    try:
        mod.rebuild_queue(mat.node_tree, mod.KIND_SHADER, props)
        _mark_rendered(mod)
        assert mod._state["textures"], "setup: nothing cached"
        bpy.data.materials.remove(mat)
        mat = None
        mod._prune_cache()
        assert not mod._state["textures"], "textures of a deleted material kept: %r" % list(
            mod._state["textures"])
    finally:
        _clear_state(mod)
        if mat is not None:
            bpy.data.materials.remove(mat)


def test_prune_caps_cache_size_keeping_recent(mod):
    mat = bpy.data.materials.new("NPV_test_cap")
    ptr = mat.node_tree.as_pointer()
    st = mod._state
    try:
        n = mod.MAX_TEXTURES + 20
        for i in range(n):
            key = "%d:N%d|" % (ptr, i)
            st["textures"][key] = object()
            st["hashes"][key] = "h"
            mod._touch(key)
        mod._prune_cache()
        assert len(st["textures"]) == mod.MAX_TEXTURES, len(st["textures"])
        assert "%d:N%d|" % (ptr, n - 1) in st["textures"], "most recent texture evicted"
        assert "%d:N0|" % ptr not in st["textures"], "oldest texture kept"
    finally:
        _clear_state(mod)
        bpy.data.materials.remove(mat)
