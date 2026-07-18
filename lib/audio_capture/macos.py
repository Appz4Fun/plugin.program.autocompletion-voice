# -*- coding: utf8 -*-
import io
import threading
import wave
from typing import List, Optional  # noqa: F401 — used in type comments

from lib.audio_capture import AudioCaptureBase
from lib.audio_capture.silence import SilenceDetector

try:
    import sounddevice as _sounddevice  # type: ignore[import]

    _SOUNDDEVICE_AVAILABLE = True
except ImportError:
    _sounddevice = None  # type: ignore[assignment]
    _SOUNDDEVICE_AVAILABLE = False

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit signed
CHANNELS = 1  # mono
MAX_DURATION_DEFAULT = 30  # seconds


class MacOSAudioCapture(AudioCaptureBase):
    """Audio capture backend for macOS using the sounddevice library.

    Uses ``sounddevice.RawInputStream`` to accumulate PCM frames in-process,
    applies RMS-based silence detection, and wraps the result as a WAV file.
    A safety-cap timer is always armed as a backstop regardless of silence
    detection.
    """

    def __init__(self, max_duration=MAX_DURATION_DEFAULT):
        # type: (int) -> None
        self._max_duration = max_duration
        self._stream = None
        self._buffer = []  # type: List[bytes]
        self._lock = threading.Lock()
        self._timer = None  # type: Optional[threading.Timer]
        self._silence_detector = None  # type: Optional[SilenceDetector]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

    def _callback(self, indata, frames, time, status):
        """sounddevice RawInputStream callback — accumulates PCM bytes."""
        chunk = bytes(indata)
        with self._lock:
            self._buffer.append(chunk)
        if self._silence_detector and self._silence_detector.process(chunk):
            self._auto_stop()

    def _auto_stop(self):
        """Stop the stream — called by silence detection or safety-cap timer."""
        if self._stream is not None:
            try:
                if self._stream.active:
                    self._stream.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # AudioCaptureBase interface
    # ------------------------------------------------------------------

    def start_recording(self):
        """Open a RawInputStream and arm the safety-cap timer."""
        if not _SOUNDDEVICE_AVAILABLE:
            raise RuntimeError(
                "sounddevice is not installed. Install it with: pip install sounddevice"
            )

        self._buffer = []
        self._silence_detector = SilenceDetector()

        self._stream = _sounddevice.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

        self._timer = threading.Timer(self._max_duration, self._auto_stop)
        self._timer.daemon = True
        self._timer.start()

    def stop_recording(self):
        # type: () -> bytes
        """Stop the stream and return WAV bytes (16kHz/16-bit/mono)."""
        if self._timer:
            self._timer.cancel()
            self._timer = None

        if self._stream is not None:
            try:
                if self._stream.active:
                    self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        with self._lock:
            pcm_data = b"".join(self._buffer)
            self._buffer = []

        return self._wrap_wav(pcm_data)

    def is_available(self):
        # type: () -> bool
        """Return True if sounddevice is importable and an input device exists."""
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            return any(d["max_input_channels"] > 0 for d in devices)
        except (ImportError, Exception):
            return False

    def list_devices(self):
        # type: () -> List[str]
        """Return input device names from sounddevice.query_devices()."""
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            return [d["name"] for d in devices if d["max_input_channels"] > 0]
        except (ImportError, Exception):
            return []
