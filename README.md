# Node Preview Thumbnails

A Blender add-on that draws a small, live-rendered **thumbnail above every node**
in the Shader, World, Geometry Nodes and Compositor editors — so you can see what
each node produces while you build and tweak.

Tested on **Blender 5.2.2** on **Windows 11** (EEVEE + Cycles, Vulkan).

## Features

- **Shader Editor** — texture / color nodes show a flat, lighting-independent
  swatch; shader-output nodes (BSDF / Output) show a lit **material ball**
  (sphere) or a flat lit plane, with adjustable World Light / Key Light.
- **World** — environment swatches; a **volume** node (fog) is shown on a lit
  sphere instead (a global world volume renders black as a plain 360°).
- **Geometry Nodes** — a small **3D render of the geometry** at each node
  (field-only sockets are skipped).
- **Compositor** — each node's **image result** (renders the scene through the
  compositor per node). Previews render on a temporary copy of the scene, so
  your Viewer node and Render Result keep their full resolution.
- **Engine** follows the scene's Render Engine (EEVEE / Cycles).
- **Auto update** (only re-renders nodes whose inputs changed) + manual Refresh.
- **Preview Scope** (All / Selected / Marked) to control which nodes preview —
  Selected follows your node selection; Marked uses per-node toggles (right-click
  menu or Mark / Unmark buttons).
- **Per-socket preview** for multi-output nodes (e.g. Texture Coordinate) — pick
  which output to preview, or show all linked outputs side by side in a 2-column
  grid.
- **Help popup** and an **Auto / English / 中文** UI toggle — Auto follows
  Blender's own language setting (non-Chinese falls back to English).

## Install

### As a Blender Extension (Blender 5.2+)
`Edit > Preferences > Get Extensions > ▼ > Install from Disk…` and pick
`dist/node_preview-1.2.0.zip`.

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
  node_preview-1.2.0.zip   Packaged extension (manifest + __init__.py)
build_extension.py                Rebuilds the extension zip from the source .py
                                  (gated on the tests below)
run_tests.py                      Runs tests/ in headless Blender
tests/                            Headless tests + GUI checklist
```

## Build the extension from source

The `.py` is the single source of truth. `extension/__init__.py` is just that
file with the `bl_info` block stripped (extensions use the manifest instead).
Run:

```
python build_extension.py
```

to regenerate `extension/__init__.py` and `dist/node_preview-<ver>.zip`.

The build is gated; nothing is written unless all of these pass:
1. `bl_info` version equals the manifest version and is newer than every other
   zip in `dist/`.
2. The headless test suite passes against the stripped extension code.
3. `blender --command extension validate` accepts the zip.

`--no-test` skips step 2 (prints a warning); don't use it for a release.

## Tests

Headless suite, run in Blender 5.2 (`BLENDER` env var overrides the binary):

```
python run_tests.py                          # against node_preview_thumbnails.py
python run_tests.py extension/__init__.py    # against the built extension
```

Checks that need the real node-editor UI are listed in
`tests/gui_checklist.md`.

## Publishing to extensions.blender.org

Before submitting, edit `extension/blender_manifest.toml`:
- Replace the placeholder `website` with a real URL (or remove the line).
- `id` must be unique on the platform.

Validate locally with:

```
blender --command extension validate dist/node_preview-1.2.0.zip
```

## License

GPL-3.0-or-later.
