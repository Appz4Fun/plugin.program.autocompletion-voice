# -*- coding: utf8 -*-
import io
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from typing import List, Optional  # noqa: F401 — used in type comments

try:
    import xbmc
    import xbmcaddon

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

from lib.audio_capture import AudioCaptureBase

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit signed
CHANNELS = 1  # mono
MAX_DURATION_DEFAULT = 30  # seconds


class CoreELECAudioCapture(AudioCaptureBase):
    """Audio capture backend for CoreELEC using arecord or ffmpeg via subprocess."""

    def __init__(self, max_duration=MAX_DURATION_DEFAULT):
        # type: (int) -> None
        self._max_duration = max_duration
        self._process = None  # type: Optional[subprocess.Popen]
        self._temp_file = None  # type: Optional[str]
        self._timer = None  # type: Optional[threading.Timer]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_device(self):
        # type: () -> str
        if _KODI_AVAILABLE:
            addon = xbmcaddon.Addon()
            device = addon.getSetting("audio_device")
            return device if device else "default"
        return "default"

    def _get_tool(self):
        # type: () -> str
        """Return 'arecord' or 'ffmpeg', probing in that order.

        Raises RuntimeError if neither tool is found on PATH.
        """
        if shutil.which("arecord"):
            return "arecord"
        if shutil.which("ffmpeg"):
            return "ffmpeg"
        raise RuntimeError(
            "No audio capture tool available: neither arecord nor ffmpeg found on PATH."
        )

    def _build_command(self, tool, output_path, device):
        # type: (str, str, str) -> List[str]
        if tool == "arecord":
            return [
                "arecord",
                "-D",
                device,
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
                output_path,
            ]
        # ffmpeg -f alsa fallback
        return [
            "ffmpeg",
            "-f",
            "alsa",
            "-i",
            device,
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-acodec",
            "pcm_s16le",
            "-f",
            "s16le",
            "-y",
            output_path,
        ]

    def _wrap_wav(self, pcm_data):
        # type: (bytes) -> bytes
        """Wrap raw PCM bytes in a WAV container and return the result."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm_data)
        return buf.getvalue()

    def _auto_terminate(self):
        """Called by the safety-cap timer; terminates the subprocess if still running."""
        if self._process and self._process.poll() is None:
            if _KODI_AVAILABLE:
                xbmc.log(
                    "Voice keyboard: max recording duration reached, auto-terminating",
                    xbmc.LOGWARNING,
                )
            self._process.terminate()

    # ------------------------------------------------------------------
    # AudioCaptureBase interface
    # ------------------------------------------------------------------

    def start_recording(self):
        """Launch the recording subprocess and arm the safety-cap timer."""
        tool = self._get_tool()
        device = self._get_device()

        tmp = tempfile.NamedTemporaryFile(suffix=".pcm", delete=False)
        self._temp_file = tmp.name
        tmp.close()

        cmd = self._build_command(tool, self._temp_file, device)

        if _KODI_AVAILABLE:
            xbmc.log(
                "Voice keyboard: starting recording with {}".format(tool),
                xbmc.LOGINFO,
            )

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._timer = threading.Timer(self._max_duration, self._auto_terminate)
        self._timer.daemon = True
        self._timer.start()

    def stop_recording(self):
        # type: () -> bytes
        """Terminate the recording subprocess and return WAV bytes."""
        if self._timer:
            self._timer.cancel()
            self._timer = None

        if self._process:
            self._process.terminate()
            self._process.wait()
            self._process = None

        pcm_data = b""
        if self._temp_file and os.path.exists(self._temp_file):
            with open(self._temp_file, "rb") as fh:
                pcm_data = fh.read()
            os.unlink(self._temp_file)
            self._temp_file = None

        return self._wrap_wav(pcm_data)

    def is_available(self):
        # type: () -> bool
        """Return True if a recording tool and an ALSA capture device are both present."""
        has_tool = bool(shutil.which("arecord") or shutil.which("ffmpeg"))
        if not has_tool:
            return False

        # Check for at least one ALSA capture device via arecord -l
        try:
            result = subprocess.run(
                ["arecord", "-l"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            if "card" in result.stdout.decode("utf-8", errors="replace").lower():
                return True
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass

        # Fallback: check /proc/asound/pcm for capture entries
        try:
            with open("/proc/asound/pcm", "r") as fh:
                content = fh.read()
            return "capture" in content.lower()
        except (IOError, OSError):
            pass

        return False

    def list_devices(self):
        # type: () -> List[str]
        """Return a list of ALSA capture device descriptions from arecord -l."""
        devices = []  # type: List[str]
        try:
            result = subprocess.run(
                ["arecord", "-l"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                if line.startswith("card "):
                    devices.append(line.strip())
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass
        return devices
