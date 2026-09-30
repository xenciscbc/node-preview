"""Several node editors at once: each is tracked on its own (tree, kind,
path, source hint), the timer rebuilds all of them, their on-screen /
priority keys are merged, the cache keeps all of their trees, and editors
redrawing in turn no longer mark previews dirty each time."""
import bpy


def _props():
    return bpy.context.scene.npv


class _Space:
    def __init__(self, ptr, obj=None, pin=False):
        self._ptr, self.id, self.id_from, self.pin = ptr, obj, None, pin

    def as_pointer(self):
        return self._ptr


class _Ctx:
    active_object = None


def _material(name):
    mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    return mat


def _gn(name):
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube")
    cube.name = "Cube"
    ng.links.new(cube.outputs[0], go.inputs[0])
    me = bpy.data.meshes.new(name + "_mesh")
    ob = bpy.data.objects.new(name + "_obj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = ng
    return ng, ob


def _setup(mod):
    mod._reset_cache()
    mod._state["editors"].clear()
    mod._state["src_hint"] = []
    mat = _material("NPV_ted_mat")
    ng, ob = _gn("NPV_ted_gn")
    shader = _Space(501, mat)
    geo = _Space(502, ob)
    return mat, ng, ob, shader, geo


def _teardown(mod, mat, ng, ob):
    mod._reset_cache()
    mod._state["editors"].clear()
    mod._state["src_hint"] = []
    mod._state["active_tree_ptr"] = mod._state["active_kind"] = None
    mod._state["active_path"] = None
    bpy.data.materials.remove(mat)
    me = ob.data
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(me)
    bpy.data.node_groups.remove(ng)


def _draw_both(mod, props, mat, ng, shader, geo):
    mp, gp = mat.node_tree.as_pointer(), ng.as_pointer()
    mod.drawing._record_editor(_Ctx(), shader, mp, mod.KIND_SHADER, [mp], props, mat.node_tree)
    mod.drawing._record_editor(_Ctx(), geo, gp, mod.KIND_GEO, [gp], props, ng)


def test_alternating_editors_do_not_mark_dirty(mod):
    props = _props()
    mat, ng, ob, shader, geo = _setup(mod)
    try:
        _draw_both(mod, props, mat, ng, shader, geo)
        mod._state["dirty"] = False
        for _ in range(5):
            _draw_both(mod, props, mat, ng, shader, geo)
        assert not mod._state["dirty"], "redrawing two editors re-queued every time"
        # A real change in one editor still does.
        props.preview_scope = "SELECTED"
        mod._state["dirty"] = False
        mat.node_tree.nodes["Noise"].select = not mat.node_tree.nodes["Noise"].select
        _draw_both(mod, props, mat, ng, shader, geo)
        assert mod._state["dirty"], "selection change in one editor not seen"
    finally:
        props.preview_scope = "ALL"
        _teardown(mod, mat, ng, ob)


def test_timer_rebuilds_every_editor(mod):
    props = _props()
    props.preview_geometry = True
    props.only_tex_shader = True
    mat, ng, ob, shader, geo = _setup(mod)
    orig = mod.queue.process_queue, mod.timer._live_space_ptrs
    mod.queue.process_queue = lambda p: False
    mod.timer._live_space_ptrs = lambda: {501, 502}
    try:
        _draw_both(mod, props, mat, ng, shader, geo)
        mod._state["dirty"] = True
        mod._timer()
        nodes = {(it["kind"], it["node"]) for it in mod._state["queue"]}
        assert (mod.KIND_SHADER, "Noise") in nodes, \
            "the shader editor was not rebuilt (only the last drawn editor was): %r" % nodes
        assert (mod.KIND_GEO, "Cube") in nodes, nodes
        geo_item = next(it for it in mod._state["queue"] if it["node"] == "Cube")
        assert geo_item["src"] == ob.name

        # Editing the material while the GN editor was drawn last still
        # re-queues the material's node.
        for it in mod._state["queue"]:
            mod._state["textures"][it["key"]] = object()
            mod._state["hashes"][it["key"]] = it["hash"]
        mod._state["queue"].clear()
        mod._state["queued_keys"].clear()
        mat.node_tree.nodes["Noise"].inputs["Scale"].default_value = 9.0
        mod._state["dirty"] = True
        mod._timer()
        assert [it["node"] for it in mod._state["queue"]] == ["Noise"], \
            [it["node"] for it in mod._state["queue"]]
    finally:
        mod.queue.process_queue, mod.timer._live_space_ptrs = orig
        props.preview_geometry = False
        _teardown(mod, mat, ng, ob)


def test_closed_editor_is_forgotten(mod):
    props = _props()
    mat, ng, ob, shader, geo = _setup(mod)
    orig = mod.timer._live_space_ptrs
    try:
        _draw_both(mod, props, mat, ng, shader, geo)
        mod.timer._live_space_ptrs = lambda: {502}
        targets = mod.timer._editor_targets()
        assert set(mod._state["editors"]) == {502}
        assert [t[1] for t in targets] == [mod.KIND_GEO], targets
    finally:
        mod.timer._live_space_ptrs = orig
        _teardown(mod, mat, ng, ob)


def test_prune_keeps_every_editors_thumbnails(mod):
    props = _props()
    mat, ng, ob, shader, geo = _setup(mod)
    prefs = bpy.context.preferences.addons.get(mod.__name__)
    orig_max = mod.queue._max_textures
    mod.queue._max_textures = lambda: 16
    try:
        _draw_both(mod, props, mat, ng, shader, geo)
        st = mod._state
        mine = []
        for tree in (mat.node_tree, ng):
            for i in range(10):
                k = mod.common._skey(tree, "n%d" % i, None)
                st["textures"][k] = object()
                mine.append(k)
        # Other, older thumbnails of a live tree that no editor shows.
        other = bpy.data.materials.new("NPV_ted_other")
        try:
            for i in range(10):
                k = mod.common._skey(other.node_tree, "o%d" % i, None)
                st["textures"][k] = object()
            mod._prune_cache()
            assert all(k in st["textures"] for k in mine), \
                "evicted thumbnails of an open editor"
        finally:
            bpy.data.materials.remove(other)
    finally:
        mod.queue._max_textures = orig_max
        _teardown(mod, mat, ng, ob)


def test_render_order_merges_every_editor(mod):
    st = mod._state
    mod._reset_cache()
    st["editors"].clear()
    st["editors"][1] = {"visible": {"a"}, "priority": set()}
    st["editors"][2] = {"visible": {"b"}, "priority": {"c"}}
    # draw_callback merges these into the sets _pop_next reads.
    vis, pri = set(), set()
    for e in st["editors"].values():
        vis |= e["visible"]
        pri |= e["priority"]
    st["visible"], st["priority"] = vis, pri
    st["queue"][:] = [{"key": k, "node": k} for k in ("x", "a", "b", "c")]
    st["queued_keys"] = {"x", "a", "b", "c"}
    order = [mod.queue._pop_next()["key"] for _ in range(4)]
    assert order == ["c", "a", "b", "x"], order
    st["editors"].clear()
    mod._reset_cache()
