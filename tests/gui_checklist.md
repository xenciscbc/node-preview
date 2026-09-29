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
| 4 | Compositor | Temp scene `NPV_check_scene` set as the window's scene, with its own compositor tree: RGB (red) -> Blur -> Group Output, plus a Mix node; enable Compositor in the Preview panel and press Refresh | RGB and Blur show red swatches; changing the RGB colour re-renders them without pressing Refresh |
| 5 | Compositor | Same as #4 | After the refresh, the temp scene's compositor tree and render resolution are unchanged, and the "Viewer Node" / "Render Result" images were not resized to thumbnail size. Afterwards restore the original window scene and delete the temp scene and its tree |
| 6 | Geometry Nodes | Temp object with two GN modifiers (A: Mesh Cube, B: Mesh Grid, each node named `Shape`); view B's tree, then A's | B's `Shape` shows the grid, A's `Shape` shows the cube (not the grid that B would turn it into) |
| 7 | Any | Let some previews render, then save the file (to a temp path via `save_as_mainfile(copy=True)`) | The scene dropdown and the saved file contain no `NPV_preview_scene`; previews keep working after the save |
| 8 | Shader | Material: Image Texture (generated grid) and a node group (Noise inside) feeding Mix -> Emission; then paint a stroke on the image in the Image Editor (texture paint) and change the Noise Scale inside the group | Image thumbnail shows the painted stroke; Mix / Emission / Output thumbnails show the new noise scale -- without pressing Refresh |
| 9 | Shader | Material: RGB -> node group (Group Input tint x Noise -> Color Ramp, via a Multiply Mix) -> Emission; then Tab into the group | Top level: the group node itself has a thumbnail. Inside: Noise, Color Ramp and Multiply have thumbnails, and Multiply shows the outer RGB tint (the value actually passed in) |
| 10 | Preferences / panel | Edit > Preferences > Add-ons > Node Preview Thumbnails; lower Max Cached Thumbnails below the current count | The panel's "Cached: n / max" line shows the count dropping to the new limit |
| 11 | Any | Open the help popup (? button in the Preview panel), in EN and in 中文; open each page (also with `bpy.ops.node.npv_help('INVOKE_DEFAULT', page=...)`) | The popup fits the window with no line cut off; the page tabs switch the text; the Groups page describes Inside Node Groups and the Cache page the Max Cached Thumbnails preference |
| 12 | Compositor | Temp scene as in #4, compositor tree: RGB (red) -> group `NPV_check_cgrp` (Group Input -> Invert -> Group Output) -> Group Output; enable Compositor, Tab into the group | Inside the group, Invert shows the inverted outer red (cyan); unticking Inside Node Groups clears the thumbnails inside the group and ticking it brings them back; the top-level group node keeps its thumbnail throughout |
| 13 | Preferences | As in #10, with the panel language set to 中文 and then EN | The "At the limit" line under Max Cached Thumbnails follows the language |
| 14 | Shader | Material with Noise, Math (Add 2 + 3, fixed inputs) and Principled; Show Values on | Math thumbnail reads `5`; Noise shows no number; while a node waits to re-render it has an orange outline, and a never-rendered one an orange `…` cell |
| 15 | Shader | Same material: Display box -> Thumbnail Size 1.5, each Position, Checkerboard on, Enlarge Active Node (and `Ctrl+Alt+Z`) | Thumbnails resize / move around the node without overlapping it; checkerboard shows around the ball; the active node's thumbnail is drawn larger and on top |
| 16 | Shader | Shader Shape = Cube, then Environment = Forest | BSDF shows a lit cube, then a ball lit by the forest HDRI; after saving, no `NPV_env_*` image is in the file |
| 17 | Shader (light) | Temp point light with nodes (Cycles): RGB (red) -> Emission -> Light Output; open its node tree | RGB shows a red swatch, Light Output a glowing ball |
| 18 | Geometry Nodes | Two temp objects sharing one GN tree (e.g. Transform with different base meshes); make each active in turn | The previews follow the active object's mesh |
| 19 | Any | Right-click a node > Export Node Preview..., 512 px | A 512x512 PNG is written and opens as an image data-block |
| 20 | Any | Play the animation with Update on Frame Change off, then on (tree with a Scene Time node) | Off: playback is smooth and previews wait until it stops. On: previews follow the frame |
| 21 | Compositor | Temp scene whose Output format is OpenEXR; enable Compositor previews | Thumbnails look normal (not grey Value swatches); the scene's output format is unchanged |
| 22 | Any | Make a node fail (e.g. compositor Render Layers in a scene without a camera) | Red outline / `!`, panel shows "n preview(s) failed"; editing another node does not re-run it; Refresh does |
| 23 | Geometry Nodes | Two temp objects sharing one GN tree; open two node editors on it, pin one (pin icon) to the non-active object | Previews settle: "Rendering... n left" disappears and stays gone; no endless re-rendering while the mouse moves between the editors |
| 24 | Geometry Nodes | Same setup, Quality High: press Refresh and switch the active object before the queue finishes | Once the queue empties, every thumbnail matches the object that is active now (none left showing the other one) |
| 25 | Any | Make a node fail as in #22, then Ctrl+Z the edit that broke it | The red outline / `!` disappears and the panel's failed count drops, without pressing Refresh |
| 26 | Shader | Large material (30+ nodes), Cycles, Quality High; select one node and press Refresh; then try Time Budget 50 vs 1000 | The selected node and the on-screen ones fill in first; at 50 ms the editor stays responsive while rendering (pan / zoom don't stall for seconds) |
| 27 | Shader | Material editor open; drag an object around in the 3D viewport | The panel never shows "Rendering..."; no thumbnail flickers or re-renders |
| 28 | Any | Try Ctrl+Alt+P / Ctrl+Alt+R / Ctrl+Alt+Z in the node editor; check Preferences > Keymap > Node Editor for conflicts | Each shortcut does its job, and none of them takes over a binding you rely on |
| 29 | Geometry Nodes | Temp object with a GN modifier whose tree has a Float group input (e.g. Transform scale driven by it); change the value on the modifier panel; then Tab into Edit Mode on the base mesh and move a vertex | The modifier value change re-renders without Refresh; the vertex move shows once you leave Edit Mode (previews render the mesh data, which Edit Mode only writes on exit). With nothing being edited, "Rendering..." goes away and stays gone (no self-triggered loop). Then edit one GN node: only that node and the ones downstream get the orange outline |
| 30 | Geometry Nodes | Temp GN object moved far from the origin (e.g. location 30, -12, 5) and rotated | Geometry thumbnails show the object centred, not empty or cut off |
| 31 | Shader + Geometry Nodes | Two node editors side by side: a material in one, a GN tree in the other; edit a node in each in turn (Auto Update on) | Both editors' changed thumbnails re-render; neither stays stale until you hover or click it. Moving the mouse between the two editors with nothing edited starts no renders |
| 32 | Shader | Material with two Material Outputs (target EEVEE and target Cycles, each wired differently); view with EEVEE, then Cycles | Each node's thumbnail shows that node, not the whole material; the Output thumbnails show that output's own result |
| 33 | Compositor | Temp scene with a VSE strip (Use Sequencer on), Render Region on and Metadata > Burn Into Image on | Compositor thumbnails show the composite, uncropped, with no burned-in text; the scene's own settings are unchanged |
| 34 | Shader | RGB Curves / Float Curve node: drag a curve point | The node's thumbnail (and downstream ones) re-render without pressing Refresh |
