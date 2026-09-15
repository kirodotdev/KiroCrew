"""Slack actions core — provider-side logic independent of dispatcher/credential.

This package holds the *logic* of talking to the Slack Web API correctly:
official request/response shapes, per-method pagination, the three-stage
external upload protocol, and text/Block-Kit business validation.

Boundary with error classification:

- This slice (S1) delivers the payload/pagination/validation/upload-stage logic
  plus negative fault tests keyed on Slack's own native error strings, and it
  records those verified strings as data
  (:mod:`kiro_crew.connections.vendors.slack.errors`). It does **not** classify
  those strings into a taxonomy.
- The error-string → classification MAPPING is delivered separately by consuming
  W01's ``kiro_crew.connections.control_plane`` ``ErrorClass`` / ``operation_error``
  (owner W01, tracked as ``it_d610cbb5``), NOT here. This package does not
  import that control plane, does not define an error-class enum, and does not
  fork W01's taxonomy.

What this package is deliberately NOT:

- It is **not** the shared connector control plane. It does not define, copy, or
  claim to be the campaign's RUN-01 typed-error taxonomy. RUN-01 is owned by the
  W01 control-plane slice (``kiro_crew.connections.control_plane``); this package
  neither imports it nor forks its enum.
- It does **not** hold credentials, open sockets, or drive a dispatcher. Every
  function here is pure: shapes in, shapes out. The live inbound path
  (Socket Mode via ``slack.transport_dispatch`` / ``slack.events``) and the
  binding/authorization logic in ``transport.py`` are untouched.
- It does **not** build a second auth / governance / retry / envelope framework.
  Retry policy classification lives in :mod:`kiro_crew.slack.retry`; this package
  only describes the *recovery logic* (e.g. how 429 recovery must avoid a
  duplicate send) as data a caller applies.

Packaging note
--------------
This package lives under ``kiro_crew.connections.vendors.slack``. Its parent
``kiro_crew.connections.vendors`` has NO ``__init__.py`` in this slice on
purpose: that container anchor is owned by the W01 control-plane slice, and this
slice neither creates nor modifies it. This package is stacked on W01's PR that
supplies the anchor. Under the repo's test invocation (``pythonpath = src`` in
``setup.cfg``) the module resolves as a PEP 420 namespace subpackage and imports
cleanly, and with the anchor present via the stack the setuptools
``packages = find:`` build discovers this package so the wheel carries it. That
packaging is verified against the stacked parent branch, since the parent owns
the anchor.
"""

from __future__ import annotations
