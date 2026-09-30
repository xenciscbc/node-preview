"""v1.4.1: a compositor preview must not re-render the user's own scene.

A Cryptomatte node (source Render) points at the user's scene. The preview
renders a throwaway copy of the scene, but Blender's render pipeline also
renders every *other* scene the compositor tree reads -- at the copy's
thumbnail resolution -- so the user's Render Result (and the realtime
compositor's Viewer, which reads it) shrank to thumbnail size after each
auto-update. Render Layers nodes were already pointed at the copy;
Cryptomatte nodes were not.
"""
import os
import tempfile

import bpy

from npv_testutil import capture_renders


def _setup(scene):
    tree = bpy.data.node_groups.new("NPV_t141c_comp", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    go = tree.nodes.new("NodeGroupOutput")
    rl = tree.nodes.new("CompositorNodeRLayers")
    rl.name = "RL"
    rl.scene = scene
    cm = tree.nodes.new("CompositorNodeCryptomatteV2")
    cm.name = "Crypto"
    cm.source = "RENDER"
    cm.scene = scene
    tree.links.new(rl.outputs["Image"], cm.inputs["Image"])
    tree.links.new(cm.outputs["Image"], go.inputs[0])
    scene.compositing_node_group = tree
    return tree, cm


def _render_result_size(scene):
    """Size of ``scene``'s own Render Result (Image.size reads 0 x 0 in
    background mode, so save it and load it back)."""
    img = next(i for i in bpy.data.images if i.type == "RENDER_RESULT")
    path = os.path.join(tempfile.gettempdir(), "npv_t141c_probe.png")
    img.save_render(path, scene=scene)
    probe = bpy.data.images.load(path)
    try:
        return tuple(probe.size)
    finally:
        bpy.data.images.remove(probe)
        os.remove(path)


def test_crypto_preview_keeps_user_render_result(mod):
    scene = bpy.context.scene
    r = scene.render
    saved = (r.resolution_x, r.resolution_y, r.resolution_percentage)
    r.resolution_x, r.resolution_y, r.resolution_percentage = 160, 80, 100
    scene.view_layers[0].use_pass_cryptomatte_object = True
    tree, cm = _setup(scene)
    try:
        bpy.ops.render.render()
        assert _render_result_size(scene) == (160, 80)
        for node in ("Crypto", "RL"):
            with capture_renders(mod) as shots:
                assert mod.renderers.render_compositor(scene, node, 32, scene.npv)
            assert shots, "no preview rendered for %s" % node
            size = _render_result_size(scene)
            assert size == (160, 80), \
                "%s preview re-rendered the user's scene at %r" % (node, size)
        assert cm.scene == scene, "user's Cryptomatte node retargeted"
    finally:
        scene.compositing_node_group = None
        bpy.data.node_groups.remove(tree)
        scene.view_layers[0].use_pass_cryptomatte_object = False
        r.resolution_x, r.resolution_y, r.resolution_percentage = saved
