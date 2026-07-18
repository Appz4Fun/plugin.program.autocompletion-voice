# -*- coding: utf8 -*-
"""RMS-based silence detection utility for in-process audio capture backends.

Note: The CoreELEC backend records via subprocess to a file, so silence
detection cannot be applied inline — audio frames are never available in
Python during that recording. This utility is designed for backends that
have in-process access to the raw audio stream, such as the macOS backend.
"""

import math
import struct

SILENCE_SECONDS_DEFAULT = 5.0
RMS_THRESHOLD_DEFAULT = 500  # out of max 32767; covers typical USB mic noise floor


class SilenceDetector(object):
    """Detects sustained silence in a 16-bit signed little-endian PCM stream.

    Call :meth:`process` with each incoming PCM chunk.  Returns ``True``
    when cumulative silence has exceeded the configured duration.  Loud
    audio resets the silence counter.

    Args:
        threshold: RMS amplitude below which audio is considered silent.
        silence_seconds: Duration of continuous silence (seconds) that
            triggers a stop signal.
        sample_rate: Sample rate of the PCM stream in Hz.
    """

    def __init__(
        self,
        threshold=RMS_THRESHOLD_DEFAULT,
        silence_seconds=SILENCE_SECONDS_DEFAULT,
        sample_rate=16000,
    ):
        # type: (float, float, int) -> None
        self._threshold = threshold
        self._silence_limit = int(silence_seconds * sample_rate)  # samples
        self._silent_frames = 0

    def reset(self):
        # type: () -> None
        """Reset the silence counter (call before starting a new recording)."""
        self._silent_frames = 0

    def process(self, pcm_chunk):
        # type: (bytes) -> bool
        """Process a chunk of raw PCM bytes (16-bit signed LE, mono).

        Returns ``True`` if cumulative silence has reached the silence limit.
        """
        rms = self._compute_rms(pcm_chunk)
        num_samples = len(pcm_chunk) // 2  # 2 bytes per 16-bit sample
        if rms < self._threshold:
            self._silent_frames += num_samples
        else:
            self._silent_frames = 0
        return self._silent_frames >= self._silence_limit

    def _compute_rms(self, pcm_chunk):
        # type: (bytes) -> float
        """Return RMS energy of a 16-bit signed LE PCM chunk."""
        n = len(pcm_chunk) // 2
        if n == 0:
            return 0.0
        samples = struct.unpack_from("<{}h".format(n), pcm_chunk)
        mean_sq = sum(x * x for x in samples) / float(n)
        return math.sqrt(mean_sq)
