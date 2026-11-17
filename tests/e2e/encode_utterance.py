#!/usr/bin/env python3
"""Text -> macOS say -> 8 kHz PCM -> UR02 ATVV ADPCM frames (hex lines).

Frame = 6-byte header (seq u16 BE, id u8, predictor s16 BE, step u8) + 122
bytes IMA ADPCM (244 samples, high nibble first) — the exact format the
add-on's BLEPacketFramer/AdpcmDecoder expect from the real remote.
Usage: encode_utterance.py "<text>" out.frames
"""
import json, os, struct, sys, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from lib.audio_capture.adpcm import STEP_TABLE, INDEX_TABLE

def tts(text, key=None):
    """Speak text with macOS `say` (16 kHz mono PCM), downsample to 8 kHz."""
    import subprocess, tempfile, wave
    path = tempfile.mktemp(suffix=".wav")
    subprocess.run(["say", "-o", path, "--file-format=WAVE", "--data-format=LEI16@16000", text], check=True)
    with wave.open(path) as w:
        raw = w.readframes(w.getnframes())
    os.remove(path)
    s = struct.unpack("<%dh" % (len(raw) // 2), raw)
    lead = [0] * 2400  # 0.3 s of silence after the press, like a real user
    return lead + [int((s[i] + s[i + 1]) / 2 * 0.35) for i in range(0, len(s) - 1, 2)]  # remote mic is quiet


def encode(samples):
    frames, pred, idx = [], 0, 0
    for seq, i in enumerate(range(0, len(samples), 244)):
        block = (samples[i:i + 244] + [0] * 244)[:244]
        hdr = struct.pack(">HBhB", seq & 0xFFFF, 0, pred, idx)
        nibbles = []
        for x in block:
            step = STEP_TABLE[idx]; diff = x - pred; nib = 0
            if diff < 0: nib = 8; diff = -diff
            delta = step >> 3
            if diff >= step: nib |= 4; diff -= step; delta += step
            if diff >= step >> 1: nib |= 2; diff -= step >> 1; delta += step >> 1
            if diff >= step >> 2: nib |= 1; delta += step >> 2
            pred = max(-32768, min(32767, pred - delta if nib & 8 else pred + delta))
            idx = max(0, min(88, idx + INDEX_TABLE[nib]))
            nibbles.append(nib)
        data = bytes((nibbles[j] << 4) | nibbles[j + 1] for j in range(0, 244, 2))
        frames.append(hdr + data)
    return frames

if __name__ == "__main__":
    frames = encode(tts(sys.argv[1]))
    with open(sys.argv[2], "w") as fh:
        fh.write("\n".join(f.hex() for f in frames) + "\n")
    print("%d frames (%.1fs)" % (len(frames), len(frames) * 244 / 8000.0))
