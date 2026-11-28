---
title: Edition editorial -- a pinned registry features its own apps on Discover
status: draft
author: iatpatelishan
created: 2026-11-17
last-audited: 2026-11-17
audited-at: c87ea0c41f
doc-pr: null
implementation-prs: [38174]
tracking-issues: [38128]
supersedes: []
superseded-by: []
---

# RFC: Edition editorial -- a pinned registry features its own apps on Discover

**Status:** draft. Nothing on main. A reference implementation is open as
[#38174](https://github.com/kirodotdev/KiroCrew/pull/38174), held until this design is agreed.
Answers [#38128](https://github.com/kirodotdev/KiroCrew/issues/38128).

## Summary

An edition that pins its own app registry through `AppsLoader.default_registries()` can opt
that registry in to featuring with `"editorial": true` on the pinned row. Kiro Crew then reads an
`editorial.json` at the root of that registry's index clone, in the same shape as the official
editorial document, and lists its sections on Discover ahead of the official ones. Nothing
changes for a build that pins no such row, which includes the public build.

## Motivation

Verified at `c87ea0c41f`.

- Discover's featured sections come from one document: `OFFICIAL_EDITORIAL_URL`
  (`src/kiro_crew/apps/official_editorial.py`), read by `load_sections`
  (`official_editorial.py`) and returned as `editorialSections` by `handle_registry`
  (`src/kiro_crew/apps/routes_registry.py`).
- That document can only usefully name apps in the official catalog: the dashboard resolves each
  `appRef` against the apps it can show and drops the rest
  (`website/src/pages/apps/useAppsData.ts`, `featuredSections`).
- A registry cannot mark its own rows featured: `featured` is stripped from external rows
  (`src/kiro_crew/apps/registry_pipeline/catalog.py`).

So an edition whose registry lists apps the official catalog does not can feature them only by
the derived pick, which it does not control.

## Goals

- An edition can curate featured sections for the apps its pinned registry lists.
- The curation lives with the registry and changes under that registry's own review, with no
  release of the edition or of Kiro Crew.
- Featured sections from an edition are read by exactly the same rules as the official ones.

## Non-goals

- No change to the official editorial document, its schema, or how official sections render.
- No new featured form (no carousel) and no video.
- No edition artwork in this version (see Open questions).
- No opt-in for an operator-configured registry.

## Design

### Opt-in

A pinned row may carry `"editorial": true`. It is honoured under the same rules as
`allowDefaultInstall` (`src/kiro_crew/apps/default_installs.py`): only on a build row from
`default_registries()`, never from `config.json`, only when the registry is in force at the
`owner` tier, and only for the literal `true`. `_allowed_registries` takes the flag name so both
flags share one check.

### Reading the document

`_fetch_and_cache_external_registry` asks `_fetch_external_registry_index` for the clone's
`editorial.json` only for an opted-in registry; every other registry's fetch makes the same call
as today. The file is read from the clone the index fetch already makes, so there is no second
clone and no new network path. It is refused when it is a symlink, larger than the official
document's byte cap, not a JSON object, or nested deeply enough to raise `RecursionError`.

### Caching

The document is kept beside the registry's index cache and replaced or removed with each index
refresh. It is written before the index, so an interrupted refresh leaves the old index beside the
new document, never a fresh index beside an older document. A copy left by a registry that is no
longer opted in is not shown.

### Listing and rendering

`handle_registry` lists the edition's sections ahead of the official ones; a failing edition
loader leaves the official sections in place. Sections go through the official projector
(`project_sections`) with artwork dropped, because artwork refs resolve against the official
catalog's origin. Each block is marked `useAppArt: true`, and Discover draws an app card in such
a block with the app's own store art, as it does for a derived pick. A block holding a collection
stays curated (no art), so no member lends the collection its art.

## Migration plan

1. **Core** ([#38174](https://github.com/kirodotdev/KiroCrew/pull/38174)). Exit criteria: a build
   with no opted-in row returns the same `GET /api/apps/registry` as before; an opted-in owner-tier
   row's `editorial.json` sections are listed first; every refusal case above leaves the index
   fetch intact.
2. **An edition** opts its pinned registry in and that registry publishes `editorial.json`. Both
   are outside this repository and inert until phase 1 ships.

## Backward compatibility

Compatible. The key is new and absent means `false`. Older clients ignore both the key and the
file.

## Security considerations

- The document is presentation only: Discover resolves every `appRef` against the apps it can
  already show, so it cannot add, install or trust anything.
- It is read only from an owner-tier, build-pinned registry, whose index is under the edition's
  change control.
- The cached copy is agent-writable, like every cache under the data home. The worst a local
  write can do is re-cut the featured list.

## Alternatives considered

- **Name the edition's apps in the official editorial.** They are not in the official catalog, so
  each reference resolves to nothing outside that edition, and the public store would carry
  another edition's curation.
- **Fetch the edition document separately.** A second clone per registry per refresh, where the
  index clone already holds the file.
- **Let a registry row carry `featured`.** Rows cannot express blocks, collections or order, and
  `featured` is stripped from external rows on purpose.

## Open questions

1. Should an edition be able to publish artwork? It would need a ref that resolves against the
   registry, served the way registry store art is today.
2. Should edition sections always come first, or should the edition say where they go relative
   to the official ones?
