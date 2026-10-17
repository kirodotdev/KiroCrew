"""Dataset-specific loaders. Every per-dataset quirk lives behind this line.

The neutral contract is ``..corpus``; an adapter's whole job is to absorb one
dataset's shape so nothing downstream has to know which file it came from.
"""

from __future__ import annotations

from .locomo import load_locomo, load_locomo_file
from .longmemeval import load_longmemeval, load_longmemeval_file

__all__ = [
    "load_locomo",
    "load_locomo_file",
    "load_longmemeval",
    "load_longmemeval_file",
]
