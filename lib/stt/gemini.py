# -*- coding: utf8 -*-
"""
Gemini Cloud STT provider.

Uses the Gemini REST API directly via stdlib urllib — no third-party SDK
required. Makes a single API call combining STT and title normalisation
(ADR-004): audio is transcribed and the result is returned as a properly
formatted movie or TV show title in one round-trip.

VENDOR DECISION: google-genai SDK dep tree contains native-code packages
(cffi, pydantic-core). Per ADR-005, vendoring is skipped; this module uses
urllib.request and http.client from the Python standard library only.
"""

import base64
import json

# Vendor path — inserted at module top level (P-004) before any vendor imports.
# Currently a no-op (no vendored packages) but required by convention.
import lib.vendor  # noqa: F401 — triggers sys.path setup

try:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
except ImportError:
    # Python 2 safety net — not expected in Kodi 19+
    from urllib2 import urlopen, Request, HTTPError, URLError  # type: ignore

try:
    import xbmc  # type: ignore
    import xbmcaddon  # type: ignore

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

from lib.stt import STTProviderBase

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

_STT_PROMPT = (
    "Transcribe exactly what the person says in this audio recording. "
    "The audio is low-quality (8kHz, from a TV remote microphone). "
    "Return ONLY the exact words you hear, nothing else. "
    "Do not guess or assume what they might be saying."
)

_STT_CANDIDATES_PROMPT = (
    "Transcribe exactly what the person says in this audio recording. "
    "The audio is low-quality (8kHz, from a TV remote microphone).\n\n"
    "You MUST return EXACTLY 100 lines. No more, no less.\n"
    "Line 1: Your best literal transcription of what they said.\n"
    "Lines 2-100: Real movie or TV show titles that sound similar to what "
    "you heard, or that the person might have been trying to say. Include "
    "variations in spelling, sequels, related titles, phonetically "
    "similar names, and titles from the same genre or franchise.\n\n"
    "Format: one option per line, no numbering, no extra text. "
    "Always return all 100 lines even if you are confident in line 1."
)


class GeminiSTTProvider(STTProviderBase):
    """STT provider that calls the Gemini REST API.

    Combines speech-to-text and title normalisation in a single API call
    (ADR-004). Uses the gemini-2.0-flash model (free-tier, multimodal).
    """

    def transcribe(self, audio_bytes):
        # type: (bytes) -> str
        """Transcribe WAV audio and return a normalised title string.

        Raises RuntimeError with a descriptive message on API failure.
        """
        addon = xbmcaddon.Addon()
        api_key = addon.getSetting("gemini_api_key")

        # Fallback: read API key directly from settings XML if getSetting
        # returns empty (Kodi may reject the setting during XML parsing
        # but the value is still in the file).
        if not api_key:
            api_key = self._read_api_key_from_file(addon)

        if not api_key:
            raise RuntimeError(
                "Gemini API key is not configured. "
                "Please set 'gemini_api_key' in the addon settings."
            )

        return self._call_gemini(audio_bytes, api_key)

    def transcribe_candidates(self, audio_bytes):
        # type: (bytes) -> list
        """Transcribe WAV audio and return up to 5 candidate title strings."""
        addon = xbmcaddon.Addon()
        api_key = addon.getSetting("gemini_api_key")
        if not api_key:
            api_key = self._read_api_key_from_file(addon)
        if not api_key:
            raise RuntimeError(
                "Gemini API key is not configured. "
                "Please set 'gemini_api_key' in the addon settings."
            )
        raw = self._call_gemini(audio_bytes, api_key, prompt=_STT_CANDIDATES_PROMPT)
        candidates = [line.strip() for line in raw.splitlines() if line.strip()]
        # Strip numbering like "1. " or "1) "
        cleaned = []
        for c in candidates:
            if len(c) > 3 and c[0].isdigit() and c[1] in ".)" and c[2] == " ":
                c = c[3:]
            elif len(c) > 4 and c[:2].isdigit() and c[2] in ".)" and c[3] == " ":
                c = c[4:]
            cleaned.append(c.strip())
        return cleaned[:100] if cleaned else [raw.strip()]

    @staticmethod
    def _read_api_key_from_file(addon):
        # type: (object) -> str
        """Read gemini_api_key directly from the settings XML file."""
        import os

        try:
            profile = xbmc.translatePath(addon.getAddonInfo("profile"))
            settings_path = os.path.join(profile, "settings.xml")
            with open(settings_path, "r") as f:
                for line in f:
                    if 'id="gemini_api_key"' in line:
                        # Extract value between > and <
                        start = line.index(">") + 1
                        end = line.index("</")
                        return line[start:end].strip()
        except Exception:
            pass
        return ""

    def _call_gemini(self, audio_bytes, api_key, prompt=None):
        # type: (bytes, str, str) -> str
        if prompt is None:
            prompt = _STT_PROMPT
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": audio_b64,
                            }
                        },
                        {"text": prompt},
                    ]
                }
            ]
        }

        url = "{}?key={}".format(_GEMINI_ENDPOINT, api_key)
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/json"})

        try:
            response = urlopen(req, timeout=30)
            data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(
                "Gemini API request failed with HTTP {}: {}".format(
                    exc.code,
                    exc.read().decode("utf-8", errors="replace"),
                )
            )
        except URLError as exc:
            raise RuntimeError("Gemini API connection failed: {}".format(exc.reason))

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            raise RuntimeError(
                "Unexpected Gemini API response structure: {}".format(exc)
            )
