"""Shared helpers for the headless tests (not a test module)."""
import contextlib
import os

import bpy


def load_pixels(path):
    """Flat RGBA float list of a PNG on disk."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        return img.pixels[:]
    finally:
        bpy.data.images.remove(img)


def opaque_rgb(px):
    return [px[i:i + 3] for i in range(0, len(px), 4) if px[i + 3] > 0.5]


def color_std(px):
    """Mean per-channel std-dev of the opaque pixels."""
    rgb = opaque_rgb(px)
    assert rgb, "render has no opaque pixels"
    n = len(rgb)
    devs = []
    for c in range(3):
        mean = sum(p[c] for p in rgb) / n
        devs.append((sum((p[c] - mean) ** 2 for p in rgb) / n) ** 0.5)
    return sum(devs) / 3


def mean_rgb(px):
    rgb = opaque_rgb(px)
    assert rgb, "render has no opaque pixels"
    n = len(rgb)
    return tuple(sum(p[c] for p in rgb) / n for c in range(3))


@contextlib.contextmanager
def capture_renders(mod):
    """Replace the add-on's PNG -> GPUTexture step (no GPU drawing in
    background mode) with one that records the rendered pixels. Yields the
    list of captured pixel arrays; renderers return a truthy marker."""
    shots = []
    orig = mod._png_to_texture

    def fake(path):
        shots.append(load_pixels(path))
        return len(shots)

    mod._png_to_texture = fake
    try:
        yield shots
    finally:
        mod._png_to_texture = orig


def datablock_names():
    """Snapshot of every ID name per collection, to detect leaks."""
    out = {}
    for attr in ("objects", "meshes", "materials", "node_groups", "scenes",
                 "worlds", "images", "cameras", "lights"):
        out[attr] = sorted(d.name for d in getattr(bpy.data, attr))
    return out


def addon_sources(mod):
    """(path, text) of every .py file of the add-on under test: all modules of
    the extension package, or the single file it was loaded from."""
    if not hasattr(mod, "__path__"):
        return [(mod.__file__, open(mod.__file__, encoding="utf-8").read())]
    pkg = mod.__path__[0]
    return [(os.path.join(pkg, fn),
             open(os.path.join(pkg, fn), encoding="utf-8").read())
            for fn in sorted(os.listdir(pkg)) if fn.endswith(".py")]
