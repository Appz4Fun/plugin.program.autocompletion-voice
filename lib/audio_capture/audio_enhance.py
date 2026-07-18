# -*- coding: utf8 -*-
"""Audio enhancement for close-range BLE remote microphone recordings.

Single-pass optimized pipeline — unpacks PCM once, processes in-place, packs once.
"""

import math
import struct


def enhance(pcm_data, sample_rate=16000):
    # type: (bytes, int) -> bytes
    """Single-pass enhancement: highpass → pre-emphasis → soft clip → normalize.

    Optimized to unpack/pack only once instead of per-stage.
    Noise gate removed — it was cutting off quiet consonants and hurting recognition.
    """
    n = len(pcm_data) // 2
    if n < 2:
        return pcm_data

    samples = list(struct.unpack_from("<{}h".format(n), pcm_data))

    # 1. High-pass filter (200Hz) — remove bass rumble and plosive pops
    rc = 1.0 / (2.0 * math.pi * 200)
    dt = 1.0 / sample_rate
    alpha = rc / (rc + dt)
    prev_in = float(samples[0])
    prev_out = float(samples[0])
    for i in range(1, n):
        curr_in = float(samples[i])
        filtered = alpha * (prev_out + curr_in - prev_in)
        prev_in = curr_in
        prev_out = filtered
        samples[i] = max(-32768, min(32767, int(filtered)))

    # 2. Pre-emphasis (0.97) — boost high frequencies for consonant clarity
    prev = samples[0]
    for i in range(1, n):
        val = samples[i] - int(0.97 * prev)
        prev = samples[i]
        samples[i] = max(-32768, min(32767, val))

    # 3. Soft clip (threshold 28000) — prevent distortion from close-range
    for i in range(n):
        s = samples[i]
        if abs(s) > 28000:
            sign = 1 if s >= 0 else -1
            excess = abs(s) - 28000
            compressed = 28000 + int(4767 * math.tanh(excess / 4767.0))
            samples[i] = sign * compressed

    # 4. Peak normalize (target 90%) — maximize volume for STT
    peak = max(abs(s) for s in samples)
    if peak > 0:
        target = int(32767 * 0.9)
        scale = target / float(peak)
        if scale > 1.0:
            for i in range(n):
                samples[i] = max(-32768, min(32767, int(samples[i] * scale)))

    return struct.pack("<{}h".format(n), *samples)


# Keep individual functions available for testing
def highpass(pcm_data, cutoff=200, sample_rate=16000):
    # type: (bytes, int, int) -> bytes
    n = len(pcm_data) // 2
    if n < 2:
        return pcm_data
    samples = list(struct.unpack_from("<{}h".format(n), pcm_data))
    rc = 1.0 / (2.0 * math.pi * cutoff)
    dt = 1.0 / sample_rate
    alpha = rc / (rc + dt)
    prev_in = float(samples[0])
    prev_out = float(samples[0])
    for i in range(1, n):
        curr_in = float(samples[i])
        filtered = alpha * (prev_out + curr_in - prev_in)
        prev_in = curr_in
        prev_out = filtered
        samples[i] = max(-32768, min(32767, int(filtered)))
    return struct.pack("<{}h".format(n), *samples)


def pre_emphasis(pcm_data, coeff=0.97):
    # type: (bytes, float) -> bytes
    n = len(pcm_data) // 2
    if n < 2:
        return pcm_data
    samples = list(struct.unpack_from("<{}h".format(n), pcm_data))
    out = [samples[0]]
    for i in range(1, n):
        val = samples[i] - coeff * samples[i - 1]
        out.append(max(-32768, min(32767, int(val))))
    return struct.pack("<{}h".format(n), *out)


def soft_clip(pcm_data, threshold=28000):
    # type: (bytes, int) -> bytes
    n = len(pcm_data) // 2
    if n == 0:
        return pcm_data
    samples = list(struct.unpack_from("<{}h".format(n), pcm_data))
    for i in range(n):
        s = samples[i]
        if abs(s) > threshold:
            sign = 1 if s >= 0 else -1
            excess = abs(s) - threshold
            max_excess = 32767 - threshold
            compressed = threshold + int(
                max_excess * math.tanh(excess / float(max_excess))
            )
            samples[i] = sign * compressed
    return struct.pack("<{}h".format(n), *samples)


def noise_gate(pcm_data, threshold=200, sample_rate=16000):
    # type: (bytes, int, int) -> bytes
    n = len(pcm_data) // 2
    if n == 0:
        return pcm_data
    samples = list(struct.unpack_from("<{}h".format(n), pcm_data))
    win = max(1, sample_rate // 50)
    out = list(samples)
    for i in range(0, n, win):
        end = min(i + win, n)
        chunk = samples[i:end]
        rms = _rms(chunk)
        if rms < threshold:
            for j in range(i, end):
                out[j] = 0
    return struct.pack("<{}h".format(n), *out)


def normalize(pcm_data, target_peak=0.9):
    # type: (bytes, float) -> bytes
    n = len(pcm_data) // 2
    if n == 0:
        return pcm_data
    samples = list(struct.unpack_from("<{}h".format(n), pcm_data))
    peak = max(abs(s) for s in samples)
    if peak == 0:
        return pcm_data
    target = int(32767 * target_peak)
    scale = target / float(peak)
    if scale <= 1.0:
        return pcm_data
    out = [max(-32768, min(32767, int(s * scale))) for s in samples]
    return struct.pack("<{}h".format(n), *out)


def _rms(samples):
    # type: (list) -> float
    if not samples:
        return 0.0
    mean_sq = sum(s * s for s in samples) / float(len(samples))
    return mean_sq**0.5
