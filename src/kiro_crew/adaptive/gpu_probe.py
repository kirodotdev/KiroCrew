"""Optional GPU memory probe for the adaptive controller.

A host that serves a local model next to the gateway can run out of GPU memory
long before it runs out of host memory. ``HostSample`` already carries host
memory, RSS and file descriptors; this leaf adds the card's free memory so
:func:`kiro_crew.adaptive.signals.classify` can treat a nearly full card the way
it treats nearly exhausted host memory: as a spawn gate signal.

What the sample follows depends on the server, and the probe cannot tell which
one it sits next to: it reports what is free, whoever holds it. Free memory falls
as sub-agents run only on a server that loads a model when a request first names
it and keeps several resident (Ollama up to ``OLLAMA_MAX_LOADED_MODELS``,
``llama-server`` in router mode up to ``--models-max``): each model a sub-agent
uses that is not yet loaded adds one. It stays flat across sub-agents that share
one model, whose slots are sized when it loads, and on servers that reserve a
fixed share of the card when they start (vLLM, SGLang, TGI). There it shows what
else is using the card.

Design rules, all pinned by ``test_adaptive_gpu_probe.py``:

* **Optional.** NVML is reached through ``pynvml`` (the ``nvidia-ml-py``
  package, the ``gpu`` extra in ``setup.cfg``). When the import or
  ``nvmlInit`` fails, the reader marks itself unavailable once and says so in
  one log line (INFO without the binding, naming the command that installs it;
  WARNING when NVML does not start), and
  every later read returns :data:`NOT_MEASURED` without touching NVML again, so
  a GPU line stays inert until a restart. A host with no NVIDIA driver pays one
  failed import per process.
* **Never raises.** Every NVML call sits inside ``try``; a failure after a
  successful init (driver reset, device fell off the bus) returns
  :data:`NOT_MEASURED` for that read and is retried on the next one.
* **Uncached, one read at a time.** A read queries NVML every time it is called:
  the controller reads at most once per sample interval (at least one second),
  so a cache would save nothing. The read is synchronous and runs on a worker
  thread of its own, which the controller's tick starts only while a GPU line is
  set and waits for at most ``GPU_READ_TIMEOUT_SECS``, never starting a second
  while one is running; it is never called on the event loop. That is the
  reader's only caller, so it holds no lock.
* **The most constrained device decides.** On a multi-GPU host the sample
  reports the free memory of the device with the least of it. A single model
  lives on one card, so summing free memory across cards would overstate the
  headroom that matters. For the same reason a device that cannot be read makes
  the whole read :data:`NOT_MEASURED`: it may be the fullest card.
* **-1 means not measured**, matching every other ``HostSample`` field, so the
  consumers' ``>= 0`` guards keep today's behaviour byte for byte on a host
  without a GPU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MIB = 1024.0 * 1024.0


@dataclass(frozen=True)
class GpuSample:
    """One GPU read. ``-1`` = not measured (no driver, no device, or a failure)."""

    vram_free_mb: float = -1.0

    @property
    def measured(self) -> bool:
        return self.vram_free_mb >= 0.0


#: The sample every failure path returns.
NOT_MEASURED = GpuSample()


class NvmlReader:
    """NVML reader that remembers a failed init. One per process is enough; see :func:`read_gpu`."""

    def __init__(self, *, importer: Callable[[], Any] | None = None) -> None:
        self._importer = importer or _import_pynvml
        self._nvml: Any = None
        self._unavailable = False

    def read(self) -> GpuSample:
        """Query NVML for the least free device's free memory. Never raises."""
        if self._unavailable:
            return NOT_MEASURED
        if self._nvml is None:
            try:
                nvml = self._importer()
                nvml.nvmlInit()
            except Exception as exc:
                self._unavailable = True
                missing = isinstance(exc, ImportError)
                hint = ""
                if missing:
                    from kiro_crew import extras  # the binding is the optional ``gpu`` extra

                    hint = "; install the binding with: " + extras.install_hint("gpu")
                # Once per reader, so once per process: the latch keeps every later
                # read off NVML, and a GPU line set in config stays inert until a
                # restart. No binding is the default install (INFO, with the command
                # that adds it); a binding whose NVML does not start is a driver
                # problem (WARNING).
                logger.log(
                    logging.INFO if missing else logging.WARNING,
                    "adaptive: GPU memory probe off for this process (%s); "
                    "agent.vram_pressure_gb and agent.vram_critical_gb have no effect "
                    "until a restart%s",
                    type(exc).__name__,
                    hint,
                )
                logger.debug("adaptive: gpu probe unavailable (no NVML)", exc_info=True)
                return NOT_MEASURED
            self._nvml = nvml
        nvml = self._nvml
        try:
            count = int(nvml.nvmlDeviceGetCount())
        except Exception:
            logger.debug("adaptive: gpu probe device count failed", exc_info=True)
            return NOT_MEASURED
        if count <= 0:
            return NOT_MEASURED
        best_free: float | None = None
        best_total = -1.0
        for index in range(count):
            try:
                handle = nvml.nvmlDeviceGetHandleByIndex(index)
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                free_mb = float(mem.free) / _MIB
                total_mb = float(mem.total) / _MIB
            except Exception:
                # The unreadable card may be the one a model fills, so no other card's
                # headroom stands in for it: the read is not measured, as for any failure
                # after init, and the next read looks again.
                logger.debug(
                    "adaptive: gpu probe memory read failed for device %d", index, exc_info=True
                )
                return NOT_MEASURED
            if best_free is None or free_mb < best_free:
                best_free, best_total = free_mb, total_mb
        if best_free is None or best_total <= 0.0:
            # A card that reports no total memory is no reading: a free count of 0
            # beside it would look like a full card and cut the gate.
            return NOT_MEASURED
        return GpuSample(vram_free_mb=best_free)


def _import_pynvml() -> Any:
    import pynvml  # type: ignore[import-not-found]  # optional extra `gpu`

    return pynvml


_reader: NvmlReader | None = None


def read_gpu() -> GpuSample:
    """Process-wide read used by the adaptive controller's tick. Never raises.

    The tick starts at most one read at a time (``AdaptiveController._read_gpu_into``),
    so building the shared reader here needs no lock.
    """
    global _reader
    try:
        if _reader is None:
            _reader = NvmlReader()
        return _reader.read()
    except Exception:  # pragma: no cover - the reader already catches; belt and braces
        logger.debug("adaptive: gpu probe failed", exc_info=True)
        return NOT_MEASURED
