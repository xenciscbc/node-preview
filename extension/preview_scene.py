# SPDX-License-Identifier: GPL-3.0-or-later
"""The hidden preview scene (camera, lights, meshes, environment) and the
render -> PNG -> GPU texture round trip."""

import os
import shutil

import bpy
import bmesh
import numpy as np
from gpu.types import GPUTexture, Buffer

from .common import (
    ENV_IMAGE_PREFIX, PREVIEW_CAM, PREVIEW_CUBE, PREVIEW_PLANE, PREVIEW_SCENE,
    PREVIEW_SPHERE, PREVIEW_SUN, _state,
)
from .eligibility import _enum_num


# --------------------------------------------------------------------------- #
#  Preview scene
# --------------------------------------------------------------------------- #
def _new_uv_mesh(name, build):
    """Mesh built by ``build(bm)`` with a UV layer. bmesh ``calc_uvs`` only
    fills an existing UV layer; it never creates one."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")
    build(bm)
    bm.to_mesh(me)
    bm.free()
    return me


def _drop_if_no_uv(obj):
    """Remove a preview object left by an older version without UVs (image
    textures would sample a single texel); returns None so it gets rebuilt."""
    if obj is None or getattr(obj.data, "uv_layers", None):
        return obj
    me = obj.data
    bpy.data.objects.remove(obj)
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)
    return None


_env_enum_cache = []


def _env_dir():
    try:
        return bpy.utils.system_resource("DATAFILES", path="studiolights/world") or ""
    except Exception:
        return ""


def _env_items(self, context):
    """'Uniform' plus Blender's bundled studio-light HDRIs."""
    items = [("UNIFORM", "Uniform", "Even white environment (World Light sets "
              "its strength)", 0)]
    d = _env_dir()
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.lower().endswith((".exr", ".hdr")))
    except OSError:
        files = []
    used = {0}
    for f in files:
        label = os.path.splitext(f)[0].replace("_", " ").title()
        items.append((f, label, "Light the preview with Blender's '%s' HDRI" % label,
                      _enum_num(f, used)))
    _env_enum_cache[:] = items
    return items


def _env_image(filename):
    """The bundled HDRI ``filename`` as an image (loaded once), or None."""
    name = ENV_IMAGE_PREFIX + filename
    img = bpy.data.images.get(name)
    if img is not None:
        return img
    path = os.path.join(_env_dir(), filename)
    if not os.path.isfile(path):
        return None
    try:
        img = bpy.data.images.load(path, check_existing=False)
    except RuntimeError:
        return None
    img.name = name
    return img


def _setup_env(wnt, bg, env, world_strength):
    """Feed the preview world's Background from a bundled HDRI, or white."""
    tex = wnt.nodes.get("NPV_env")
    img = _env_image(env) if env and env != "UNIFORM" else None
    if img is None:
        if tex is not None:
            wnt.nodes.remove(tex)
        bg.inputs[0].default_value = (1, 1, 1, 1)
    else:
        if tex is None:
            tex = wnt.nodes.new("ShaderNodeTexEnvironment")
            tex.name = "NPV_env"
        tex.image = img
        if not bg.inputs[0].is_linked:
            wnt.links.new(tex.outputs["Color"], bg.inputs[0])
    bg.inputs[1].default_value = world_strength


def ensure_preview_scene(res, world_strength=1.0, sun_strength=2.0,
                         engine="BLENDER_EEVEE", env="UNIFORM"):
    scn = bpy.data.scenes.get(PREVIEW_SCENE)
    if scn is None:
        scn = bpy.data.scenes.new(PREVIEW_SCENE)
    # Render at the user's current frame (animated geometry, drivers, keyed
    # material values), not the preview scene's own frame 1.
    user_scene = bpy.context.scene
    if user_scene is not None and user_scene != scn:
        scn.frame_current = user_scene.frame_current
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
    r.image_settings.color_depth = "8"
    try:
        scn.view_settings.view_transform = "Standard"
        scn.display_settings.display_device = "sRGB"
    except Exception:
        pass

    if scn.world is None:
        scn.world = bpy.data.worlds.get("NPV_world") or bpy.data.worlds.new("NPV_world")
    wnt = scn.world.node_tree
    bg = next((n for n in wnt.nodes if n.bl_idname == "ShaderNodeBackground"), None)
    if bg is None:
        bg = wnt.nodes.new("ShaderNodeBackground")
        wout = next((n for n in wnt.nodes if n.bl_idname == "ShaderNodeOutputWorld"), None) \
            or wnt.nodes.new("ShaderNodeOutputWorld")
        wnt.links.new(bg.outputs[0], wout.inputs["Surface"])
    _setup_env(wnt, bg, env, world_strength)

    plane = _drop_if_no_uv(bpy.data.objects.get(PREVIEW_PLANE))
    if plane is None:
        me = _new_uv_mesh(PREVIEW_PLANE + "_mesh", lambda bm: bmesh.ops.create_grid(
            bm, x_segments=1, y_segments=1, size=1.0, calc_uvs=True))
        plane = bpy.data.objects.new(PREVIEW_PLANE, me)
    if plane.name not in scn.collection.objects:
        scn.collection.objects.link(plane)
    plane.location = (0, 0, 0)
    plane.rotation_euler = (0, 0, 0)
    plane.scale = (1.04, 1.04, 1.0)

    sphere = _drop_if_no_uv(bpy.data.objects.get(PREVIEW_SPHERE))
    if sphere is None:
        me = _new_uv_mesh(PREVIEW_SPHERE + "_mesh", lambda bm: bmesh.ops.create_uvsphere(
            bm, u_segments=48, v_segments=24, radius=0.92, calc_uvs=True))
        for poly in me.polygons:
            poly.use_smooth = True
        sphere = bpy.data.objects.new(PREVIEW_SPHERE, me)
    if sphere.name not in scn.collection.objects:
        scn.collection.objects.link(sphere)
    sphere.location = (0, 0, 0)

    cube = _drop_if_no_uv(bpy.data.objects.get(PREVIEW_CUBE))
    if cube is None:
        me = _new_uv_mesh(PREVIEW_CUBE + "_mesh", lambda bm: bmesh.ops.create_cube(
            bm, size=1.0, calc_uvs=True))
        cube = bpy.data.objects.new(PREVIEW_CUBE, me)
    if cube.name not in scn.collection.objects:
        scn.collection.objects.link(cube)
    cube.location = (0, 0, 0)
    # Tilted so three faces show (a cube seen face-on reads as a square).
    cube.rotation_euler = (0.7854, 0.6155, 0.0)   # isometric: 3 faces equal
    cube.scale = (0.98, 0.98, 0.98)
    cube.hide_render = True      # only render_shader's Cube shape shows it

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


def _linear_to_srgb(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92,
                    1.055 * np.power(v, 1.0 / 2.4) - 0.055)


def _load_render(path):
    """Read a preview render back: (width, height, float32 RGBA pixels ready
    to display, number or None). ``foreach_get`` into a numpy array is far
    faster than ``img.pixels[:]`` (a Python list of w*h*4 floats).

    A '.exr' render is a Value swatch encoded by _value_emission(): scene-
    linear R = max(v, 0), G = max(-v, 0). It is shown as the grey the PNG path
    would give (Standard view = sRGB, clipped) and, when every pixel holds the
    same number, that number is returned for drawing on the thumbnail."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            return None
        px = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
    finally:
        bpy.data.images.remove(img)
    value = None
    if path.lower().endswith(".exr"):
        px = px.reshape(-1, 4)
        a = px[:, 3]
        solid = a > 0.5
        r = np.where(a > 1e-6, px[:, 0] / np.maximum(a, 1e-6), 0.0)
        g = np.where(a > 1e-6, px[:, 1] / np.maximum(a, 1e-6), 0.0)
        if solid.any():
            v = (r - g)[solid]
            lo, hi = float(v.min()), float(v.max())
            if hi - lo <= 1e-3 * max(1.0, abs(hi), abs(lo)):
                value = float(v.mean())
        grey = _linear_to_srgb(r).astype(np.float32)
        px = np.stack([grey, grey, grey, a], axis=1).reshape(-1)
    return w, h, np.ascontiguousarray(px, dtype=np.float32), value


def _png_to_texture(path):
    got = _load_render(path)
    if got is None:
        return None
    w, h, px, value = got
    _state["last_value"] = value
    buf = Buffer("FLOAT", w * h * 4, px)
    return GPUTexture((w, h), format="RGBA16F", data=buf)


def _finish(path):
    """Hand a finished render on: to the thumbnail cache, or -- while the
    Export operator runs -- copied to the chosen file."""
    dst = _state.get("export_to")
    if dst:
        shutil.copyfile(path, dst)
        return True
    return _png_to_texture(path)


def _render_scene(scn):
    ext = ".exr" if scn.render.image_settings.file_format == "OPEN_EXR" else ".png"
    path = os.path.join(bpy.app.tempdir, "npv_render" + ext)
    # The previous preview's file must not pass for this one if the render
    # writes nothing.
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    scn.render.filepath = path
    # Override only the scene: adding a window makes render report FINISHED
    # without writing the file.
    with bpy.context.temp_override(scene=scn):
        bpy.ops.render.render(write_still=False)
    # Save the result ourselves: write_still=True logs a "Saved: '...'" line
    # to the console for every thumbnail, burying real errors. save_render
    # with the scene applies its colour management and file format, so the
    # file is identical to what write_still wrote.
    img = next((i for i in bpy.data.images if i.type == "RENDER_RESULT"), None) \
        or bpy.data.images.get("Render Result")
    if img is None:
        raise RuntimeError("render produced no Render Result")
    img.save_render(path, scene=scn)
    if not os.path.isfile(path):
        raise RuntimeError("render finished without writing %s" % path)
    return path
