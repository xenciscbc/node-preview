#!/usr/bin/env python3
"""Build the Blender Extension package from the single-file add-on source.

Reads ``node_preview_thumbnails.py`` (the legacy add-on, which carries a
``bl_info`` block and the version), strips ``bl_info`` to produce
``extension/__init__.py`` (extensions use ``blender_manifest.toml`` instead),
and zips the manifest + entry file into ``dist/node_preview_thumbnails-<ver>.zip``.

Usage:  python build_extension.py
"""
import os
import re
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "node_preview_thumbnails.py")
EXT_DIR = os.path.join(HERE, "extension")
DIST_DIR = os.path.join(HERE, "dist")
MANIFEST = os.path.join(EXT_DIR, "blender_manifest.toml")


def read_field(manifest_text, field, default):
    m = re.search(r'^%s\s*=\s*"([^"]+)"' % field, manifest_text, re.MULTILINE)
    return m.group(1) if m else default


def main():
    src = open(SRC, "r", encoding="utf-8").read()

    # Strip the bl_info = { ... } block: extensions use the manifest instead.
    stripped, n = re.subn(r"\nbl_info\s*=\s*\{.*?\n\}\n", "\n", src, count=1,
                          flags=re.DOTALL)
    if n != 1:
        raise SystemExit("Could not find a bl_info block to strip.")
    note = ("\n# NOTE: This is the Blender Extension build. Metadata lives in\n"
            "# blender_manifest.toml (no bl_info needed for extensions).\n")
    stripped = stripped.replace('"""\n\nimport os', '"""\n' + note + "\nimport os", 1)

    os.makedirs(EXT_DIR, exist_ok=True)
    os.makedirs(DIST_DIR, exist_ok=True)
    open(os.path.join(EXT_DIR, "__init__.py"), "w", encoding="utf-8").write(stripped)

    manifest_text = open(MANIFEST, "r", encoding="utf-8").read()
    ext_id = read_field(manifest_text, "id", "node_preview")
    version = read_field(manifest_text, "version", "0.0.0")
    zip_path = os.path.join(DIST_DIR, "%s-%s.zip" % (ext_id, version))
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(MANIFEST, "blender_manifest.toml")
        z.write(os.path.join(EXT_DIR, "__init__.py"), "__init__.py")

    print("Built %s" % zip_path)


if __name__ == "__main__":
    main()
