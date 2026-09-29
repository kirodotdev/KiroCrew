"""The durability pair: the writer that makes this task's state survive replacement.

A crew task is replaced routinely -- every deploy, every failed health check, every
platform update -- and its filesystem goes with it. This package is what makes the
state outlive that, and it is deliberately one package rather than two, because a
backup whose restore does not return the bytes is worse than an honest gap: nothing
warns.

Three parts, and the seam between them is the object key:

* :mod:`container.sidecar.store` -- the bucket, as ``put`` and ``get`` and nothing else.
* :mod:`container.sidecar.backup` -- one cycle: what must be durable, copied from one
  descriptor per object with no staged duplicate and nothing skipped for its size.
* :mod:`container.sidecar.restore` -- the two authority files, back on disk before the
  backend starts, validated so bytes it would silently ignore refuse the boot instead.

Which process runs which half is a consequence of when each is needed. The restore must
COMPLETE before the backend starts, so the supervisor calls it directly, in its startup
order, where that is enforced. The backup runs for the life of the task, so it is this
package's ``__main__``: a third child the supervisor starts after the front.

The keys both halves address come from :mod:`container.common.keys`, which the front's
on-demand transcript fetch reads too. One derivation, three callers: drift between a
writer and a reader is invisible, because a GET that misses looks exactly like a
customer who never had a conversation.
"""

from __future__ import annotations

__all__: list[str] = []
