# -*- coding: utf8 -*-
import abc
import os
import sys
from typing import List

try:
    import xbmcaddon

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False


class AudioCaptureBase(abc.ABC):
    """Abstract interface for audio capture backends."""

    @abc.abstractmethod
    def start_recording(self):
        """Begin capturing audio in the background."""
        pass

    @abc.abstractmethod
    def stop_recording(self):
        # type: () -> bytes
        """Stop capturing and return WAV bytes."""
        pass

    @abc.abstractmethod
    def is_available(self):
        # type: () -> bool
        """Return True if this backend can record on the current system."""
        pass

    @abc.abstractmethod
    def list_devices(self):
        # type: () -> List[str]
        """Return a list of available capture device descriptions."""
        pass


def _get_audio_source():
    # type: () -> str
    """Read the audio_source setting. Returns 'alsa' (default) or 'ble'."""
    if _KODI_AVAILABLE:
        try:
            addon = xbmcaddon.Addon()
            source = addon.getSetting("audio_source")
            if source:
                return source
        except Exception:
            pass
    return ""


def _is_coreelec():
    # type: () -> bool
    """Detect CoreELEC by checking release files."""
    if os.path.exists("/etc/coreelec-release"):
        return True
    try:
        with open("/etc/os-release", "r") as f:
            for line in f:
                if line.strip() == 'ID="coreelec"' or line.strip() == "ID=coreelec":
                    return True
    except (IOError, OSError):
        pass
    return False


def get_audio_backend():
    # type: () -> AudioCaptureBase
    """Detect the platform and return an appropriate AudioCaptureBase instance.

    Raises RuntimeError if no supported platform is detected.
    """
    if _is_coreelec():
        audio_source = _get_audio_source()
        if audio_source == "ble":
            from lib.audio_capture.ble import BLEAudioCapture

            return BLEAudioCapture()

        from lib.audio_capture.coreelec import CoreELECAudioCapture

        return CoreELECAudioCapture()

    if sys.platform == "darwin":
        from lib.audio_capture.macos import MacOSAudioCapture

        return MacOSAudioCapture()

    raise RuntimeError(
        "No supported audio capture platform detected for '{}'. "
        "Supported platforms: CoreELEC, macOS (future).".format(sys.platform)
    )
