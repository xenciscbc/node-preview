"""build_extension.py's release gate compares the version with git tags
(dist/ is no longer tracked). Plain Python; ``mod`` is unused."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_extension as be  # noqa: E402

def _gate(tags, head="aaa", tagged=None, version="1.4.0"):
    saved = be.release_tags, be.tag_commit, be._git
    be.release_tags = lambda: tags
    be.tag_commit = lambda v: tagged
    be._git = lambda *a: head
    try:
        be.check_version(version)
        return None
    except SystemExit as exc:
        return str(exc)
    finally:
        be.release_tags, be.tag_commit, be._git = saved


def test_newer_than_every_tag_passes(mod):
    assert _gate(["1.2.0", "1.3.0"]) is None
    assert _gate([]) is None


def test_not_newer_than_a_tag_fails(mod):
    assert "not newer" in _gate(["1.3.0", "1.4.1"])
    assert "not newer" in _gate(["1.10.0"]), "versions compared as strings"


def test_own_tag_must_be_head(mod):
    assert _gate(["1.3.0", "1.4.0"], head="aaa", tagged="aaa") is None
    assert "already tagged" in _gate(["1.3.0", "1.4.0"], head="aaa", tagged="bbb")


def test_version_must_be_x_y_z(mod):
    assert "X.Y.Z" in _gate([], version="1.4")
    assert "X.Y.Z" in _gate([], version="0.0.0-dev")


def test_no_git_fails(mod):
    assert "git tags" in _gate(None)


def test_real_checkout_has_release_tags(mod):
    tags = be.release_tags()
    if tags is None:
        print("  (skipped: git not available)")
        return
    assert all(t.count(".") == 2 for t in tags), tags


def test_zip_ships_the_package_without_caches(mod):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for rel in ("blender_manifest.toml", "__init__.py", "sub/x.py",
                    "__pycache__/a.pyc", "sub/__pycache__/b.pyc", "y.pyc",
                    ".hidden", ".git/config"):
            path = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        arcs = [arc for _path, arc in be.package_files(tmp)]
    assert arcs == ["__init__.py", "blender_manifest.toml", "sub/x.py"], arcs
    real = [arc for _path, arc in be.package_files()]
    assert "blender_manifest.toml" in real and "__init__.py" in real, real


def test_manifest_strings_fit_blenders_limits(mod):
    # 'extension validate' rejects a tagline or permission text over 64
    # characters, or one ending in punctuation (the files permission once
    # had 70 and only failed at the final build step).
    import re
    text = open(be.MANIFEST, encoding="utf-8").read()
    perms = text.split("[permissions]", 1)[1] if "[permissions]" in text else ""
    checks = [("tagline", be.read_field(text, "tagline", ""))]
    checks += re.findall(r'^(\w+)\s*=\s*"([^"]*)"', perms, re.MULTILINE)
    for key, val in checks:
        assert val, "%s is empty" % key
        assert len(val) <= 64, "%s is %d characters (max 64): %r" % (key, len(val), val)
        assert val[-1] not in ".!?,;:", "%s ends with punctuation: %r" % (key, val)
