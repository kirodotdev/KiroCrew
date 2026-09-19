"""Slack actions core — provider-side logic independent of dispatcher/credential.

This package holds the *logic* of talking to the Slack Web API correctly:
official request/response shapes, per-method pagination, the three-stage
external upload protocol, and text/Block-Kit business validation.

Error classification:

- The payload/pagination/validation/upload-stage logic plus negative fault tests
  key on Slack's own native error strings, and those verified strings are
  recorded as data (:mod:`kiro_crew.connections.vendors.slack.errors`).
- The error-string → classification MAPPING lives in
  :mod:`kiro_crew.connections.vendors.slack.error_mapping`. It CONSUMES W01's
  ``kiro_crew.connections.control_plane`` ``ErrorClass`` / ``operation_error``
  taxonomy (``it_d610cbb5`` tracks that consuming relationship): it imports the
  control plane's closed set, classifies each recorded Slack native string into
  it, and neither defines its own error-class enum nor forks W01's taxonomy.

What this package is deliberately NOT:

- It is **not** the shared connector control plane. It does not define, copy, or
  claim to own the campaign's RUN-01 typed-error taxonomy. RUN-01 is owned by the
  W01 control-plane slice (``kiro_crew.connections.control_plane``); the mapping
  here imports that taxonomy and classifies into it rather than forking its enum.
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
purpose: that container anchor belongs to the W01 control-plane slice, which
this slice neither creates nor modifies. Under the repo's test invocation
(``pythonpath = src`` in ``setup.cfg``) the module resolves as a PEP 420
namespace subpackage and imports cleanly, and with the anchor present the
setuptools ``packages = find:`` build discovers this package so the wheel
carries it. Packaging is verified against ``main``, which provides the anchor.
"""

from __future__ import annotations
