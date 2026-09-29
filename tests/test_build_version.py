"""build_extension.py's release gate compares the version with git tags
(dist/ is no longer tracked). Plain Python; ``mod`` is unused."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_extension as be  # noqa: E402

SRC = '"version": (1, 4, 0),'


def _gate(tags, head="aaa", tagged=None, src=SRC, version="1.4.0"):
    saved = be.release_tags, be.tag_commit, be._git
    be.release_tags = lambda: tags
    be.tag_commit = lambda v: tagged
    be._git = lambda *a: head
    try:
        be.check_version(src, "node_preview", version)
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


def test_bl_info_must_match_manifest(mod):
    assert "!=" in _gate([], src='"version": (1, 3, 9),')


def test_no_git_fails(mod):
    assert "git tags" in _gate(None)


def test_real_checkout_has_release_tags(mod):
    tags = be.release_tags()
    if tags is None:
        print("  (skipped: git not available)")
        return
    assert all(t.count(".") == 2 for t in tags), tags


def test_extension_build_carries_the_note(mod):
    src = open(be.SRC, "r", encoding="utf-8").read()
    out = be.strip_bl_info(src)
    assert "bl_info = {" not in out, "bl_info kept"
    assert "# NOTE: This is the Blender Extension build." in out, \
        "extension note not inserted"


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
