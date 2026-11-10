"""Which PyTorch wheel Plumb-4B's environment installs, and whether it may use CUDA.

The other local presets stay on the CPU wheel: their launchers pin ``device="cpu"``.
Plumb follows ``decisions.provider.gpu``. Absent or false keeps today's CPU wheel
and ``--device cpu``. True, on a Linux host whose NVIDIA driver can run one of the
pinned torch's published CUDA builds, selects that wheel and ``--device cuda``.
The launcher still loads on CPU unless that environment's torch reports CUDA.

The driver probe is ``nvidia-smi`` from a fixed system directory, not ``PATH``:
the gateway's PATH can lead with a directory the agent can write. The result is
cached for the process. A provider read and the two-second status poll share it,
so a wedged driver delays the first read and not every poll after it.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading

from kiro_crew import platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger("kirocrew.decisions.local_torch")

#: Catalog id of the one preset whose launcher accepts a GPU.
PLUMB_MODEL_ID = "plumb-4b"

#: The PyTorch CPU-only wheel index (Linux). Other platforms' PyPI torch is CPU.
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

#: CUDA builds of the pinned torch (``torch==2.14.1``), newest first. A wheel runs
#: on a driver whose reported CUDA version is at least the wheel's. The cu128
#: index publishes no 2.14.1 wheel; 12.8 and 12.9 drivers take the 12.6 build.
_CUDA_INDEXES: tuple[tuple[tuple[int, int], str], ...] = (
    ((13, 2), "https://download.pytorch.org/whl/cu132"),
    ((13, 0), "https://download.pytorch.org/whl/cu130"),
    ((12, 6), "https://download.pytorch.org/whl/cu126"),
)

#: How long a driver query may stall a provider read. nvidia-smi returns at once
#: on a healthy host and can sit forever when the driver is wedged.
_NVIDIA_SMI_TIMEOUT_SECS = 5

_CUDA_VERSION = re.compile(r"CUDA Version:\s*(\d+)\.(\d+)")

_driver_lock = threading.Lock()
_driver_probed = False
_driver_cuda: tuple[int, int] | None = None


def parse_cuda_version(text: str) -> tuple[int, int] | None:
    """The ``CUDA Version`` nvidia-smi prints, or None when the line is absent."""
    match = _CUDA_VERSION.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def cuda_index_for_driver(version: tuple[int, int]) -> str:
    """The wheel index *version* can run, or the CPU index when none can."""
    for need, url in _CUDA_INDEXES:
        if version >= need:
            return url
    return TORCH_CPU_INDEX


def clear_driver_cuda_cache() -> None:
    """Drop the cached driver probe. Tests use this; the gateway does not."""
    global _driver_probed, _driver_cuda
    with _driver_lock:
        _driver_probed = False
        _driver_cuda = None


def driver_cuda_version() -> tuple[int, int] | None:
    """The host driver's CUDA version, or None when it cannot be read."""
    global _driver_probed, _driver_cuda
    with _driver_lock:
        if not _driver_probed:
            _driver_cuda = _read_driver_cuda()
            _driver_probed = True
        return _driver_cuda


def _read_driver_cuda() -> tuple[int, int] | None:
    binary = platform_compat.trusted_system_bin_quiet("nvidia-smi")
    if binary is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed system binary, no shell
            [binary],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_NVIDIA_SMI_TIMEOUT_SECS,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        logger.info("nvidia-smi could not be run; Plumb stays on the CPU build")
        return None
    if proc.returncode != 0:
        logger.info("nvidia-smi exited %s; Plumb stays on the CPU build", proc.returncode)
        return None
    version = parse_cuda_version(proc.stdout or "")
    if version is None:
        logger.info("nvidia-smi reported no CUDA version; Plumb stays on the CPU build")
    return version


def cuda_unavailable_reason() -> str | None:
    """Why Plumb cannot take a CUDA wheel, or None when it can.

    ``unsupported_platform`` on anything but Linux, ``no_driver`` when nvidia-smi
    cannot be read, ``driver_too_old`` when the newest published build of the
    pinned torch needs a newer driver. Empty means a CUDA index is installable.
    """
    if not sys.platform.startswith("linux"):
        return "unsupported_platform"
    version = driver_cuda_version()
    if version is None:
        return "no_driver"
    if cuda_index_for_driver(version) == TORCH_CPU_INDEX:
        return "driver_too_old"
    return None


def plumb_gpu_requested() -> bool:
    """Whether the owner turned Plumb's GPU switch on. Absent reads as off."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = live.snapshot() or KiroCrewConfig.load()
    provider = getattr(getattr(cfg, "decisions", None), "provider", None)
    return getattr(provider, "gpu", None) is True


def plumb_use_cuda() -> bool:
    """Whether this host should install a CUDA torch for Plumb and pass ``--device cuda``."""
    return plumb_gpu_requested() and cuda_unavailable_reason() is None


def linux_torch_index(model_id: str) -> str:
    """The PyTorch index for a Linux install of *model_id*.

    Plumb takes a CUDA index only when the owner asked and the driver can run
    one. Every other preset, and Plumb otherwise, takes the CPU index.
    """
    if model_id != PLUMB_MODEL_ID or not plumb_use_cuda():
        return TORCH_CPU_INDEX
    version = driver_cuda_version()
    if version is None:
        return TORCH_CPU_INDEX
    return cuda_index_for_driver(version)


def plumb_gpu_view() -> dict[str, object]:
    """What the Decisions card shows for Plumb's GPU switch."""
    reason = cuda_unavailable_reason()
    return {
        "available": reason is None,
        "enabled": plumb_use_cuda(),
        "reason": reason or "",
    }
