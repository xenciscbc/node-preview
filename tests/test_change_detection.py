"""Change detection: only changed nodes (and their downstream) re-render."""
import bpy


def _material():
    mat = bpy.data.materials.new("NPV_test_cd")
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.name = "Noise"
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.name = "Ramp"
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.name = "Wave"
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.name = "Out"
    bsdf = nt.nodes.new("ShaderNodeBsdfDiffuse")
    bsdf.name = "BSDF"
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Color"])
    nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
    return mat


def _hashes(mod, nt):
    memo = {}
    return {n.name: mod.upstream_hash(n, memo) for n in nt.nodes}


def test_upstream_hash_tracks_upstream_only(mod):
    mat = _material()
    try:
        nt = mat.node_tree
        before = _hashes(mod, nt)
        assert _hashes(mod, nt) == before, "hash not stable without changes"

        nt.nodes["Noise"].location.x += 300  # cosmetic
        nt.nodes["Noise"].select = not nt.nodes["Noise"].select
        assert _hashes(mod, nt) == before, "moving/selecting a node changed hashes"

        nt.nodes["Noise"].inputs["Scale"].default_value += 1.0
        after = _hashes(mod, nt)
        for name in ("Noise", "Ramp", "BSDF", "Out"):
            assert after[name] != before[name], "%s hash did not change" % name
        assert after["Wave"] == before["Wave"], "unrelated node hash changed"

        before = after
        nt.nodes["Ramp"].color_ramp.elements[0].color = (1, 0, 0, 1)
        after = _hashes(mod, nt)
        assert after["Ramp"] != before["Ramp"], "colour ramp edit not detected"
        assert after["Noise"] == before["Noise"], "upstream node hash changed"
    finally:
        bpy.data.materials.remove(mat)


def test_tree_signature(mod):
    mat = _material()
    try:
        nt = mat.node_tree
        sig = mod.tree_signature(nt)
        nt.nodes["Wave"].location.y -= 100
        assert mod.tree_signature(nt) == sig, "moving a node changed the signature"
        nt.nodes["Wave"].mute = True
        s2 = mod.tree_signature(nt)
        assert s2 != sig, "mute not detected"
        nt.links.new(nt.nodes["Wave"].outputs["Color"], nt.nodes["BSDF"].inputs["Color"])
        assert mod.tree_signature(nt) != s2, "relink not detected"
    finally:
        bpy.data.materials.remove(mat)


def test_rebuild_queue_only_requeues_changed(mod):
    mat = _material()
    props = bpy.context.scene.npv
    st = mod._state
    try:
        nt = mat.node_tree
        mod.rebuild_queue(nt, mod.KIND_SHADER, props)
        first = {it["node"] for it in st["queue"]}
        assert {"Noise", "Ramp", "Wave", "Out", "BSDF"} <= first, first

        # Pretend everything rendered.
        for it in st["queue"]:
            st["textures"][it["key"]] = object()
            st["hashes"][it["key"]] = it["hash"]
        st["queue"].clear()
        st["queued_keys"].clear()

        mod.rebuild_queue(nt, mod.KIND_SHADER, props)
        assert not st["queue"], "unchanged tree re-queued %r" % st["queue"]

        nt.nodes["Ramp"].color_ramp.elements[0].color = (0, 1, 0, 1)
        mod.rebuild_queue(nt, mod.KIND_SHADER, props)
        again = {it["node"] for it in st["queue"]}
        assert again == {"Ramp", "BSDF", "Out"}, again
    finally:
        for k in ("textures", "hashes", "queue", "queued_keys"):
            st[k].clear()
        bpy.data.materials.remove(mat)
