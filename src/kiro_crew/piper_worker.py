"""Owned Piper subprocess: one loaded voice, explicit bounded PCM/request framing.

Only the sandboxed child loads a voice; the gateway can probe the optional API.
Stdout is exclusively this protocol; provider diagnostics belong on stderr.
There is no listener or shared file.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from typing import BinaryIO

MAX_HEADER_BYTES = 1024
MAX_FRAME_BYTES = 76800  # 200 ms of mono int16 at the maximum supported rate.
MAX_REQUEST_BYTES = 160000
MAX_TEXT_CHARS = 20000
MAX_AUDIO_BYTES = 24 * 1024 * 1024


def write_frame(output: BinaryIO, kind: str, request_id: str = "", pcm: bytes = b"", **meta):
    header = json.dumps({"type": kind, "id": request_id, **meta}, ensure_ascii=False).encode(
        "utf-8"
    )
    if len(header) > MAX_HEADER_BYTES or len(pcm) > MAX_FRAME_BYTES:
        raise ValueError("oversize worker frame")
    output.write(struct.pack("!II", len(header), len(pcm)))
    output.write(header)
    output.write(pcm)
    output.flush()


def serve(voice, synthesis_config, source: BinaryIO, output: BinaryIO) -> int:
    """Serve serial requests; any provider/protocol fault retires this process."""
    sample_rate = voice.config.sample_rate
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
        raise ValueError("unsupported sample rate")
    write_frame(output, "ready", sample_rate=sample_rate)
    while line := source.readline(MAX_REQUEST_BYTES + 1):
        request_id = ""
        try:
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise ValueError("oversize worker request")
            request = json.loads(line)
            request_id = request["id"]
            phrases = request["phrases"]
            scale = request["length_scale"]
            if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
                request_id = ""
                raise ValueError("invalid request identity")
            if (
                not isinstance(phrases, list)
                or not phrases
                or any(not isinstance(p, str) or not 0 < len(p) <= 240 for p in phrases)
                or sum(map(len, phrases)) > MAX_TEXT_CHARS
                or isinstance(scale, bool)
                or not isinstance(scale, (int, float))
                or not math.isfinite(scale)
                or scale <= 0
            ):
                raise ValueError("invalid synthesis request")
            config = synthesis_config(length_scale=scale)
            total = 0
            chunk_bytes = int(sample_rate * 0.2) * 2
            for phrase in phrases:
                for chunk in voice.synthesize(phrase, config):
                    if (
                        chunk.sample_rate != sample_rate
                        or chunk.sample_width != 2
                        or chunk.sample_channels != 1
                    ):
                        raise ValueError("unsupported PCM format")
                    pcm = chunk.audio_int16_bytes
                    total += len(pcm)
                    if len(pcm) % 2 or total > MAX_AUDIO_BYTES:
                        raise ValueError("invalid or oversize PCM")
                    for start in range(0, len(pcm), chunk_bytes):
                        write_frame(
                            output,
                            "pcm",
                            request_id,
                            pcm[start : start + chunk_bytes],
                            sample_rate=sample_rate,
                        )
            if not total:
                raise ValueError("empty PCM")
            write_frame(output, "done", request_id)
        except Exception:
            # A native error may include user text. Keep diagnostics private and
            # bounded, and never allow a failed request to poison the next one.
            write_frame(output, "error", request_id, code="voice_synthesis_failed")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        from piper import PiperVoice, SynthesisConfig

        voice = PiperVoice.load(args.model, config_path=args.config)
        return serve(voice, SynthesisConfig, sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        write_frame(sys.stdout.buffer, "error", code="voice_synthesis_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
