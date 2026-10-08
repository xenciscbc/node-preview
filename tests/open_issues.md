# Open issues (found in code review)

Found by reading the code at commit `0f04085` (after the module split), then
**reproduced and fixed headless** in a cloud session. That session ran the
suite with the `bpy` 5.2.2 module from PyPI and Mesa software rendering, not
with a Blender GUI.

- Each issue has a regression test in `tests/test_v142_issues.py`. NPV-12's
  is in `tests/test_build_version.py`.
- Every one of these tests fails on `0f04085` and passes on `9a6c8dc`.
- The full suite passes: 171 tests.

**GUI check (local session, Blender 5.2.2 via MCP, 2026-10-08):** the branch
code was swapped into the live session in memory and each issue's GUI steps
were run; results are on each Status line. The full suite also passes in
real Blender 5.2.2 (172 tests with the new NPV-05 one).
- NPV-01 .. NPV-04 and NPV-06 .. NPV-11: GUI verified (NPV-09 and NPV-12:
  headless only, as agreed).
- NPV-05: its fix broke Refresh (only the first batch rendered when a plain
  rebuild ran meanwhile). Fixed and re-verified; see its Status.

If a GUI check fails, reopen the issue, add a headless test that catches the
failure if one is possible, and fix it.

## How the issues were worked (and how to work a new one)

1. **Confirm the bug first.** Paste the issue's repro test into a test file
   (e.g. `tests/test_v142_issues.py`, which `run_tests.py` picks up) and run
   `python run_tests.py extension <test name>` on the unfixed code. It should
   **FAIL** with the assertion message given.
   - If it passes, either the repro is wrong (fix the test, not the add-on) or
     the bug isn't real. Then mark the issue *Not reproduced* below, with what
     you saw.
   - If it errors (wrong API name, missing setup), fix the test until it fails
     on the assertion for the reason described.
2. **Fix it.** Then run the same test and see it **PASS**. Keep it as a
   regression test, and run the whole suite: `python run_tests.py`.
3. **Run the GUI steps** where an issue has them. Use the rules at the top of
   `tests/gui_checklist.md`: temporary `NPV_check_*` data only, and restore the
   user's state. Add a row to the checklist when the check is worth repeating.
4. **Update the issue's Status line** with the fix commit, or *Not reproduced*
   / *Won't fix* plus the reason.

Shared helpers for the repro tests below. The tests as committed, with more
cases, are in `tests/test_v142_issues.py`:

```python
import types
import bpy


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    st = mod._state
    for k in ("textures", "hashes", "failed", "queue", "queued_keys", "values"):
        st[k].clear()


def _pretend_rendered(mod):
    """Every queued item rendered with its hash; the queue is empty."""
    st = mod._state
    for it in st["queue"]:
        st["textures"][it["key"]] = object()
        st["hashes"][it["key"]] = it["hash"]
    st["queue"].clear()
    st["queued_keys"].clear()


def _queued_nodes(mod):
    return {it["node"] for it in mod._state["queue"]}
```

## Summary

| ID | Severity | Area | Symptom (all fixed in `9a6c8dc`, NPV-05 again after its GUI check; GUI verified) |
|---|---|---|---|
| NPV-01 | Medium | Shader | Shape / World Light / Key Light / HDRI changes don't re-render a node whose *previewed* output is a shader but whose *first* output is not |
| NPV-02 | Low | World | Key Light changes don't re-render world volume previews |
| NPV-03 | Medium | Shader | Texture painting while Auto Update or Show Previews is off is never picked up |
| NPV-04 | Medium | All | Switching an editor to another tree keeps rendering the old tree's queued previews |
| NPV-05 | Low | All | Undoing an edit before its renders finish renders those nodes twice and leaves wrong "waiting" markers |
| NPV-06 | Medium | All | Muting / unmuting a link doesn't re-render |
| NPV-07 | Medium | Geometry Nodes | Edits behind an Image / Object / Collection *socket* don't re-render |
| NPV-08 | Medium | Geometry Nodes | Geometry previews depend on scale, quaternion rotation and parent, but changing them doesn't re-render (decided: neutralise them) |
| NPV-09 | Low | Groups | Group interface changes (Default Input, min / max) don't re-render |
| NPV-10 | Low | Geometry Nodes | Previews use render visibility of modifiers, not viewport visibility |
| NPV-11 | Medium | Compositor | Two scenes sharing a compositor tree: previews always show the first scene, and can overwrite the other scene's Render Result |
| NPV-12 | Low | Build | Rebuilding a tagged release ignores uncommitted changes |

The same root cause sits behind several of these. The hash that decides
whether to re-render (`queue.py` `_rebuild_queue`, `hashing.py`) is missing
something the renderer actually uses. Fixing that by adding the missing input
to the hash is usually enough. Don't broaden the hash with volatile values:
memory addresses, transforms of objects that aren't previewed.

---

## NPV-01 — The lighting signature is chosen by the wrong socket

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv01_light_sig_follows_previewed_socket`, `test_npv01_show_all_outputs_relights_only_the_shader_output` fails on `0f04085` and passes now; **GUI verified** (Blender 5.2.2, MCP): group with outputs Color, BSDF, only BSDF linked; Shader Shape -> Cube, Key Light 2 -> 6, Environment -> forest each re-rendered the group node and the Output.
- **Where:** `queue.py` `_rebuild_queue` (the `renders_as_shader(node)` test
  before `extra = esig + "|" + lsig`) and `eligibility.py`
  `renders_as_shader`, compared with `renderers.py` `render_shader`
  (`is_shader = ... osock.type == "SHADER"`, `shape = props.shader_shape if
  is_shader else "PLANE"`).
- **What happens:**
  - The hash adds the lighting signature (`_light_sig`: shape, World Light,
    Key Light, Environment) only when the node's **first enabled output** is
    a shader.
  - The renderer picks the lit sphere / cube from the **previewed output**.
  - So a node whose shader output isn't its first one, previewed through that
    shader output, renders lit, but its hash ignores the lighting.
  - The same applies to **Show All Linked Outputs** and to a manually chosen
    Preview Socket.
- **GUI steps:**
  1. Make a shader node group with outputs `Color` (first) and `BSDF`
     (shader).
  2. Use it in a temporary material, with only `BSDF` linked to the Material
     Output.
  3. Let it render. The group node shows a lit ball.
  4. Change Shader Shape to Cube, then Key Light, then Environment.
- **Expect:** the group node's thumbnail re-renders each time. Now it stays a
  stale sphere.

```python
def test_npv01_light_sig_follows_previewed_socket(mod):
    grp = bpy.data.node_groups.new("NPV_t_npv01_grp", "ShaderNodeTree")
    grp.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    grp.interface.new_socket("BSDF", in_out="OUTPUT", socket_type="NodeSocketShader")
    gout = grp.nodes.new("NodeGroupOutput")
    rgb = grp.nodes.new("ShaderNodeRGB")
    bsdf = grp.nodes.new("ShaderNodeBsdfDiffuse")
    grp.links.new(rgb.outputs[0], gout.inputs["Color"])
    grp.links.new(bsdf.outputs[0], gout.inputs["BSDF"])
    mat = bpy.data.materials.new("NPV_t_npv01")
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    g = nt.nodes.new("ShaderNodeGroup")
    g.name = "G"
    g.node_tree = grp
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(g.outputs["BSDF"], out.inputs["Surface"])
    props = _props()
    old_shape = props.shader_shape
    try:
        _clear(mod)
        mod.queue.rebuild_queue(nt, mod.common.KIND_SHADER, props)
        assert "G" in _queued_nodes(mod)
        _pretend_rendered(mod)
        props.shader_shape = "CUBE" if old_shape != "CUBE" else "SPHERE"
        mod.queue.rebuild_queue(nt, mod.common.KIND_SHADER, props)
        assert "G" in _queued_nodes(mod), \
            "shape change did not re-queue a node previewed through its shader output"
    finally:
        props.shader_shape = old_shape
        _clear(mod)
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(grp)
```

- **Acceptance:**
  - The test passes.
  - With Show All Linked Outputs on, a shape change re-queues the shader
    output's key but not the colour output's (that one renders flat).

## NPV-02 — Key Light doesn't re-render world volume previews

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv02_world_volume_follows_key_light` fails on `0f04085` and passes now; **GUI verified**: Key Light re-renders Volume Scatter only; Background and World Output are not re-queued. Note: the re-rendered volume preview looks the same: with EEVEE it is pixel-identical at Key Light 0 and 20 (max diff 0), with Cycles almost (max diff 0.043). The sun barely reaches the fog sphere, so this costs one render and shows no change.
- **Where:** `queue.py` `_rebuild_queue` adds `lsig` only for `KIND_SHADER`.
  `renderers.py` `render_world` passes `props.sun_strength` to
  `ensure_preview_scene`, and in the volume branch that sun lights the sphere
  inside the fog.
- **GUI steps:**
  1. Make a temporary world with Volume Scatter → World Output `Volume`.
  2. Open it in the World shader editor.
  3. Move the Key Light slider.
- **Expect:** the Volume Scatter thumbnail re-renders. Now it doesn't.

```python
def test_npv02_world_volume_follows_key_light(mod):
    world = bpy.data.worlds.new("NPV_t_npv02")
    world.use_nodes = True
    nt = world.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    vol = nt.nodes.new("ShaderNodeVolumeScatter")
    vol.name = "Vol"
    wout = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(vol.outputs[0], wout.inputs["Volume"])
    props = _props()
    old = props.sun_strength
    try:
        _clear(mod)
        mod.queue.rebuild_queue(nt, mod.common.KIND_WORLD, props)
        assert "Vol" in _queued_nodes(mod)
        _pretend_rendered(mod)
        props.sun_strength = old + 3.0
        mod.queue.rebuild_queue(nt, mod.common.KIND_WORLD, props)
        assert "Vol" in _queued_nodes(mod), "Key Light change did not re-queue the volume"
    finally:
        props.sun_strength = old
        _clear(mod)
        bpy.data.worlds.remove(world)
```

- **Acceptance:**
  - The test passes.
  - A non-volume world node (e.g. Background) is **not** re-queued by a Key
    Light change, since it doesn't use the sun.

## NPV-03 — Texture paint while Auto Update / Show Previews is off is lost

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv03_image_updates_counted_while_auto_update_off` fails on `0f04085` and passes now; **GUI verified**: with Auto Update off, then with Show Previews off, real `paint.image_paint` strokes only bumped the counter (no re-hash, no render); turning it back on re-rendered Image Texture, Emission and Output with the new strokes, without Refresh.
- **Where:** `timer.py` `_on_depsgraph`. The `img_gen` counter (part of
  `hashing.py` `_image_sig`) is bumped only after the early `return` for
  `not props.enabled or not props.auto_update`.
  - The object-data loop above it is deliberately run before that return.
    Its comment says "an edit made meanwhile must not be missed once
    previews come back".
  - Turning Auto Update / Show Previews back on doesn't reset the cache
    (`props.py` `_toggle_auto_update`, `_toggle_enabled`).
  - The image's other signature fields don't change either: name, source,
    `is_dirty` (already True after the first stroke), and mtime (unsaved).
- **GUI steps:**
  1. Make a material with an Image Texture of a generated image, and paint
     one stroke so the image is dirty.
  2. Let the previews render.
  3. Turn Auto Update off (or press Ctrl+Alt+P), and paint more strokes.
  4. Turn it back on.
- **Expect:** the Image Texture and downstream thumbnails show the new
  strokes without pressing Refresh. Now they stay stale.

```python
def test_npv03_image_updates_counted_while_auto_update_off(mod):
    img = bpy.data.images.new("NPV_t_npv03", 8, 8)
    props = _props()
    upd = types.SimpleNamespace(id=img, is_updated_geometry=False,
                                is_updated_transform=False, is_updated_shading=True)
    dg = types.SimpleNamespace(updates=[upd])
    st = mod._state
    try:
        props.auto_update = False
        before = st["img_gen"].get(img.name, 0)
        mod.timer._on_depsgraph(bpy.context.scene, dg)
        assert st["img_gen"].get(img.name, 0) == before + 1, \
            "image update while Auto Update is off was not counted"
    finally:
        props.auto_update = True
        st["img_gen"].pop(img.name, None)
        bpy.data.images.remove(img)
```

- **Acceptance:**
  - The test passes, and the same holds with `props.enabled = False`.
  - While previews are off, the change still only bumps the counter. Nothing
    is set dirty, rebuilt or rendered.

## NPV-04 — Switching an editor to another tree keeps rendering the old one

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv04_switching_tree_drops_old_pending_renders` fails on `0f04085` and passes now; **GUI verified**: compositor scenes with 15 / 4 nodes; an edit queued all 15, and switching the window scene with 7 still queued dropped them; only the new tree's nodes rendered after the switch. Switching back showed the 8 cached thumbnails at once and re-rendered only the 7 stale ones.
- **Where:** `drawing.py` `_record_editor` overwrites the editor's entry when
  it now shows another tree, and never calls `_editors_gone` for the old one.
  `queue.py` `_rebuild_queue` only cleans up keys with the *current* tree's
  prefix. So queued items of the tree the editor showed before stay queued
  and are rendered, though no editor shows them.
  `_editors_gone`'s docstring says pending renders of an editor nobody shows
  should be dropped.
- **GUI steps:**
  1. Compositor previews on, Quality High, a temporary scene with a
     compositor tree of about 15 nodes.
  2. Press Refresh, and before the queue empties switch the window to another
     scene with its own compositor tree (or, in the Shader editor, switch to
     another material).
- **Expect:**
  - "Rendering... n left" counts only the new tree's nodes.
  - The old tree's renders stop. Now they keep running, a full scene render
    each, and the UI stalls.

```python
def test_npv04_switching_tree_drops_old_pending_renders(mod):
    def _mat(name):
        m = bpy.data.materials.new(name)
        nt = m.node_tree
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.name = "Noise"
        return m

    a, b = _mat("NPV_t_npv04_a"), _mat("NPV_t_npv04_b")
    space = type("Space", (), {"pin": False, "id": a, "id_from": None,
                               "as_pointer": lambda self: 4242})()
    ctx = type("Ctx", (), {"active_object": None})()
    props = _props()
    st = mod._state
    K = mod.common.KIND_SHADER
    try:
        _clear(mod)
        st["editors"].clear()
        ta, tb = a.node_tree, b.node_tree
        mod.drawing._record_editor(ctx, space, ta.as_pointer(), K, [ta.as_pointer()], props, ta)
        mod.queue.rebuild_queue(ta, K, props)
        assert any(it["key"].startswith("%d:" % ta.as_pointer()) for it in st["queue"])
        # The same editor now shows material b.
        space.id = b
        mod.drawing._record_editor(ctx, space, tb.as_pointer(), K, [tb.as_pointer()], props, tb)
        mod.queue.rebuild_queue(tb, K, props)
        left = [it["key"] for it in st["queue"]
                if it["key"].startswith("%d:" % ta.as_pointer())]
        assert not left, "renders of the tree the editor left are still queued: %r" % left
    finally:
        st["editors"].clear()
        _clear(mod)
        bpy.data.materials.remove(a)
        bpy.data.materials.remove(b)
```

- **Acceptance:**
  - The test passes.
  - The old tree's **thumbnails** stay cached; only its pending renders go.
    Switching back shows them at once.
  - A second editor that still shows the old tree keeps its pending renders.
    Extend the test with a second fake space on `ta` to check this.

## NPV-05 — Undo before the queue finishes leaves items with the newer hash

- **Status:** Fixed in `9a6c8dc`; its fix broke Refresh, fixed again in the
  GUI-check commit. GUI verified (Blender 5.2.2, MCP).
  - The GUI check found the regression: Refresh queues nodes whose hash equals
    the shown one, and the plain rebuild a compositor render triggers (it sets
    `dirty`) dequeued them through the new `_dequeue` in `_enqueue`'s early
    returns. A 15-node compositor tree rendered 2 nodes on Refresh, then
    nothing.
  - Fix: queue items remember `force`; a pending forced item skips both early
    returns and is only kept current (hash, source), so it renders the state
    it is rendered in and stores that hash. Headless
    `test_npv05_refresh_survives_a_plain_rebuild` fails on `784c146` and
    passes now.
  - GUI after the fix: Refresh renders all 15 compositor nodes. Refresh, edit,
    immediate Ctrl+Z ends with every thumbnail at the undone state, the queue
    empty and no orange outlines. The original step (21-node material, edit,
    Ctrl+Z 0.4 s later): the 16 nodes still queued are dropped, the 5 already
    re-rendered render once more, and the hashes match the pre-edit ones.
- **Where:** `queue.py` `_enqueue`.
  - The early return (`hashes[key] == h and key in textures`) runs before the
    `queued_keys` branch.
  - When an edit has queued a node with hash B and an undo brings it back to
    the shown hash A, the item stays queued with B.
  - It renders state A, but stores hash B. The next rebuild sees A ≠ B and
    renders it again.
  - Meanwhile a correct thumbnail shows the orange "waiting" outline.
  - The `failed` early return just below has the same ordering.
- **GUI steps:**
  1. In a material of about 20 nodes with Quality High, change the first
     node's input, and Ctrl+Z at once.
- **Expect:** the downstream nodes don't re-render, or at most once, and no
  orange outline stays on thumbnails that already match. Now each renders
  twice.

```python
def test_npv05_undo_to_shown_hash_dequeues(mod):
    mat = bpy.data.materials.new("NPV_t_npv05")
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    props = _props()
    st = mod._state
    K = mod.common.KIND_SHADER
    try:
        _clear(mod)
        mod.queue.rebuild_queue(nt, K, props)
        _pretend_rendered(mod)
        scale = noise.inputs["Scale"]
        scale.default_value += 1.0          # edit -> queued with hash B
        mod.queue.rebuild_queue(nt, K, props)
        assert "Noise" in _queued_nodes(mod)
        scale.default_value -= 1.0          # undo -> back to the shown hash A
        mod.queue.rebuild_queue(nt, K, props)
        assert "Noise" not in _queued_nodes(mod), \
            "node still queued (with the undone hash) after returning to the shown state"
    finally:
        _clear(mod)
        bpy.data.materials.remove(mat)
```

- **Acceptance:**
  - The test passes, and `queue` and `queued_keys` stay in sync.
  - A failed key whose hash returns to the failed hash while queued is
    dequeued too.

## NPV-06 — Muting a link doesn't re-render

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv06_link_mute_changes_hash` fails on `0f04085` and passes now; **GUI verified**: `node.links_mute` (the Ctrl+Alt drag operator) on Noise -> Ramp re-rendered Ramp, BSDF and Output when muting, when changing Ramp's Fac while muted, and when unmuting.
- **Where:** `hashing.py` `upstream_hash` hashes a linked input as
  `(from_socket, upstream hash)` and `tree_signature` hashes each link's ends.
  Neither includes `NodeLink.is_muted`. A muted link still shows in
  `inp.links` / `is_linked`, but the input evaluates to its default value,
  which isn't hashed while the input is linked.
- **GUI steps:**
  1. Make a material Noise → Color Ramp → BSDF.
  2. Ctrl+Alt+drag across the Noise → Ramp link to mute it, then mute it
     again to unmute.
- **Expect:** Ramp, BSDF and Output re-render both times. Now they don't.

```python
def test_npv06_link_mute_changes_hash(mod):
    mat = bpy.data.materials.new("NPV_t_npv06")
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    link = nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    try:
        h0 = mod.hashing.upstream_hash(ramp, {})
        s0 = mod.hashing.tree_signature(nt)
        link.is_muted = True
        assert mod.hashing.upstream_hash(ramp, {}) != h0, "link mute not in upstream_hash"
        assert mod.hashing.tree_signature(nt) != s0, "link mute not in tree_signature"
    finally:
        bpy.data.materials.remove(mat)
```

- **Acceptance:**
  - The test passes.
  - A muted link's input is hashed by its default value, so changing that
    default while the link is muted re-renders too.

## NPV-07 — Data behind an ID socket isn't hashed (Image / Object / Collection)

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv07_gn_image_socket_follows_paint`, `test_npv07_object_info_follows_the_other_object` fails on `0f04085` and passes now; **GUI verified**: a real paint stroke on the image in the GN Image Texture socket re-rendered Set Position and Join; editing the Object Info object in Edit Mode rendered nothing until leaving Edit Mode, then ObjInfo and Join; moving it (Relative) re-rendered them. Note: in Original mode moving that object re-renders too (`_object_sig` always hashes `matrix_world`): extra renders, not stale ones; the acceptance's third bullet asked for Relative only.
- **Where:** `hashing.py` `_socket_default`.
  - For an ID-valued socket, `float(v)` fails, so the value becomes `str(v)`,
    i.e. `<bpy_struct, Image("x") at 0x…>`: a name plus a memory address.
  - `_image_sig` (paint counter, `is_dirty`, mtime) is only applied to node
    *properties* (the shader Image Texture node). Geometry Nodes take the
    image, object or collection through an input *socket*, so none of their
    content is hashed.
  - `queue.py` `_geo_source_sig` only covers the previewed object's own
    data.
  - The memory address also makes the hash change whenever the ID moves in
    memory, e.g. after undo, for no visible reason.
- **GUI steps:**
  1. A temporary GN object: Grid → Set Position, with Offset driven by an
     **Image Texture (GN)** node whose Image socket holds a generated image.
  2. Paint on that image in the Image Editor.
  3. Separately: an **Object Info** node pointing at a second temporary
     object. Edit that object's mesh and leave Edit Mode.
- **Expect:** the downstream geometry thumbnails re-render after the paint
  stroke, and after the other object's edit. Now they don't (only Refresh
  updates them).

```python
def test_npv07_gn_image_socket_follows_paint(mod):
    img = bpy.data.images.new("NPV_t_npv07", 8, 8)
    ng = bpy.data.node_groups.new("NPV_t_npv07_gn", "GeometryNodeTree")
    tex = ng.nodes.new("GeometryNodeImageTexture")
    tex.inputs["Image"].default_value = img
    st = mod._state
    try:
        h0 = mod.hashing.upstream_hash(tex, {})
        assert "0x" not in repr(mod.hashing._socket_default(tex.inputs["Image"])), \
            "ID socket hashed by memory address"
        st["img_gen"][img.name] = st["img_gen"].get(img.name, 0) + 1   # a paint stroke
        assert mod.hashing.upstream_hash(tex, {}) != h0, \
            "paint on an image in a GN Image socket did not change the hash"
    finally:
        st["img_gen"].pop(img.name, None)
        bpy.data.node_groups.remove(ng)
        bpy.data.images.remove(img)
```

- **Acceptance:**
  - The test passes, and no hash contains a memory address.
  - The Object Info GUI case re-renders after the other object's mesh edit.
    The object's data fingerprint (`_data_sig`, already used for the source
    object) is one way to do this.
  - Moving that other object only re-renders when the node uses its
    transform (Object Info in Relative mode).

## NPV-08 — Geometry previews depend on transforms the hash ignores

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv08_geo_preview_ignores_rotation_mode`, `test_npv08_geo_preview_ignores_scale_parent_constraints`, `test_npv08_tree_reading_self_transform_keeps_it` fails on `0f04085` and passes now; **GUI verified**: a quaternion-rotated cone previews upright (it previewed tilted before); scale (1, 1, 3), a rotated parent and a constraint change neither the hash nor the preview after Refresh. With Self Object -> Object Info in the tree, moving the object re-rendered Cone, Object Info and Set Position; moving / rotating / scaling a plain GN object re-rendered nothing.
- **Where:** `renderers.py` `render_geo` sets `obj2.location` and
  `obj2.rotation_euler` to zero. The copy keeps:
  - `scale`
  - `rotation_quaternion` / axis-angle (when `rotation_mode` isn't Euler)
  - delta transforms
  - the parent
  - constraints

  Transform-only depsgraph updates are skipped on purpose
  (`timer.py` `_on_depsgraph`), and no transform is part of the hash. So the
  preview's orientation and proportions follow those values, but don't update
  when they change. They jump on the next unrelated edit. An Euler-mode object
  previews unrotated while a quaternion-mode object (common after glTF import)
  previews rotated.
- **GUI steps:**
  1. A temporary GN object with an asymmetric shape (e.g. Mesh Cone).
     `rotation_mode = 'QUATERNION'`.
  2. Rotate it 90° about X. Separately, scale it (1, 1, 3), and parent it to a
     rotated empty.
- **Decision: (a) neutral transform, with one exception.**
  - The preview ignores scale, every rotation mode (Euler, quaternion,
    axis-angle), delta transforms, the parent and constraints, as it already
    ignores location and Euler rotation. Change only the copy `obj2`: clear
    its parent, disable its constraints, and leave its `matrix_world` at
    identity.
  - **Exception:** when the tree, or a group it uses, has a node that reads
    object transforms, the preview differs from the viewport without them.
    Examples: Self Object, or Object Info in Relative mode. Then keep the
    previewed object's transform, add it (`matrix_world`, rounded) to that
    tree's geometry hash, and re-render when it changes.
  - Why (a):
    - The code already intends it: location and Euler rotation are zeroed.
      Only the other transform parts were missed, so today an Euler-mode
      object previews upright and a quaternion-mode one (common after glTF
      import) previews rotated.
    - Transform-only updates are skipped on purpose (`timer.py`
      `_on_depsgraph`; README: "Moving objects no longer triggers
      re-hashing"). Hashing transforms would re-render every GN preview on
      every tick of a rotate or scale drag.
    - Node trees work in object space. The transform is applied after them,
      so "what this node makes" is the local-space result. `_frame_object`
      already hides uniform scale.
  - Accepted cost: non-uniform scale (e.g. a squashed object) doesn't show in
    the preview.

```python
def test_npv08_geo_preview_ignores_rotation_mode(mod):
    """A quaternion rotation must not show in the preview, as an
    Euler rotation doesn't. Compares the rendered pixels of the same object
    with and without the rotation."""
    from npv_testutil import capture_renders
    ng = bpy.data.node_groups.new("NPV_t_npv08_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    cone = ng.nodes.new("GeometryNodeMeshCone")
    cone.name = "Cone"
    ng.links.new(cone.outputs["Mesh"], go.inputs[0])
    me = bpy.data.meshes.new("NPV_t_npv08_mesh")
    ob = bpy.data.objects.new("NPV_t_npv08_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    try:
        with capture_renders(mod) as shots:
            assert mod.renderers.render_geo(ob, "Cone", 48, _props())
            ob.rotation_mode = "QUATERNION"
            ob.rotation_quaternion = (0.7071068, 0.7071068, 0.0, 0.0)  # 90 deg about X
            bpy.context.view_layer.update()
            assert mod.renderers.render_geo(ob, "Cone", 48, _props())
        assert shots[0] == shots[1], "quaternion rotation changed the geometry preview"
    finally:
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)
```

- **Acceptance:**
  - The test passes, and the same holds for non-uniform scale, delta
    transforms, axis-angle rotation, a rotated parent and a constraint that
    rotates the object.
  - Exception: a tree with Self Object, or with Object Info in Relative mode,
    re-queues its geometry nodes when the object is rotated or moved. A tree
    without them doesn't, and moving a plain GN object still re-renders
    nothing (README).
  - The user's object keeps its parent, constraints and transform: only
    `obj2` is changed.
  - `test_v140_framing` still passes. It expects a scaled or parented object
    to stay framed. If the change alters what it checks, update its
    docstring to match.

## NPV-09 — Group interface settings aren't hashed

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv09_interface_settings_in_signature` fails on `0f04085` and passes now; Not GUI-checked (headless covers it, as agreed).
- **Where:** `hashing.py` `tree_signature` covers nodes, unlinked defaults,
  output defaults and links, but not `tree.interface`. Interface settings
  change the result without touching any node:
  - a GN input's **Default Input** (Value → Position / Normal / Index), used
    when the group node's input is unlinked
  - **min / max**, which clamp the value passed in

  The group node's own socket `default_value` doesn't change either.
- **GUI steps:**
  1. A GN group with a Vector input wired to Set Position Offset, used in a
     temporary GN object, with the group node's input unlinked.
  2. In the group's interface panel, set the input's Default Input to
     Position.
- **Expect:** the group node's thumbnail, and the previews inside the group,
  re-render. Now they don't.

```python
def test_npv09_interface_settings_in_signature(mod):
    ng = bpy.data.node_groups.new("NPV_t_npv09", "GeometryNodeTree")
    vec = ng.interface.new_socket("Offset", in_out="INPUT", socket_type="NodeSocketVector")
    flt = ng.interface.new_socket("Amount", in_out="INPUT", socket_type="NodeSocketFloat")
    try:
        s0 = mod.hashing.tree_signature(ng)
        vec.default_input = "POSITION"
        s1 = mod.hashing.tree_signature(ng)
        assert s1 != s0, "Default Input change not in tree_signature"
        flt.max_value = 0.5
        assert mod.hashing.tree_signature(ng) != s1, "interface max not in tree_signature"
    finally:
        bpy.data.node_groups.remove(ng)
```

- **Acceptance:**
  - The test passes.
  - Renaming an interface socket or changing its description or tooltip
    doesn't change the signature. Only settings that change evaluation
    count.

## NPV-10 — GN previews use the modifiers' render visibility

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv10_geo_preview_uses_viewport_visibility` fails on `0f04085` and passes now; **GUI verified**: GN modifier with Render off previews the GN cone (before: the base-mesh cube); Subdivision with Levels Viewport 0 / Render 3 previews the cube (before: the level-3 sphere). The user's modifier settings are unchanged.
- **Where:** `renderers.py` `render_geo` renders through
  `preview_scene._render_scene` (`render.render`). Modifiers are evaluated
  with `show_render`, and Subdivision uses its render levels. Neither the
  previewed modifier `m2` nor the modifiers before it are forced visible,
  although the user is looking at the viewport result.
- **GUI steps:**
  1. A temporary GN object; turn off the GN modifier's **Render** (camera)
     toggle, keeping the viewport toggle on.
  2. Separately: put a Subdivision modifier before it, with Levels Viewport
     1 and Render 3.
- **Expect:** the thumbnails show what the viewport shows. Now the first case
  shows the bare base mesh (no GN effect at all), and the second uses the
  render subdivision level.

```python
def test_npv10_geo_preview_uses_viewport_visibility(mod):
    from npv_testutil import capture_renders, opaque_rgb
    ng = bpy.data.node_groups.new("NPV_t_npv10_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube")
    cube.name = "Cube"
    ng.links.new(cube.outputs["Mesh"], go.inputs[0])
    me = bpy.data.meshes.new("NPV_t_npv10_mesh")      # empty base mesh
    ob = bpy.data.objects.new("NPV_t_npv10_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    m = ob.modifiers.new("GN", "NODES")
    m.node_group = ng
    m.show_render = False
    try:
        with capture_renders(mod) as shots:
            assert mod.renderers.render_geo(ob, "Cube", 48, _props())
        px = shots[0]
        assert len(opaque_rgb(px)) / (len(px) / 4) > 0.05, \
            "preview is empty: the GN modifier was skipped because its Render toggle is off"
        assert m.show_render is False, "the user's modifier was changed"
    finally:
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)
```

- **Acceptance:**
  - The test passes.
  - The user's modifiers are untouched: only the copy `obj2`'s modifiers are
    changed.
  - Modifiers that are hidden in the viewport stay hidden in the preview.

## NPV-11 — Compositor tree shared by two scenes

- **Status:** Fixed in `9a6c8dc`. Headless `test_npv11_shared_comp_tree_prefers_the_window_scene` fails on `0f04085` and passes now; **GUI verified**: scenes A and B share one compositor tree (Render Layers scene = B -> Blur -> Group Output + Viewer); after F12 on B and with B the window scene, previews render through B, and the Render Result and "Viewer Node" stay 640 x 360 after the previews and after an Auto Update re-render.
- **Where:** `sources.py` `resolve_source` (KIND_COMP) returns the **first**
  scene in `bpy.data.scenes` whose `compositing_node_group` is the tree. The
  window's scene is never preferred: `drawing.py` `_editor_hint` records no
  scene.
  - In 5.x the compositor tree is an ID chosen in the editor header, and
    `Scene.copy()` shares it (see the comment in `render_compositor`), so
    sharing is common.
  - `renderers.py` `render_compositor` then copies the wrong scene. It only
    redirects nodes whose `scene == <that scene>` to the copy.
  - A Render Layers / Cryptomatte node reading the *other* scene is not
    redirected. The render pipeline then re-renders that scene at thumbnail
    size and overwrites its Render Result, which is the problem v1.4.1 fixed
    for the single-scene case.
- **GUI steps:**
  1. Temporary scenes `NPV_check_A` and `NPV_check_B` (different camera or
     objects) sharing one compositor tree. In the tree: a Render Layers node
     with scene = B → Blur → Group Output.
  2. Render B once (F12) so it has a full-size Render Result.
  3. Make B the window's scene, enable Compositor previews, and press
     Refresh.
- **Expect:**
  - The thumbnails show B's render.
  - B's Render Result and the "Viewer Node" image keep their full size.

  Now the thumbnails come from A, and B's Render Result is resized to
  thumbnail size. Restore the window's scene, and delete both scenes and the
  tree afterwards.

```python
def test_npv11_shared_comp_tree_prefers_the_window_scene(mod):
    """Headless stand-in: with a scene hint for B (however the fix records the
    editor's scene), resolve_source must return B, not the first scene."""
    tree = bpy.data.node_groups.new("NPV_t_npv11", "CompositorNodeTree")
    a = bpy.data.scenes.new("NPV_t_npv11_A")
    b = bpy.data.scenes.new("NPV_t_npv11_B")
    a.compositing_node_group = tree
    b.compositing_node_group = tree
    st = mod._state
    saved = st["src_hint"]
    try:
        st["src_hint"] = [("SCENE", b.name)]   # adapt to the fix's hint format
        assert mod.sources.resolve_source(tree, mod.common.KIND_COMP) == ("SCENE", b.name)
    finally:
        st["src_hint"] = saved
        bpy.data.scenes.remove(a)
        bpy.data.scenes.remove(b)
        bpy.data.node_groups.remove(tree)
```

- **Acceptance:**
  - The test passes, with the hint coming from the editor's window scene
    in `_editor_hint`.
  - The GUI check above passes.
  - In `render_compositor`, nodes that read *any* scene other than the copy
    are either redirected or documented as rendering that scene. At least the
    previewed scene's own nodes must never render the original.

## NPV-12 — Rebuilding a tagged release ignores uncommitted changes

- **Status:** Fixed in `9a6c8dc`. Headless `test_build_version::test_rebuilding_a_tag_needs_a_clean_tree` fails on `0f04085` and passes now; no GUI check needed.
- **Where:** `build_extension.py` `check_version` accepts an already-tagged
  version when the tag points at HEAD ("rebuilding that release"), but never
  checks the working tree. The zip is built from the files on disk.
- **Steps (no Blender needed for the gate itself):**
  1. `git checkout v1.4.1`.
  2. Edit any file under `extension/`.
  3. `python build_extension.py --no-test`.
- **Expect:** the build stops because `extension/` has uncommitted changes
  while rebuilding a tagged release. Now it writes
  `dist/node_preview-1.4.1.zip` with code that isn't the release's.
- **Acceptance:**
  - The dirty check applies **only** to the rebuild-a-tag case.
  - The normal flow is untouched (README "Releasing": bump, build, then
    commit, so it builds from a dirty tree on purpose).
  - The README's build-gate rule 1 mentions the new check.

---

## Not bugs, but worth fixing alongside

- *(Fixed in `9a6c8dc`.)* `README.md` "Releasing", step 3: "Once the tag
  exists, the next build refuses the same version." This was only true on
  another commit.
- `i18n.py`: the keys `help_tip` and `only_marked` are defined in both
  languages but never used.
- `tests/blender_runner.py`, `run_tests.py`, `tests/npv_testutil.py` and
  `tests/test_package_layout.py` still accept a single-file add-on, which no
  longer exists since the legacy file was dropped. This is dead code, not a
  bug.

## Observations from the GUI check (not reopened)

- **NPV-02:** the Key Light re-render of a world volume shows no visible
  change (EEVEE: identical pixels at Key Light 0 and 20). Harmless, but the
  render buys nothing; dropping the sun from the volume hash would be the
  cheaper choice.
- **NPV-07:** an Object Info node in Original mode re-renders when the other
  object moves, though its Geometry output doesn't depend on the move
  (`hashing._object_sig` always hashes `matrix_world`). Extra renders only.
- **Compositor, Render Layers:** Blender's own node preview (the eye toggle
  on Render Layers, on by default) is drawn under the add-on's thumbnail and
  shows through its transparent background as a second, smaller image. Not
  new in this batch; turning that node's preview off removes it.
- **Viewer Node size:** once during the session the "Viewer Node" image was
  found at 256 x 256 (the thumbnail size) after a long run of compositor
  checks (shared tree, failing Render Layers, Ctrl+Z, a module reload).
  None of those steps reproduced it when repeated one by one, and the Render
  Result stayed 640 x 360 whenever it was measured. Watch for it with
  checklist #5 / #53.

## Known limits of the fixes

- **NPV-07:** an object read through an Object / Collection socket is hashed
  by its own data and its transform. Its *own modifiers* are not hashed: the
  node reads the evaluated object. Changing a modifier on that other object
  re-renders only with Refresh.
- **NPV-07 / NPV-08:**
  - `_state["xform_watch"]` (the objects whose moves re-hash) only grows
    until the cache is reset, e.g. on file load.
  - An object that a tree no longer reads still triggers a re-hash when
    moved. Nothing re-renders, because the hash is unchanged.
- **NPV-11:** a compositor node reading a *third* scene (neither the
  previewed one nor its copy) still renders that scene at thumbnail size. The
  comment in `render_compositor` says so.

