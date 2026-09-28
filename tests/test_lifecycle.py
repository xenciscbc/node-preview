"""register / unregister and file-load behaviour (v1.1.2, v1.1.3 regressions)."""
import bpy


def _count(handlers, fn):
    return sum(1 for h in handlers if h is fn)


def _assert_registered(mod):
    h = bpy.app.handlers
    assert _count(h.depsgraph_update_post, mod._on_depsgraph) == 1, "depsgraph handler count != 1"
    assert _count(h.load_post, mod._on_load_post) == 1, "load_post handler count != 1"
    assert _count(h.save_pre, mod._on_save_pre) == 1, "save_pre handler count != 1"
    assert bpy.app.timers.is_registered(mod._timer), "preview timer not registered"
    assert hasattr(bpy.types.Scene, "npv"), "Scene.npv missing"


def test_register_unregister_cycle(mod):
    _assert_registered(mod)
    mod.unregister()
    h = bpy.app.handlers
    assert _count(h.depsgraph_update_post, mod._on_depsgraph) == 0
    assert _count(h.load_post, mod._on_load_post) == 0
    assert _count(h.save_pre, mod._on_save_pre) == 0
    assert not bpy.app.timers.is_registered(mod._timer), "timer left running"
    assert not hasattr(bpy.types.Scene, "npv"), "Scene.npv left behind"
    assert mod._state["draw_handle"] is None
    mod.register()
    _assert_registered(mod)


def test_file_load_keeps_addon_alive(mod):
    # Stale state that belongs to the "old" file.
    mod._state["textures"]["1:x|"] = object()
    mod._state["hashes"]["1:x|"] = "h"
    mod._state["queue"].append({"key": "1:x|"})
    mod._state["queued_keys"].add("1:x|")
    mod._state["rendering"] = True
    mod._state["dirty"] = False

    bpy.ops.wm.read_homefile(use_factory_startup=True)

    _assert_registered(mod)
    for k in ("textures", "hashes", "queue", "queued_keys"):
        assert not mod._state[k], "_state[%r] not reset on load" % k
    assert mod._state["rendering"] is False
    assert mod._state["dirty"] is True
    assert getattr(bpy.context.scene, "npv", None) is not None


def test_file_load_restores_missing_timer(mod):
    bpy.app.timers.unregister(mod._timer)
    bpy.ops.wm.read_homefile(use_factory_startup=True)
    assert bpy.app.timers.is_registered(mod._timer), "load_post did not restore the timer"
