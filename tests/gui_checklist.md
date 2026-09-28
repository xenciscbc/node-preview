# GUI checklist (run via Blender MCP before each bump)

Covers what the headless suite (`python run_tests.py`) cannot: thumbnails as
actually drawn in the node editor. Rules:

- Never modify the user's own objects or materials. Create temporary
  `NPV_check_*` datablocks, make them active, and delete them afterwards;
  restore the previously active object.
- To test unreleased code without touching the installed extension, load
  `extension/__init__.py` with `importlib` and swap only the changed functions
  into the live `bl_ext.*.node_preview` module (in-memory only; gone on restart).
- Screenshot the editor area with `get_screenshot_of_area_as_image`.

## Checks

| # | Setup | Expect |
|---|---|---|
| 1 | Material: Image Texture (generated COLOR_GRID) -> Principled Base Color, on a temp object; Shader Editor | Image Texture thumbnail shows the grid, not a flat colour; BSDF ball shows the texture wrapped on the sphere |
