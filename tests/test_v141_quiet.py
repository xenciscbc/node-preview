"""v1.4.1: preview renders don't print Blender's "Saved: '...'" line (one per
thumbnail buried real [NodePreview] errors in the console)."""
import os
import sys
import tempfile

import bpy

from npv_testutil import capture_renders, color_std


def _capture_fds(fn):
    """Run fn() with fds 1 and 2 (C-level output included) redirected to a
    temp file; returns (fn's result, captured text)."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    with tempfile.TemporaryFile() as tmp:
        os.dup2(tmp.fileno(), 1)
        os.dup2(tmp.fileno(), 2)
        try:
            result = fn()
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
        tmp.seek(0)
        text = tmp.read().decode("utf-8", "replace")
    return result, text


def _material():
    mat = bpy.data.materials.new("NPV_t141_quiet")
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    tex = nt.nodes.new("ShaderNodeTexChecker")
    tex.name = "Checker"
    tex.inputs["Scale"].default_value = 4.0
    emit = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs[0], out.inputs["Surface"])
    return mat


def test_preview_render_prints_no_saved_line(mod):
    mat = _material()
    props = bpy.context.scene.npv
    try:
        mod.ensure_preview_scene(32)
        with capture_renders(mod) as shots:
            ok, text = _capture_fds(lambda: mod.render_shader(mat, "Checker", 32, props))
        assert ok, "render failed"
        assert "Saved:" not in text, "Blender's 'Saved:' line is back: %r" % text[-300:]
        assert shots and color_std(shots[0]) > 0.05, "render is flat / empty"
    finally:
        bpy.data.materials.remove(mat)


def test_value_swatch_exr_still_written_quietly(mod):
    # The EXR path (Value numbers) goes through the same save.
    mat = bpy.data.materials.new("NPV_t141_val")
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    m = nt.nodes.new("ShaderNodeMath")
    m.name = "Src"
    m.operation = "ADD"
    m.inputs[0].default_value, m.inputs[1].default_value = 1.25, 2.0
    emit = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(m.outputs[0], emit.inputs["Color"])
    nt.links.new(emit.outputs[0], out.inputs["Surface"])
    got = []
    orig = mod._png_to_texture
    mod._png_to_texture = lambda p: got.append(mod._load_render(p)) or True
    try:
        ok, text = _capture_fds(lambda: mod.render_shader(mat, "Src", 16, bpy.context.scene.npv))
    finally:
        mod._png_to_texture = orig
        bpy.data.materials.remove(mat)
    assert ok and got, "EXR swatch render failed"
    assert "Saved:" not in text, text[-300:]
    assert got[0][3] is not None and abs(got[0][3] - 3.25) < 1e-3, got[0][3]
