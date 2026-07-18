# -*- coding: utf8 -*-
"""
Local Whisper STT provider.

Uses faster-whisper with the Tiny INT8 model for on-device speech recognition.
Deps and model are installed/downloaded on first use (P-018, P-019).
Falls back gracefully if initialization fails — the factory handles fallback (P-020).
"""

import os
import tempfile

from lib.stt import STTProviderBase

try:
    import xbmc  # type: ignore
except ImportError:
    xbmc = None  # type: ignore


class WhisperSTTProvider(STTProviderBase):
    """STT provider using local faster-whisper (Tiny INT8)."""

    def __init__(self):
        from lib.stt.whisper_deps import ensure_deps  # noqa: PLC0415
        from lib.stt.whisper_model import ensure_model  # noqa: PLC0415

        if not ensure_deps():
            raise RuntimeError("Failed to install faster-whisper dependencies.")
        self._model_dir = ensure_model()
        if not self._model_dir:
            raise RuntimeError("Failed to download Whisper Tiny model.")
        self._model = None  # Lazy: loaded after deps are on sys.path

    def _get_model(self):
        if self._model is None:
            import faster_whisper  # noqa: PLC0415

            self._model = faster_whisper.WhisperModel(
                self._model_dir, compute_type="int8"
            )
        return self._model

    def transcribe(self, audio_bytes):
        # type: (bytes) -> str
        """Transcribe WAV bytes and return a normalized title string."""
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with open(tmp_path, "wb") as f:
                f.write(audio_bytes)

            model = self._get_model()
            segments, _ = model.transcribe(tmp_path)
            raw_text = " ".join(s.text for s in segments).strip()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        from lib.stt.normalizer import normalize_title  # noqa: PLC0415

        return normalize_title(raw_text)
