"""v1.3.0: the help popup is split into pages and covers the v1.1.7-v1.3.0
features; every UI string is translated."""
import bpy


def _pages(mod, lang):
    return [pid for pid, _label in mod.TR[lang]["help_tabs"]]


def test_help_pages_match_between_languages(mod):
    en, zh = _pages(mod, "EN"), _pages(mod, "ZH")
    assert en == zh, (en, zh)
    for lang in ("EN", "ZH"):
        help_ = mod.TR[lang]["help"]
        assert set(help_) == set(en), (lang, sorted(help_))
        for pid in en:
            assert help_[pid], "%s help page %s is empty" % (lang, pid)


def test_help_operator_pages_match_tabs(mod):
    prop = bpy.ops.node.npv_help.get_rna_type().properties["page"]
    ids = [it.identifier for it in prop.enum_items]
    assert ids == _pages(mod, "EN"), ids


def test_help_covers_groups_and_cache(mod):
    pages = _pages(mod, "EN")
    assert "GROUPS" in pages and "CACHE" in pages, pages
    groups = " ".join(t for _k, t in mod.TR["EN"]["help"]["GROUPS"])
    cache = " ".join(t for _k, t in mod.TR["EN"]["help"]["CACHE"])
    assert "Compositor" in groups and "Inside Node Groups" in groups, \
        "compositor group toggle not documented"
    assert "Max Cached Thumbnails" in cache and "Preferences" in cache, cache


def test_every_string_is_translated(mod):
    en, zh = set(mod.TR["EN"]), set(mod.TR["ZH"])
    assert en == zh, ("missing in ZH", sorted(en - zh), "missing in EN", sorted(zh - en))
    for key in ("comp_groups", "pref_limit_fmt"):
        assert key in en, key


def test_pref_limit_text_formats(mod):
    for lang in ("EN", "ZH"):
        text = mod.TR[lang]["pref_limit_fmt"] % (8, 32, 128)
        assert "8" in text and "32" in text and "128" in text, text
