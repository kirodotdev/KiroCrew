"""Where the gateway and the spec are known to disagree, by case, with the reason.

A maintainer rules a difference in before it is listed. A case named here must fail (a strict xfail): when the gateway or the spec is fixed
the case passes, the run fails, and the row is deleted. Keep it empty when you can.
The key is the case's test id, ``<operationId>:<case name>``.
"""

from __future__ import annotations

DIFFERENCES: dict[str, str] = {}
