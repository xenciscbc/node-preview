"""The add-on is split into modules (extension/*.py); __init__.py imports the
names it uses from them. A test that replaces such a name on ``mod``
(``mod.fn = fake``) only changes __init__'s copy: code inside the module that
defines it keeps calling the original, so the test would silently stop testing
what it means to. A function tests replace is therefore replaced on its own
module (``mod.<module>.fn = fake``) and nobody imports it by name: callers
elsewhere use ``<module>.fn(...)``. Plain Python checks; ``mod`` is the loaded
package."""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# mod.a = ...  /  mod.m.a = ...  /  mod.a, mod.m.b = ...  /  setattr(mod, "a", ...)
_ASSIGN = re.compile(r"^\s*((?:mod(?:\.\w+)+\s*,\s*)*mod(?:\.\w+)+)\s*=(?!=)",
                     re.MULTILINE)
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


def _name_imports(path):
    """(module, name) of every ``from .<module> import <name>`` in a file."""
    out = set()
    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            out |= {(node.module, a.name) for a in node.names}
    return out


def _patched_names():
    """(file, "name" or "module.name") of every ``mod.<...>`` a test or test
    helper assigns to."""
    out = set()
    for fn in sorted(os.listdir(HERE)):
        if not fn.endswith(".py"):
            continue
        text = open(os.path.join(HERE, fn), encoding="utf-8").read()
        for m in _ASSIGN.finditer(text):
            out |= {(fn, n) for n in re.findall(r"mod\.(\w+(?:\.\w+)*)", m.group(1))}
        out |= {(fn, n) for n in _SETATTR.findall(text)}
    return out


def test_patches_reach_every_caller(mod):
    subs = _submodules(mod)
    owners = {}
    for sub, path in subs.items():
        for name in _defined(path):
            owners[name] = sub
    imports = set()
    for path in list(subs.values()) + [mod.__file__]:
        imports |= _name_imports(path)
    bad = []
    for fn, dotted in sorted(_patched_names()):
        parts = dotted.split(".")
        if len(parts) == 1:
            if dotted in owners:
                bad.append("%s replaces mod.%s, defined in %s.py: replace "
                           "mod.%s.%s instead" % (fn, dotted, owners[dotted],
                                                  owners[dotted], dotted))
        elif len(parts) == 2 and parts[0] in subs:
            sub, name = parts
            if name not in _defined(subs[sub]):
                bad.append("%s replaces mod.%s, not defined in %s.py" % (fn, dotted, sub))
            if (sub, name) in imports:
                bad.append("%s replaces mod.%s, but it is imported by name "
                           "(from .%s import %s): call it as %s.%s(...)"
                           % (fn, dotted, sub, name, sub, name))
    assert not bad, "\n  ".join([""] + bad)


def test_patch_scan_sees_the_known_patches(mod):
    # Guards the regexes above: if they stop matching, the check passes vacuously.
    names = {n for _fn, n in _patched_names()}
    assert {"preview_scene._render_scene", "preview_scene._png_to_texture",
            "sources._kind_enabled", "_live_space_ptrs", "process_queue",
            "bpy"} <= names, sorted(names)


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
