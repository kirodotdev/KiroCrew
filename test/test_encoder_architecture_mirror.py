"""`_ENCODER_ARCHITECTURES` must mirror the vendored llama.cpp runtime.

The set names the architectures the vendored llama.cpp runs without a KV cache
(the `res = nullptr` cases of `llama_model::create_memory`, plus `t5encoder`).
`llama_decode()` routes them through `encode()`, which aborts the whole process
with an uncatchable SIGABRT unless one physical micro-batch holds every input
token, so `_model_context_policy()` gives each of them one physical micro-batch
the size of the whole 2,048-token context (or of their trained position count,
when the GGUF declares a smaller one). The runtime is a shipped binary, so nothing
in the source tree can
reproduce that switch; what CAN be checked is that the runtime still spells every
listed architecture. llama.cpp keeps its architecture names in one string table
(`LLM_ARCH_NAMES` in `src/llama-arch.cpp`), and every entry lands in the
`libllama` binary as a NUL-terminated string, so a vendored-runtime bump that
removes or renames a listed architecture fails here rather than in an operator's
gateway.

An architecture the bump ADDS to the cache-less set is invisible to this test:
its name is in the binary but not in the set, and no byte pattern says which
switch case it belongs to. That direction stays a manual step of the bump
procedure in `src/kiro_crew/_vendor/README.md`.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.embeddings import _ENCODER_ARCHITECTURES, _LIBS_DIR_NAME, _VENDOR_DIR

_LIBS_ROOT = _VENDOR_DIR / _LIBS_DIR_NAME


def _vendored_llama_libraries() -> list[Path]:
    """Every vendored `libllama` binary present, whatever platforms the tree carries.

    The ggml libraries carry no architecture names; `libllama.so`,
    `libllama.dylib` and `llama.dll` do.
    """
    return sorted(
        path
        for path in _LIBS_ROOT.rglob("*")
        if path.is_file() and "llama" in path.name.lower() and "ggml" not in path.name.lower()
    )


def test_vendored_tree_carries_a_llama_library() -> None:
    """Fail, never skip: an empty tree would make the mirror check vacuous."""
    assert _vendored_llama_libraries(), f"no libllama binary under {_LIBS_ROOT}"


def test_every_encoder_architecture_is_spelled_by_every_vendored_runtime() -> None:
    """Each listed name occurs NUL-terminated in each vendored `libllama`.

    Only the terminating NUL is required. GNU ld tail-merges a string that is
    the suffix of another into that longer string, so in the Linux x86_64 build
    the table entry for `bert` points into the middle of `modern-bert\\0` and no
    `\\0bert\\0` exists; the runtime still knows the name. The price of that
    tolerance is that a name which is the suffix of another listed name is also
    satisfied by the longer one, so a bump that dropped `bert` alone while
    keeping `modern-bert` would not fail here.
    """
    libraries = _vendored_llama_libraries()
    assert libraries, f"no libllama binary under {_LIBS_ROOT}"
    for library in libraries:
        data = library.read_bytes()
        missing = sorted(
            name for name in _ENCODER_ARCHITECTURES if name.encode("ascii") + b"\0" not in data
        )
        assert not missing, (
            f"{library.relative_to(_LIBS_ROOT)} does not spell architecture(s) {missing}; "
            "re-mirror _ENCODER_ARCHITECTURES against llama_model::create_memory in the "
            "vendored llama.cpp (see src/kiro_crew/_vendor/README.md)"
        )
