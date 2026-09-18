"""The crew bundle producer.

Owns curation (deny-by-default) and the four-entry bundle the image layer copies
in. See ``PACKAGING-CONTRACT.md`` section "T1 -- curation and the bundle
producer" for the interface the other tracks depend on, and the module docstring
of :mod:`packaging.build` for why this is a fresh, self-contained port rather
than a copy of ``serving/smc/bundle.py``.
"""
