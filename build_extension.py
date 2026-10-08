#!/usr/bin/env python3
"""Build the Blender Extension zip from the ``extension/`` package.

``extension/`` is the source: ``blender_manifest.toml`` (metadata and the
version) plus the add-on's Python files. They are zipped into
``dist/<id>-<ver>.zip``. ``dist/`` is not tracked in git: the zip is uploaded
to a GitHub Release (see README).

Release gate -- nothing is written unless every step passes:
  1. The manifest version is newer than every release tag (``vX.Y.Z``) in
     git. If ``v<version>`` itself already exists it must point at HEAD
     (rebuilding that release), not at another commit, and ``extension/``
     must have no uncommitted changes.
  2. The headless test suite (run_tests.py) passes against ``extension/``.
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


def check_version(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail("manifest version %r is not X.Y.Z" % version)
    tags = release_tags()
    if tags is None:
        fail("can't list git tags (run from a git checkout with git on PATH)")
    newest = max((t for t in tags if t != version), key=_vtuple, default=None)
    if newest and _vtuple(version) <= _vtuple(newest):
        fail("version %s is not newer than the release tag v%s "
             "(bump the manifest version)" % (version, newest))
    if version in tags and tag_commit(version) != _git("rev-parse", "HEAD"):
        fail("v%s is already tagged on another commit; bump the version "
             "(or check out v%s to rebuild that release)" % (version, version))
    if version in tags and _git("status", "--porcelain", "--", "extension"):
        # Rebuilding a release must give the release's files, not edits on
        # top of it. (A new version is built before it is committed.)
        fail("rebuilding the tagged release v%s, but extension/ has "
             "uncommitted changes" % version)
    if not tags:
        print("NOTE: no release tags found; run 'git fetch --tags' if this "
              "is a fresh clone")


def package_files(src_dir=EXT_DIR):
    """(path on disk, path in the zip) of every file that ships: everything
    under ``src_dir`` except caches and hidden files, sorted."""
    out = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs
                         if d != "__pycache__" and not d.startswith("."))
        for fn in sorted(files):
            if fn.startswith(".") or fn.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(root, fn)
            out.append((path, os.path.relpath(path, src_dir).replace(os.sep, "/")))
    return out


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
    manifest_text = open(MANIFEST, "r", encoding="utf-8").read()
    ext_id = read_field(manifest_text, "id", "node_preview")
    version = read_field(manifest_text, "version", "0.0.0")

    check_version(version)

    if skip_tests:
        print("WARNING: tests skipped (--no-test)")
    elif run_tests.run(EXT_DIR) != 0:
        fail("tests failed; nothing was written")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_zip = os.path.join(tmp, "%s-%s.zip" % (ext_id, version))
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for path, arc in package_files():
                z.write(path, arc)
        validate_zip(tmp_zip)

        os.makedirs(DIST_DIR, exist_ok=True)
        zip_path = os.path.join(DIST_DIR, os.path.basename(tmp_zip))
        shutil.copyfile(tmp_zip, zip_path)

    print("Built %s" % zip_path)


if __name__ == "__main__":
    main()
