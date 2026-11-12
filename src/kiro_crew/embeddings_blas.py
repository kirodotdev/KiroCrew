"""Keep last-token embedding decodes off ggml's BLAS backend.

The macOS vendored runtime registers a ``BLAS`` device (``libggml-blas``, linked
against Accelerate). ggml hands it a matrix multiply only when every dimension is
at least ``_BLAS_MIN_BATCH``, and for a quantized weight it first dequantizes the
whole matrix to float32 into a work buffer that grows to the largest matrix it has
seen and is held until the context is freed. In embedding mode llama.cpp still
applies the vocabulary-sized output projection to every token, so the first
decode of 32 or more tokens leaves a ``n_vocab * n_embd * 4``-byte buffer behind:
592.5 MiB for the shipped model (151,669 x 1,024). A decode micro-batch below the
threshold never reaches that backend, so the projection runs on the CPU kernels
that short inputs already use. Linux and Windows ship no BLAS backend, so their
micro-batch is left exactly as the context policy chose it.
"""

from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger("kiro_crew.embeddings")

# ggml-blas offloads a mat-mul only when ne0, ne1 and ne10 are all >= this
# (``min_batch`` in ggml-blas.cpp's supports_op); ne1 is the decode token count.
_BLAS_MIN_BATCH = 32
_BLAS_DEVICE_NAME = "BLAS"


def blas_safe_ubatch(n_ubatch: int, n_batch: int, has_blas: bool) -> int:
    """The decode micro-batch for a last-token decoder on this runtime.

    Only a bounded micro-batch (``n_ubatch < n_batch``) is lowered: a context that
    needs every token in one micro-batch (non-causal files, mean or CLS pooling)
    gets ``n_ubatch == n_batch`` from the policy and keeps it, because splitting
    its input would change or abort its vectors.
    """
    if not has_blas or n_ubatch >= n_batch:
        return n_ubatch
    return min(n_ubatch, _BLAS_MIN_BATCH - 1)


def runtime_has_blas_backend(native_lib: object) -> bool:
    """Whether the loaded llama.cpp runtime registered a ``BLAS`` device.

    *native_lib* is the vendored binding's ``ctypes.CDLL`` for libllama, whose
    dependency closure exports ggml's device registry. Any failure to read it
    answers False, which keeps the policy's micro-batch unchanged.
    """
    try:
        count = native_lib.ggml_backend_dev_count  # type: ignore[attr-defined]
        get = native_lib.ggml_backend_dev_get  # type: ignore[attr-defined]
        name = native_lib.ggml_backend_dev_name  # type: ignore[attr-defined]
        count.restype, count.argtypes = ctypes.c_size_t, []
        get.restype, get.argtypes = ctypes.c_void_p, [ctypes.c_size_t]
        name.restype, name.argtypes = ctypes.c_char_p, [ctypes.c_void_p]
        for index in range(int(count())):
            raw = name(get(index))
            if raw is not None and raw.decode("utf-8", "replace") == _BLAS_DEVICE_NAME:
                return True
    except Exception:  # noqa: BLE001 - an unreadable registry keeps the policy as-is
        logger.debug(
            "ggml device registry unreadable; keeping the policy micro-batch", exc_info=True
        )
    return False
