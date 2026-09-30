"""v1.3.0: previews for nodes inside compositor node groups, with an
'Inside Node Groups' toggle under Compositor."""
import bpy

from npv_testutil import capture_renders, datablock_names, mean_rgb


def _clear_state(mod):
    for k in ("textures", "hashes", "queue", "queued_keys"):
        mod._state[k].clear()


def _tree_state(tree):
    return (sorted(n.name for n in tree.nodes),
            sorted((l.from_node.name, l.from_socket.identifier,
                    l.to_node.name, l.to_socket.identifier) for l in tree.links),
            [(i.name, i.in_out) for i in tree.interface.items_tree])


def _setup(scene):
    """Scene compositor: RGB (red) -> group 'G' -> Group Output. Inside the
    group, 'Inner Mix' passes the group input through (factor 0, B = blue)."""
    grp = bpy.data.node_groups.new("NPV_test_cgrp", "CompositorNodeTree")
    grp.interface.new_socket("In", in_out="INPUT", socket_type="NodeSocketColor")
    grp.interface.new_socket("Out", in_out="OUTPUT", socket_type="NodeSocketColor")
    gi = grp.nodes.new("NodeGroupInput")
    go = grp.nodes.new("NodeGroupOutput")
    mix = grp.nodes.new("ShaderNodeMix")
    mix.name = "Inner Mix"
    mix.data_type = "RGBA"
    mix.inputs["Factor"].default_value = 0.0
    mix.inputs["B"].default_value = (0.0, 0.0, 1.0, 1.0)
    grp.links.new(gi.outputs[0], mix.inputs["A"])
    grp.links.new(mix.outputs["Result"], go.inputs[0])

    tree = bpy.data.node_groups.new("NPV_test_ctop", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    out = tree.nodes.new("NodeGroupOutput")
    rgb = tree.nodes.new("CompositorNodeRGB")
    rgb.outputs[0].default_value = (0.9, 0.1, 0.1, 1.0)
    inst = tree.nodes.new("CompositorNodeGroup")
    inst.node_tree = grp
    inst.name = "G"
    tree.links.new(rgb.outputs[0], inst.inputs[0])
    tree.links.new(inst.outputs[0], out.inputs[0])
    tree.nodes.active = inst
    scene.compositing_node_group = tree
    return tree, grp


def _teardown(mod, scene, tree, grp):
    _clear_state(mod)
    scene.npv.comp_groups = True
    scene.compositing_node_group = None
    bpy.data.node_groups.remove(tree)
    bpy.data.node_groups.remove(grp)


def test_comp_groups_defaults_on(mod):
    assert bpy.context.scene.npv.comp_groups is True


def test_comp_node_inside_group_previews_with_outer_input(mod):
    scene = bpy.context.scene
    props = scene.npv
    tree, grp = _setup(scene)
    try:
        _clear_state(mod)
        mod.queue.rebuild_queue(grp, mod.common.KIND_COMP, props, path=[tree, grp])
        items = [it for it in mod._state["queue"] if it["node"] == "Inner Mix"]
        assert items, "Inner Mix not queued: %r" % [i["node"] for i in mod._state["queue"]]
        mod._state["queue"][:] = items
        tstate, gstate = _tree_state(tree), _tree_state(grp)
        snap = datablock_names()
        with capture_renders(mod) as shots:
            mod.queue.process_queue(props)
        assert shots, "nothing rendered for Inner Mix"
        r, g, b = mean_rgb(shots[0])
        assert r > g + 0.3 and r > b + 0.3, "inner node not red from outer input: %r" % ((r, g, b),)
        assert _tree_state(tree) == tstate, "user compositor tree modified"
        assert _tree_state(grp) == gstate, "user compositor group modified"
        assert scene.compositing_node_group == tree, "user compositor tree swapped"
        assert datablock_names() == snap, "compositor group preview leaked datablocks"
    finally:
        _teardown(mod, scene, tree, grp)


def test_comp_groups_toggle_off_skips_and_drops(mod):
    scene = bpy.context.scene
    props = scene.npv
    tree, grp = _setup(scene)
    try:
        _clear_state(mod)
        key = mod.common._skey(grp, "Inner Mix", None)
        mod._state["textures"][key] = object()
        props.comp_groups = False
        mod.queue.rebuild_queue(grp, mod.common.KIND_COMP, props, path=[tree, grp])
        assert not mod._state["queue"], "queued inside a group with the toggle off"
        assert key not in mod._state["textures"], "stale group thumbnail kept"
        # The top level is unaffected by the toggle.
        mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, props, path=[tree])
        assert any(it["node"] == "G" for it in mod._state["queue"]), \
            "top-level group node not queued"
    finally:
        _teardown(mod, scene, tree, grp)
