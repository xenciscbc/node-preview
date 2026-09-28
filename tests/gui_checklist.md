# GUI checklist (run via Blender MCP before each bump)

Covers what the headless suite (`python run_tests.py`) cannot: thumbnails as
actually drawn in the node editor. Rules:

- The MCP talks to the user's live Blender session. Never modify the user's own
  objects, materials, scenes or compositor trees. Create temporary
  `NPV_check_*` datablocks, make them active, and delete them afterwards;
  restore the previously active object and the window's scene.
- Test the code about to ship: ideally the user installs `dist/<id>-<ver>.zip`
  and restarts Blender. If they can't, load `extension/__init__.py` with
  `importlib` and swap the changed functions into the live
  `bl_ext.*.node_preview` module (in-memory only; gone on restart), and say
  which checks that swap does not cover.
- Previews render on a timer: after setting something up, wait a moment (or
  call `bpy.ops.node.npv_refresh()` from the editor's context) before taking
  the screenshot.
- Screenshot the editor area with `get_screenshot_of_area_as_image`.

## Checks

| # | Editor | Setup | Expect |
|---|---|---|---|
| 1 | Shader | Material: Image Texture (generated COLOR_GRID) -> Principled Base Color, on a temp object | Image Texture thumbnail shows the grid, not a flat colour; BSDF ball shows the texture wrapped on the sphere |
| 2 | Geometry Nodes | Temp object `NPV_check_gn` with a GN modifier: Mesh Cube -> Set Position (Offset <- Noise Texture Color) -> Group Output | Cube and Set Position show a grey 3D render of their geometry (Set Position visibly deformed); Noise Texture shows a noise swatch, not a flat colour |
| 3 | Geometry Nodes | Same as #2: change the Cube's Size a few times, letting auto-update refresh each time | Cube and Set Position thumbnails update; Noise Texture does not re-render. No `NPV_check_gn_mesh.001` (or any new orphan mesh) appears in `bpy.data.meshes` |
| 4 | Compositor | Temp scene `NPV_check_scene` set as the window's scene, with its own compositor tree: RGB (red) -> Blur -> Group Output, plus a Mix node; press Refresh in the Preview panel (compositor is manual only) | RGB and Blur show red swatches; nodes update only after Refresh |
| 5 | Compositor | Same as #4 | After the refresh, the temp scene's compositor tree and render resolution are unchanged, and the "Viewer Node" / "Render Result" images were not resized to thumbnail size. Afterwards restore the original window scene and delete the temp scene and its tree |
| 6 | Geometry Nodes | Temp object with two GN modifiers (A: Mesh Cube, B: Mesh Grid, each node named `Shape`); view B's tree, then A's | B's `Shape` shows the grid, A's `Shape` shows the cube (not the grid that B would turn it into) |
| 7 | Any | Let some previews render, then save the file (to a temp path via `save_as_mainfile(copy=True)`) | The scene dropdown and the saved file contain no `NPV_preview_scene`; previews keep working after the save |
