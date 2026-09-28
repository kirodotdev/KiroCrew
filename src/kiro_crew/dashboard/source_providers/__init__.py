"""Owners behind the source-provider handler.

``kiro_crew.dashboard.handlers.source_providers`` keeps the HTTP handlers and the
dashboard owner gate, and re-exports every other name it defined -- and each
import callers reach through it -- from the module here that holds it. In import
order, each owner importing only owners earlier in this list:

* ``contract`` -- the refs, payload shapes, plugin protocol and errors;
* ``sanitize`` -- redaction of provider text and the aggregate payload cap;
* ``hosts`` -- the self-managed GitLab/Jira allowlists and the chip switch;
* ``plugins`` -- the registered-provider seam;
* ``links`` -- which URLs are source links: validation into refs;
* ``runner`` -- the bounded, audited provider CLI;
* ``projection`` -- the vocabulary every provider read and both caches share;
* ``github``, ``gitlab`` -- the CLI-backed provider reads;
* ``adf`` -- Atlassian Document Format to markdown;
* ``jira`` -- the REST-backed Jira reads;
* ``chip_status`` -- the sidebar chip cache, its deltas and the visibility gate;
* ``cache`` -- the coalesced full-payload, checks, issue and contributor reads;
* ``chip_refresh`` -- the background chip reads;
* ``mutations`` and ``review`` -- the owner-authenticated writes.

Tests and callers patch these names through the handler, which forwards the write
to the owner. A sibling is therefore always reached AS A MODULE
(``runner._run_json(...)``) and only a type is imported by name: a copied
function or constant would keep the value it had at import and never see the
patch.
"""

#: Every owner logs under the handler's historical name, so logging configuration
#: written against it keeps applying after the split.
LOGGER_NAME = "kiro_crew.dashboard.handlers.source_providers"
