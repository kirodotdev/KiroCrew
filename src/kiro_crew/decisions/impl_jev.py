"""Former name of :mod:`kiro_crew.decisions.impl_systemone`, kept for one release.

The System One client moved; this name now IS that module (one module object,
not a copy), so a reader that imports or patches through the old path reaches
the same functions. ``JevOracle``, ``JevProtocolError`` and ``JevHttpError`` are
aliases defined there.
"""

import sys

from kiro_crew.decisions import impl_systemone as _impl_systemone

sys.modules[__name__] = _impl_systemone
