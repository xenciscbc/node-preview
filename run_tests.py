#!/usr/bin/env python3
"""Run the headless test suite in Blender.

Usage:  python run_tests.py [addon.py] [filter]

``addon.py`` defaults to ``node_preview_thumbnails.py``. The Blender binary is
taken from the BLENDER environment variable, falling back to the default
Blender 5.2 install location on Windows, then ``blender`` on PATH.
Exits non-zero if any test fails.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ADDON = os.path.join(HERE, "node_preview_thumbnails.py")
RUNNER = os.path.join(HERE, "tests", "blender_runner.py")
WIN_DEFAULT = r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"


def find_blender():
    exe = os.environ.get("BLENDER")
    if exe:
        return exe
    if os.path.exists(WIN_DEFAULT):
        return WIN_DEFAULT
    exe = shutil.which("blender")
    if exe:
        return exe
    raise SystemExit("Blender not found; set the BLENDER environment variable.")


def run(addon=DEFAULT_ADDON, pattern=""):
    cmd = [find_blender(), "-b", "--factory-startup", "--python-exit-code", "1",
           "--python", RUNNER, "--", os.path.abspath(addon)]
    if pattern:
        cmd.append(pattern)
    return subprocess.call(cmd)


def main():
    args = sys.argv[1:]
    addon = args[0] if args else DEFAULT_ADDON
    pattern = args[1] if len(args) > 1 else ""
    sys.exit(run(addon, pattern))


if __name__ == "__main__":
    main()
