# -*- coding: utf8 -*-
"""
ElevenLabs Scribe STT provider (stdlib only, per ADR-005).

POST https://api.elevenlabs.io/v1/speech-to-text  (multipart: file, model_id)
Header: xi-api-key. Response JSON has a top-level "text".
"""

import json
import uuid

from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

import xbmcaddon  # type: ignore

from lib.stt import STTProviderBase

_ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"
_DEFAULT_MODEL = "scribe_v1"


def _multipart(fields, file_field, filename, file_bytes, mime):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                boundary, name, value
            ).encode("utf-8")
        )
    parts.append(
        (
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"; "
            "filename=\"{}\"\r\nContent-Type: {}\r\n\r\n"
        ).format(boundary, file_field, filename, mime).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append("\r\n--{}--\r\n".format(boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary={}".format(boundary)


class ElevenLabsSTTProvider(STTProviderBase):
    def transcribe(self, audio_bytes):
        addon = xbmcaddon.Addon()
        api_key = addon.getSetting("elevenlabs_api_key").strip()
        if not api_key:
            raise RuntimeError("ElevenLabs API key is not configured")
        model = addon.getSetting("elevenlabs_model").strip() or _DEFAULT_MODEL

        fields = {"model_id": model, "tag_audio_events": "false"}
        lang = addon.getSetting("autocomplete_lang").strip()
        if lang:
            fields["language_code"] = lang
        body, ctype = _multipart(fields, "file", "voice.wav", audio_bytes, "audio/wav")
        req = Request(
            _ENDPOINT,
            data=body,
            headers={"xi-api-key": api_key, "Content-Type": ctype},
        )
        try:
            data = json.loads(urlopen(req, timeout=30).read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(
                "ElevenLabs HTTP {}: {}".format(
                    exc.code, exc.read().decode("utf-8", errors="replace")[:300]
                )
            )
        except URLError as exc:
            raise RuntimeError("ElevenLabs connection failed: {}".format(exc.reason))
        return (data.get("text") or "").strip()
