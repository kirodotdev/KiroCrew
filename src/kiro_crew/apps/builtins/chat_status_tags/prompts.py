"""The reconcile prompt — editable, with a shipped default.

The ``sdlc-tag-reconcile`` cron used to carry its full instructions inline in
``app.json``. That made the one thing an operator most wants to tune — WHAT the
reconciler looks for and how aggressively it promotes — unreachable without
editing the manifest, which a builtin app's user cannot do.

The reconciler reaches the gateway's chat API through the credentialed
``chat_status_tags_api`` MCP tool, NOT by minting a dashboard token. A
``kirocrew token`` mint is refused by Kiro Crew's shipped security deny floor for
every agent (the ``credential-exfil-kirocrew-token`` rule), so a prompt that
minted a token would silently no-op every run. The tool is the ONLY credentialed
path the reconciler has to that API.

So the instructions live here as ``DEFAULT_RECONCILE_PROMPT``. The prompt reaches
the agent as the reconcile cron's OWN ``message`` — a trusted instruction channel
that arrives as the agent's own instructions and needs no tool call — NOT as a
file the agent must fetch and then distrust. Two live pod runs proved the
file-read design broken on both counts: one run read the operator's edit, judged
it untrusted data, and ignored it wholesale; the other could not read the file at
all because every path to it was approval-gated, so it produced nothing.

The plain-text file (``settings.py``, ``reconcile-prompt.md``) remains the
PERSISTENCE layer of record: the app page reads and writes it, and the effective
prompt is pushed into the live cron's ``message`` whenever it is saved/reset
(``backend/routes.py``) and re-synced on startup / heal / repair / enable-toggle
(``hooks.on_startup`` and the repair path), because ``register_app_crons_with_service``
rebuilds the cron from the IMMUTABLE manifest and would otherwise clobber a custom
prompt back to this default on every restart.

The manifest cron ``message`` in ``app.json`` is this exact default text; a test
asserts the two match byte-for-byte so they cannot drift.

Keep this constant as the single source of truth for the default: the manifest
cron message, the ``GET`` route's ``defaultPrompt`` field, and the ``is_default``
comparison all read it, so editing the wording here (and the identical manifest
string) is the only edit needed to change the shipped default.
"""

from __future__ import annotations

#: The full reconcile instructions, shipped as the default and seeded to
#: ``reconcile-prompt.md`` on startup. Promotions are ONE-WAY (never a
#: downgrade) and the health tags (stuck/network/error) are never touched.
DEFAULT_RECONCILE_PROMPT = (
    "You are the SDLC tag reconciler for dashboard chats. Backstop the two "
    "transitions an idle chat's agent forgets: a chat that OWNS an open pull "
    "request should carry the 'review' status tag, and a chat whose owned pull "
    "requests have ALL merged should carry 'done'. Promotions only — NEVER "
    "downgrade a status tag, and never touch the stuck/network/error health "
    "tags. You reach the gateway's chat API ONLY through the "
    "`chat_status_tags_api` MCP tool — call it with {method, path, slot_key?, "
    "query?, body_json?}. Do NOT mint a dashboard token: `kirocrew token` is "
    "refused by security policy and would make this run silently do nothing. "
    "Procedure: (1) call `chat_status_tags_api` with method='GET' "
    "path='/slots'. This returns every slot; each slot object carries `key`, "
    "`tags` (its current tag ids), and `source_links` — the pull-request, "
    "merge-request and issue URLs already extracted from that slot's messages. "
    "You CANNOT read a chat's raw messages (there is no message-detail path in "
    "this surface, by design); `source_links` is your ONLY view of what a chat "
    "references, so work from it. Keep idle slots whose status tag is not "
    "already 'done'. (2) For each candidate, take the pull-request URLs from "
    "its `source_links` entries whose `kind` is 'change' and that match "
    "github.com/<owner>/<repo>/pull/<n>; skip slots with none. (3) A chat that "
    "references a pull request in its own transcript is treated as OWNING it — "
    "`source_links` already reflects what the chat worked on, so every 'change' "
    "link on the slot counts as owned. (4) Check each owned PR's state with "
    "`gh pr view <url> --json state,mergedAt`. A PR counts as merged ONLY when "
    "`mergedAt` is non-null; a closed-but-unmerged PR is neither merged nor "
    "open — it never justifies 'done'. (5) Decide: all owned PRs "
    "merged -> 'done'; any owned PR open -> 'review'; otherwise leave "
    "unchanged. (6) Apply only strict promotions in the order planned < todo < "
    "implementation < review < done: call method='GET' path='/tags' to map tag "
    "names to ids (creating a missing status tag with method='POST' "
    'path=\'/tags\' body_json=\'{"name": "<name>", "status": true}\'), then '
    "call method='PUT' path='/slots/{slot}/tags' with the slot's `key` as "
    "slot_key and body_json holding the slot's current tag ids minus any "
    "status-tag id plus the new status tag's id. If `gh` is unavailable, "
    "produce NO output and stop. If nothing changed, produce NO output. "
    "Otherwise report one line per promotion."
)
