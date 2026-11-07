---
title: Keep a slash-command name a saved workflow gave up by renaming bound to that workflow
status: draft
author: kirocrew agent session, directed by jeeshofone
created: 2026-11-02
last-audited: 2026-11-02
audited-at: d6d40e23a2
doc-pr: 28632
implementation-prs: []
tracking-issues: [23680]
supersedes: []
superseded-by: []
---

# RFC: Keep a slash-command name a saved workflow gave up by renaming bound to that workflow

Status: draft. Acceptance requested from a maintainer of the workflow library. Part of [#23680](https://github.com/kirodotdev/KiroCrew/issues/23680). Main-side references were read at `d6d40e23a2`. References to the removal reservation were read on [#27528](https://github.com/kirodotdev/KiroCrew/pull/27528) at head `5423c59f1a`, which is open and not on main.

## Summary

Renaming a saved workflow's slash-command name frees the old name at once. A later workflow can be saved under it, and a `/workflow <old name>` written into a scheduled message or a prompt then runs that later workflow with no notice. `rfc-workflow-library-removal` reserves the id and slug a workflow holds when it is removed, and says in its §4 that a slug given up by a rename is not reserved and that #23680 tracks it.

This document recommends that each record keep the slugs it gave up, in a new `former_slugs` list, and that slug allocation count them as taken. A `/workflow <former slug>` reaches the renamed workflow and the run notice names its current command. Removing the workflow reserves its former slugs together with its id and slug, through the same `_refs` set #27528 adds.

## Motivation

### Current state on main

- A record stores `id`, `slug`, `name` and `revisions`. A revision stores `revision`, `source` and `created_at` and no slug (`src/kiro_crew/workflows/library.py`, `_create_unlocked`).
- `_update_unlocked` overwrites `slug` with the newly allocated value and keeps no trace of the old one.
- `_unique_slug(requested, excluding_id=...)` (`library.py`) counts only the slugs live records hold now.

So the library cannot tell what a workflow used to be called, and the four steps in #23680 reproduce on any build: save `deploy`, rename it to `deploy-old`, save another workflow as `deploy`, and the written-down `/workflow deploy` runs the second one.

### What #27528 adds and leaves out

[#27528](https://github.com/kirodotdev/KiroCrew/pull/27528) is the removal implementation, moved from #19040, which `rfc-workflow-library-removal.md` still records in `implementation-prs`. It adds `_refs(definition)` (a record's id and slug), `_reserved_refs` (the refs every removed record holds) and `_taken_refs(definitions, excluding_id=...)`, which slug allocation uses. Its docstring for `_reserved_refs` says a slug a rename gave up is not in the set. The removal design is otherwise complete, so this gap is the one place a written-down command can still change meaning silently.

## Goals

- A slug a workflow gave up by renaming is never handed to a different workflow.
- A written-down `/workflow <former slug>` keeps running the workflow it named when it was written, and says the command has moved.
- Removing a workflow reserves its former slugs as well as its current id and slug.

## Non-goals

- A command to release a former slug. If one is wanted later, it is a separate change.
- Back-filling former slugs for renames made before this lands. No record holds that history, so those names stay free.
- Changing how a workflow's display `name` is matched by search.

## 1. Design

### 1.1 Stored state

`_update_unlocked` appends the outgoing slug to a `former_slugs` list on the record when a rename changes `slug`, skipping duplicates and the new slug itself. A rename back to a former slug removes it from the list, so a record never lists its own current slug. Records written before this change have no `former_slugs` and read as an empty list.

### 1.2 Refs

`_refs(definition)` returns the id, the slug and every former slug. Everything #27528 builds on `_refs` then covers former slugs with no further change:

- `_taken_refs` makes slug allocation skip them for every other record, so a new workflow asking for `deploy` gets `deploy-2`. A create with `exact_slug` (the path that has already shown `/workflow <slug>`) raises `WorkflowSlugTakenError` instead, as it does for a live slug; for a former slug its message says the slug is held as a former name of another workflow, not that it "is already a workflow". The allocation response names the refusal (the requested slug is held as a former slug of another workflow), so the caller knows why the number was added. A former slug is never released; that stays a non-goal.
- `_reserved_refs` reserves a removed record's former slugs for good.

### 1.3 Lookup

The lookup order #27528 sets (id first, then current slug) gains a third step: a ref that matches no id and no current slug reaches the live record that lists it in `former_slugs`. `/workflow`, the run route and `workflow_run` with `workflow` then start that record, and the run's first transcript line says "`/workflow <former slug>` now runs as `/workflow <slug>`", so the person can update what they wrote down. Former-slug resolution is a separate lookup, `resolve_for_run`, used only by these run paths and by read-only `get` callers that ask for it with `follow_former=True`. Plain `get`, which the save, rename and remove paths call to find their target, keeps matching only an id or a current slug. The PATCH and DELETE routes do not: they accept only an id or a current slug, so a stale `DELETE deploy` sent after `deploy` was renamed away answers not found and cannot remove the renamed workflow.

### 1.4 Lists

Lists and `/workflow` completion offer only the current slug. A former slug is a redirect, not a second name.

## 2. Alternatives considered

| Option | Result |
| --- | --- |
| Do nothing | The silent rebinding in #23680 stays. Removal is safe and rename is not, which is the inconsistency the removal RFC's §4 (`rfc-workflow-library-removal.md`) records. |
| Reserve the former slug but answer not found | Safe, but every scheduled message that names the old command breaks on the first rename. The redirect costs one lookup step and keeps them working. |
| Store the slug on each revision and derive former slugs | Same reservation, but a revision would carry a field that has nothing to do with its source, and allocation would scan every revision of every record. A list on the record is smaller and read once. |
| Refuse a rename while a cron job names the old slug | Covers only cron messages, not prompts or notes, and couples the library to the scheduler. |

## 3. Interaction with #27528

This design builds on #27528 and stacks on it: it changes `_refs`, `_update_unlocked` and the lookup helper that #27528 introduces in `library.py`. The implementation PR opens after #27528 merges, or on its branch if a maintainer prefers one review. If #27528 is closed, the same design applies to main's `_unique_slug` with a former-slug check added there, without the removal step in Goal 3.

## 4. Migration plan

| Phase | PR | Exit criteria |
| --- | --- | --- |
| 1. Former slugs | one PR after #27528 | A rename records the outgoing slug; a new workflow requesting it gets a numbered slug; `/workflow <former slug>` runs the renamed workflow and names its current command; removing the workflow refuses its former slugs for every lookup; records with no `former_slugs` load unchanged |

## 5. Test plan

One regression test in `test/test_workflows_library.py`: save `deploy`, rename it to `deploy-old`, save a second workflow requesting `deploy`. Assert that the second gets `deploy-2` that `resolve_for_run("deploy")` returns the first, and that plain `get("deploy")` returns nothing, so a PATCH or DELETE sent to `deploy` answers not found. A create with `exact_slug` for `deploy` raises `WorkflowSlugTakenError` with the former-slug message. Before the change, the second gets `deploy` and the lookup returns it, so the test fails on main. A second assertion in the same test removes the first workflow and checks that `deploy` still reaches nothing and is still not handed out.

## 6. Backward compatibility

`former_slugs` is optional on read. An older build reading a newer record ignores the field and behaves as it does today. No record is rewritten until it is next renamed.

## 7. Security considerations

None new. The redirect can reach only a record the caller could already reach by its id, and only for reads and runs. Changing or removing a record still needs its id or current slug (section 1.3), so a destructive call written against a former slug cannot reach the record that gave it up.

## 8. Open questions

1. Should the redirect notice also appear in a cron job's own run log, so a job owner sees it without opening the transcript?
