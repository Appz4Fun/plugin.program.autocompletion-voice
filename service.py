# -*- coding: utf8 -*-
# Voice keyboard service — runs at Kodi startup, manages voice input lifecycle

import json
import os
import subprocess
import sys
import threading
import time

# Ensure the addon directory is on sys.path so lazy imports from worker/GLib
# threads can resolve lib.* modules.
_addon_dir = os.path.dirname(os.path.abspath(__file__))
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

try:
    import xbmc
    import xbmcaddon
    import xbmcgui

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

from lib.audio_capture import get_audio_backend
from lib.rate_limiter import RateLimiter
from lib.stt import get_stt_provider
from lib.stt.gemini import GeminiSTTProvider  # noqa: F401 — pre-import for thread access

DEBOUNCE_SECONDS = 0.5


def bingie_search_on_top():
    """True only when Bingie's custom search window (1109 -> 11109) is the
    window the user is looking at."""
    try:
        return xbmcgui.getCurrentWindowId() in (1109, 11109) and not xbmc.getCondVisibility(
            "Player.HasVideo + Window.IsVisible(fullscreenvideo)"
        )
    except Exception:
        return False


class VoiceService(object):
    """Polls for voice activation, captures audio, and injects transcribed text."""

    _STATE_IDLE = "idle"
    _STATE_LISTENING = "listening"
    _STATE_PROCESSING = "processing"

    def __init__(self):
        self._state = self._STATE_IDLE
        self._lock = threading.Lock()
        self._last_activation_time = 0.0
        self._rate_limiter = RateLimiter()
        self._ble_backend = None
        self._stt_provider = None
        self._mic_button = None

    def _get_state(self):
        with self._lock:
            return self._state

    def _set_state(self, state):
        with self._lock:
            self._state = state

    def _check_activation(self):
        """Return True if the voice_keyboard_activate property is set (and clear it)."""
        now = time.time()
        if now - self._last_activation_time < DEBOUNCE_SECONDS:
            return False
        window = xbmcgui.Window(10000)
        prop = window.getProperty("voice_keyboard_activate")
        if prop:
            window.clearProperty("voice_keyboard_activate")
            self._last_activation_time = now
            return True
        return False

    def _bingie_search_visible(self):
        """Bingie skin's built-in search window (Custom_1109_BingieSearch).

        It has its own on-screen keyboard that writes to
        Skin.String(CustomSearchTerm) instead of using the virtual keyboard.
        """
        # The search window stays open UNDER fullscreen video after playing
        # something from it, and Window.IsVisible(1109) keeps returning True
        # there — so require it to be the topmost window, with no dialog
        # (e.g. the select dialog) over it.
        return xbmc.getSkinDir() == "skin.bingie" and bingie_search_on_top()

    @staticmethod
    def _set_bingie_search(text):
        """Fill Bingie's search box the way its own keys do, and refresh."""
        safe = text.replace("\\", "").replace('"', "").strip()
        xbmc.executebuiltin('Skin.SetString(CustomSearchTerm,"{}")'.format(safe))
        xbmc.executebuiltin("SetProperty(CustomSearch,1,home)")

    def _keyboard_visible(self):
        """Return True if a supported text-entry screen is on screen."""
        return bool(
            xbmc.getCondVisibility("Window.IsVisible(virtualkeyboard)")
        ) or self._bingie_search_visible()

    def _inject_text(self, text):
        """Inject text into the active keyboard.

        Bingie search: set the skin string its keys write to and trigger its
        refresh. Otherwise Input.SendText (done=False keeps keyboard open).
        """
        if self._bingie_search_visible() and not xbmc.getCondVisibility(
            "Window.IsVisible(virtualkeyboard)"
        ):
            self._set_bingie_search(text)
            return
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "Input.SendText",
                "params": {"text": text, "done": False},
                "id": 1,
            }
        )
        xbmc.executeJSONRPC(payload)

    def _commands_enabled(self):
        from lib import title_mapper

        return (
            xbmcaddon.Addon().getSetting("voice_commands") != "false"
            and title_mapper.enabled()
        )

    def _worker(self, mode="dictate"):
        """Worker thread: capture → transcribe → inject (dictate) or run a
        voice command (command). Always returns state to idle."""
        progress = None
        try:
            provider_name = xbmcaddon.Addon().getSetting("stt_provider") or "gemini"

            # Gate Gemini calls against free-tier rate limits before recording.
            if provider_name == "gemini":
                if not self._rate_limiter.can_call():
                    reason = self._rate_limiter.get_limit_reason()
                    if reason == "rpm":
                        msg = "Rate limit reached — try again in a moment"
                    else:
                        msg = "Daily rate limit reached — try again tomorrow"
                    xbmc.log(
                        "Voice keyboard rate limited: {}".format(reason),
                        xbmc.LOGWARNING,
                    )
                    xbmcgui.Dialog().notification(
                        "Voice Input", msg, xbmcgui.NOTIFICATION_WARNING, 5000
                    )
                    return

            # Persistent indicator: stays up while listening, flips to
            # "Transcribing..." after capture, closed in the finally below —
            # so there is never silent dead air between speech and result.
            progress = xbmcgui.DialogProgressBG()
            progress.create("Voice Input", "Listening...")
            backend = self._ble_backend if self._ble_backend else get_audio_backend()
            if backend is None:
                xbmcgui.Dialog().notification(
                    "Voice Input",
                    "No microphone detected",
                    xbmcgui.NOTIFICATION_ERROR,
                    5000,
                )
                return

            try:
                # BLE backend with shared btmon: audio is already being fed
                # externally, so use external_feed=True to skip spawning
                # a second btmon process.
                if backend is self._ble_backend:
                    # Usually already capturing since the button press
                    # (_on_ble_voice_start); only start if not.
                    if not getattr(backend, "_recording", False):
                        backend.start_recording(external_feed=True)
                else:
                    backend.start_recording()
                # BLE backend buffers asynchronously; wait for silence or timeout.
                if hasattr(backend, "wait_for_silence"):
                    backend.wait_for_silence()
                # Tell the remote the session is over the moment capture ends
                # (turns its LED off) — before the WAV build, which can take
                # a second on this hardware. Covers every stop reason.
                if getattr(self, "_ble_control_char_path", None):
                    from lib.audio_capture.ble import _send_mic_close

                    _send_mic_close(self._ble_control_char_path)
                wav_bytes = backend.stop_recording()
                # Debug aid: keep the last capture on disk so audio-quality
                # problems can be inspected (this is what STT actually gets).
                if wav_bytes:
                    try:
                        with open(
                            "/storage/.kodi/temp/voice_last.wav", "wb"
                        ) as dump:
                            dump.write(wav_bytes)
                    except OSError:
                        pass
            except Exception as exc:
                xbmc.log(
                    "Voice keyboard microphone error: {}".format(exc), xbmc.LOGWARNING
                )
                xbmcgui.Dialog().notification(
                    "Voice Input",
                    "Microphone error — check connection",
                    xbmcgui.NOTIFICATION_ERROR,
                    5000,
                )
                return

            # Too short to be speech (tap, or released instantly): a header-only
            # or few-ms WAV makes STT APIs fail with "corrupted audio".
            if wav_bytes and len(wav_bytes) < 44 + int(16000 * 2 * 0.3):
                xbmc.log(
                    "Voice keyboard: capture too short ({} bytes), ignoring".format(
                        len(wav_bytes)
                    ),
                    xbmc.LOGINFO,
                )
                wav_bytes = None
            if not wav_bytes:
                xbmcgui.Dialog().notification(
                    "Voice Input",
                    "No speech detected",
                    xbmcgui.NOTIFICATION_ERROR,
                    5000,
                )
                return

            self._set_state(self._STATE_PROCESSING)
            progress.update(75, message="Transcribing...")
            provider = self._stt_provider if self._stt_provider else get_stt_provider()
            from lib import title_mapper

            if mode == "command":
                from lib import voice_commands

                try:
                    raw_text = provider.transcribe(wav_bytes)
                except RuntimeError as exc:
                    xbmc.log("Voice command STT failed: {}".format(exc), xbmc.LOGWARNING)
                    xbmcgui.Dialog().notification(
                        "Voice", "Transcription failed", xbmcgui.NOTIFICATION_ERROR, 4000
                    )
                    return
                if provider_name == "gemini":
                    self._rate_limiter.record_call()
                xbmc.log("Voice command heard: {!r}".format(raw_text), xbmc.LOGINFO)
                if not raw_text.strip():
                    xbmcgui.Dialog().notification(
                        "Voice", "Heard nothing usable", xbmcgui.NOTIFICATION_WARNING, 3000
                    )
                    return
                progress.update(90, message='"{}"'.format(raw_text[:60]))
                plan = voice_commands.interpret(raw_text)
                progress.close()
                progress = None
                if plan is None:
                    xbmcgui.Dialog().notification(
                        "Voice", "Command LLM unavailable — check key", xbmcgui.NOTIFICATION_ERROR, 4000
                    )
                    return
                done = voice_commands.execute(plan, self._set_bingie_search)
                if done and plan.get("say"):
                    xbmcgui.Dialog().notification(
                        "Voice", plan["say"], xbmcgui.NOTIFICATION_INFO, 2500
                    )
                return

            use_llm = title_mapper.enabled()
            try:
                if use_llm:
                    # Fast literal transcript, then the LLM maps it to titles
                    # (skips Gemini's slow 100-line candidate prompt).
                    raw_text = provider.transcribe(wav_bytes)
                    xbmc.log(
                        "Voice keyboard STT raw: {!r}".format(raw_text), xbmc.LOGINFO
                    )
                    progress.update(90, message="Matching title...")
                    candidates = title_mapper.map_titles(raw_text) or [raw_text]
                else:
                    candidates = provider.transcribe_candidates(wav_bytes)
            except RuntimeError as exc:
                xbmc.log(
                    "Voice keyboard transcription failed: {}".format(exc),
                    xbmc.LOGWARNING,
                )
                xbmcgui.Dialog().notification(
                    "Voice Input",
                    "Transcription failed: {}".format(exc),
                    xbmcgui.NOTIFICATION_ERROR,
                    5000,
                )
                return

            if provider_name == "gemini":
                self._rate_limiter.record_call()

            xbmc.log(
                "Voice keyboard STT heard: {!r} (candidates: {})".format(
                    candidates[0] if candidates else "", candidates
                ),
                xbmc.LOGINFO,
            )
            # Close the busy indicator before showing results (or the picker).
            progress.close()
            progress = None

            if not any(c.strip() for c in candidates):
                xbmcgui.Dialog().notification(
                    "Voice Input",
                    "Heard nothing usable — try again",
                    xbmcgui.NOTIFICATION_ERROR,
                    5000,
                )
                return

            if len(candidates) <= 1:
                result = candidates[0] if candidates else ""
            else:
                idx = xbmcgui.Dialog().select("Voice Input — pick a title", candidates)
                if idx < 0:
                    return  # user cancelled
                result = candidates[idx]

            xbmc.log(
                "Voice keyboard: injecting text {!r}".format(result), xbmc.LOGINFO
            )
            self._inject_text(result)
        except Exception as exc:
            xbmc.log("Voice keyboard worker error: {}".format(exc), xbmc.LOGWARNING)
            xbmcgui.Dialog().notification(
                "Voice Input",
                "Unexpected error — check log",
                xbmcgui.NOTIFICATION_ERROR,
                5000,
            )
        finally:
            self._discard_capture()
            if progress is not None:
                try:
                    progress.close()
                except Exception:
                    pass
            self._set_state(self._STATE_IDLE)

    def _discard_capture(self):
        """Stop an early-started BLE capture that will not be used."""
        backend = self._ble_backend
        if backend is not None and getattr(backend, "_recording", False):
            try:
                backend.stop_recording()
            except Exception:
                pass

    def _send_ble_ack(self):
        """Send ack to control characteristic — fire-and-forget.

        Uses Popen (no wait) so it doesn't block the btmon reader thread.
        """
        if not (
            hasattr(self, "_ble_control_char_path") and self._ble_control_char_path
        ):
            return
        from lib.audio_capture.ble import _send_voice_ack_fast

        _send_voice_ack_fast(self._ble_control_char_path)

    def _on_ble_voice_start(self):
        """Callback from btmon reader when mic button is pressed.

        NOTE: This is called from the btmon reader thread. We only do
        thread-safe checks here (state, debounce) and defer the keyboard
        visibility check + worker launch to a new thread where Kodi API
        calls are safe.

        MIC_OPEN is only sent when a new session actually starts: every
        MIC_OPEN resets the remote's ~17s internal mic timer, so re-sending
        it on the clone firmware's ff retries needlessly extends the
        session (and how long the remote's LED stays lit).
        """
        now = time.time()
        elapsed = now - self._last_activation_time
        if elapsed < DEBOUNCE_SECONDS:
            return
        state = self._get_state()
        if state != self._STATE_IDLE:
            return
        # Open the remote's mic for this new session.
        self._send_ble_ack()
        # Set state immediately to prevent double-triggers
        self._set_state(self._STATE_LISTENING)
        self._last_activation_time = now
        # Start buffering audio NOW, in this thread. The worker first makes
        # Kodi API calls (window checks, settings, progress dialog) that can
        # take seconds when Kodi is busy; audio arriving before capture
        # started used to be dropped, clipping the first words.
        if self._ble_backend is not None:
            try:
                self._ble_backend.start_recording(external_feed=True)
            except Exception:
                pass

        if _KODI_AVAILABLE:
            xbmc.log(
                "Voice keyboard: mic button pressed, launching worker", xbmc.LOGINFO
            )

        def _check_and_run():
            if not self._keyboard_visible() and self._commands_enabled():
                xbmc.log("Voice keyboard: no text field, command mode", xbmc.LOGINFO)
                self._worker(mode="command")
                return
            if not self._keyboard_visible():
                if _KODI_AVAILABLE:
                    xbmc.log(
                        "Voice keyboard: keyboard not visible, ignoring",
                        xbmc.LOGINFO,
                    )
                # Close the remote's mic (LED off) and ignore the rest of this
                # stream instead of re-triggering on every audio packet.
                self._ignore_audio_until = time.time() + 3.0
                self._discard_capture()
                if getattr(self, "_ble_control_char_path", None):
                    try:
                        from lib.audio_capture.ble import _send_mic_close

                        _send_mic_close(self._ble_control_char_path)
                    except Exception:
                        pass
                self._set_state(self._STATE_IDLE)
                return
            self._worker()

        t = threading.Thread(target=_check_and_run)
        t.daemon = True
        t.start()

    def _start_ble_monitor(self):
        """Start BLE voice monitor if audio_source is BLE and remote is available.

        Sets up D-Bus notifications on status, audio, and control characteristics,
        then starts a single shared btmon subprocess that handles both mic button
        detection and audio data capture.
        """
        from lib.audio_capture import _get_audio_source

        if _get_audio_source() != "ble":
            return False
        try:
            from lib.audio_capture.ble import (
                BLEAudioCapture,
                _find_char_path,
                _start_notify,
                VOICE_STATUS_UUID,
                VOICE_DATA_UUID,
                VOICE_CONTROL_UUID,
            )

            self._ble_backend = BLEAudioCapture()
            self._stt_provider = get_stt_provider()

            # Find and enable notifications on status char (mic button)
            status_path = _find_char_path(VOICE_STATUS_UUID)
            if status_path is None:
                xbmc.log(
                    "Voice keyboard BLE: no status characteristic found",
                    xbmc.LOGWARNING,
                )
                return False
            _start_notify(status_path)

            # Find and enable notifications on audio data char
            audio_path = _find_char_path(VOICE_DATA_UUID)
            if audio_path:
                _start_notify(audio_path)

            # Find control char and pre-enable voice mode.
            # Writing 0x01 at startup tells the remote the host is ready
            # for voice data, so it enters voice mode immediately on button
            # press instead of doing a quick press/release cycle.
            control_path = _find_char_path(VOICE_CONTROL_UUID)
            self._ble_control_char_path = control_path
            if control_path:
                from lib.audio_capture.ble import _send_get_caps

                _send_get_caps(control_path)
                xbmc.log(
                    "Voice keyboard BLE: sent ATVV GET_CAPS handshake",
                    xbmc.LOGINFO,
                )

            xbmc.log(
                "Voice keyboard BLE: monitoring for mic button press", xbmc.LOGINFO
            )
            return True
        except Exception as exc:
            xbmc.log(
                "Voice keyboard BLE monitor failed: {}".format(exc),
                xbmc.LOGWARNING,
            )
            return False

    def _start_btmon_watcher(self):
        """Start a single btmon subprocess for both mic detection and audio capture.

        btmon captures raw HCI traffic below the D-Bus layer, bypassing
        Kodi's GLib main loop. This shared process handles:
        - Handle 0x0042 (status): mic button press/release detection
        - Handle 0x003f (audio): voice data capture → fed to BLE backend
        """
        self._btmon_proc = subprocess.Popen(
            ["btmon"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._btmon_thread = threading.Thread(
            target=self._btmon_reader_safe, daemon=True
        )
        self._btmon_thread.start()
        xbmc.log("Voice keyboard: btmon watcher thread started", xbmc.LOGINFO)

    def _btmon_reader_safe(self):
        """Wrapper that catches and logs exceptions from _btmon_reader."""
        try:
            self._btmon_reader()
        except Exception as exc:
            if _KODI_AVAILABLE:
                xbmc.log(
                    "Voice keyboard: btmon reader crashed: {}".format(exc),
                    xbmc.LOGWARNING,
                )

    def _btmon_reader(self):
        """Read btmon output, detect mic button and capture audio data.

        Unified reader for the single shared btmon process. Handles:
        - Handle 0x0042 + Data[1]: 08/ff → mic button press → _on_ble_voice_start()
        - Handle 0x0042 + Data[1]: 00 → button released → stop capture (push-to-talk)
        - Handle 0x003f + Data[N]: hex → audio data → BLE backend feed

        Also auto-triggers voice start when audio data appears on 0x003f
        while idle — this catches cases where the status notification (0x0042)
        was too brief for btmon to capture.
        """
        import re

        # btmon prints notification payloads two ways depending on version:
        #   old:  "Data[1]: ff"           (hex on the same line)
        #   5.8x: "Data[1]:" then hexdump lines "ff    ." (16 bytes/line)
        data_re = re.compile(r"Data\[(\d+)\]:\s*([0-9a-fA-F]*)\s*$")
        hexdump_re = re.compile(r"^\s+((?:[0-9a-fA-F]{2} ?){1,16})(?:\s{2,}|$)")
        handle_re = re.compile(r"Handle: (0x[0-9a-fA-F]{4})")
        cur_handle = None
        pending = None  # [handle, nbytes, hex_chars]
        # Blocking readline (daemon thread): select() on the buffered pipe
        # misses lines already pulled into Python's buffer.
        proc = self._btmon_proc
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()

            if pending is not None:
                m = hexdump_re.match(line)
                if m:
                    pending[2] += m.group(1).replace(" ", "")
                    if len(pending[2]) >= pending[1] * 2:
                        self._dispatch_notification(
                            pending[0], pending[2][: pending[1] * 2]
                        )
                        pending = None
                    continue
                # Payload ended early (unexpected format): use what we have.
                if pending[2]:
                    self._dispatch_notification(pending[0], pending[2])
                pending = None

            m = handle_re.search(line)
            if m:
                cur_handle = m.group(1).lower()
                continue
            m = data_re.search(line)
            if m and cur_handle:
                if m.group(2):
                    self._dispatch_notification(cur_handle, m.group(2))
                else:
                    pending = [cur_handle, int(m.group(1)), ""]
                continue
            if line and not line.startswith(" "):
                cur_handle = None  # new btmon packet

    def _dispatch_notification(self, handle, hexdata):
        """Act on one GATT notification payload from btmon."""
        hexdata = hexdata.lower()
        if handle == "0x0042":
            # Voice button (ATVV): 08 = START_SEARCH; clone firmwares (UR02)
            # retry with ff while waiting for MIC_OPEN.
            if hexdata in ("ff", "08"):
                if _KODI_AVAILABLE:
                    xbmc.log(
                        "Voice keyboard BLE: mic button detected via btmon",
                        xbmc.LOGINFO,
                    )
                self._on_ble_voice_start()
            # ATVV AUDIO_END / button release (0x00). Short press (<2s): keep
            # listening until silence; held >=2s: release stops capture. The
            # remote's own end-of-stream 00 also arrives past 2s.
            # Push-to-talk: button release (00) ends the capture at once.
            # A 00 within 0.35 s of the press is treated as firmware noise.
            elif hexdata == "00":
                backend = self._ble_backend
                if backend is not None and getattr(backend, "_recording", False):
                    if time.time() - self._last_activation_time >= 0.35:
                        backend.signal_stream_end()
                        if getattr(self, "_ble_control_char_path", None):
                            try:
                                from lib.audio_capture.ble import _send_mic_close

                                _send_mic_close(self._ble_control_char_path)
                            except Exception:
                                pass
        elif handle == "0x003f":
            # Audio arriving while idle: the status notification was missed.
            if self._get_state() == self._STATE_IDLE and time.time() >= getattr(
                self, "_ignore_audio_until", 0
            ):
                if _KODI_AVAILABLE:
                    xbmc.log(
                        "Voice keyboard BLE: audio data triggered voice start",
                        xbmc.LOGINFO,
                    )
                self._on_ble_voice_start()
            if self._ble_backend:
                self._ble_backend.feed_audio_data(hexdata)

    def run(self):
        """Main service loop. Polls for activation until Kodi requests abort."""
        monitor = xbmc.Monitor()
        xbmc.log("Voice keyboard service started")

        ble_active = self._start_ble_monitor()
        if ble_active:
            xbmc.log("Voice keyboard: BLE mic button activation enabled")
            self._start_btmon_watcher()

        ble_retry_counter = 0

        while not monitor.abortRequested():
            # Retry BLE monitor setup if it failed at startup (remote may
            # not have been connected yet).
            if not ble_active:
                ble_retry_counter += 1
                if ble_retry_counter >= 20:  # Every ~10 seconds
                    ble_retry_counter = 0
                    ble_active = self._start_ble_monitor()
                    if ble_active:
                        xbmc.log("Voice keyboard: BLE connected on retry", xbmc.LOGINFO)
                        self._start_btmon_watcher()

            # Poll window property for activation (works in both BLE and non-BLE
            # modes). In BLE mode this enables on-screen mic button activation
            # alongside the remote button via RunScript.
            if self._get_state() == self._STATE_IDLE:
                if self._check_activation():
                    mode = "dictate" if self._keyboard_visible() else (
                        "command" if self._commands_enabled() else None
                    )
                    if mode:
                        self._set_state(self._STATE_LISTENING)
                        t = threading.Thread(target=self._worker, args=(mode,))
                        t.daemon = True
                        t.start()
            monitor.waitForAbort(0.5)

        if hasattr(self, "_btmon_proc") and self._btmon_proc:
            self._btmon_proc.terminate()
            try:
                self._btmon_proc.wait(timeout=2)
            except Exception:
                self._btmon_proc.kill()
        xbmc.log("Voice keyboard service stopped")


if _KODI_AVAILABLE:
    VoiceService().run()
