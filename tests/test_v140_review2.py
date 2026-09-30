"""Second v1.4.0 review: linked data in editor hints, object-data content
the GN previews depend on (attribute values, weights, curve / text settings),
and two editors showing one tree through different sources."""
import os
import tempfile

import bpy


def _props():
    return bpy.context.scene.npv


def _clear(mod):
    mod._reset_cache()
    mod._state["src_hint"] = []
    mod._state["editors"].clear()


class _Space:
    def __init__(self, ptr, obj, pin=False, path=()):
        self._ptr, self.id, self.id_from, self.pin, self.path = ptr, obj, None, pin, path

    def as_pointer(self):
        return self._ptr


class _Ctx:
    active_object = None


# --------------------------------------------------------------------------- #
#  1. Linked data keeps its library in the editor hint
# --------------------------------------------------------------------------- #
def test_editor_hint_keeps_the_library_of_linked_data(mod):
    name = "NPV_tr2_lib_mat"
    mat = bpy.data.materials.new(name)
    path = os.path.join(tempfile.mkdtemp(), "npv_tr2_lib.blend")
    bpy.data.libraries.write(path, {mat})
    with bpy.data.libraries.load(path, link=True) as (_src, dst):
        dst.materials = [name]
    linked = next(m for m in bpy.data.materials if m.name == name and m.library)
    try:
        hint = mod._editor_hint(_Ctx(), _Space(1, linked))
        assert hint == [("MAT", mod._idref(linked))], hint
        mod._state["src_hint"] = hint
        assert mod.resolve_source(linked.node_tree, mod.KIND_SHADER) == \
            ("MAT", mod._idref(linked)), "linked hint resolved to the local material"
        assert mod._editor_hint(_Ctx(), _Space(2, mat)) == [("MAT", name)]
    finally:
        mod._state["src_hint"] = []
        bpy.data.libraries.remove(linked.library)
        bpy.data.materials.remove(mat)


# --------------------------------------------------------------------------- #
#  2. Object data content
# --------------------------------------------------------------------------- #
def _changed(mod, data, obj, edit):
    """Does ``edit()`` change the fingerprint (after the update event)?"""
    mod._state["data_sigs"].clear()
    mod._state["data_gen"].clear()
    before = mod.queue._data_sig(data, obj)
    edit()
    assert mod.queue._data_sig(data, obj) == before, "fingerprint recomputed without an update"
    mod.queue._mark_data_changed(mod._idref(data))
    return mod.queue._data_sig(data, obj) != before


def _mesh_obj():
    me = bpy.data.meshes.new("NPV_tr2_mesh")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    ob = bpy.data.objects.new("NPV_tr2_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    return me, ob


def test_mesh_attribute_edits_change_the_fingerprint(mod):
    me, ob = _mesh_obj()
    try:
        attr = me.attributes.new("npv_w", "FLOAT", "POINT")
        assert _changed(mod, me, ob, lambda: setattr(attr.data[1], "value", 0.7)), \
            "attribute value (vertex paint / custom data) not seen"
        col = me.color_attributes.new("npv_col", "FLOAT_COLOR", "POINT")
        assert _changed(mod, me, ob, lambda: setattr(col.data[0], "color", (1, 0, 0, 1)))
        assert _changed(mod, me, ob, lambda: setattr(me.vertices[2], "co", (0, 2, 0)))
        # Selection is not part of what a preview shows.
        assert not _changed(mod, me, ob, lambda: setattr(me.vertices[0], "select",
                                                         not me.vertices[0].select))
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


# Weights get their own mesh: Blender 5.0.1 itself crashes when a mesh with
# assigned vertex-group weights has an attribute value written and is then
# freed (reproduced without the add-on), so the two edits aren't combined.
def test_weight_edits_change_the_fingerprint(mod):
    me, ob = _mesh_obj()
    try:
        vg = ob.vertex_groups.new(name="npv_g")
        vg.add([0, 1], 0.5, "REPLACE")
        assert _changed(mod, me, ob, lambda: vg.add([1], 0.9, "REPLACE")), \
            "vertex-group weight (weight paint) not seen"
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


def test_curve_and_text_settings_change_the_fingerprint(mod):
    cu = bpy.data.curves.new("NPV_tr2_curve", "CURVE")
    sp = cu.splines.new("BEZIER")
    sp.bezier_points.add(1)
    txt = bpy.data.curves.new("NPV_tr2_text", "FONT")
    try:
        assert _changed(mod, cu, None, lambda: setattr(cu, "bevel_depth", 0.2)), "bevel"
        assert _changed(mod, cu, None, lambda: setattr(cu, "extrude", 0.3)), "extrude"
        assert _changed(mod, cu, None, lambda: setattr(sp.bezier_points[1], "radius", 2.0)), \
            "spline radius"
        assert _changed(mod, cu, None, lambda: setattr(sp.bezier_points[0], "tilt", 1.0)), \
            "spline tilt"
        assert _changed(mod, txt, None, lambda: setattr(txt, "size", 2.5)), "text size"
        assert _changed(mod, txt, None, lambda: setattr(txt, "body", "npv")), "text body"
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.curves.remove(cu)
        bpy.data.curves.remove(txt)


def test_volume_settings_change_the_fingerprint(mod):
    vol = bpy.data.volumes.new("NPV_tr2_vol")
    try:
        assert _changed(mod, vol, None, lambda: setattr(vol, "frame_offset", 3))
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.volumes.remove(vol)


# --------------------------------------------------------------------------- #
#  3. Two editors, one tree, two sources
# --------------------------------------------------------------------------- #
def _shared_gn():
    ng = bpy.data.node_groups.new("NPV_tr2_gn", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "Xform"
    ng.links.new(gi.outputs[0], tr.inputs[0])
    ng.links.new(tr.outputs[0], go.inputs[0])
    obs = []
    for nm in ("NPV_tr2_a", "NPV_tr2_b"):
        me = bpy.data.meshes.new(nm + "_mesh")
        ob = bpy.data.objects.new(nm, me)
        bpy.context.scene.collection.objects.link(ob)
        ob.modifiers.new("GN", "NODES").node_group = ng
        obs.append(ob)
    return ng, obs


def _mark_rendered(mod):
    st = mod._state
    for it in st["queue"]:
        st["textures"][it["key"]] = object()
        st["hashes"][it["key"]] = it["hash"]
    st["queue"].clear()
    st["queued_keys"].clear()


def test_two_editors_on_one_tree_keep_their_own_thumbnails(mod):
    ng, (a, b) = _shared_gn()
    props = _props()
    props.preview_geometry = True
    tp = ng.as_pointer()
    sa, sb = _Space(701, a, pin=True), _Space(702, b, pin=True)
    orig = mod.timer._live_space_ptrs, mod.queue.process_queue
    mod.timer._live_space_ptrs = lambda: {701, 702}
    mod.queue.process_queue = lambda p: False
    try:
        _clear(mod)
        for sp in (sa, sb):
            ent = mod.drawing._record_editor(_Ctx(), sp, tp, mod.KIND_GEO, [tp], props, ng)
            ent["ctx"] = mod.drawing._editor_view_ctx(sp, ng, mod.KIND_GEO, ent["hint"])
        ctx_a = mod._state["editors"][701]["ctx"]
        ctx_b = mod._state["editors"][702]["ctx"]
        assert ctx_a and ctx_b and ctx_a != ctx_b

        mod._state["dirty"] = True
        mod._timer()
        srcs = sorted(it["src"] for it in mod._state["queue"] if it["node"] == "Xform")
        assert srcs == sorted([a.name, b.name]), srcs
        # Each render lands under its own editor's key (what its draw looks up).
        for it in mod._state["queue"]:
            want = ctx_a if it["src"] == a.name else ctx_b
            assert it["key"].endswith("#" + want), (it["key"], want)
        _mark_rendered(mod)

        # An edit elsewhere: rebuilding both views re-queues nothing -- they
        # no longer overwrite each other's hashes.
        mod._state["dirty"] = True
        mod._timer()
        assert not mod._state["queue"], \
            "two editors on one tree re-render each other: %r" % mod._state["queue"]
    finally:
        mod.timer._live_space_ptrs, mod.queue.process_queue = orig
        props.preview_geometry = False
        _clear(mod)
        for ob in (a, b):
            me = ob.data
            bpy.data.objects.remove(ob)
            bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


def test_group_entered_from_two_materials_keeps_both(mod):
    g = bpy.data.node_groups.new("NPV_tr2_grp", "ShaderNodeTree")
    g.interface.new_socket("In", in_out="INPUT", socket_type="NodeSocketColor")
    g.interface.new_socket("Out", in_out="OUTPUT", socket_type="NodeSocketColor")
    gi, go = g.nodes.new("NodeGroupInput"), g.nodes.new("NodeGroupOutput")
    mix = g.nodes.new("ShaderNodeMix")
    mix.name = "Inner Mix"
    mix.data_type = "RGBA"
    g.links.new(gi.outputs[0], mix.inputs["A"])
    g.links.new(mix.outputs["Result"], go.inputs[0])
    mats = []
    for nm, col in (("NPV_tr2_ma", (1, 0, 0, 1)), ("NPV_tr2_mb", (0, 0, 1, 1))):
        m = bpy.data.materials.new(nm)
        inst = m.node_tree.nodes.new("ShaderNodeGroup")
        inst.node_tree = g
        inst.inputs[0].default_value = col
        m.node_tree.nodes.active = inst
        mats.append(m)
    props = _props()
    try:
        _clear(mod)
        # Two editors, each in the group entered from its own material.
        for i, m in enumerate(mats):
            chain = mod._instance_chain([m.node_tree, g])
            mod._state["editors"][800 + i] = {
                "tree": g.as_pointer(), "visible": set(), "priority": set(),
                "ctx": mod.common._view_ctx(("MAT", m.name), chain)}
        keys = {}
        for m in mats:
            mod._state["src_hint"] = [("MAT", m.name)]
            before = {it["key"] for it in mod._state["queue"]}
            mod.queue.rebuild_queue(g, mod.KIND_SHADER, props, path=[m.node_tree, g])
            keys[m.name] = {it["key"] for it in mod._state["queue"]} - before
        a, b = (keys[m.name] for m in mats)
        assert a and b and not (a & b), "one group, two materials: keys collide"
        assert {it["key"] for it in mod._state["queue"]} == a | b, \
            "the second material's rebuild dropped the first one's renders"
    finally:
        _clear(mod)
        for m in mats:
            bpy.data.materials.remove(m)
        bpy.data.node_groups.remove(g)


# --------------------------------------------------------------------------- #
#  Third pass: cache eviction per context, missed edits, vertex groups
# --------------------------------------------------------------------------- #
def test_prune_evicts_other_contexts_of_an_open_tree(mod):
    mat = bpy.data.materials.new("NPV_tr2_prune")
    st = mod._state
    orig = mod.queue._max_textures
    mod.queue._max_textures = lambda: 4
    try:
        _clear(mod)
        tree = mat.node_tree
        st["editors"][900] = {"tree": tree.as_pointer(), "ctx": "cur",
                              "visible": set(), "priority": set()}
        cur = [mod.common._skey(tree, "n%d" % i, None, "cur") for i in range(3)]
        old = [mod.common._skey(tree, "n%d" % i, None, "old%d" % j)
               for i in range(3) for j in range(3)]
        for k in old + cur:          # the current ones are the newest
            st["textures"][k] = object()
            mod.queue._touch(k)
        mod._prune_cache()
        assert all(k in st["textures"] for k in cur), "evicted what the editor shows"
        assert len(st["textures"]) <= 4, \
            "thumbnails for other sources of an open tree never age out (%d)" % len(st["textures"])
    finally:
        mod.queue._max_textures = orig
        _clear(mod)
        bpy.data.materials.remove(mat)


class _Upd:
    def __init__(self, idt, name):
        self.id = type("ID", (), {"id_type": idt, "name": name, "library": None})()
        self.is_updated_transform = self.is_updated_geometry = False
        self.is_updated_shading = False


def test_data_edit_with_auto_update_off_is_not_missed(mod):
    me, ob = _mesh_obj()
    props = _props()
    try:
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        before = mod.queue._data_sig(me, ob)
        props.auto_update = False
        me.vertices[0].co = (0.3, 0.3, 0.0)
        dg = type("DG", (), {"updates": (_Upd("MESH", me.name),)})()
        mod._on_depsgraph(bpy.context.scene, dg)
        props.auto_update = True
        assert mod.queue._data_sig(me, ob) != before, \
            "edit made while Auto Update was off kept the old fingerprint"
    finally:
        props.auto_update = True
        mod._state["data_sigs"].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


def test_vertex_group_rename_changes_the_geo_hash(mod):
    ng, (a, _b) = _shared_gn()
    try:
        _clear(mod)
        vg = a.vertex_groups.new(name="npv_mask")
        before = mod.queue._geo_source_sig(a.name, ng)
        vg.name = "npv_other"          # an object update only; mesh untouched
        assert mod.queue._geo_source_sig(a.name, ng) != before, "vertex group rename not seen"
    finally:
        _clear(mod)
        for ob in (a, _b):
            me = ob.data
            bpy.data.objects.remove(ob)
            bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(ng)


def test_weight_paint_mode_defers_the_weight_hash(mod):
    me, ob = _mesh_obj()
    painting = type("Obj", (), {"mode": "WEIGHT_PAINT", "vertex_groups": ob.vertex_groups})()
    try:
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        first = mod.queue._data_sig(me, ob)
        ref = mod._idref(me)
        calls = []
        orig = mod.queue._compute_data_sig
        mod.queue._compute_data_sig = lambda d, o=None: calls.append(1) or orig(d, o)
        try:
            mod.queue._mark_data_changed(ref)
            assert mod.queue._data_sig(me, painting) == first and not calls, \
                "re-hashed on every brush dab"
            assert mod._state["data_sigs"][(ref, False)][0] != mod._state["data_gen"][ref], \
                "lost the pending change"
            me.vertices[0].co = (0.9, 0.0, 0.0)
            assert mod.queue._data_sig(me, ob) != first and calls, \
                "change not picked up after leaving Weight Paint"
        finally:
            mod.queue._compute_data_sig = orig
    finally:
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


def test_object_geometry_update_invalidates_its_data(mod):
    # Sculpting / foreach_set + update_tag() can tag only the object.
    me, ob = _mesh_obj()
    try:
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        before = mod.queue._data_sig(me, ob)
        me.vertices[1].co = (3.0, 0.0, 0.0)
        upd = _Upd("OBJECT", ob.name)
        upd.id = type("ID", (), {"id_type": "OBJECT", "name": ob.name,
                                 "library": None, "data": me})()
        upd.is_updated_geometry = True
        mod._on_depsgraph(bpy.context.scene, type("DG", (), {"updates": (upd,)})())
        assert mod.queue._data_sig(me, ob) != before, "object-only geometry update missed"
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


def test_shared_mesh_weight_fingerprint_per_object_kind(mod):
    me, a = _mesh_obj()
    b = bpy.data.objects.new("NPV_tr2_obj_b", me)
    bpy.context.scene.collection.objects.link(b)
    try:
        vg = a.vertex_groups.new(name="npv_g")
        vg.add([0], 0.5, "REPLACE")
        mod._state["data_sigs"].clear()
        mod._state["data_gen"].clear()
        # With vertex groups on one object only, the other reads none (the
        # names live on the mesh from Blender 3.0, so b may see them too;
        # either way each fingerprint notices the change on its own).
        sa0, sb0 = mod.queue._data_sig(me, a), mod.queue._data_sig(me, b)
        vg.add([0], 0.9, "REPLACE")
        mod.queue._mark_data_changed(mod._idref(me))
        sb1 = mod.queue._data_sig(me, b)          # b rebuilds first ...
        sa1 = mod.queue._data_sig(me, a)          # ... a still sees the change
        assert sa1 != sa0, "weights change lost when another object rebuilt first"
    finally:
        mod._state["data_sigs"].clear()
        bpy.data.objects.remove(b)
        bpy.data.objects.remove(a)
        bpy.data.meshes.remove(me)


def test_failed_count_is_per_view(mod):
    mat = bpy.data.materials.new("NPV_tr2_fail")
    try:
        _clear(mod)
        tree = mat.node_tree
        mod._state["failed"][mod.common._skey(tree, "n", None, "ctxA")] = "h"
        seen = []

        class L:
            enabled = True

            def __getattr__(self, n):
                return lambda *a, **k: L() if n in ("row", "column", "box") else (
                    seen.append(k.get("text")) if n == "label" else None)

        class Space:
            type, tree_type, shader_type, edit_tree = "NODE_EDITOR", mod.KIND_SHADER, "OBJECT", tree

            def as_pointer(self):
                return 950

        ctx = type("C", (), {"space_data": Space(), "scene": bpy.context.scene,
                             "active_node": None})()
        mod._state["editors"][950] = {"tree": tree.as_pointer(), "ctx": "ctxB",
                                      "visible": set(), "priority": set()}
        mod.NPV_PT_panel.draw(type("P", (), {"layout": L()})(), ctx)
        fail_txt = mod._t(bpy.context.scene.npv, "failed_fmt") % 1
        assert fail_txt not in seen, "another source's failure counted here"
        mod._state["editors"][950]["ctx"] = "ctxA"
        seen.clear()
        mod.NPV_PT_panel.draw(type("P", (), {"layout": L()})(), ctx)
        assert fail_txt in seen, seen
    finally:
        _clear(mod)
        bpy.data.materials.remove(mat)
