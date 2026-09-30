"""Runs inside Blender (background mode). Do not run directly; use run_tests.py.

Loads the add-on under test from the path given after ``--`` (the extension
package directory, or a single ``.py`` file), registers it, then runs every
``test_*`` function found in ``tests/test_*.py``.
Each test function receives the loaded add-on module. Exits with status 1 if
any test fails so the caller can gate on it.
"""
import importlib.util
import os
import sys
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # for npv_testutil


def _load_addon(path):
    # A directory is imported as a package, so its modules can import each
    # other relatively (``from . import x``) as they do inside Blender.
    if os.path.isdir(path):
        spec = importlib.util.spec_from_file_location(
            "npv_under_test", os.path.join(path, "__init__.py"),
            submodule_search_locations=[path])
    else:
        spec = importlib.util.spec_from_file_location("npv_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _test_modules(pattern):
    for fn in sorted(os.listdir(HERE)):
        if fn.startswith("test_") and fn.endswith(".py"):
            spec = importlib.util.spec_from_file_location(fn[:-3], os.path.join(HERE, fn))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            for name in sorted(dir(m)):
                if name.startswith("test_") and (not pattern or pattern in name):
                    yield "%s::%s" % (fn[:-3], name), getattr(m, name)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        raise SystemExit("usage: blender -b --python blender_runner.py -- <addon> [filter]")
    addon_path = os.path.abspath(argv[0])
    pattern = argv[1] if len(argv) > 1 else ""

    print("Blender %s" % bpy.app.version_string)
    print("Add-on under test: %s" % addon_path)
    mod = _load_addon(addon_path)
    mod.register()

    failed = []
    total = 0
    try:
        for tid, fn in _test_modules(pattern):
            total += 1
            mod._cleanup_datablocks()
            try:
                fn(mod)
            except Exception:
                failed.append(tid)
                print("FAIL %s" % tid)
                traceback.print_exc()
            else:
                print("PASS %s" % tid)
    finally:
        mod.unregister()

    print("\n%d passed, %d failed" % (total - len(failed), len(failed)))
    sys.stdout.flush()
    if failed or total == 0:
        sys.exit(1)


main()
