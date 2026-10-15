"""The 'Advanced' Compositor Scene Cache mode: a checklist of what the scene
cache checks on objects no window's depsgraph evaluates (hidden in the
viewport, or in collections excluded from the window's view layer).

A1 its defaults equal Simple (same signature; the cache tests pass again in
Advanced), A2 each item catches the edit it is for and, unticked, keeps the
cache, A3 unticking Object Data skips a heavy mesh, A4 the cost line's
state, A5 ticking an item re-renders no thumbnail, A6 Simple / Off are
unaffected, plus the panel's Advanced box.
"""
import contextlib

import bpy

import test_comp_scene_cache as base
from npv_testutil import capture_renders

RES = base.RES


@contextlib.contextmanager
def advanced(props, **ticks):
    """Mode Advanced with the default checklist, changed by ``ticks``
    (item id -> bool); everything put back afterwards."""
    items = [p for p in props.bl_rna.properties.keys() if p.startswith("comp_sig_")]
    saved = {p: getattr(props, p) for p in items}
    props.comp_cache_mode = "ADVANCED"
    try:
        for p in items:
            props.property_unset(p)
        for k, v in ticks.items():
            setattr(props, "comp_sig_" + k, v)
        yield
    finally:
        for p, v in saved.items():
            setattr(props, p, v)
        props.comp_cache_mode = "SIMPLE"


def _with_selection(edit):
    """``edit`` plus a selection in the same evaluation: the update lists the
    scene, so the empty-updates flag check doesn't fire and only the
    signature can tell."""
    cube = bpy.data.objects["Cube"]

    def run():
        edit()
        cube.select_set(not cube.select_get())

    return run


# --------------------------------------------------------------------------- #
#  A1 Defaults equal Simple
# --------------------------------------------------------------------------- #
def test_advanced_defaults_equal_simple_signature(mod):
    R = mod.renderers
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        with base._unseen_object(scene, image=True) as (col, ob, img):
            ob.hide_viewport = True
            base._gn_transform(ob)
            base._limit_z(ob)
            base._flush()
            try:
                simple = R._unseen_content_sig(scene, R._components(props))
                skey = R._comp_cache_key(scene, RES, props)
                with advanced(props):
                    assert R._components(props) == R.SIMPLE_COMPONENTS
                    adv = R._unseen_content_sig(scene, R._components(props))
                    akey = R._comp_cache_key(scene, RES, props)
                assert adv == simple, "Advanced defaults gave another signature"
                assert akey == skey, "Advanced defaults gave another cache key"
            finally:
                ng = ob.modifiers[0].node_group
                ob.modifiers.clear()
                bpy.data.node_groups.remove(ng)


# A1(b): the invalidation, reuse and one-render tests again, in Advanced.
_RERUN = (
    "test_one_scene_render_for_many_previews",
    "test_tree_without_render_layers_renders_no_cache",
    "test_preview_renders_do_not_drop_the_cache",
    "test_compositor_edit_reuses_cache",
    "test_scene_edit_rebuilds_cache",
    "test_object_move_rebuilds_cache",
    "test_scene_settings_and_frame_change_the_key",
    "test_working_space_change_rebuilds_cache",
    "test_refresh_rebuilds_cache",
    "test_export_keeps_thumbnail_cache",
    "test_edit_in_excluded_collection_rebuilds_cache",
    "test_edit_of_viewport_hidden_object_rebuilds_cache",
    "test_move_of_viewport_hidden_object_rebuilds_cache",
    "test_image_edit_of_viewport_hidden_object_rebuilds_cache",
    "test_image_edit_in_excluded_collection_rebuilds_cache",
    "test_unseen_edit_with_a_selection_rebuilds_cache",
    "test_hidden_objects_left_alone_keep_cache",
    "test_gn_tree_edit_of_viewport_hidden_object_rebuilds_cache",
    "test_gn_tree_edit_in_excluded_collection_rebuilds_cache",
    "test_constraint_edit_of_viewport_hidden_object_rebuilds_cache",
    "test_boolean_cutter_of_hidden_object_moved_rebuilds_cache",
    "test_object_info_source_of_hidden_object_edited_rebuilds_cache",
    "test_constraint_edit_with_a_selection_rebuilds_cache",
    "test_unseen_signature_once_per_batch",
    "test_cache_exr_is_lossless_whatever_the_output_codec",
    "test_eevee_exposure_change_rebuilds_cache",
)


def _rerun(name):
    def test(mod):
        base.MODE[0] = "ADVANCED"
        try:
            getattr(base, name)(mod)
        finally:
            base.MODE[0] = "SIMPLE"
    test.__name__ = "test_advanced_" + name[len("test_"):]
    return test


for _name in _RERUN:
    globals()["test_advanced_" + _name[len("test_"):]] = _rerun(_name)
del _name


# --------------------------------------------------------------------------- #
#  A2 Each item
# --------------------------------------------------------------------------- #
def _item_case(mod, item, build, image=False, halves=(True, False)):
    """Run the fixture ``build`` twice -- with ``item`` ticked (the next
    preview renders the scene once and equals the per-node render) and
    unticked (the cache is kept) -- every other item at its default.
    build(scene, vl, vl2, col, ob, x) -> (Render Layers view layer, edit,
    cleanup or None); x is the BSDF, or the image with ``image``."""
    for ticked in halves:
        with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
            props = scene.npv
            with base._unseen_object(scene, image=image) as (col, ob, x):
                layer, edit, cleanup = build(scene, vl, vl2, col, ob, x)
                try:
                    base._flush()
                    with advanced(props, **{item: ticked}):
                        base._unseen_edit_case(
                            mod, scene, tree, layer, _with_selection(edit),
                            quiet=False, rebuild=ticked)
                except AssertionError as exc:
                    raise AssertionError("%s %s: %s" % (
                        item, "ticked" if ticked else "unticked", exc))
                finally:
                    ob.modifiers.clear()
                    ob.constraints.clear()
                    ob.parent = None
                    if cleanup is not None:
                        cleanup()


def _hidden(ob, vl):
    ob.hide_viewport = True
    return vl.name


def test_item_xform(mod):
    def build(scene, vl, vl2, col, ob, bsdf):
        return _hidden(ob, vl), lambda: setattr(ob, "location", (0.0, 0.0, 3.5)), None
    _item_case(mod, "xform", build)


def test_item_modifiers(mod):
    def build(scene, vl, vl2, col, ob, bsdf):
        ng, tr = base._gn_transform(ob)
        return (_hidden(ob, vl), lambda: setattr(
            tr.inputs["Scale"], "default_value", (0.01, 0.01, 0.01)),
            lambda: bpy.data.node_groups.remove(ng))
    _item_case(mod, "modifiers", build)


def test_item_constraints(mod):
    def build(scene, vl, vl2, col, ob, bsdf):
        c = base._limit_z(ob)
        return _hidden(ob, vl), lambda: setattr(c, "min_z", 50.0), None
    _item_case(mod, "constraints", build)


def test_item_data(mod):
    # The corner nearest the camera pushed to the centre: the bounding box
    # (the object's dimensions, always checked) stays the same.
    def build(scene, vl, vl2, col, ob, bsdf):
        def edit():
            v = max(ob.data.vertices, key=lambda v: v.co.x - v.co.y + v.co.z)
            v.co = (0.0, 0.0, 0.0)
            ob.data.update()
        return _hidden(ob, vl), edit, None
    _item_case(mod, "data", build)


def test_item_materials(mod):
    def build(scene, vl, vl2, col, ob, bsdf):
        return _hidden(ob, vl), lambda: setattr(
            bsdf.inputs["Emission Color"], "default_value", (0.0, 0.0, 1.0, 1.0)), None
    _item_case(mod, "materials", build)


def test_item_images(mod):
    def build(scene, vl, vl2, col, ob, img):
        return _hidden(ob, vl), lambda: setattr(
            img, "generated_color", (0.0, 0.0, 1.0, 1.0)), None
    _item_case(mod, "images", build, image=True)


def _empty(col, name):
    e = bpy.data.objects.new(name, None)
    col.objects.link(e)
    e.hide_viewport = True
    e.hide_render = True
    return e


def test_item_refs_boolean_operand_modifier(mod):
    # 7(a): an existing modifier's value on a Boolean operand that is disabled
    # in renders (its mesh and transform untouched).
    def build(scene, vl, vl2, col, ob, bsdf):
        cutter = base._helper_object(col, "NPV_tca_cutter", 1.0)
        disp = cutter.modifiers.new("NPV_tca_disp", "DISPLACE")
        disp.strength = 0.0
        m = ob.modifiers.new("NPV_tca_bool", "BOOLEAN")
        m.operation = "INTERSECT"
        m.object = cutter
        return (_hidden(ob, vl), lambda: setattr(disp, "strength", 4.0),
                lambda: (ob.modifiers.clear(), base._remove_object(cutter)))
    _item_case(mod, "refs", build)


def test_item_refs_object_info_source_material(mod):
    # 7(b): a Geometry Nodes Object Info source's material, edited
    # (assigning another material to its slot rebuilds the depsgraph
    # relations, which every mode notices through the updates).
    def build(scene, vl, vl2, col, ob, bsdf):
        src = base._helper_object(col, "NPV_tca_src", 2.4)
        red = bpy.data.materials.new("NPV_tca_red")
        blue = bpy.data.materials.new("NPV_tca_blue")
        for m, c in ((red, (1.0, 0.0, 0.0, 1.0)), (blue, (0.0, 0.0, 1.0, 1.0))):
            b = m.node_tree.nodes["Principled BSDF"]
            b.inputs["Emission Color"].default_value = c
            b.inputs["Emission Strength"].default_value = 5.0
        src.data.materials.append(red)
        ng = bpy.data.node_groups.new("NPV_tca_gnref", "GeometryNodeTree")
        ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        ng.nodes.new("NodeGroupInput")
        go = ng.nodes.new("NodeGroupOutput")
        oi = ng.nodes.new("GeometryNodeObjectInfo")
        oi.inputs["Object"].default_value = src
        ng.links.new(oi.outputs["Geometry"], go.inputs[0])
        ob.modifiers.new("NPV_tca_gnref", "NODES").node_group = ng

        def edit():
            red.node_tree.nodes["Principled BSDF"].inputs[
                "Emission Color"].default_value = (0.0, 0.0, 1.0, 1.0)

        def cleanup():
            ob.modifiers.clear()
            bpy.data.node_groups.remove(ng)
            base._remove_object(src)
            bpy.data.materials.remove(red)
            bpy.data.materials.remove(blue)
        return _hidden(ob, vl), edit, cleanup
    _item_case(mod, "refs", build)


def test_item_refs_constraint_target_moved(mod):
    # 7(c): a Copy Location target of a viewport-hidden owner.
    def build(scene, vl, vl2, col, ob, bsdf):
        tgt = _empty(col, "NPV_tca_target")
        c = ob.constraints.new("COPY_LOCATION")
        c.target = tgt
        return (_hidden(ob, vl), lambda: setattr(tgt, "location", (0.0, 0.0, 50.0)),
                lambda: (ob.constraints.clear(), bpy.data.objects.remove(tgt)))
    _item_case(mod, "refs", build)


def test_item_refs_parent_empty_moved(mod):
    # 7(d): a parent Empty disabled in viewports and renders, of an owner in
    # a collection the window's view layer excludes.
    def build(scene, vl, vl2, col, ob, bsdf):
        par = _empty(col, "NPV_tca_parent")
        ob.parent = par
        vl.layer_collection.children[col.name].exclude = True
        return (vl2.name, lambda: setattr(par, "location", (0.0, 0.0, 50.0)),
                lambda: (setattr(ob, "parent", None), bpy.data.objects.remove(par)))
    _item_case(mod, "refs", build)


def test_item_particles_setting(mod):
    # 8(a): a particle setting value (hair length).
    def build(scene, vl, vl2, col, ob, bsdf):
        ob.modifiers.new("NPV_tca_ps", "PARTICLE_SYSTEM")
        st = ob.particle_systems[0].settings
        st.type = "HAIR"
        st.count = 2000
        st.hair_length = 0.2
        return (_hidden(ob, vl), lambda: setattr(st, "hair_length", 3.0),
                lambda: (ob.modifiers.clear(), bpy.data.particles.remove(st)))
    _item_case(mod, "particles", build)


def test_item_particles_displace_texture(mod):
    # 8(b): a Displace modifier's texture setting.
    def build(scene, vl, vl2, col, ob, bsdf):
        tex = bpy.data.textures.new("NPV_tca_tex", "CLOUDS")
        m = ob.modifiers.new("NPV_tca_disp", "DISPLACE")
        m.texture = tex
        m.strength = 2.0
        return (_hidden(ob, vl), lambda: setattr(tex, "intensity", 0.0),
                lambda: (ob.modifiers.clear(), bpy.data.textures.remove(tex)))
    _item_case(mod, "particles", build)


def _emission_driver(bsdf, expression):
    fc = bsdf.inputs["Emission Strength"].driver_add("default_value")
    fc.driver.type = "SCRIPTED"
    fc.driver.expression = expression
    return fc.driver


def test_item_drivers_expression(mod):
    # 9(a): a driver expression on a material node value. Ticked half only
    # (approved for this item):
    # editing an expression rebuilds the depsgraph relations, so the update
    # lists every visible object and the cache is dropped in every mode --
    # the unticked half can't keep it (checked below).
    def build(scene, vl, vl2, col, ob, bsdf):
        d = _emission_driver(bsdf, "5")
        return _hidden(ob, vl), lambda: setattr(d, "expression", "0"), None
    _item_case(mod, "drivers", build, halves=(True,))


def test_driver_expression_edit_is_reported_by_blender(mod):
    # Why 9(a) has no unticked half: the edit itself reaches the updates.
    mat = bpy.data.materials.new("NPV_tca_drv")
    try:
        d = _emission_driver(mat.node_tree.nodes["Principled BSDF"], "5")
        base._flush()
        seen = []
        probe = lambda _s, dg: seen.append([u.id.id_type for u in dg.updates])
        bpy.app.handlers.depsgraph_update_post.append(probe)
        try:
            d.expression = "0"
            base._flush()
        finally:
            bpy.app.handlers.depsgraph_update_post.remove(probe)
        assert seen and "OBJECT" in seen[0], seen
    finally:
        bpy.data.materials.remove(mat)


def test_item_drivers_target_value(mod):
    # 9(b): a SINGLE_PROP driver target's value (a custom property).
    def build(scene, vl, vl2, col, ob, bsdf):
        mat = ob.data.materials[0]
        mat["npv_k"] = 5.0
        d = _emission_driver(bsdf, "k")
        v = d.variables.new()
        v.name = "k"
        v.type = "SINGLE_PROP"
        v.targets[0].id_type = "MATERIAL"
        v.targets[0].id = mat
        v.targets[0].data_path = '["npv_k"]'
        return _hidden(ob, vl), lambda: mat.__setitem__("npv_k", 0.0), None
    _item_case(mod, "drivers", build)


# --------------------------------------------------------------------------- #
#  A3 - A6
# --------------------------------------------------------------------------- #
def test_unticked_data_skips_heavy_mesh(mod):
    R = mod.renderers
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        me = bpy.data.meshes.new("NPV_tca_heavy")
        import bmesh
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=256, v_segments=128, radius=1.0)
        bm.to_mesh(me)
        bm.free()
        ob = bpy.data.objects.new("NPV_tca_heavy", me)
        scene.collection.objects.link(ob)
        ob.hide_viewport = True
        seen = []
        orig = mod.queue._compute_data_sig
        mod.queue._compute_data_sig = lambda data, o=None: seen.append(data.name) \
            or orig(data, o)
        try:
            for tick in (True, False):
                seen.clear()
                with advanced(props, data=tick):
                    R._comp_cache_key(scene, RES, props)
                assert (me.name in seen) == tick, \
                    "Object Data %s: data fingerprints %r" % (tick, seen)
        finally:
            mod.queue._compute_data_sig = orig
            bpy.data.objects.remove(ob)
            bpy.data.meshes.remove(me)


def test_cost_state_recorded_and_cleared(mod):
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        with base._unseen_object(scene) as (col, ob, bsdf):
            ob.hide_viewport = True
            base._ball_tree(tree, scene)
            with advanced(props, refs=True):
                cube = bpy.data.objects["Cube"]
                c = ob.constraints.new("COPY_LOCATION")
                tgt = _empty(col, "NPV_tca_target")
                c.target = tgt
                try:
                    mod._state["comp_sig_stats"].clear()
                    base._preview(mod, scene, "Blur")
                    st = mod._state["comp_sig_stats"].get(scene.as_pointer())
                    assert st is not None, "no cost state after a cached preview"
                    assert st["ms"] >= 0.0 and st["unseen"] == 1 and st["refs"] == 1, st
                    mod.queue._reset_cache()
                    assert not mod._state["comp_sig_stats"], "_reset_cache kept it"
                finally:
                    ob.constraints.clear()
                    bpy.data.objects.remove(tgt)
                    assert cube


def test_checkbox_change_requeues_nothing(mod):
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        with base._unseen_object(scene) as (col, ob, bsdf):
            ob.hide_viewport = True
            base._ball_tree(tree, scene)
            st = mod._state
            with advanced(props):
                base._drop(mod)
                base._preview(mod, scene, "Blur")
                for k in ("queue", "queued_keys", "hashes"):
                    st[k].clear()
                mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, props, path=[tree])
                for it in st["queue"]:          # as if rendered
                    st["hashes"][it["key"]] = it["hash"]
                    st["textures"][it["key"]] = object()
                st["queue"].clear()
                st["queued_keys"].clear()
                props.comp_sig_drivers = True
                props.comp_sig_data = False
                mod.queue.rebuild_queue(tree, mod.common.KIND_COMP, props, path=[tree])
                assert not st["queue"], "a checkbox change queued thumbnails"
                with base.engine_renders() as c:
                    base._preview(mod, scene, "Blur")
                    base._preview(mod, scene, "Blur")
                assert c.engine() == 1, "after a checkbox change: %d scene renders" \
                    % c.engine()


def test_simple_ignores_checkboxes(mod):
    R = mod.renderers
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        with base._unseen_object(scene) as (col, ob, bsdf):
            ob.hide_viewport = True
            base._flush()
            key = R._comp_cache_key(scene, RES, props)
            items = ["comp_sig_" + i for i in R.SIG_ITEMS]
            saved = {p: getattr(props, p) for p in items}
            try:
                for p in items:
                    setattr(props, p, not getattr(props, p))
                assert R._comp_cache_key(scene, RES, props) == key, \
                    "Simple's key follows the Advanced checklist"
            finally:
                for p, v in saved.items():
                    setattr(props, p, v)


def test_switching_simple_and_advanced_keeps_cache(mod):
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        with base._unseen_object(scene) as (col, ob, bsdf):
            ob.hide_viewport = True
            base._ball_tree(tree, scene)
            base._drop(mod)
            base._preview(mod, scene, "Blur")
            with base.engine_renders() as c:
                with advanced(props):
                    base._preview(mod, scene, "Blur")
                    assert base._cached(mod)
                base._preview(mod, scene, "Blur")
            assert c.engine() == 0, "switching Simple / Advanced rendered the scene"
            assert base._cached(mod)
            with advanced(props, refs=True):
                with base.engine_renders() as c:
                    base._preview(mod, scene, "Blur")
                assert c.engine() == 1, "another checklist kept the old key"


def test_off_still_never_uses_cache(mod):
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        base._ball_tree(tree, scene)
        with advanced(props):
            base._preview(mod, scene, "Blur")
            assert base._cached(mod)
        try:
            props.comp_cache_mode = "OFF"
            assert mod._state["comp_cache"] is None
            with base.engine_renders() as c:
                base._preview(mod, scene, "Blur")
                base._preview(mod, scene, "Blur")
            assert c.engine() == 2 and mod._state["comp_cache"] is None
        finally:
            props.comp_cache_mode = "SIMPLE"


# --------------------------------------------------------------------------- #
#  Panel
# --------------------------------------------------------------------------- #
class _Layout:
    def __init__(self, rec):
        self.rec = rec
        self.enabled = True

    def _child(self, *a, **k):
        return _Layout(self.rec)

    row = column = box = split = _child

    def prop(self, data, name, **k):
        assert name in data.bl_rna.properties, name
        self.rec["props"].append(name)

    def prop_enum(self, data, name, value, **k):
        self.rec["enums"].append((name, value, k.get("text")))

    def operator(self, idname, **k):
        return type("OpProps", (), {})()

    def label(self, text="", **k):
        self.rec["labels"].append(text)

    def separator(self, **k):
        pass


def _draw_panel(mod, tree):
    class Space:
        type, tree_type, shader_type = "NODE_EDITOR", mod.common.KIND_COMP, "OBJECT"
        edit_tree = tree

        def as_pointer(self):
            return 1

    class Ctx:
        space_data, scene, active_node = Space(), bpy.context.scene, None

    rec = {"props": [], "enums": [], "labels": []}
    mod.NPV_PT_panel.draw(type("P", (), {"layout": _Layout(rec)})(), Ctx())
    return rec


def test_panel_advanced_box(mod):
    with base.comp_scene(mod) as (scene, tree, vl, vl2, mat):
        props = scene.npv
        saved = props.preview_compositor
        props.preview_compositor = True
        try:
            sig_props = ["comp_sig_" + i for i in mod.renderers.SIG_ITEMS]
            rec = _draw_panel(mod, tree)
            assert ("comp_cache_mode", "ADVANCED", mod.TR["EN"]["comp_cache_advanced"]) \
                in rec["enums"] or any(e[1] == "ADVANCED" for e in rec["enums"]), rec["enums"]
            assert not set(sig_props) & set(rec["props"]), "checklist shown in Simple"
            with advanced(props):
                mod._state["comp_sig_stats"].clear()
                rec = _draw_panel(mod, tree)
                assert [p for p in rec["props"] if p in sig_props] == sig_props, rec["props"]
                none = {mod.TR[l]["comp_sig_cost_none"] for l in ("EN", "ZH")}
                assert none & set(rec["labels"]), "no placeholder before the first check"
                mod._state["comp_sig_stats"][scene.as_pointer()] = {
                    "ms": 12.0, "unseen": 3, "refs": 1}
                rec = _draw_panel(mod, tree)
                lang = "ZH" if mod.TR["ZH"]["comp_sig_cost_fmt"] % 12.0 in rec["labels"] \
                    else "EN"
                tr = mod.TR[lang]
                assert tr["comp_sig_cost_fmt"] % 12.0 in rec["labels"], rec["labels"]
                assert tr["comp_sig_cost_objs_fmt"] % (3, 1) in rec["labels"], rec["labels"]
            for p in sig_props:
                d = props.bl_rna.properties[p].description
                assert "If unticked, press Refresh after changing this" in d, (p, d)
        finally:
            props.preview_compositor = saved
            mod._state["comp_sig_stats"].clear()
