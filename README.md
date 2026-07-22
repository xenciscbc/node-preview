# Node Preview Thumbnails

A Blender add-on that draws a small, live-rendered **thumbnail above every node**
in the Shader, World, Geometry Nodes and Compositor editors — so you can see what
each node produces while you build and tweak.

Tested on **Blender 5.2** (EEVEE + Cycles, Vulkan).

## Features

- **Shader Editor** — texture / color nodes show a flat, lighting-independent
  swatch; shader-output nodes (BSDF / Output) show a lit **material ball**
  (sphere) or a flat lit plane, with adjustable World Light / Key Light.
- **World** — environment swatches; a **volume** node (fog) is shown on a lit
  sphere instead (a global world volume renders black as a plain 360°).
- **Geometry Nodes** — a small **3D render of the geometry** at each node
  (field-only sockets are skipped).
- **Compositor** — each node's **image result** (renders the scene through the
  compositor per node).
- **Engine** follows the scene's Render Engine (EEVEE / Cycles).
- **Auto update** (only re-renders nodes whose inputs changed) + manual Refresh.
- **Only Marked Nodes** mode to save resources — turn previews on per node via
  the node right-click menu or Mark / Unmark buttons.
- **Help popup** and an **Auto / English / 中文** UI toggle — Auto follows
  Blender's own language setting (non-Chinese falls back to English).

## Install

### As a Blender Extension (Blender 5.2+)
`Edit > Preferences > Get Extensions > ▼ > Install from Disk…` and pick
`dist/node_preview-1.0.0.zip`.

### As a legacy add-on
`Edit > Preferences > Add-ons > ▼ > Install from Disk…` and pick
`node_preview_thumbnails.py`.

Then open the Shader / World / Geometry Nodes / Compositor editor, press **N**
for the sidebar, and use the **Preview** tab.

## Project layout

```
node_preview_thumbnails.py        Main source (legacy add-on, includes bl_info)
extension/
  blender_manifest.toml           Extension manifest (metadata for Extensions)
  __init__.py                     Extension entry — generated from the .py above
                                  with the bl_info block removed
dist/
  node_preview-1.0.0.zip   Packaged extension (manifest + __init__.py)
build_extension.py                Rebuilds the extension zip from the source .py
```

## Build the extension from source

The `.py` is the single source of truth. `extension/__init__.py` is just that
file with the `bl_info` block stripped (extensions use the manifest instead).
Run:

```
python build_extension.py
```

to regenerate `extension/__init__.py` and `dist/node_preview_thumbnails-<ver>.zip`.

## Publishing to extensions.blender.org

Before submitting, edit `extension/blender_manifest.toml`:
- Replace the placeholder `website` with a real URL (or remove the line).
- `id` must be unique on the platform.

Validate locally with:

```
blender --command extension validate dist/node_preview-1.0.0.zip
```

## License

GPL-2.0-or-later.
