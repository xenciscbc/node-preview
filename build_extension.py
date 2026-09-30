#!/usr/bin/env python3
"""Build the Blender Extension package from the single-file add-on source.

Reads ``node_preview_thumbnails.py`` (the legacy add-on, which carries a
``bl_info`` block and the version), strips ``bl_info`` to produce
``extension/__init__.py`` (untracked; extensions use ``blender_manifest.toml``),
and zips the manifest + entry file into ``dist/<id>-<ver>.zip``. ``dist/`` is
not tracked in git: the zip is uploaded to a GitHub Release (see README).

Release gate -- nothing is written unless every step passes:
  1. bl_info version == manifest version, and it is newer than every release
     tag (``vX.Y.Z``) in git. If ``v<version>`` itself already exists it must
     point at HEAD (rebuilding that release), not at another commit.
  2. The headless test suite (run_tests.py) passes against the stripped
     extension code, i.e. exactly what ships.
  3. ``blender --command extension validate`` accepts the zip.

Usage:  python build_extension.py [--no-test]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import run_tests

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "node_preview_thumbnails.py")
EXT_DIR = os.path.join(HERE, "extension")
DIST_DIR = os.path.join(HERE, "dist")
MANIFEST = os.path.join(EXT_DIR, "blender_manifest.toml")


def read_field(manifest_text, field, default):
    m = re.search(r'^%s\s*=\s*"([^"]+)"' % field, manifest_text, re.MULTILINE)
    return m.group(1) if m else default


def fail(msg):
    raise SystemExit("BUILD STOPPED: " + msg)


def _vtuple(s):
    return tuple(int(x) for x in s.split("."))


def _git(*args):
    """Output of a git command in this checkout, or None if git fails."""
    try:
        r = subprocess.run(["git"] + list(args), cwd=HERE, capture_output=True,
                           text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return r.stdout.strip()


def release_tags():
    """Versions of the ``vX.Y.Z`` tags in this checkout, or None without git."""
    out = _git("tag", "--list", "v*")
    if out is None:
        return None
    return [m.group(1) for m in (re.fullmatch(r"v(\d+\.\d+\.\d+)", t)
                                 for t in out.split()) if m]


def tag_commit(version):
    return _git("rev-parse", "--verify", "--quiet", "v%s^{commit}" % version)


def check_version(src, ext_id, version):
    m = re.search(r'"version"\s*:\s*\((\d+),\s*(\d+),\s*(\d+)\)', src)
    if not m:
        fail("no version in bl_info")
    bl_version = ".".join(m.groups())
    if bl_version != version:
        fail("bl_info version %s != manifest version %s" % (bl_version, version))
    tags = release_tags()
    if tags is None:
        fail("can't list git tags (run from a git checkout with git on PATH)")
    newest = max((t for t in tags if t != version), key=_vtuple, default=None)
    if newest and _vtuple(version) <= _vtuple(newest):
        fail("version %s is not newer than the release tag v%s "
             "(bump bl_info and the manifest)" % (version, newest))
    if version in tags and tag_commit(version) != _git("rev-parse", "HEAD"):
        fail("v%s is already tagged on another commit; bump the version "
             "(or check out v%s to rebuild that release)" % (version, version))
    if not tags:
        print("NOTE: no release tags found; run 'git fetch --tags' if this "
              "is a fresh clone")


def strip_bl_info(src):
    # Replace the bl_info = { ... } block with a note: extensions use the
    # manifest instead.
    note = ("\n# NOTE: This is the Blender Extension build. Metadata lives in\n"
            "# blender_manifest.toml (no bl_info needed for extensions).\n")
    stripped, n = re.subn(r"\nbl_info\s*=\s*\{.*?\n\}\n", lambda _m: note, src,
                          count=1, flags=re.DOTALL)
    if n != 1:
        fail("could not find a bl_info block to strip")
    return stripped


def validate_zip(zip_path):
    r = subprocess.run([run_tests.find_blender(), "--factory-startup", "--command",
                        "extension", "validate", zip_path],
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "Success" not in out:
        print(out)
        fail("extension validate rejected the zip")


def main():
    skip_tests = "--no-test" in sys.argv[1:]
    src = open(SRC, "r", encoding="utf-8").read()
    manifest_text = open(MANIFEST, "r", encoding="utf-8").read()
    ext_id = read_field(manifest_text, "id", "node_preview")
    version = read_field(manifest_text, "version", "0.0.0")

    check_version(src, ext_id, version)
    stripped = strip_bl_info(src)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_init = os.path.join(tmp, "__init__.py")
        open(tmp_init, "w", encoding="utf-8").write(stripped)

        if skip_tests:
            print("WARNING: tests skipped (--no-test)")
        elif run_tests.run(tmp_init) != 0:
            fail("tests failed; nothing was written")

        tmp_zip = os.path.join(tmp, "%s-%s.zip" % (ext_id, version))
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(MANIFEST, "blender_manifest.toml")
            z.write(tmp_init, "__init__.py")
        validate_zip(tmp_zip)

        os.makedirs(EXT_DIR, exist_ok=True)
        os.makedirs(DIST_DIR, exist_ok=True)
        open(os.path.join(EXT_DIR, "__init__.py"), "w", encoding="utf-8").write(stripped)
        zip_path = os.path.join(DIST_DIR, os.path.basename(tmp_zip))
        shutil.copyfile(tmp_zip, zip_path)

    print("Built %s" % zip_path)


if __name__ == "__main__":
    main()
