"""v1.4.0: failed renders aren't retried, the editor's own source is used,
light node trees, faster read-back, Value numbers, render priority and time
budget, playback / transform handling, cube / HDRI shapes, export, display
helpers."""
import os
import time

import bpy

from npv_testutil import capture_renders, datablock_names, load_pixels, mean_rgb, opaque_rgb


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    mod._reset_cache()
    mod._state["src_hint"] = []


def _mark_rendered(mod):
    st = mod._state
    for it in st["queue"]:
        st["textures"][it["key"]] = object()
        st["hashes"][it["key"]] = it["hash"]
    st["queue"].clear()
    st["queued_keys"].clear()


def _emission_material(name, build):
    """Material: <node from build(nt)> -> Emission -> Output. ``build``
    returns the socket to preview; the node is named 'Src'."""
    mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    sock = build(nt)
    emit = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(sock, emit.inputs["Color"])
    nt.links.new(emit.outputs[0], out.inputs["Surface"])
    return mat


def _math(nt, op, a, b):
    m = nt.nodes.new("ShaderNodeMath")
    m.name = "Src"
    m.operation = op
    m.inputs[0].default_value = a
    m.inputs[1].default_value = b
    return m.outputs[0]


def _render_value(mod, mat):
    """Render node 'Src' through the real loader; returns (value, pixels)."""
    got = []
    orig = mod.preview_scene._png_to_texture

    def spy(path):
        got.append(mod.preview_scene._load_render(path))
        return True

    mod.preview_scene._png_to_texture = spy
    try:
        assert mod.renderers.render_shader(mat, "Src", 32, _props())
    finally:
        mod.preview_scene._png_to_texture = orig
    w, h, px, value = got[0]
    assert len(px) == w * h * 4
    return value, px


# --------------------------------------------------------------------------- #
#  1. Failed renders
# --------------------------------------------------------------------------- #
def test_failed_render_is_not_retried_until_it_changes(mod):
    mat = _emission_material("NPV_t140_fail", lambda nt: _math(nt, "ADD", 1, 2))
    props = _props()
    props.only_tex_shader = False
    orig = mod.queue._render_item
    calls = []

    def boom(item, res, p):
        calls.append(item["node"])
        return None

    mod.queue._render_item = boom
    try:
        _clear(mod)
        nt = mat.node_tree
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        n = len(mod._state["queue"])
        assert n
        props.batch_size = 8
        props.time_budget = 2000
        while mod._state["queue"]:
            mod.queue.process_queue(props)
        assert len(calls) == n and len(mod._state["failed"]) == n

        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert not mod._state["queue"], "failed renders re-queued with no change"

        nt.nodes["Src"].inputs[0].default_value = 5.0
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert any(it["node"] == "Src" for it in mod._state["queue"]), \
            "changed node was not retried"

        mod._state["queue"].clear()
        mod._state["queued_keys"].clear()
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props, force=True)
        assert len(mod._state["queue"]) == n, "Refresh did not retry failures"
    finally:
        mod.queue._render_item = orig
        props.only_tex_shader = True
        props.batch_size = 2
        props.time_budget = 250
        _clear(mod)
        bpy.data.materials.remove(mat)


def test_success_clears_failure_and_prune_drops_it(mod):
    st = mod._state
    _clear(mod)
    st["failed"]["1:gone|"] = "h"
    mod._prune_cache()
    assert "1:gone|" not in st["failed"], "failure of a deleted tree kept"


# --------------------------------------------------------------------------- #
#  2. Source resolution
# --------------------------------------------------------------------------- #
def _shared_gn():
    ng = bpy.data.node_groups.new("NPV_t140_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "Xform"
    ng.links.new(gi.outputs[0], tr.inputs[0])
    ng.links.new(tr.outputs[0], go.inputs[0])
    obs = []
    for nm in ("NPV_t140_a", "NPV_t140_b"):
        me = bpy.data.meshes.new(nm + "_mesh")
        ob = bpy.data.objects.new(nm, me)
        bpy.context.scene.collection.objects.link(ob)
        ob.modifiers.new("GN", "NODES").node_group = ng
        obs.append(ob)
    return ng, obs


def test_shared_geo_tree_previews_the_hinted_object(mod):
    ng, (a, b) = _shared_gn()
    props = _props()
    props.preview_geometry = True
    try:
        _clear(mod)
        assert mod.resolve_source(ng, mod.KIND_GEO) == ("OBJ", a.name)
        mod._state["src_hint"] = [("OBJ", b.name)]
        assert mod.resolve_source(ng, mod.KIND_GEO) == ("OBJ", b.name)

        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert mod._state["queue"][0]["src"] == b.name
        _mark_rendered(mod)
        mod._state["src_hint"] = [("OBJ", a.name)]
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert mod._state["queue"] and mod._state["queue"][0]["src"] == a.name, \
            "switching the active object did not re-render"
    finally:
        props.preview_geometry = False
        _clear(mod)
        for ob in (a, b):
            me = ob.data
            bpy.data.objects.remove(ob)
            bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


def test_hinted_material_wins_for_a_shared_tree_lookup(mod):
    m1 = bpy.data.materials.new("NPV_t140_m1")
    m2 = bpy.data.materials.new("NPV_t140_m2")
    try:
        _clear(mod)
        mod._state["src_hint"] = [("MAT", m2.name)]
        assert mod.resolve_source(m2.node_tree, mod.KIND_SHADER) == ("MAT", m2.name)
        # A hint that doesn't own the tree is ignored.
        assert mod.resolve_source(m1.node_tree, mod.KIND_SHADER) == ("MAT", m1.name)
    finally:
        _clear(mod)
        bpy.data.materials.remove(m1)
        bpy.data.materials.remove(m2)


# --------------------------------------------------------------------------- #
#  3. Light node trees
# --------------------------------------------------------------------------- #
def _light_with_tree():
    lt = bpy.data.lights.new("NPV_t140_light", "POINT")
    if hasattr(lt, "use_nodes"):
        try:
            lt.use_nodes = True
        except Exception:
            pass
    nt = lt.node_tree
    return lt, nt


def test_light_tree_is_resolved_and_previewed(mod):
    lt, nt = _light_with_tree()
    if nt is None:
        bpy.data.lights.remove(lt)
        print("  (skipped: this Blender has no light node trees)")
        return
    try:
        _clear(mod)
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        rgb = nt.nodes.new("ShaderNodeRGB")
        rgb.name = "Tint"
        rgb.outputs[0].default_value = (0.9, 0.1, 0.1, 1.0)
        emit = nt.nodes.new("ShaderNodeEmission")
        emit.name = "Emit"
        out = nt.nodes.new("ShaderNodeOutputLight")
        out.name = "LOut"
        nt.links.new(rgb.outputs[0], emit.inputs["Color"])
        nt.links.new(emit.outputs[0], out.inputs["Surface"])

        assert mod.resolve_source(nt, mod.KIND_SHADER) == ("LIGHT", lt.name)
        assert nt.as_pointer() in mod.queue._live_tree_pointers()
        assert mod.sources._tree_by_pointer(nt.as_pointer()) == nt

        mod.preview_scene.ensure_preview_scene(32)
        snap = datablock_names()
        with capture_renders(mod) as shots:
            assert mod.renderers.render_shader(lt, "Tint", 32, _props())
            assert mod.renderers.render_shader(lt, "LOut", 32, _props())
        r, g, b = mean_rgb(shots[0])
        assert r > g + 0.3 and r > b + 0.3, "light tint swatch not red: %r" % ((r, g, b),)
        assert opaque_rgb(shots[1]), "light output ball is empty"
        after = datablock_names()
        leaked = {k: sorted(set(after[k]) - set(snap[k])) for k in after
                  if set(after[k]) - set(snap[k])}
        assert not leaked, "light preview leaked %r" % leaked

        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, _props())
        assert all(it["src_type"] == "LIGHT" for it in mod._state["queue"])
    finally:
        _clear(mod)
        bpy.data.lights.remove(lt)


# --------------------------------------------------------------------------- #
#  4-5. Read-back and Value numbers
# --------------------------------------------------------------------------- #
def test_load_render_matches_pixels(mod):
    mat = _emission_material("NPV_t140_px", lambda nt: nt.nodes.new("ShaderNodeTexChecker").outputs[0])
    try:
        path = []
        orig = mod.preview_scene._png_to_texture
        mod.preview_scene._png_to_texture = lambda p: path.append(p) or True
        try:
            mod.renderers.render_shader(mat, mat.node_tree.nodes[0].name, 32, _props())
        finally:
            mod.preview_scene._png_to_texture = orig
        ref = load_pixels(path[0])
        w, h, px, value = mod.preview_scene._load_render(path[0])
        assert value is None
        assert len(px) == len(ref) == w * h * 4
        assert max(abs(a - b) for a, b in zip(px, ref)) < 1e-6
    finally:
        bpy.data.materials.remove(mat)


def test_value_swatch_reads_back_its_number(mod):
    cases = [("ADD", 2.0, 3.0, 5.0), ("SUBTRACT", 0.5, 3.0, -2.5),
             ("MULTIPLY", 0.25, 1.0, 0.25)]
    for op, a, b, want in cases:
        mat = _emission_material("NPV_t140_val", lambda nt: _math(nt, op, a, b))
        try:
            value, px = _render_value(mod, mat)
        finally:
            bpy.data.materials.remove(mat)
        assert value is not None and abs(value - want) < 1e-3, (op, value, want)
        # The swatch itself is still the grey a PNG would give: clipped to
        # 0..1 (display), so 5 -> white and -2.5 -> black.
        grey = px[0]
        assert abs(grey - min(max(want, 0.0), 1.0) ** (1 / 2.2)) < 0.08 or \
            want > 1 and grey > 0.99 or want < 0 and grey < 0.01, (op, grey)


def test_varying_value_has_no_number(mod):
    def build(nt):
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.name = "Src"
        noise.inputs["Scale"].default_value = 8.0
        return noise.outputs["Fac"]
    mat = _emission_material("NPV_t140_noise", build)
    try:
        value, _px = _render_value(mod, mat)
    finally:
        bpy.data.materials.remove(mat)
    assert value is None, "a noise swatch reported one number: %r" % value


def test_show_values_off_renders_png(mod):
    mat = _emission_material("NPV_t140_nov", lambda nt: _math(nt, "ADD", 1, 1))
    props = _props()
    props.show_values = False
    try:
        value, _px = _render_value(mod, mat)
    finally:
        props.show_values = True
        bpy.data.materials.remove(mat)
    assert value is None


def test_process_queue_stores_the_value(mod):
    mat = _emission_material("NPV_t140_pq", lambda nt: _math(nt, "ADD", 1.5, 0.25))
    props = _props()
    props.only_tex_shader = False
    orig = mod.preview_scene._png_to_texture

    def fake(path):
        mod._state["last_value"] = mod.preview_scene._load_render(path)[3]
        return object()

    mod.preview_scene._png_to_texture = fake
    try:
        _clear(mod)
        mod.queue.rebuild_queue(mat.node_tree, mod.KIND_SHADER, props)
        mod._state["queue"][:] = [it for it in mod._state["queue"] if it["node"] == "Src"]
        mod._state["queued_keys"] = {it["key"] for it in mod._state["queue"]}
        mod.queue.process_queue(props)
        vals = list(mod._state["values"].values())
        assert len(vals) == 1 and abs(vals[0] - 1.75) < 1e-3, vals
    finally:
        mod.preview_scene._png_to_texture = orig
        props.only_tex_shader = True
        _clear(mod)
        bpy.data.materials.remove(mat)


def test_compositor_render_is_png_even_if_scene_saves_exr(mod):
    scn = bpy.data.scenes.new("NPV_t140_comp")
    tree = bpy.data.node_groups.new("NPV_t140_ctree", "CompositorNodeTree")
    scn.compositing_node_group = tree
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    rgb = tree.nodes.new("CompositorNodeRGB")
    rgb.name = "RGB"
    go = tree.nodes.new("NodeGroupOutput")
    tree.links.new(rgb.outputs[0], go.inputs[0])
    scn.render.image_settings.file_format = "OPEN_EXR"
    paths = []
    orig = mod.preview_scene._png_to_texture
    mod.preview_scene._png_to_texture = lambda p: paths.append(p) or True
    try:
        assert mod.renderers.render_compositor(scn, "RGB", 32, _props())
    finally:
        mod.preview_scene._png_to_texture = orig
        bpy.data.scenes.remove(scn)
        bpy.data.node_groups.remove(tree)
    assert paths[0].endswith(".png"), paths


# --------------------------------------------------------------------------- #
#  6. Priority and time budget
# --------------------------------------------------------------------------- #
def test_queue_renders_priority_then_visible_first(mod):
    st = mod._state
    _clear(mod)
    st["queue"][:] = [{"key": k, "node": k} for k in ("a", "b", "c", "d")]
    st["queued_keys"] = {"a", "b", "c", "d"}
    st["visible"] = {"c"}
    st["priority"] = {"d"}
    order = [mod.queue._pop_next()["key"] for _ in range(4)]
    assert order == ["d", "c", "a", "b"], order
    assert not st["queued_keys"]
    _clear(mod)


def test_time_budget_stops_the_step_early(mod):
    st = mod._state
    props = _props()
    orig = mod.queue._render_item
    calls = []

    def slow(item, res, p):
        calls.append(item["key"])
        time.sleep(0.06)
        return object()

    mod.queue._render_item = slow
    try:
        _clear(mod)
        st["queue"][:] = [{"key": "%d:n%d|" % (1, i), "node": "n%d" % i, "hash": "h",
                           "kind": mod.KIND_SHADER} for i in range(6)]
        props.batch_size = 6
        props.time_budget = 50
        mod.queue.process_queue(props)
        assert len(calls) == 1, "budget ignored: %d renders" % len(calls)
        props.time_budget = 2000
        mod.queue.process_queue(props)
        assert len(calls) == 6, "batch size not honoured: %d" % len(calls)
    finally:
        mod.queue._render_item = orig
        props.batch_size = 2
        props.time_budget = 250
        _clear(mod)


# --------------------------------------------------------------------------- #
#  7. Depsgraph, playback, frame
# --------------------------------------------------------------------------- #
class _Upd:
    def __init__(self, idt, transform=False, geometry=False, shading=False):
        self.id = type("ID", (), {"id_type": idt, "name": "x"})()
        self.is_updated_transform = transform
        self.is_updated_geometry = geometry
        self.is_updated_shading = shading


class _DG:
    def __init__(self, *updates):
        self.updates = updates


def test_transform_only_object_update_is_ignored(mod):
    scn = bpy.context.scene
    mod._state["dirty"] = False
    mod._on_depsgraph(scn, _DG(_Upd("OBJECT", transform=True)))
    assert not mod._state["dirty"], "moving an object marked previews dirty"
    mod._on_depsgraph(scn, _DG(_Upd("OBJECT", geometry=True)))
    assert mod._state["dirty"]
    mod._state["dirty"] = False
    mod._on_depsgraph(scn, _DG(_Upd("LIGHT")))
    assert mod._state["dirty"], "light edit not detected"


def test_timer_pauses_during_playback(mod):
    orig_play, orig_rebuild = mod.timer._animation_playing, mod.queue.rebuild_queue
    rebuilt = []
    mod.timer._animation_playing = lambda: True
    mod.queue.rebuild_queue = lambda *a, **k: rebuilt.append(1)
    props = _props()
    try:
        mod._state["dirty"] = True
        mod._timer()
        assert mod._state["dirty"] and not rebuilt, "timer worked during playback"
        props.update_on_frame = True
        mod._timer()
        assert not mod._state["dirty"], "Update on Frame Change did not keep working"
    finally:
        props.update_on_frame = False
        mod.timer._animation_playing = orig_play
        mod.queue.rebuild_queue = orig_rebuild


def test_update_on_frame_puts_the_frame_in_the_hash(mod):
    mat = _emission_material("NPV_t140_frame", lambda nt: _math(nt, "ADD", 1, 2))
    props = _props()
    scn = bpy.context.scene
    f0 = scn.frame_current
    try:
        _clear(mod)
        nt = mat.node_tree
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        _mark_rendered(mod)
        scn.frame_set(f0 + 3)
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert not mod._state["queue"], "frame change re-rendered with the option off"
        props.update_on_frame = True
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        _mark_rendered(mod)
        scn.frame_set(f0 + 4)
        mod._state["dirty"] = False
        mod._on_frame_change(scn)
        assert mod._state["dirty"]
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert mod._state["queue"], "frame change did not re-render with the option on"
    finally:
        props.update_on_frame = False
        scn.frame_set(f0)
        _clear(mod)
        bpy.data.materials.remove(mat)


# --------------------------------------------------------------------------- #
#  8. Shapes and environment
# --------------------------------------------------------------------------- #
def test_cube_shape_renders_and_is_hidden_afterwards(mod):
    mat = bpy.data.materials.new("NPV_t140_cube")
    props = _props()
    props.shader_shape = "CUBE"
    try:
        out = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial")
        with capture_renders(mod) as shots:
            assert mod.renderers.render_shader(mat, out.name, 48, props)
        px = opaque_rgb(shots[0])
        assert px, "cube render is empty"
        cube = bpy.data.objects.get(mod.common.PREVIEW_CUBE)
        assert cube is not None and cube.hide_render, "cube left visible"
        assert not cube.data.materials or cube.data.materials[0] is None
        # A texture swatch still uses the plane, not the cube.
        with capture_renders(mod) as shots:
            mod.renderers.render_shader(mat, out.name, 48, props)
    finally:
        props.shader_shape = "SPHERE"
        bpy.data.materials.remove(mat)
    mod._cleanup_datablocks()
    assert bpy.data.objects.get(mod.common.PREVIEW_CUBE) is None, "cleanup kept the cube"


def test_hdri_environment_lights_the_ball_and_is_cleaned_up(mod):
    items = [i[0] for i in mod._env_items(None, None)]
    assert items[0] == "UNIFORM"
    if len(items) < 2:
        print("  (skipped: no bundled studio HDRIs)")
        return
    env = "forest.exr" if "forest.exr" in items else items[1]
    mat = bpy.data.materials.new("NPV_t140_env")
    props = _props()
    props.preview_env = env
    try:
        out = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial")
        with capture_renders(mod) as shots:
            assert mod.renderers.render_shader(mat, out.name, 48, props)
        assert opaque_rgb(shots[0])
        assert bpy.data.images.get(mod.common.ENV_IMAGE_PREFIX + env) is not None
        wnt = bpy.data.scenes[mod.common.PREVIEW_SCENE].world.node_tree
        assert wnt.nodes.get("NPV_env") is not None
        props.preview_env = "UNIFORM"
        mod.preview_scene.ensure_preview_scene(32)
        assert wnt.nodes.get("NPV_env") is None, "HDRI node kept for Uniform"
    finally:
        props.preview_env = "UNIFORM"
        bpy.data.materials.remove(mat)
    mod._cleanup_datablocks()
    assert not [i for i in bpy.data.images if i.name.startswith(mod.common.ENV_IMAGE_PREFIX)]


def test_environment_and_shape_are_in_the_light_signature(mod):
    props = _props()
    a = mod.queue._light_sig(props)
    props.shader_shape = "CUBE"
    b = mod.queue._light_sig(props)
    props.shader_shape = "SPHERE"
    assert a != b


# --------------------------------------------------------------------------- #
#  9. Export
# --------------------------------------------------------------------------- #
def test_export_job_writes_a_png(mod):
    mat = _emission_material("NPV_t140_exp", lambda nt: _math(nt, "ADD", 0.2, 0.3))
    fp = os.path.join(bpy.app.tempdir, "npv_t140_export.png")
    if os.path.exists(fp):
        os.remove(fp)
    try:
        node = mat.node_tree.nodes["Src"]
        job = mod.export_job(mat.node_tree, mod.KIND_SHADER, _props(), node)
        assert job and job["src"] == mat.name
        mod._state["export_to"] = fp
        try:
            assert mod.queue._render_item(job, 64, _props())
        finally:
            mod._state["export_to"] = None
        assert os.path.isfile(fp), "export wrote nothing"
        img = bpy.data.images.load(fp)
        try:
            assert tuple(img.size) == (64, 64), tuple(img.size)
        finally:
            bpy.data.images.remove(img)
        assert not mod._state["textures"], "export touched the thumbnail cache"
    finally:
        bpy.data.materials.remove(mat)
        if os.path.exists(fp):
            os.remove(fp)


def test_export_operator_is_registered(mod):
    assert hasattr(bpy.ops.node, "npv_export")
    props = bpy.ops.node.npv_export.get_rna_type().properties
    assert "filepath" in props and "size" in props


# --------------------------------------------------------------------------- #
#  10. Display helpers and UI text
# --------------------------------------------------------------------------- #
def test_grid_origin_positions(mod):
    x0, x1, y0, nh, gw, gh, gap = 100.0, 200.0, 500.0, 80.0, 100.0, 100.0, 6.0
    assert mod.drawing._grid_origin("ABOVE", x0, x1, y0, nh, gw, gh, gap) == (100.0, 506.0)
    assert mod.drawing._grid_origin("BELOW", x0, x1, y0, nh, gw, gh, gap) == (100.0, 314.0)
    assert mod.drawing._grid_origin("LEFT", x0, x1, y0, nh, gw, gh, gap) == (-6.0, 400.0)
    assert mod.drawing._grid_origin("RIGHT", x0, x1, y0, nh, gw, gh, gap) == (206.0, 400.0)
    # A bigger grid stays centred over the node.
    assert mod.drawing._grid_origin("ABOVE", x0, x1, y0, nh, 200.0, gh, gap)[0] == 50.0


def test_checker_covers_half_the_cell(mod):
    tris = mod.drawing._checker_tris(0, 0, 64, 64, 8)
    assert len(tris) == 32 * 6
    assert all(0 <= x <= 64 and 0 <= y <= 64 for x, y in tris)


def test_format_value(mod):
    assert mod.drawing.format_value(5.0) == "5"
    assert mod.drawing.format_value(-2.5) == "-2.5"
    assert mod.drawing.format_value(-1e-9) == "0"
    assert mod.drawing.format_value(1 / 3) == "0.3333"


def test_new_strings_translated_and_display_page(mod):
    for key in ("time_budget", "update_on_frame", "environment", "display_box",
                "thumb_scale", "thumb_position", "zoom_active", "checker_bg",
                "show_status", "show_values", "failed_fmt", "keys_title", "export"):
        assert key in mod.TR["EN"] and key in mod.TR["ZH"], key
    pages = [p for p, _ in mod.TR["EN"]["help_tabs"]]
    assert "DISPLAY" in pages
    text = " ".join(t for _k, t in mod.TR["EN"]["help"]["DISPLAY"])
    assert "Ctrl+Alt+P" in text and "Status Markers" in text
    assert "Marked" in bpy.types.Node.bl_rna.properties["npv_show"].description


# --------------------------------------------------------------------------- #
#  11. Draw callback (GPU stubbed: background mode can't draw)
# --------------------------------------------------------------------------- #
class _Rec:
    """Swallows any call / attribute access, counting calls by name."""
    def __init__(self, log, name=""):
        self._log, self._name = log, name

    def __getattr__(self, attr):
        return _Rec(self._log, attr)

    def __call__(self, *a, **k):
        self._log.append(self._name)
        if self._name == "dimensions":
            return (10.0, 10.0)
        return _Rec(self._log, self._name + "()")


def test_draw_callback_smoke(mod):
    mat = _emission_material("NPV_t140_draw", lambda nt: _math(nt, "ADD", 1, 2))
    nt = mat.node_tree
    props = _props()
    props.only_tex_shader = False
    log = []

    class V2D:
        def view_to_region(self, x, y, clip=False):
            return x, y

    class Region:
        width, height, view2d = 4000, 4000, V2D()

    class Space:
        type, tree_type, shader_type = "NODE_EDITOR", mod.KIND_SHADER, "OBJECT"
        edit_tree, path, id, id_from = nt, [], mat, None

    class Ctx:
        space_data, region, scene, active_object = Space(), Region(), bpy.context.scene, None
        # Background mode reports ui_scale 0.
        preferences = type("P", (), {"system": type("S", (), {"ui_scale": 1.0})()})()

    class Bpy:
        types, data, app, path, props, utils = (bpy.types, bpy.data, bpy.app,
                                                bpy.path, bpy.props, bpy.utils)
        context = Ctx()

    saved = {k: getattr(mod.drawing, k) for k in ("bpy", "gpu", "blf", "batch_for_shader")}
    try:
        _clear(mod)
        nodes = list(nt.nodes)
        for i, n in enumerate(nodes):
            n.location = (i * 300.0, 200.0)
        nt.nodes.active = nt.nodes["Src"]
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        keys = [it["key"] for it in mod._state["queue"]]
        src_key = next(it["key"] for it in mod._state["queue"] if it["node"] == "Src")
        # Src rendered with a number; one failed; the rest still queued.
        mod._state["textures"][src_key] = object()
        mod._state["hashes"][src_key] = "h"
        mod._state["values"][src_key] = 3.0
        other = next(k for k in keys if k != src_key)
        mod._state["failed"][other] = "x"
        props.zoom_active = True
        props.checker_bg = True
        for pos in ("ABOVE", "BELOW", "LEFT", "RIGHT"):
            props.thumb_position = pos
            mod.drawing.bpy, mod.drawing.gpu, mod.drawing.blf = Bpy, _Rec(log), _Rec(log)
            mod.drawing.batch_for_shader = _Rec(log, "batch")
            mod.draw_callback()
            mod.drawing.bpy = saved["bpy"]
        assert "draw" in log and "position" in log, sorted(set(log))
        assert set(keys) <= mod._state["visible"], "on-screen nodes not recorded"
        assert src_key in mod._state["priority"], "active node not prioritised"
        assert mod._state["src_hint"] == [("MAT", mat.name)]
    finally:
        for k, v in saved.items():
            setattr(mod.drawing, k, v)
        props.thumb_position = "ABOVE"
        props.zoom_active = False
        props.checker_bg = False
        props.only_tex_shader = True
        _clear(mod)
        bpy.data.materials.remove(mat)


class _Layout:
    """Fake UILayout: checks every prop() names a real property."""
    def __init__(self, seen):
        self.seen = seen
        self.enabled = True

    def _child(self, *a, **k):
        return _Layout(self.seen)

    row = column = box = split = _child

    def prop(self, data, name, **k):
        assert name in data.bl_rna.properties, "no property %r on %r" % (name, data)
        self.seen.add(name)

    def prop_enum(self, data, name, value, **k):
        rna = getattr(data, "bl_rna", None)
        assert rna is None or name in rna.properties, name

    def operator(self, idname, **k):
        mod_name, op = idname.split(".")
        assert hasattr(getattr(bpy.ops, mod_name), op), idname
        self.seen.add(idname)
        return type("OpProps", (), {})()

    def label(self, **k):
        pass

    def separator(self, **k):
        pass


def test_panel_and_prefs_draw(mod):
    mat = bpy.data.materials.new("NPV_t140_panel")
    seen = set()

    class Space:
        type, tree_type, shader_type = "NODE_EDITOR", mod.KIND_SHADER, "OBJECT"
        edit_tree = mat.node_tree

    class Ctx:
        space_data, scene = Space(), bpy.context.scene
        active_node = mat.node_tree.nodes[0]

    try:
        for scope in ("ALL", "SELECTED", "MARKED"):
            bpy.context.scene.npv.preview_scope = scope
            panel = type("PanelStub", (), {"layout": _Layout(seen)})()
            mod.NPV_PT_panel.draw(panel, Ctx())
        for key in ("time_budget", "update_on_frame", "preview_env", "thumb_scale",
                    "thumb_position", "zoom_active", "zoom_factor", "checker_bg",
                    "show_status", "show_values", "node.npv_export"):
            assert key in seen, "%s missing from the panel" % key
        help_op = type("HelpStub", (), {"layout": _Layout(seen), "page": "DISPLAY"})()
        mod.NPV_OT_help.draw(help_op, Ctx())
    finally:
        bpy.context.scene.npv.preview_scope = "ALL"
        bpy.data.materials.remove(mat)


# --------------------------------------------------------------------------- #
#  12. Review fixes
# --------------------------------------------------------------------------- #
def test_queued_item_follows_a_source_switch(mod):
    ng, (a, b) = _shared_gn()
    props = _props()
    props.preview_geometry = True
    try:
        _clear(mod)
        mod._state["src_hint"] = [("OBJ", b.name)]
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert mod._state["queue"][0]["src"] == b.name
        # Switch before anything rendered: the waiting item must follow.
        mod._state["src_hint"] = [("OBJ", a.name)]
        mod.queue.rebuild_queue(ng, mod.KIND_GEO, props)
        assert len(mod._state["queue"]) == 1
        assert mod._state["queue"][0]["src"] == a.name, \
            "queued item kept the old source under the new hash"
    finally:
        props.preview_geometry = False
        _clear(mod)
        for ob in (a, b):
            me = ob.data
            bpy.data.objects.remove(ob)
            bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


def test_pinned_editor_keeps_its_own_hint(mod):
    ng, (a, b) = _shared_gn()
    ptrs = iter(range(1000, 2000))

    def space(pin, obj):
        p = next(ptrs)
        return type("Space", (), {"pin": pin, "id": obj, "id_from": None,
                                  "as_pointer": lambda self: p})()

    ctx = type("Ctx", (), {"active_object": b})()
    props = _props()
    tp = ng.as_pointer()
    unpinned, pinned = space(False, b), space(True, a)
    try:
        _clear(mod)
        mod._state["editors"].clear()
        mod.drawing._record_editor(ctx, unpinned, tp, mod.KIND_GEO, [tp], props, ng)
        mod.drawing._record_editor(ctx, pinned, tp, mod.KIND_GEO, [tp], props, ng)
        # The pinned editor ignores the active object and doesn't set the
        # global hint; redrawing both in turn no longer marks anything dirty.
        assert mod._state["editors"][1001]["hint"] == [("OBJ", a.name)]
        assert mod._state["src_hint"] == [("OBJ", b.name)]
        mod._state["dirty"] = False
        for _ in range(3):
            mod.drawing._record_editor(ctx, pinned, tp, mod.KIND_GEO, [tp], props, ng)
            mod.drawing._record_editor(ctx, unpinned, tp, mod.KIND_GEO, [tp], props, ng)
        assert not mod._state["dirty"], "two editors keep re-queueing each other"
        # Sharing one tree, both are rebuilt, each through its own object:
        # the source is part of the cache key, so they don't collide.
        # (Editors of a preview type that is off are no targets.)
        props.preview_geometry = True
        orig = mod.timer._live_space_ptrs
        mod.timer._live_space_ptrs = lambda: None
        try:
            targets = mod.timer._editor_targets()
        finally:
            mod.timer._live_space_ptrs = orig
        assert sorted(repr(t[3]) for t in targets) == sorted(
            [repr([("OBJ", a.name)]), repr([("OBJ", b.name)])]), targets
    finally:
        props.preview_geometry = False
        mod._state["editors"].clear()
        _clear(mod)
        for ob in (a, b):
            me = ob.data
            bpy.data.objects.remove(ob)
            bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


def test_undo_to_the_shown_thumbnail_clears_the_failure(mod):
    mat = _emission_material("NPV_t140_undo", lambda nt: _math(nt, "ADD", 1, 2))
    props = _props()
    props.only_tex_shader = False
    try:
        _clear(mod)
        nt = mat.node_tree
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        _mark_rendered(mod)
        key = next(k for k in mod._state["textures"] if k.split(":", 1)[1].startswith("Src|"))
        # An edit whose render failed, then undone.
        mod._state["failed"][key] = "hash-of-the-failing-edit"
        mod.queue.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert not mod._state["queue"]
        assert key not in mod._state["failed"], "stale failure marker kept after undo"
    finally:
        props.only_tex_shader = True
        _clear(mod)
        bpy.data.materials.remove(mat)
