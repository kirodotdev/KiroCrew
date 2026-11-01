"""A stand-in ``Llama`` importable by the embedding child process in tests.

The child resolves its model class from a ``module:callable`` spec, so a test
points it at :func:`load_class` here (with this directory on ``PYTHONPATH``).
The same class runs in-process too, which is what lets a test compare the two
paths on identical vectors.

Vectors are full-precision floats derived from the text, so an exact equality
proves the pipe hands back the bits the model produced. A text may carry one
control prefix, honoured identically in both processes:

- ``__sleep:<secs>`` sleeps (a slow native call),
- ``__spin:<secs>`` runs a pure-Python busy loop (holds the GIL),
- ``__crash`` exits the process at once (a native crash),
- ``__print`` writes to stdout first (native noise on the protocol stream),
- ``__threads`` returns the programmed thread counts in the first two slots.
"""

from __future__ import annotations

import hashlib
import os
import random
import sys
import time

DIM = 16


class _Ctx:
    def __init__(self) -> None:
        self.threads = (0, 0)

    def set_n_threads(self, n_threads: int, n_threads_batch: int) -> None:
        self.threads = (n_threads, n_threads_batch)


def vector_for(text: str) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(DIM)]


class FakeLlama:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self._ctx = _Ctx()

    def n_embd(self) -> int:
        return DIM

    def create_embedding(self, texts: list[str]) -> dict:
        data = []
        for text in texts:
            if text.startswith("__sleep:"):
                time.sleep(float(text.split(":", 1)[1]))
            elif text.startswith("__spin:"):
                end = time.monotonic() + float(text.split(":", 1)[1])
                n = 0
                while time.monotonic() < end:
                    n += 1
            elif text == "__crash":
                os._exit(3)
            elif text == "__print":
                print("native noise on stdout")
                sys.stdout.flush()
            if text == "__threads":
                vec = [float(self._ctx.threads[0]), float(self._ctx.threads[1])] + [0.0] * (DIM - 2)
            else:
                vec = vector_for(text)
            data.append({"embedding": vec})
        return {"data": data}

    def close(self) -> None:
        pass


def load_class() -> type:
    return FakeLlama
