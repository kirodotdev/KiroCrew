"""The closed grammar of board tag ids that may carry an agent-write grant.

Dependency-free on purpose: :mod:`kiro_crew.context` screens the trusted
``[BOARD]`` line with this grammar, and ``context`` must stay free of
``kiro_crew.dashboard`` imports (seventeen dashboard modules import it; a
reversed edge is a latent cycle). :mod:`kiro_crew.dashboard.chat_tag_grants`
enforces the same grammar at the grant mint and re-exports these names.

The ONLY tag ids that may ever carry a grant: every id the dashboard mints is
``uuid4().hex[:12]`` (``chat_tags.create_tag_definition``); the built-in
workflow states are the fixed code-level set below. Nothing else can be a
legitimately granted tag, so a grant on any other id is a planted one:
``tags.json`` is agent-writable and an agent that hand-writes a tag whose NAME
reads benign in the dashboard but whose ID spells an instruction would, once a
human toggled that tag's status (a routine PATCH that mints a grant), ride the
trusted ``[BOARD]`` rail with an id the human never saw. Pinning the grammar at
the mint closes that class structurally -- the injection heuristic on the rail
becomes redundant instead of the last defense. Extending the defaults is a code
change here, by design; a test pins this set equal to the status ids in
``DashboardState._DEFAULT_TAGS``.
"""

from __future__ import annotations

import re

DEFAULT_TAG_IDS = frozenset({"planned", "todo", "implementation", "review", "done"})
_DASHBOARD_TAG_ID_RE = re.compile(r"[0-9a-f]{12}")


def is_grantable_tag_id(tag_id: object) -> bool:
    """True only for an id the dashboard could have minted or a built-in default.

    The grammar is closed on purpose: a slug that is not one of these is an id
    the dashboard never issued, however plausible it reads.
    """
    return isinstance(tag_id, str) and (
        tag_id in DEFAULT_TAG_IDS or _DASHBOARD_TAG_ID_RE.fullmatch(tag_id) is not None
    )
