"""Regressions fixed in v1.3.0."""
import bpy


# --- RGB / Value nodes keep their value in an *output* socket --------------- #
def _rgb_material():
    mat = bpy.data.materials.new("NPV_test_rgbval")
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    rgb = nt.nodes.new("ShaderNodeRGB")
    val = nt.nodes.new("ShaderNodeValue")
    emit = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(rgb.outputs[0], emit.inputs["Color"])
    nt.links.new(val.outputs[0], emit.inputs["Strength"])
    return mat, rgb, val, emit


def test_rgb_and_value_changes_change_the_hash(mod):
    mat, rgb, val, emit = _rgb_material()
    try:
        h0 = mod.hashing.upstream_hash(emit, {})
        rgb.outputs[0].default_value = (0.1, 0.8, 0.2, 1.0)
        h1 = mod.hashing.upstream_hash(emit, {})
        assert h1 != h0, "changing an RGB node's colour did not change the hash"
        val.outputs[0].default_value = 3.0
        h2 = mod.hashing.upstream_hash(emit, {})
        assert h2 != h1, "changing a Value node did not change the hash"
    finally:
        bpy.data.materials.remove(mat)


def test_rgb_change_inside_group_changes_group_signature(mod):
    g = bpy.data.node_groups.new("NPV_test_rgbgrp", "ShaderNodeTree")
    g.interface.new_socket("Out", in_out="OUTPUT", socket_type="NodeSocketColor")
    rgb = g.nodes.new("ShaderNodeRGB")
    go = g.nodes.new("NodeGroupOutput")
    g.links.new(rgb.outputs[0], go.inputs[0])
    try:
        s0 = mod.hashing.tree_signature(g)
        rgb.outputs[0].default_value = (0.9, 0.1, 0.1, 1.0)
        assert mod.hashing.tree_signature(g) != s0, "RGB change inside a group not detected"
    finally:
        bpy.data.node_groups.remove(g)


def test_compositor_rgb_change_requeues(mod):
    scene = bpy.context.scene
    props = scene.npv
    tree = bpy.data.node_groups.new("NPV_test_crgb", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    rgb = tree.nodes.new("CompositorNodeRGB")
    rgb.name = "RGB"
    go = tree.nodes.new("NodeGroupOutput")
    tree.links.new(rgb.outputs[0], go.inputs[0])
    scene.compositing_node_group = tree
    st = mod._state
    try:
        for k in ("textures", "hashes", "queue", "queued_keys"):
            st[k].clear()
        mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, props, path=[tree])
        assert any(it["node"] == "RGB" for it in st["queue"]), "RGB not queued"
        for it in st["queue"]:
            st["textures"][it["key"]] = object()
            st["hashes"][it["key"]] = it["hash"]
        st["queue"].clear()
        st["queued_keys"].clear()
        rgb.outputs[0].default_value = (0.1, 0.1, 0.9, 1.0)
        mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, props, path=[tree])
        assert any(it["node"] == "RGB" for it in st["queue"]), \
            "RGB colour change did not re-queue its preview"
    finally:
        for k in ("textures", "hashes", "queue", "queued_keys"):
            st[k].clear()
        scene.compositing_node_group = None
        bpy.data.node_groups.remove(tree)


# --- dropping thumbnails must redraw the node editors ----------------------- #
def test_timer_redraws_after_dropping_thumbnails(mod):
    scene = bpy.context.scene
    props = scene.npv
    tree = bpy.data.node_groups.new("NPV_test_cdrop", "CompositorNodeTree")
    grp = bpy.data.node_groups.new("NPV_test_cdropg", "CompositorNodeTree")
    inst = tree.nodes.new("CompositorNodeGroup")
    inst.node_tree = grp
    scene.compositing_node_group = tree
    st = mod._state
    calls = []
    orig = (mod.queue._tag_node_editors, mod.sources._resolve_active, mod.sources._kind_enabled,
            props.enabled, props.auto_update, props.comp_groups)
    try:
        for k in ("textures", "hashes", "queue", "queued_keys"):
            st[k].clear()
        st["textures"][mod.common._skey(grp, "Inner", None)] = object()
        props.enabled = props.auto_update = True
        props.comp_groups = False
        mod.queue._tag_node_editors = lambda: calls.append(1)
        mod.sources._resolve_active = lambda: (grp, mod.common.KIND_COMP, [tree, grp])
        mod.sources._kind_enabled = lambda kind, p: True
        st["dirty"] = True
        mod._timer()
        assert not st["textures"], "group thumbnail not dropped"
        assert calls, "thumbnails dropped without redrawing the node editors"
    finally:
        (mod.queue._tag_node_editors, mod.sources._resolve_active, mod.sources._kind_enabled,
         props.enabled, props.auto_update, props.comp_groups) = orig
        for k in ("textures", "hashes", "queue", "queued_keys"):
            st[k].clear()
        scene.compositing_node_group = None
        bpy.data.node_groups.remove(tree)
        bpy.data.node_groups.remove(grp)
