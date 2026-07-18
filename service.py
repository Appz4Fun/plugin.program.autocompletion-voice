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

    def _keyboard_visible(self):
        """Return True if the virtual keyboard is currently on screen."""
        return bool(xbmc.getCondVisibility("Window.IsVisible(virtualkeyboard)"))

    def _inject_text(self, text):
        """Inject text via Input.SendText JSON-RPC. done=False keeps keyboard open."""
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "Input.SendText",
                "params": {"text": text, "done": False},
                "id": 1,
            }
        )
        xbmc.executeJSONRPC(payload)

    def _worker(self):
        """Worker thread: capture → transcribe → inject. Always returns state to idle."""
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

            xbmcgui.Dialog().notification(
                "Voice Input", "Listening...", xbmcgui.NOTIFICATION_INFO, 3000
            )
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
                    backend.start_recording(external_feed=True)
                else:
                    backend.start_recording()
                # BLE backend buffers asynchronously; wait for silence or timeout.
                if hasattr(backend, "wait_for_silence"):
                    backend.wait_for_silence()
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

            if not wav_bytes:
                xbmcgui.Dialog().notification(
                    "Voice Input",
                    "No speech detected",
                    xbmcgui.NOTIFICATION_ERROR,
                    5000,
                )
                return

            self._set_state(self._STATE_PROCESSING)
            xbmcgui.Dialog().notification(
                "Voice Input", "Processing...", xbmcgui.NOTIFICATION_INFO, 3000
            )
            provider = self._stt_provider if self._stt_provider else get_stt_provider()
            try:
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
            self._set_state(self._STATE_IDLE)

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

        The ack is always sent regardless of debounce/state — the remote
        needs the ack to keep the voice session alive.
        """
        # Always ack the mic press to keep the remote's voice session alive,
        # even if we're debouncing or already in a voice session.
        self._send_ble_ack()

        now = time.time()
        elapsed = now - self._last_activation_time
        if elapsed < DEBOUNCE_SECONDS:
            return
        state = self._get_state()
        if state != self._STATE_IDLE:
            return
        # Set state immediately to prevent double-triggers
        self._set_state(self._STATE_LISTENING)
        self._last_activation_time = now

        if _KODI_AVAILABLE:
            xbmc.log(
                "Voice keyboard: mic button pressed, launching worker", xbmc.LOGINFO
            )

        def _check_and_run():
            if not self._keyboard_visible():
                if _KODI_AVAILABLE:
                    xbmc.log(
                        "Voice keyboard: keyboard not visible, ignoring",
                        xbmc.LOGINFO,
                    )
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
        - Handle 0x0042 + Data[1]: ff → mic button press → _on_ble_voice_start()
        - Handle 0x003f + Data[N]: hex → audio data → BLE backend feed

        Also auto-triggers voice start when audio data appears on 0x003f
        while idle — this catches cases where the status notification (0x0042)
        was too brief for btmon to capture.
        """
        import re
        import select as _select

        data_re = re.compile(r"Data\[(\d+)\]:\s*([0-9a-fA-F]+)")
        prev_line = ""
        while self._btmon_proc and self._btmon_proc.poll() is None:
            r, _, _ = _select.select([self._btmon_proc.stdout], [], [], 0.05)
            if not r:
                continue
            raw = self._btmon_proc.stdout.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").rstrip()

            # Voice button (ATVV): Data[1]: 08 = START_SEARCH; clone
            # firmwares (UR02) retry with ff while waiting for MIC_OPEN.
            if "Handle: 0x0042" in prev_line and (
                "Data[1]: ff" in line or "Data[1]: 08" in line
            ):
                if _KODI_AVAILABLE:
                    xbmc.log(
                        "Voice keyboard BLE: mic button detected via btmon",
                        xbmc.LOGINFO,
                    )
                self._on_ble_voice_start()

            # ATVV AUDIO_END: remote finished streaming this utterance.
            elif "Handle: 0x0042" in prev_line and "Data[1]: 00" in line:
                backend = self._ble_backend
                if backend is not None and getattr(backend, "_recording", False):
                    backend.signal_stream_end()

            # Audio data: Handle 0x003f, Data[N]: hex
            elif "Handle: 0x003f" in prev_line:
                m = data_re.search(line)
                if m:
                    # If audio data arrives while idle, auto-trigger voice start.
                    # This catches the case where the status ff notification was
                    # too brief for btmon to output before it was replaced by 00.
                    if self._get_state() == self._STATE_IDLE:
                        if _KODI_AVAILABLE:
                            xbmc.log(
                                "Voice keyboard BLE: audio data triggered voice start",
                                xbmc.LOGINFO,
                            )
                        self._on_ble_voice_start()

                    if self._ble_backend:
                        self._ble_backend.feed_audio_data(m.group(2))

            prev_line = line

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
                    if self._keyboard_visible():
                        self._set_state(self._STATE_LISTENING)
                        t = threading.Thread(target=self._worker)
                        t.daemon = True
                        t.start()
            monitor.waitForAbort(0.5)

        if hasattr(self, "_btmon_proc") and self._btmon_proc:
            self._btmon_proc.terminate()
        xbmc.log("Voice keyboard service stopped")


if _KODI_AVAILABLE:
    VoiceService().run()
