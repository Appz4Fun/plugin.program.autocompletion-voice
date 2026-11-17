# -*- coding: utf8 -*-
"""
Optional LLM pass: map a raw voice transcript to real movie / TV titles.

Presets call each vendor's official REST API directly (stdlib only, ADR-005).
The user supplies only an API key; the model ID can be overridden in settings.
Any failure returns None so the caller falls back to the raw transcript.
"""

import json
import re

from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

import xbmc  # type: ignore
import xbmcaddon  # type: ignore

PRESETS = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-opus-5-5",
    },
    "openai": {
        "url": "https://api.openai.com/v1/responses",
        "model": "gpt-6.1-sol",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-4-argon",
    },
}

_PROMPT = (
    "A viewer said this into a TV remote microphone to search for something "
    "to watch. The speech-to-text may contain mishearings.\n\n"
    "Transcript: \"{}\"\n\n"
    "Reply with up to 5 real movie or TV show titles they most likely meant, "
    "best match first, one per line, correctly spelled and capitalised. "
    "If it is clearly not a title (e.g. an actor or a genre), put the "
    "corrected search phrase first. No numbering, no extra text."
)


def _post(url, headers, payload, timeout):
    headers = dict(headers, **{"Content-Type": "application/json"})
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        return json.loads(urlopen(req, timeout=timeout).read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(
            "HTTP {}: {}".format(
                exc.code, exc.read().decode("utf-8", errors="replace")[:300]
            )
        )
    except URLError as exc:
        raise RuntimeError("connection failed: {}".format(exc.reason))


def _call_anthropic(prompt, key, model, timeout):
    # Opus 5.5: thinking is always on; effort "low" keeps latency down.
    # No temperature (rejected), no assistant prefill.
    data = _post(
        PRESETS["anthropic"]["url"],
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
        {
            "model": model,
            "max_tokens": 1024,
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout,
    )
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("model refused")
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )


def _call_openai(prompt, key, model, timeout):
    data = _post(
        PRESETS["openai"]["url"],
        {"Authorization": "Bearer {}".format(key)},
        {"model": model, "input": prompt, "reasoning": {"effort": "medium"}},
        timeout,
    )
    out = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    out.append(c.get("text", ""))
    return "".join(out)


def _call_gemini(prompt, key, model, timeout):
    data = _post(
        PRESETS["gemini"]["url"].format(model=model),
        {"x-goog-api-key": key},
        {"contents": [{"parts": [{"text": prompt}]}]},
        timeout,
    )
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


_CALLERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gemini": _call_gemini,
}


def enabled():
    return (xbmcaddon.Addon().getSetting("llm_provider") or "off") in _CALLERS


def _resolve():
    """Return (provider, key, model) for the configured LLM, or None."""
    addon = xbmcaddon.Addon()
    provider = addon.getSetting("llm_provider") or "off"
    if provider not in _CALLERS:
        return None
    key = (
        addon.getSetting("llm_key_{}".format(provider)).strip()
        or addon.getSetting("llm_api_key").strip()
    )
    if not key and provider == "gemini":
        key = addon.getSetting("gemini_api_key").strip()
    if not key:
        xbmc.log("Voice keyboard LLM: no API key for {}".format(provider), xbmc.LOGWARNING)
        return None
    model = addon.getSetting("llm_model").strip() or PRESETS[provider]["model"]
    return provider, key, model


def call_llm(prompt, timeout=20):
    """Send one prompt to the configured LLM. Returns text, or None on failure."""
    cfg = _resolve()
    if cfg is None:
        return None
    provider, key, model = cfg
    try:
        return _CALLERS[provider](prompt, key, model, timeout)
    except Exception as exc:
        xbmc.log(
            "Voice keyboard LLM ({} {}) failed: {}".format(provider, model, exc),
            xbmc.LOGWARNING,
        )
        return None


def map_titles(transcript):
    """Return a list of candidate titles, or None to use the raw transcript."""
    if not transcript.strip():
        return None
    text = call_llm(_PROMPT.format(transcript))
    if text is None:
        return None
    titles = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*\u2022]|\d{1,2}[.)])\s+", "", line).strip().strip('"')
        if line and line not in titles:
            titles.append(line)
    return titles[:5] or None
