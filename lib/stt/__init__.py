# -*- coding: utf8 -*-
import abc


class STTProviderBase(abc.ABC):
    """Abstract interface for speech-to-text providers."""

    @abc.abstractmethod
    def transcribe(self, audio_bytes):
        # type: (bytes) -> str
        """Transcribe WAV audio bytes and return a normalized title string."""
        pass

    def transcribe_candidates(self, audio_bytes):
        # type: (bytes) -> list
        """Transcribe WAV audio and return multiple candidate strings.

        Default implementation wraps transcribe() in a single-item list.
        Providers can override to return multiple candidates.
        """
        return [self.transcribe(audio_bytes)]


def get_stt_provider():
    # type: () -> STTProviderBase
    """Return an STT provider based on the stt_provider addon setting.

    Raises NotImplementedError for providers that are not yet implemented.
    Raises ValueError for unrecognised provider names.
    """
    import xbmcaddon  # type: ignore  # noqa: PLC0415

    addon = xbmcaddon.Addon()
    provider = addon.getSetting("stt_provider") or "gemini"

    if provider == "gemini":
        from lib.stt.gemini import GeminiSTTProvider  # noqa: PLC0415

        return GeminiSTTProvider()

    if provider == "whisper":
        try:
            from lib.stt.whisper import WhisperSTTProvider  # noqa: PLC0415

            return WhisperSTTProvider()
        except Exception as exc:
            try:
                import xbmc  # type: ignore  # noqa: PLC0415

                xbmc.log(
                    "Whisper STT init failed: {}. Falling back to Gemini.".format(exc),
                    xbmc.LOGWARNING,
                )
            except ImportError:
                pass
            from lib.stt.gemini import GeminiSTTProvider  # noqa: PLC0415

            return GeminiSTTProvider()

    raise ValueError("Unknown STT provider: '{}'".format(provider))
