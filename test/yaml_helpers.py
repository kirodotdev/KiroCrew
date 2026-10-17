"""Parse YAML through an explicit loader class, without calling ``yaml.load``.

CloudFormation templates carry intrinsic tags (``!Sub``/``!Ref``/``!GetAtt``/
``!If``) that ``yaml.safe_load`` refuses, so the deploy tests register tag
constructors on a ``yaml.SafeLoader`` subclass and parse with that.

Driving the loader instance directly is exactly what ``yaml.load`` does with an
explicit ``Loader=``, so the parse is unchanged — but it keeps the safe base
class as the only construction path and leaves no ``yaml.load`` call for
scanners that key on the call name alone and cannot see that the loader
subclasses ``SafeLoader`` (bandit B506 / "Unsafe YAML Load").
"""

from __future__ import annotations

from typing import IO, Any

import yaml


def load_with(loader_cls: type, stream: str | bytes | IO[Any]) -> Any:
    """Parse ONE YAML document from ``stream`` using ``loader_cls``.

    ``loader_cls`` MUST derive from ``yaml.SafeLoader``. A loader that can
    construct arbitrary Python objects is refused rather than run, so widening
    the parse by swapping the class fails loudly instead of silently.
    """
    if not (isinstance(loader_cls, type) and issubclass(loader_cls, yaml.SafeLoader)):
        raise TypeError(f"loader must subclass yaml.SafeLoader, got {loader_cls!r}")
    loader = loader_cls(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
