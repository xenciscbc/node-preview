"""v1.2.0: previews for nodes inside node groups, group nodes themselves,
and the user-adjustable thumbnail cache limit."""
import bpy

from npv_testutil import capture_renders, datablock_names, mean_rgb


def _clear_state(mod):
    for k in ("textures", "hashes", "queue", "queued_keys"):
        mod._state[k].clear()


def _mark_rendered(mod):
    st = mod._state
    for it in st["queue"]:
        st["textures"][it["key"]] = object()
        st["hashes"][it["key"]] = it["hash"]
    st["queue"].clear()
    st["queued_keys"].clear()


def _passthrough_group(name, inner=None):
    """Group with a Color input routed through a node named 'Inner Mix'
    (factor 0 -> passes A = the group input). If ``inner`` is given, the
    colour first goes through an instance of that group ('Inner Group')."""
    g = bpy.data.node_groups.new(name, "ShaderNodeTree")
    g.interface.new_socket("In", in_out="INPUT", socket_type="NodeSocketColor")
    g.interface.new_socket("Out", in_out="OUTPUT", socket_type="NodeSocketColor")
    gi = g.nodes.new("NodeGroupInput")
    go = g.nodes.new("NodeGroupOutput")
    src = gi.outputs[0]
    if inner is not None:
        ig = g.nodes.new("ShaderNodeGroup")
        ig.node_tree = inner
        ig.name = "Inner Group"
        g.links.new(src, ig.inputs[0])
        src = ig.outputs[0]
    mix = g.nodes.new("ShaderNodeMix")
    mix.name = "Inner Mix"
    mix.data_type = "RGBA"
    mix.inputs["Factor"].default_value = 0.0
    mix.inputs["B"].default_value = (0.0, 0.0, 1.0, 1.0)
    g.links.new(src, mix.inputs["A"])
    g.links.new(mix.outputs["Result"], go.inputs[0])
    return g


def _material_using(name, group, rgb):
    mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    c = nt.nodes.new("ShaderNodeRGB")
    c.name = "Colour"
    c.outputs[0].default_value = rgb
    inst = nt.nodes.new("ShaderNodeGroup")
    inst.node_tree = group
    inst.name = "Group"
    emit = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(c.outputs[0], inst.inputs[0])
    nt.links.new(inst.outputs[0], emit.inputs["Color"])
    nt.links.new(emit.outputs[0], out.inputs["Surface"])
    nt.nodes.active = inst
    return mat


def _render_node(mod, tree, path, node_name):
    """Queue ``tree`` (reached through ``path``) and render the item for
    ``node_name``; returns its mean colour."""
    props = bpy.context.scene.npv
    _clear_state(mod)
    mod.rebuild_queue(tree, mod.KIND_SHADER, props, path=path)
    items = [it for it in mod._state["queue"] if it["node"] == node_name]
    assert items, "%s not queued (queue: %r)" % (node_name, [i["node"] for i in mod._state["queue"]])
    mod._state["queue"][:] = items
    with capture_renders(mod) as shots:
        mod.process_queue(props)
    assert shots, "nothing rendered for %s" % node_name
    return mean_rgb(shots[0])


def test_node_inside_group_previews_with_outer_input(mod):
    g = _passthrough_group("NPV_test_g1")
    mat = _material_using("NPV_test_gm1", g, (0.9, 0.05, 0.05, 1.0))
    try:
        r, gg, b = _render_node(mod, g, [mat.node_tree, g], "Inner Mix")
        assert r > gg + 0.3 and r > b + 0.3, "inner node not red from outer input: %r" % ((r, gg, b),)
    finally:
        _clear_state(mod)
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(g)


def test_node_inside_nested_group(mod):
    inner = _passthrough_group("NPV_test_in")
    outer = _passthrough_group("NPV_test_out", inner=inner)
    mat = _material_using("NPV_test_gm2", outer, (0.05, 0.9, 0.05, 1.0))
    outer.nodes.active = outer.nodes["Inner Group"]
    try:
        r, g, b = _render_node(mod, inner, [mat.node_tree, outer, inner], "Inner Mix")
        assert g > r + 0.3 and g > b + 0.3, "nested inner node not green: %r" % ((r, g, b),)
    finally:
        _clear_state(mod)
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(outer)
        bpy.data.node_groups.remove(inner)


def test_group_render_leaves_no_copies(mod):
    g = _passthrough_group("NPV_test_g3")
    mat = _material_using("NPV_test_gm3", g, (0.9, 0.05, 0.05, 1.0))
    try:
        mod.ensure_preview_scene(32)
        snap = datablock_names()
        _render_node(mod, g, [mat.node_tree, g], "Inner Mix")
        assert datablock_names() == snap, "group preview leaked datablocks"
    finally:
        _clear_state(mod)
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(g)


def test_switching_material_context_requeues(mod):
    g = _passthrough_group("NPV_test_g4")
    m1 = _material_using("NPV_test_gm4a", g, (0.9, 0.05, 0.05, 1.0))
    m2 = _material_using("NPV_test_gm4b", g, (0.05, 0.05, 0.9, 1.0))
    props = bpy.context.scene.npv
    try:
        _clear_state(mod)
        mod.rebuild_queue(g, mod.KIND_SHADER, props, path=[m1.node_tree, g])
        assert mod._state["queue"], "nothing queued inside the group"
        _mark_rendered(mod)
        mod.rebuild_queue(g, mod.KIND_SHADER, props, path=[m1.node_tree, g])
        assert not mod._state["queue"], "same context re-queued"
        mod.rebuild_queue(g, mod.KIND_SHADER, props, path=[m2.node_tree, g])
        assert any(it["node"] == "Inner Mix" for it in mod._state["queue"]), \
            "entering the group from another material did not re-queue"
    finally:
        _clear_state(mod)
        bpy.data.materials.remove(m1)
        bpy.data.materials.remove(m2)
        bpy.data.node_groups.remove(g)


def test_group_node_itself_gets_a_preview(mod):
    g = _passthrough_group("NPV_test_g5")
    mat = _material_using("NPV_test_gm5", g, (0.9, 0.05, 0.05, 1.0))
    props = bpy.context.scene.npv
    assert props.only_tex_shader, "test expects the default 'Only Texture / Shader Nodes'"
    try:
        r, gg, b = _render_node(mod, mat.node_tree, [mat.node_tree], "Group")
        assert r > gg + 0.3 and r > b + 0.3, "group node preview not red: %r" % ((r, gg, b),)
    finally:
        _clear_state(mod)
        bpy.data.materials.remove(mat)
        bpy.data.node_groups.remove(g)


def test_geometry_node_inside_group(mod):
    inner = bpy.data.node_groups.new("NPV_test_gg", "GeometryNodeTree")
    inner.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    go = inner.nodes.new("NodeGroupOutput")
    grid = inner.nodes.new("GeometryNodeMeshGrid")
    grid.name = "Inner Grid"
    grid.inputs["Vertices X"].default_value = 4
    grid.inputs["Vertices Y"].default_value = 4
    tf = inner.nodes.new("GeometryNodeTransform")      # the group's own output
    tf.inputs["Scale"].default_value = (2, 2, 2)
    inner.links.new(grid.outputs[0], tf.inputs["Geometry"])
    inner.links.new(tf.outputs[0], go.inputs[0])
    root = bpy.data.node_groups.new("NPV_test_gr", "GeometryNodeTree")
    root.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    rgo = root.nodes.new("NodeGroupOutput")
    inst = root.nodes.new("GeometryNodeGroup")
    inst.node_tree = inner
    inst.name = "Group"
    cube = root.nodes.new("GeometryNodeMeshCube")      # something else at top level
    join = root.nodes.new("GeometryNodeJoinGeometry")
    root.links.new(inst.outputs[0], join.inputs[0])
    root.links.new(cube.outputs[0], join.inputs[0])
    root.links.new(join.outputs[0], rgo.inputs[0])
    root.nodes.active = inst
    me = bpy.data.meshes.new("NPV_test_gmesh")
    ob = bpy.data.objects.new("NPV_test_gobj", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.modifiers.new("GN", "NODES").node_group = root
    props = bpy.context.scene.npv
    counts = []
    orig = mod._render_scene

    def spy(scn):
        with bpy.context.temp_override(scene=scn, view_layer=scn.view_layers[0]):
            dg = bpy.context.evaluated_depsgraph_get()
            dg.update()
            for o in scn.collection.objects:
                if o.type == "MESH" and not o.name.startswith("NPV_preview"):
                    counts.append(len(o.evaluated_get(dg).data.vertices))
        raise RuntimeError("stop")

    try:
        _clear_state(mod)
        mod.rebuild_queue(inner, mod.KIND_GEO, props, path=[root, inner])
        items = [it for it in mod._state["queue"] if it["node"] == "Inner Grid"]
        assert items, "inner geometry node not queued: %r" % [i["node"] for i in mod._state["queue"]]
        mod._state["queue"][:] = items
        mod._render_scene = spy
        mod.process_queue(props)
    finally:
        mod._render_scene = orig
        _clear_state(mod)
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
        bpy.data.node_groups.remove(root)
        bpy.data.node_groups.remove(inner)
    assert counts == [16], "inner grid preview rendered %r verts, want [16]" % counts


def test_prune_never_evicts_the_active_tree(mod):
    # Evicting thumbnails the user is looking at would blank them and make
    # them re-render (then get evicted again) on every edit.
    ma = bpy.data.materials.new("NPV_test_act")
    mb = bpy.data.materials.new("NPV_test_other")
    pa, pb = ma.node_tree.as_pointer(), mb.node_tree.as_pointer()
    st = mod._state
    orig = mod._max_textures
    saved = st["active_tree_ptr"]
    mod._max_textures = lambda: 16
    try:
        st["active_tree_ptr"] = pa
        for i in range(20):
            for p in (pa, pb):
                key = "%d:N%d|" % (p, i)
                st["textures"][key] = object()
                st["hashes"][key] = "h"
                mod._touch(key)
        mod._prune_cache()
        active = [k for k in st["textures"] if k.startswith("%d:" % pa)]
        other = [k for k in st["textures"] if k.startswith("%d:" % pb)]
        assert len(active) == 20, "active editor's thumbnails evicted (%d left)" % len(active)
        assert not other, "other trees' thumbnails kept over the limit: %d" % len(other)
    finally:
        mod._max_textures = orig
        st["active_tree_ptr"] = saved
        _clear_state(mod)
        bpy.data.materials.remove(ma)
        bpy.data.materials.remove(mb)


def test_cache_limit_preference(mod):
    prop = mod.NPVAddonPrefs.bl_rna.properties["max_textures"]
    assert (prop.default, prop.hard_min, prop.hard_max) == (256, 16, 4096), \
        (prop.default, prop.hard_min, prop.hard_max)
    assert mod.NPVAddonPrefs.is_registered, "preferences class not registered"
    assert mod._max_textures() == 256, "fallback limit"
    mat = bpy.data.materials.new("NPV_test_lim")
    ptr = mat.node_tree.as_pointer()
    st = mod._state
    orig = mod._max_textures
    mod._max_textures = lambda: 16
    try:
        for i in range(40):
            key = "%d:N%d|" % (ptr, i)
            st["textures"][key] = object()
            st["hashes"][key] = "h"
            mod._touch(key)
        mod._prune_cache()
        assert len(st["textures"]) == 16, len(st["textures"])
    finally:
        mod._max_textures = orig
        _clear_state(mod)
        bpy.data.materials.remove(mat)
