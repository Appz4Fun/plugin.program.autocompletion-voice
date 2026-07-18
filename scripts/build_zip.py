#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

"""Build the Kodi addon zip file.

The addon sources live at the repository root, but Kodi (and the Appz4Fun
repository builder) require the zip to contain a single top-level folder
named exactly like the addon id, so every file is archived under
plugin.program.autocompletion/.
"""

import argparse
import os
import xml.etree.ElementTree as ET
import zipfile

ADDON_ID = "plugin.program.autocompletion"

SKIP_DIRS = {".git", ".github", "scripts", "__pycache__", ".pytest_cache"}
SKIP_FILES = {".DS_Store", ".gitignore"}
SKIP_EXT = {".pyc"}
FILE_ATTR = 0o100644 << 16


def _parse_local_xml(path):
    """Parse trusted repo XML without enabling DTD/entity declarations."""
    with open(path, "rb") as fh:
        xml_bytes = fh.read()
    upper_xml = xml_bytes.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise ET.ParseError("DTD/entity declarations are not supported")
    return ET.ElementTree(ET.fromstring(xml_bytes))


def build_zip(addon_dir=".", output_dir="."):
    addon_xml_path = os.path.join(addon_dir, "addon.xml")
    if not os.path.isfile(addon_xml_path):
        raise SystemExit(
            "build_zip: addon.xml not found at {!r}; "
            "is the addon_dir argument correct?".format(addon_xml_path)
        )
    try:
        tree = _parse_local_xml(addon_xml_path)
    except (ET.ParseError, OSError) as exc:
        raise SystemExit(
            "build_zip: failed to parse {!r}: {}".format(addon_xml_path, exc)
        )
    root = tree.getroot()
    if root is None:
        raise SystemExit(
            "build_zip: {!r} has no root element".format(addon_xml_path)
        )
    if root.attrib.get("id") != ADDON_ID:
        raise SystemExit(
            "build_zip: addon.xml id {!r} does not match expected {!r}".format(
                root.attrib.get("id"), ADDON_ID
            )
        )
    version = root.attrib.get("version")
    if not version:
        raise SystemExit(
            "build_zip: {!r} has no `version` attribute on the root "
            "<addon> element; can't build a versioned zip name.".format(addon_xml_path)
        )
    zip_path = os.path.join(output_dir, "{}-{}.zip".format(ADDON_ID, version))

    os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for walk_root, dirs, files in os.walk(addon_dir):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                if f in SKIP_FILES or os.path.splitext(f)[1] in SKIP_EXT:
                    continue
                filepath = os.path.join(walk_root, f)
                rel = os.path.relpath(filepath, addon_dir).replace(os.sep, "/")
                info = zipfile.ZipInfo("{}/{}".format(ADDON_ID, rel))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = FILE_ATTR
                with open(filepath, "rb") as fh:
                    zf.writestr(info, fh.read())

    size = os.path.getsize(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        entries = len(zf.namelist())
    print("Created {} ({} entries, {:.0f} KB)".format(zip_path, entries, size / 1024))
    return zip_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=".", help="Directory to write zip to")
    args = parser.parse_args()
    build_zip(output_dir=args.output_dir)
