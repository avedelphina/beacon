#!/usr/bin/env python3
"""Make the app's notification tones (apps/apple/Beacon/Sounds/*.wav).

iOS lets an app choose only sounds it ships, not the system's list, so these are ours: generated
here, from sine waves, so that there is nobody's licence to keep. Run again to change them.
The names are Beacon's too (backend/apns.py, SOUNDS)."""

import math
import struct
import wave
from pathlib import Path

RATE = 22050
OUT = Path(__file__).resolve().parent.parent / "apps" / "apple" / "Beacon" / "Sounds"

# name: [(start s, frequency Hz, length s, loudness, partials)]
BELL = ((1, 1.0), (2.76, 0.4), (5.4, 0.2))
SOFT = ((1, 1.0), (2, 0.2))
TONES = {
    "chime": [(0.00, 659.3, 0.7, 0.6, SOFT), (0.18, 523.3, 0.9, 0.6, SOFT)],
    "ping": [(0.00, 1318.5, 0.6, 0.55, SOFT)],
    "knock": [(0.00, 196.0, 0.12, 0.9, SOFT), (0.16, 196.0, 0.14, 0.9, SOFT)],
    "rise": [(0.00, 523.3, 0.25, 0.5, SOFT), (0.12, 659.3, 0.25, 0.55, SOFT), (0.24, 784.0, 0.5, 0.6, SOFT)],
    "bell": [(0.00, 440.0, 1.6, 0.6, BELL)],
}


def render(notes) -> bytes:
    length = max(start + span for start, _, span, _, _ in notes)
    samples = [0.0] * int(length * RATE)
    for start, frequency, span, loudness, partials in notes:
        total = sum(weight for _, weight in partials)
        for i in range(int(span * RATE)):
            t = i / RATE
            envelope = min(1.0, t / 0.005) * math.exp(-4.0 * t / span) * min(1.0, (span - t) / 0.02)
            value = sum(weight * math.sin(2 * math.pi * frequency * ratio * t) for ratio, weight in partials)
            at = int(start * RATE) + i
            if at < len(samples):
                samples[at] += loudness * envelope * value / total
    return b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32000)) for s in samples)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, notes in TONES.items():
        with wave.open(str(OUT / f"{name}.wav"), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(render(notes))
        print(name)
