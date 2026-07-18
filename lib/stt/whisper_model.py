# -*- coding: utf8 -*-
"""
Model manager for the local Whisper STT path.

Downloads the Whisper Tiny INT8 model from Hugging Face Hub on first use,
stores it in Kodi userdata profile/models/, shows a progress dialog (P-021).
"""

import os

try:
    from urllib.request import urlopen
    from urllib.error import URLError
except ImportError:
    from urllib2 import urlopen, URLError  # type: ignore

try:
    import xbmcvfs  # type: ignore

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

try:
    import xbmcgui  # type: ignore

    _GUI_AVAILABLE = True
except ImportError:
    _GUI_AVAILABLE = False

try:
    import xbmc  # type: ignore
except ImportError:
    xbmc = None  # type: ignore

_HF_REPO = "Systran/faster-whisper-tiny"
_HF_BASE_URL = "https://huggingface.co/{}/resolve/main/{{}}".format(_HF_REPO)

_MODEL_FILES = ["model.bin", "config.json", "tokenizer.json", "vocabulary.json"]

# Minimum expected file sizes for basic integrity check
_MIN_FILE_SIZES = {
    "model.bin": 37_000_000,  # ~37 MB
    "config.json": 100,
    "tokenizer.json": 1_000,
    "vocabulary.json": 1_000,
}


def _get_model_dir():
    # type: () -> str
    if _KODI_AVAILABLE:
        return xbmcvfs.translatePath("special://profile/models/whisper-tiny-int8/")
    import tempfile

    return os.path.join(tempfile.gettempdir(), "kodi_voice_models", "whisper-tiny-int8")


def _model_files_exist(model_dir):
    # type: (str) -> bool
    """Return True if all required model files exist and meet minimum size."""
    for fname in _MODEL_FILES:
        fpath = os.path.join(model_dir, fname)
        if not os.path.isfile(fpath):
            return False
        min_size = _MIN_FILE_SIZES.get(fname, 0)
        if min_size > 0 and os.path.getsize(fpath) < min_size:
            return False
    return True


def _download_file(url, dest_path, timeout=60):
    # type: (str, str, int) -> bool
    """Download url to dest_path. Returns True on success."""
    try:
        resp = urlopen(url, timeout=timeout)
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(32768)
                if not chunk:
                    break
                f.write(chunk)
        return True
    except (URLError, OSError, IOError):
        return False


def _download_model(model_dir):
    # type: (str) -> str
    """Download all model files to model_dir. Returns model_dir on success, '' on failure."""
    try:
        os.makedirs(model_dir)
    except OSError:
        pass  # already exists

    dialog = None
    if _GUI_AVAILABLE:
        dialog = xbmcgui.DialogProgress()
        dialog.create("Whisper STT", "Downloading Whisper model...")

    try:
        total_files = len(_MODEL_FILES)
        for i, fname in enumerate(_MODEL_FILES):
            if dialog and dialog.iscanceled():
                dialog.close()
                return ""

            pct = int(i * 100 / total_files)
            if dialog:
                dialog.update(pct, "Downloading {}...".format(fname))

            url = _HF_BASE_URL.format(fname)
            dest = os.path.join(model_dir, fname)

            if not _download_file(url, dest):
                if dialog:
                    dialog.close()
                if xbmc:
                    xbmc.log(
                        "Whisper: failed to download {}".format(fname),
                        xbmc.LOGWARNING,
                    )
                return ""

        if dialog:
            dialog.update(100, "Model ready.")
            dialog.close()
        return model_dir
    except Exception as exc:
        if dialog:
            try:
                dialog.close()
            except Exception:
                pass
        if xbmc:
            xbmc.log("Whisper: model download error: {}".format(exc), xbmc.LOGWARNING)
        return ""


def ensure_model():
    # type: () -> str
    """Ensure the Whisper Tiny INT8 model is available.

    Returns the model directory path on success, empty string on failure.
    """
    model_dir = _get_model_dir()
    if _model_files_exist(model_dir):
        return model_dir
    return _download_model(model_dir)
