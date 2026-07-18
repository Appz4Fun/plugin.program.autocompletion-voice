# -*- coding: utf8 -*-
"""
Standalone title normaliser for the local Whisper STT path.

Takes raw transcription text and calls Gemini to reformat it as a properly
cased and spelled movie or TV show title. If no API key is configured, the
raw text is returned unchanged (P-019 graceful fallback — never crash the
voice pipeline over a missing key).
"""

import json

try:
    from urllib.request import urlopen, Request
    from urllib.error import URLError
except ImportError:
    from urllib2 import urlopen, Request, URLError  # type: ignore

try:
    import xbmcaddon  # type: ignore

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)

_NORMALIZE_PROMPT = (
    "Normalize the following speech transcription as a properly formatted "
    "movie or TV show title with correct casing and spelling. "
    "Return only the title text, nothing else.\n\nTranscription: {}"
)


def normalize_title(raw_text):
    # type: (str) -> str
    """Normalize raw transcription text as a movie/TV show title.

    Calls Gemini to apply proper casing and spelling. If no API key is
    configured, returns raw_text unchanged (P-019 graceful fallback).
    """
    addon = xbmcaddon.Addon()
    api_key = addon.getSetting("gemini_api_key")

    if not api_key:
        return raw_text

    return _call_gemini_normalize(raw_text, api_key)


def _call_gemini_normalize(text, api_key):
    # type: (str, str) -> str
    payload = {"contents": [{"parts": [{"text": _NORMALIZE_PROMPT.format(text)}]}]}

    url = "{}?key={}".format(_GEMINI_ENDPOINT, api_key)
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"})

    try:
        response = urlopen(req, timeout=30)
        data = json.loads(response.read().decode("utf-8"))
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (URLError, KeyError, IndexError):
        # Graceful fallback: any failure returns raw text (P-019)
        return text
