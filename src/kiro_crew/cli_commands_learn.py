"""``kirocrew learn``: save, list and remove learned corrections (lessons).

``_learn`` writes and reads the lesson store without loading the embedding model, so
what it prints says when a saved lesson's vector is filled in later
(``_LEARN_EMBED_NOTE``, ``_LEARN_DEDUP_NOTE``); ``_lesson_scope_suffix`` renders a
lesson's repo scope.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import json
import sys

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.learn import LessonStore
from kiro_crew.project_scope import scope_is_admissible, scope_selector_is_inadmissible
from kiro_crew.security import redact, redact_credentials, redact_exfiltration_urls
from kiro_crew.terminal_safe import _TERMINAL_CTRL_RE
from kiro_crew.validation import normalize_lesson_category
from kiro_crew.vector_memory import (
    LessonWriteOutcome,
    VectorMemoryStore,
    _lesson_display_text,
    _lesson_scope,
    _lesson_scope_unusable,
)

# Appended to every `learn add` output that wrote something. This command builds
# its store with no embed_fn (loading the embedding model would add its startup
# cost to every CLI invocation), so an insert lands with a NULL vector and an
# enrichment CLEARS the stored one (the upsert keeps a vector only when the value
# is unchanged). Either way the row is repaired by the gateway's boot-time
# re-embed sweep, not by this process — an unqualified success message would
# overstate what happened, and a user searching semantically before the next
# gateway start would not find the lesson they were just told was saved.
# "once its embedding backend is ready" is the sweep's own guarantee, not
# hedging: _wait_then_backfill defers the sweep to a later boot when the
# embedding model has not landed, so "on its next start" would over-promise.
_LEARN_EMBED_NOTE = (
    "  Stored and keyword-searchable now; the embedding vector is filled by the\n"
    "  gateway's re-embed sweep after it next starts, once its embedding backend\n"
    "  is ready."
)


# INSERTED only. An enrichment resolves against the ONE existing row it rewrites
# (write_lesson pass 1 sets ``matched`` and pass 2's generic scan runs over
# ``[] if matched else lesson_rows``), so the substring/topic-overlap claim is
# true only for an insert — printing it on ENRICHED would report checks that
# never ran, the same defect this change fixes.
_LEARN_DEDUP_NOTE = (
    "  (Semantic dedup did not run at write time; substring/topic-overlap dedup\n" "  still did.)"
)


def _lesson_scope_suffix(scope: str | None) -> str:
    """Render a lesson's ``repo_scope`` so same-rule rows in two scopes read apart.

    A lesson's identity is ``(rule, repo_scope)`` and ``learn remove --repo-scope``
    selects by it, so a listing that hid the scope showed two distinct lessons as
    one duplicated line and gave the user nothing to pass. ``""`` is the global
    row and renders bare (the common case); a fragment names that scope; ``None``
    marks a stored scope the store cannot use, which only an unselective remove
    reaches -- worth saying where the user decides which flag to pass. A stored
    fragment the credential redactor would alter is printed as unusable too,
    like the dashboard list: the selector has to round-trip byte-exact, so it
    cannot be printed redacted, and printing it raw is not an option.
    """
    if scope is None:
        return " (scope: unusable)"
    if not scope:
        return ""
    # Strip terminal controls FIRST, then judge the stripped text: a credential
    # split by an embedded escape sequence would pass the redactor whole and be
    # reassembled by the stripping, so only the value that is printed is judged.
    printable = _TERMINAL_CTRL_RE.sub("", scope)
    if not printable or redact(printable) != printable:
        return " (scope: unusable)"
    return f" (scope: {printable})"


def _learn(args: argparse.Namespace) -> None:
    """Save, list, or remove learned corrections."""
    from kiro_crew.config.memory_sections import memory_writes_off_key

    jsonl_store = LessonStore()
    cfg = KiroCrewConfig.load()
    action = getattr(args, "learn_action", None)
    # Global persistence switch (memory.persistence_enabled): the CLI writes to
    # the store directly (no HTTP), so it carries its own check mirroring the
    # POST /api/lessons refusal. Refused ahead of the vector store because
    # constructing and init-ing it creates or migrates memory.db, which a
    # refused write must not do; list and remove still need that store, so the
    # check is scoped to the add action. memory.writes_enabled is refused the
    # same way.
    writes_off = memory_writes_off_key(cfg.memory)
    if action == "add" and writes_off is not None:
        print(
            "Lesson NOT saved: persistent memory writes are disabled "
            f"({writes_off} is false). Re-enable with: "
            f"kirocrew config set {writes_off} true"
        )
        return
    vs = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim, config=cfg)
    vs.init()
    try:
        if action == "add":
            rule = args.rule
            category = args.category
            negative = getattr(args, "negative", None)
            # The reporting form, not the bool. Most falsy returns mean the lesson
            # is already stored exactly as submitted, so reading one as "the vector
            # store did not take it" and writing a second record into lessons.jsonl
            # would leave a redundant record for a lesson that was fine, and print
            # "Saved:" when nothing needed saving.
            #
            # The one return that really does mean "nothing was stored" is a REFUSAL,
            # and routing that into the JSONL store would be worse than redundant:
            # that store validates no content at all, so a value this store rejected
            # (an injection-pattern clause) would land there anyway, and the context
            # builder reads lessons.jsonl whenever the vector store holds no lessons.
            #
            # So there is no fallback here, not a narrowed one. There is no "vector
            # store unavailable" state to fall back from: ``vs`` is constructed and
            # ``init()``-ed unconditionally above.
            result = vs.write_lesson(rule, category, negative)
            neg = f" ({negative})" if negative else ""
            # What the save COST. The store's dedup rules tombstone a stored lesson
            # the submitted rule contains or heavily overlaps, and printing "Saved:"
            # plus the dedup NOTE below without ever naming the removed rule would
            # hide that adding a conditional rule retires the general rule inside it,
            # leaving a row with is_deleted=1 as the only trace. Printed in
            # full for every outcome that can carry it, because the row is a
            # tombstone: `learn list` cannot show it and this is the last readable
            # copy.
            lost = ""
            if result.superseded:
                # Strip terminal controls FIRST, then redact. The order is the whole
                # correctness of this function, and the intuitive order is the wrong
                # one: redacting first leaves `AKIA<CSI>IOSFODNN7EXAMPLE` untouched,
                # because the escape breaks the token so the credential regex does not
                # match it -- and the control-strip then REASSEMBLES the complete
                # credential and prints it. Measured: redact-then-strip leaks that
                # input verbatim; strip-then-redact masks it. Stripping can only ever
                # JOIN characters, never split a token, so running it first can only
                # help the regex, never hide a credential from it.
                #
                # Both treatments are needed: an OSC payload stored in a rule
                # would otherwise be interpreted by the terminal, and a credential
                # in a lesson would otherwise be printed. This block prints a rule the user did NOT ask
                # to see, on the one path guaranteed to surface it.
                #
                # Redacting at all, rather than control-stripping only, is
                # deliberate. `learn list` prints every stored lesson with
                # control-stripping alone, and that looks like the convention to
                # match -- but it is an EXPLICIT request to see the store, while this
                # block surfaces a lesson the user is about to lose. The right
                # comparison is the route, which delivers the SAME field and does
                # redact, so controls-only would make one feature's two delivery paths
                # disagree about whether a superseded rule may carry a secret. A lesson
                # written by history consolidation also holds model output the user
                # never typed, so "their own store" is not "their own knowledge".
                def _safe(text: str) -> str:
                    text = _TERMINAL_CTRL_RE.sub("", text)
                    text, _ = redact_exfiltration_urls(text)
                    text, _ = redact_credentials(text)
                    return text

                lines = "".join(f"\n    - {_safe(s)}" for s in result.superseded)
                lost = (
                    f"\n  REMOVED {len(result.superseded)} stored "
                    f"lesson{'s' if len(result.superseded) != 1 else ''} this rule "
                    f"contains or overlaps:{lines}\n"
                    "  Those are no longer in effect. A verbatim re-add is DECLINED "
                    "while this rule is stored, so to restore one exactly, "
                    "`learn remove` this rule first; wording that shares few "
                    "significant words with it can coexist."
                )
            # The category is echoed ONLY where the store adopted the submitted one.
            # It is write-once (write_lesson's exact-rule pass, `resolve_exact_rule` in
            # vector_memory_runtime/lessons.py, builds an enrichment with the STORED
            # category, falling back to the submitted one only when the row has none),
            # so an insert is the single outcome where what was typed is what is held.
            # Anything else printing it would show a value the store may not have --
            # the same defect this PR fixes on the reporting side. `learn list` is
            # where stored values belong.
            if result.outcome is LessonWriteOutcome.INSERTED:
                print(
                    f"Saved: {rule}{neg} [{category}]\n{_LEARN_EMBED_NOTE}\n"
                    f"{_LEARN_DEDUP_NOTE}{lost}"
                )
            elif result.outcome is LessonWriteOutcome.ENRICHED:
                # No _LEARN_DEDUP_NOTE here: an enrichment matched its existing row in
                # pass 1, which SKIPS the generic dedup scan. Instead say what the
                # rewrite cost — the upsert cleared the vector the row already had.
                print(
                    f"Updated the stored lesson with this clause: {rule}{neg}\n"
                    "  The stored category is kept; `learn list` shows it.\n"
                    "  This rewrite cleared the row's existing embedding vector.\n"
                    + _LEARN_EMBED_NOTE
                )
            elif result.outcome is LessonWriteOutcome.UNCHANGED:
                # Nothing was written, and the store keeps the stored category and
                # NOT-clause rather than rewriting them on a re-submit.
                print(
                    f"Already stored, nothing written: {rule}\n"
                    "  A re-submit keeps the stored category and NOT-clause; "
                    "changing one means `learn remove` then `learn add`."
                )
            elif result.outcome is LessonWriteOutcome.DEDUPED:
                covered = f"Not saved: an existing lesson already covers this ({result.reason})"
                print(f"{covered}{lost}")
            else:
                # REFUSED -- and any outcome a later change adds, which is deliberate:
                # every branch above names ONE outcome, so a new one lands here and
                # exits non-zero rather than being silently reported as a success. A
                # non-zero exit so a script driving this command sees the failure.
                reason = f" ({result.reason})" if result.reason else ""
                print(
                    f"NOT saved: the memory store refused this lesson{reason}{lost}",
                    file=sys.stderr,
                )
                sys.exit(1)

        elif action == "list":
            vs_lessons = vs.get_lessons()
            if vs_lessons:
                for e in vs_lessons:
                    val = json.loads(e["value_json"])
                    # Rendered text for either storage shape: mapping-shaped rows
                    # (write_lesson's format and the onboarding import's) would
                    # otherwise print as a Python dict repr.
                    #
                    # The label reads the row's own category so this surface agrees
                    # with the dashboard's lessons panel; a legacy string row
                    # carries none, and the shared helper supplies the store's
                    # own "knowledge" default (display policy, strict=False).
                    category = normalize_lesson_category(
                        val.get("category") if isinstance(val, dict) else None,
                        strict=False,
                    )
                    text = _TERMINAL_CTRL_RE.sub("", _lesson_display_text(val) or str(val))
                    # The store's own readers decide the row's scope, so this
                    # surface and ``remove --repo-scope`` agree on what it is.
                    scope = _lesson_scope_suffix(
                        None if _lesson_scope_unusable(val) else (_lesson_scope(val) or ""),
                    )
                    print(f"  [{_TERMINAL_CTRL_RE.sub('', category)}] {text}{scope}")
            else:
                lessons = jsonl_store.load_all()
                if not lessons:
                    print("No lessons.")
                    return
                for le in lessons:
                    neg = f" — {_TERMINAL_CTRL_RE.sub('', str(le.negative))}" if le.negative else ""
                    # Same display policy as the vector-store branch above and
                    # the dashboard's JSONL path: a blank/legacy category gets
                    # the store's own "knowledge" default instead of printing [].
                    category = normalize_lesson_category(le.category, strict=False)
                    # Same classification the JSONL store's scope-selective remove
                    # applies: None is global, anything else is judged admissible.
                    stored_scope: str | None
                    if le.repo_scope is None:
                        stored_scope = ""
                    elif scope_is_admissible(le.repo_scope):
                        stored_scope = le.repo_scope
                    else:
                        stored_scope = None
                    scope = _lesson_scope_suffix(stored_scope)
                    print(
                        f"  [{_TERMINAL_CTRL_RE.sub('', category)}] {_TERMINAL_CTRL_RE.sub('', str(le.rule))}{neg}{scope}"
                    )

        elif action == "remove":
            # A lesson's identity is (rule, repo_scope), so a scoped and a
            # global lesson can share rule text. ``--repo-scope`` (None when
            # absent) restricts the delete to one scope; omitted, it matches
            # every scope. Passed to whichever store is live so both callers
            # -- this CLI and the agent-facing MCP path -- carry the same
            # discriminator. A selector the write surface would refuse (a
            # bare "/", an absolute path, a dot segment) is refused up front:
            # no admissibly stored row carries it, so canonical folding would
            # land the delete on rows the caller never named.
            repo_scope = getattr(args, "repo_scope", None)
            if repo_scope is not None and scope_selector_is_inadmissible(repo_scope):
                print(
                    f"--repo-scope does not name a usable scope: {repo_scope!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if vs.get_lessons() and vs.delete_lesson(args.query, repo_scope):
                print(f"Removed lessons matching: {args.query}")
            elif jsonl_store.remove(args.query, repo_scope):
                print(f"Removed lessons matching: {args.query}")
            else:
                print(f"No lessons match: {args.query}")

        else:
            print("Usage: kirocrew learn {add|list|remove}")
    finally:
        vs.close()
