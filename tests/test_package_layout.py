"""The add-on is split into modules (extension/*.py); __init__.py imports the
names it uses from them. A test that replaces such a name on ``mod``
(``mod.fn = fake``) only changes __init__'s copy: code inside the module that
defines it keeps calling the original, so the test would silently stop testing
what it means to. Plain Python checks; ``mod`` is the loaded package."""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# mod.a = ...  /  mod.a, mod.b = ...  /  setattr(mod, "a", ...)
_ASSIGN = re.compile(r"^\s*((?:mod\.\w+\s*,\s*)*mod\.\w+)\s*=(?!=)", re.MULTILINE)
_SETATTR = re.compile(r"setattr\(\s*mod\s*,\s*[\"'](\w+)[\"']")


def _submodules(mod):
    if not hasattr(mod, "__path__"):
        return {}
    pkg = mod.__path__[0]
    return {fn[:-3]: os.path.join(pkg, fn) for fn in sorted(os.listdir(pkg))
            if fn.endswith(".py") and fn != "__init__.py"}


def _defined(path):
    """Names a module defines at top level (def / class / assignment)."""
    out = set()
    for node in ast.parse(open(path, encoding="utf-8").read()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                out |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
    return out


def _patched_names():
    """(test file, name) of every ``mod.<name>`` a test assigns to."""
    out = set()
    for fn in sorted(os.listdir(HERE)):
        if not (fn.startswith("test_") and fn.endswith(".py")):
            continue
        text = open(os.path.join(HERE, fn), encoding="utf-8").read()
        for m in _ASSIGN.finditer(text):
            out |= {(fn, n) for n in re.findall(r"mod\.(\w+)", m.group(1))}
        out |= {(fn, n) for n in _SETATTR.findall(text)}
    return out


def test_patched_names_live_in_init(mod):
    owners = {}
    for sub, path in _submodules(mod).items():
        for name in _defined(path):
            owners[name] = sub
    bad = sorted("%s replaces mod.%s, defined in %s.py" % (fn, n, owners[n])
                 for fn, n in _patched_names() if n in owners)
    assert not bad, ("replace these on the defining module (mod.<module>.<name>) "
                     "and have callers look them up there:\n  " + "\n  ".join(bad))


def test_patch_scan_sees_the_known_patches(mod):
    # Guards the regexes above: if they stop matching, the check passes vacuously.
    names = {n for _fn, n in _patched_names()}
    assert {"_render_scene", "_live_space_ptrs", "process_queue", "bpy"} <= names, names


def test_imported_names_are_the_modules_objects(mod):
    # __init__ must hold the submodule's own object, not a copy or a stale one.
    subs = _submodules(mod)
    assert subs, "the add-on under test is not a package"
    for sub, path in subs.items():
        m = getattr(mod, sub)
        for name in _defined(path):
            if name in vars(mod):
                assert getattr(mod, name) is getattr(m, name), "%s.%s" % (sub, name)


def test_reload_list_covers_every_module_in_order(mod):
    # __init__ reloads _SUBMODULES in order when Blender reloads the add-on
    # after an update; a module missing from it would keep its old code, and
    # one reloaded before a module it imports from would bind the old names.
    subs = _submodules(mod)
    order = list(mod._SUBMODULES)
    assert sorted(order) == sorted(subs), (order, sorted(subs))
    for i, sub in enumerate(order):
        tree = ast.parse(open(subs[sub], encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                dep = node.module.split(".")[0]
                assert dep in order[:i], "%s imports .%s but is reloaded before it" % (sub, dep)
