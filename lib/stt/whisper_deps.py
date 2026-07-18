# -*- coding: utf8 -*-
"""
Runtime dependency installer for the local Whisper STT path.

Installs faster-whisper and ctranslate2 to the Kodi userdata profile deps
directory at runtime — keeps compiled binaries out of the addon repo (C1, PF-006).
"""

import os
import subprocess
import sys

try:
    import xbmcvfs  # type: ignore

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

try:
    import xbmc  # type: ignore
except ImportError:
    xbmc = None  # type: ignore

_PACKAGES = ["faster-whisper", "ctranslate2"]


def _get_deps_dir():
    # type: () -> str
    if _KODI_AVAILABLE:
        return xbmcvfs.translatePath("special://profile/deps/")
    import tempfile

    return os.path.join(tempfile.gettempdir(), "kodi_voice_deps")


def _check_faster_whisper():
    # type: () -> bool
    """Return True if faster_whisper can be imported."""
    try:
        import importlib

        importlib.import_module("faster_whisper")
        return True
    except ImportError:
        return False


def _install_deps(deps_dir):
    # type: (str) -> bool
    """Run pip to install faster-whisper deps into deps_dir. Returns True on success."""
    if xbmc:
        xbmc.log("Whisper: installing deps to {}".format(deps_dir), xbmc.LOGINFO)
    try:
        os.makedirs(deps_dir)
    except OSError:
        pass  # already exists
    try:
        result = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--target", deps_dir] + _PACKAGES
        )
        return result == 0
    except Exception as exc:
        if xbmc:
            xbmc.log("Whisper: pip install failed: {}".format(exc), xbmc.LOGWARNING)
        return False


def ensure_deps():
    # type: () -> bool
    """Ensure faster-whisper and ctranslate2 are available.

    Returns True if deps are available (already installed or freshly installed).
    Returns False on failure — never raises.
    """
    deps_dir = _get_deps_dir()
    if deps_dir not in sys.path:
        sys.path.insert(0, deps_dir)
    if _check_faster_whisper():
        return True
    if not _install_deps(deps_dir):
        return False
    return _check_faster_whisper()
