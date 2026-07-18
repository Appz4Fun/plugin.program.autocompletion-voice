# -*- coding: utf8 -*-
"""BLE audio capture backend for Bluetrum-based voice remotes (UR-02, G20S).

Uses btmon subprocess to capture raw HCI traffic for both mic button detection
and audio data. This bypasses Kodi's D-Bus/GLib main loop which blocks all
D-Bus signal dispatch to Python addons.

Protocol (reverse-engineered from Ugoos UR-02):
  - Voice service UUID: ab5e0001-5a21-4f05-bc7d-af01f617b664
  - Audio data char:    ab5e0003 (ATT handle 0x003f) — IMA ADPCM, 8kHz mono
  - Status char:        ab5e0004 (ATT handle 0x0042) — mic button signals
  - Control char:       ab5e0002 (ATT handle 0x003d) — write ack
  - Frame groups: 1 header (20B, 6B metadata + 14B audio) + 5 cont (20B) + 1 partial (8B)

Voice activation signals on status handle 0x0042:
  - Data[1]: ff — mic button pressed
  - Data[1]: 00 — mic button released
  - Data[4]: 040301XX — voice session ended
"""

import io
import re
import select
import struct
import subprocess
import threading
import time as _time
import wave
from typing import Callable, List, Optional  # noqa: F401

try:
    import xbmc

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

from lib.audio_capture import AudioCaptureBase
from lib.audio_capture.adpcm import AdpcmDecoder
from lib.audio_capture.silence import SilenceDetector

VOICE_SERVICE_UUID = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
VOICE_CONTROL_UUID = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
VOICE_DATA_UUID = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
VOICE_STATUS_UUID = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

# ATT attribute handles (from btmon HCI capture)
ATT_HANDLE_AUDIO = "003f"  # voice data notifications
ATT_HANDLE_STATUS = "0042"  # mic button press/release

BLE_SAMPLE_RATE = 8000
BLE_SAMPLE_WIDTH = 2
BLE_CHANNELS = 1
MAX_DURATION_DEFAULT = 30
STREAM_STOP_SECONDS = 0.5

# Regex to extract hex data from btmon "Data[N]: hex" lines
_DATA_RE = re.compile(r"Data\[(\d+)\]:\s*([0-9a-fA-F]+)")


# ---------------------------------------------------------------------------
# D-Bus helpers — one-shot queries only (work fine even with Kodi running)
# ---------------------------------------------------------------------------


def _find_char_path(uuid, device_address=None):
    # type: (str, Optional[str]) -> Optional[str]
    """Find the D-Bus object path for a GATT characteristic by UUID.

    Uses busctl subprocess calls: Kodi's bundled Python has no dbus module
    on CoreELEC/LibreELEC, and an in-process GLib main loop would be blocked
    by Kodi anyway (same reason audio capture goes through btmon).
    """
    try:
        tree = subprocess.run(
            ["busctl", "tree", "org.bluez", "--list"],
            capture_output=True,
            timeout=10,
        )
        for line in tree.stdout.decode("utf-8", "replace").splitlines():
            path = line.strip()
            # GATT characteristics live at .../dev_XX/serviceNNNN/charNNNN;
            # skip descriptor paths (charNNNN/descNNNN) below them.
            if "/char" not in path or "/desc" in path:
                continue
            if device_address:
                addr_part = device_address.replace(":", "_").upper()
                if addr_part not in path:
                    continue
            prop = subprocess.run(
                ["busctl", "get-property", "org.bluez", path,
                 "org.bluez.GattCharacteristic1", "UUID"],
                capture_output=True,
                timeout=10,
            )
            # Output shape: s "ab5e0004-5a21-4f05-bc7d-af01f617b664"
            out = prop.stdout.decode("utf-8", "replace")
            if '"' in out and out.split('"')[1].lower() == uuid.lower():
                return path
    except Exception as exc:
        if _KODI_AVAILABLE:
            xbmc.log(
                "Voice keyboard BLE: busctl characteristic lookup failed: {}".format(exc),
                xbmc.LOGWARNING,
            )
    return None


def _start_notify(char_path):
    # type: (str) -> bool
    """Call StartNotify on a GATT characteristic via dbus-send."""
    try:
        subprocess.run(
            [
                "dbus-send",
                "--system",
                "--type=method_call",
                "--dest=org.bluez",
                char_path,
                "org.bluez.GattCharacteristic1.StartNotify",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except Exception:
        return False


# The ab5e... service is Google's Android TV Voice Service (ATVV). The host
# opens the mic by WRITING a command to the control char (ab5e0002); the
# remote only streams audio after MIC_OPEN. Opcodes (host→remote):
#   0x0A GET_CAPS, 0x0C MIC_OPEN (param: 0x01 = ADPCM 8kHz/16bit),
#   0x0D MIC_CLOSE
# Remote→host on the status char (ab5e0004):
#   0x08 START_SEARCH (voice button pressed — host must send MIC_OPEN),
#   0x0B CAPS_RESP, 0x04 AUDIO_START, 0x00 AUDIO_END
# (Some clone firmwares, incl. UR02, retry with 0xff while waiting for
# MIC_OPEN — treated the same as START_SEARCH.)
_MIC_OPEN_BYTES = "array:byte:0x0c,0x00,0x01"
_GET_CAPS_BYTES = "array:byte:0x0a,0x00,0x04"


def _write_control(control_char_path, value_arg, wait):
    # type: (str, str, bool) -> bool
    """Write a command to the voice control characteristic via dbus-send."""
    cmd = [
        "dbus-send",
        "--system",
        "--type=method_call",
        "--dest=org.bluez",
        control_char_path,
        "org.bluez.GattCharacteristic1.WriteValue",
        value_arg,
        "dict:string:variant:",
    ]
    try:
        if wait:
            subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
            )
        else:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        return True
    except Exception:
        return False


def _send_voice_ack(control_char_path):
    # type: (str) -> bool
    """Send MIC_OPEN (ADPCM 8kHz) — the ATVV command that starts audio."""
    return _write_control(control_char_path, _MIC_OPEN_BYTES, wait=True)


def _send_voice_ack_fast(control_char_path):
    # type: (str) -> None
    """Fire-and-forget MIC_OPEN — Popen without waiting.

    Used on mic button press where speed matters more than confirmation.
    """
    _write_control(control_char_path, _MIC_OPEN_BYTES, wait=False)


def _send_get_caps(control_char_path):
    # type: (str) -> bool
    """Send GET_CAPS — the ATVV handshake some firmwares require before
    honoring MIC_OPEN. Safe no-op on firmwares that don't."""
    return _write_control(control_char_path, _GET_CAPS_BYTES, wait=True)


# ---------------------------------------------------------------------------
# Packet framer
# ---------------------------------------------------------------------------


class BLEPacketFramer(object):
    """Reassembles ATVV audio frames from GATT notification payloads.

    UR02 frame = 128 bytes over 7 notifications (6x20B + 1x8B): a 6-byte
    header (seq u16 BE, id u8, predictor s16 BE, step u8) followed by 122
    bytes of IMA ADPCM. The header state MUST seed the decoder for its
    frame — the remote's encoder does not run continuously across frames,
    so decoding the concatenated stream without reseeding produces loud
    robotic garble (measured RMS 17383 vs 937 on the same capture).
    """

    MAX_FRAME_BYTES = 256  # resync guard if a terminator packet is lost

    def __init__(self):
        self._buf = b""

    def reset(self):
        self._buf = b""

    def process_packet(self, data):
        # type: (bytes) -> Optional[tuple]
        """Feed one notification payload.

        Returns (predictor, step_index, adpcm_bytes) once a full frame is
        assembled (payloads shorter than 20B terminate a frame), else None.
        """
        self._buf += data
        if len(data) >= 20:
            if len(self._buf) > self.MAX_FRAME_BYTES:
                self._buf = b""  # lost the terminator — drop and resync
            return None
        frame, self._buf = self._buf, b""
        if len(frame) < 7:
            return None
        predictor = struct.unpack(">h", frame[3:5])[0]
        step_index = frame[5]
        if step_index > 88:
            return None  # corrupt header — skip frame
        return (predictor, step_index, frame[6:])


# ---------------------------------------------------------------------------
# Audio capture backend — uses btmon for audio data
# ---------------------------------------------------------------------------


class BLEAudioCapture(AudioCaptureBase):
    """Audio capture backend for Bluetrum BLE voice remotes on CoreELEC.

    Uses btmon subprocess to capture raw HCI audio notifications, decodes
    IMA ADPCM to PCM, and returns 8kHz/16-bit/mono WAV.
    """

    def __init__(self, max_duration=MAX_DURATION_DEFAULT, device_address=None):
        # type: (int, Optional[str]) -> None
        self._max_duration = max_duration
        self._device_address = device_address
        self._lock = threading.Lock()
        self._pcm_buffer = []  # type: List[bytes]
        self._framer = BLEPacketFramer()
        self._decoder = AdpcmDecoder()
        self._silence_detector = None  # type: Optional[SilenceDetector]
        self._stop_event = threading.Event()
        self._recording = False
        self._timer = None  # type: Optional[threading.Timer]
        self._proc = None  # type: Optional[subprocess.Popen]
        self._reader_thread = None  # type: Optional[threading.Thread]
        self._last_packet_time = 0.0

    @staticmethod
    def _upsample_2x(pcm_data):
        # type: (bytes) -> bytes
        """Upsample 16-bit signed LE PCM by 2x using linear interpolation.

        Doubles the sample rate (8kHz → 16kHz) which helps STT models that
        expect higher quality input. Linear interpolation avoids aliasing
        artifacts that simple sample duplication would cause.
        """
        n = len(pcm_data) // 2
        if n < 2:
            return pcm_data
        samples = struct.unpack_from("<{}h".format(n), pcm_data)
        out = []
        for i in range(n - 1):
            out.append(samples[i])
            out.append((samples[i] + samples[i + 1]) // 2)
        out.append(samples[-1])
        out.append(samples[-1])
        return struct.pack("<{}h".format(len(out)), *out)

    def _build_wav(self):
        # type: () -> bytes
        from lib.audio_capture.audio_enhance import enhance as _enhance_audio

        with self._lock:
            pcm_data = b"".join(self._pcm_buffer)
            self._pcm_buffer = []
        # Upsample 8kHz → 16kHz for better STT recognition
        pcm_data = self._upsample_2x(pcm_data)
        # Enhance: pre-emphasis → noise gate → normalize
        pcm_data = _enhance_audio(pcm_data, sample_rate=BLE_SAMPLE_RATE * 2)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(BLE_CHANNELS)
            wf.setsampwidth(BLE_SAMPLE_WIDTH)
            wf.setframerate(BLE_SAMPLE_RATE * 2)  # 16kHz after upsampling
            wf.writeframes(pcm_data)
        return buf.getvalue()

    def _on_voice_data(self, value):
        # type: (bytes) -> None
        if not self._recording:
            return
        frame = self._framer.process_packet(bytes(value))
        if frame is None:
            return
        predictor, step_index, audio_bytes = frame
        self._decoder.predictor = predictor
        self._decoder.step_index = step_index
        pcm = self._decoder.decode(audio_bytes)
        with self._lock:
            self._pcm_buffer.append(pcm)
            self._last_packet_time = _time.monotonic()
            count = len(self._pcm_buffer)
        if count == 1 and _KODI_AVAILABLE:
            xbmc.log("Voice keyboard BLE: first audio packet received", xbmc.LOGWARNING)
        if self._silence_detector and self._silence_detector.process(pcm):
            self._stop_event.set()

    def _reader_loop(self):
        """Read btmon output and extract audio data from handle 0x003f."""
        prev_line = ""
        while self._recording and self._proc and self._proc.poll() is None:
            r, _, _ = select.select([self._proc.stdout], [], [], 0.05)
            if not r:
                continue
            raw = self._proc.stdout.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").rstrip()

            # Match: previous line has "Handle: 0x003f", current has "Data[N]: hex"
            if "Handle: 0x" + ATT_HANDLE_AUDIO in prev_line:
                m = _DATA_RE.search(line)
                if m:
                    hex_data = m.group(2)
                    try:
                        self._on_voice_data(bytes.fromhex(hex_data))
                    except (ValueError, Exception):
                        pass
            prev_line = line

    def _auto_stop(self):
        if _KODI_AVAILABLE:
            xbmc.log(
                "Voice keyboard BLE: max duration reached, stopping",
                xbmc.LOGWARNING,
            )
        self._stop_event.set()

    def signal_stream_end(self):
        # type: () -> None
        """End the capture early — remote sent AUDIO_END on the status char."""
        if _KODI_AVAILABLE:
            xbmc.log(
                "Voice keyboard BLE: AUDIO_END received, stopping capture",
                xbmc.LOGINFO,
            )
        self._stop_event.set()

    # ------------------------------------------------------------------
    # AudioCaptureBase interface
    # ------------------------------------------------------------------

    def start_recording(self, external_feed=False):
        """Prepare to capture audio data.

        Args:
            external_feed: If True, audio data will be fed via feed_audio_data()
                from an external btmon reader (shared process). If False,
                starts its own btmon subprocess.
        """
        self._pcm_buffer = []
        self._framer.reset()
        self._decoder.reset()
        try:
            open("/storage/.kodi/temp/voice_raw.hex", "w").close()
        except OSError:
            pass
        self._silence_detector = SilenceDetector(sample_rate=BLE_SAMPLE_RATE)
        self._stop_event.clear()
        self._last_packet_time = 0.0
        self._recording = True
        self._external_feed = external_feed

        if not external_feed:
            # Enable notifications on audio data characteristic
            audio_path = _find_char_path(VOICE_DATA_UUID, self._device_address)
            if audio_path:
                _start_notify(audio_path)

            self._proc = subprocess.Popen(
                ["btmon"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            self._reader_thread = threading.Thread(target=self._reader_loop)
            self._reader_thread.daemon = True
            self._reader_thread.start()

        if _KODI_AVAILABLE:
            xbmc.log("Voice keyboard BLE: listening for voice data", xbmc.LOGINFO)

        self._timer = threading.Timer(self._max_duration, self._auto_stop)
        self._timer.daemon = True
        self._timer.start()

    def feed_audio_data(self, hex_data):
        # type: (str) -> None
        """Feed audio data from an external btmon reader.

        Called by the shared btmon process when it sees audio data
        on handle 0x003f.
        """
        try:
            # Debug aid: keep the raw notification payloads of the last
            # session so framing/decoder problems can be analyzed offline.
            if self._recording:
                try:
                    with open("/storage/.kodi/temp/voice_raw.hex", "a") as fh:
                        fh.write(hex_data + "\n")
                except OSError:
                    pass
            self._on_voice_data(bytes.fromhex(hex_data))
        except (ValueError, Exception):
            pass

    def stop_recording(self):
        # type: () -> bytes
        """Stop listening and return WAV bytes (8kHz/16-bit/mono)."""
        self._recording = False

        if self._timer:
            self._timer.cancel()
            self._timer = None

        if not getattr(self, "_external_feed", False):
            if self._proc:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None

            if self._reader_thread:
                self._reader_thread.join(timeout=2)
                self._reader_thread = None

        return self._build_wav()

    def wait_for_silence(self, timeout=None):
        # type: (Optional[float]) -> bool
        """Block until silence/stream-stop is detected or timeout."""
        if timeout is None:
            timeout = self._max_duration
        deadline = _time.monotonic() + timeout

        while _time.monotonic() < deadline:
            if self._stop_event.is_set():
                return True
            with self._lock:
                last = self._last_packet_time
                has_data = len(self._pcm_buffer) > 0
            if has_data and last > 0:
                idle = _time.monotonic() - last
                if idle >= STREAM_STOP_SECONDS:
                    return True
            _time.sleep(0.05)
        return False

    def is_available(self):
        # type: () -> bool
        """Return True if a Bluetrum voice remote is paired and connected."""
        try:
            return _find_char_path(VOICE_DATA_UUID, self._device_address) is not None
        except Exception:
            return False

    def list_devices(self):
        # type: () -> List[str]
        """Return a list of paired Bluetrum voice remotes."""
        devices = []  # type: List[str]
        try:
            import dbus  # type: ignore[import]
            from dbus.mainloop.glib import DBusGMainLoop  # type: ignore[import]

            DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            manager = dbus.Interface(
                bus.get_object("org.bluez", "/"),
                "org.freedesktop.DBus.ObjectManager",
            )
            for path, interfaces in manager.GetManagedObjects().items():
                dev = interfaces.get("org.bluez.Device1")
                if dev:
                    uuids = [str(u) for u in dev.get("UUIDs", [])]
                    if VOICE_SERVICE_UUID in uuids:
                        name = str(dev.get("Name", "Unknown"))
                        addr = str(dev.get("Address", ""))
                        devices.append("{} [{}]".format(name, addr))
        except Exception:
            pass
        return devices
