# Crewmate dashboard instances

A template is shared. An **instance** is one crewmate's copy of one template, and the
copy is the whole design: a built-in template shipping a new version does not change
what a crewmate's Dashboard tab shows.

Two modules and one route, each with one job:

| Piece | Where | What it owns |
|---|---|---|
| registry | `dashboard_templates/catalog.py` | the built-in templates, one loader |
| instance | `dashboard_templates/instance.py` | reading the copy and deriving its state |
| route | `dashboard/handlers/member_dashboard.py` | what the frame reads |

## The instance module reads and never writes

`instance.py` decodes the record under the member's own space, answers which of four
states that copy is in, and reads back a staged page. It has no `adopt`, no `edit`, no
`rollback`, no preview STAGING and no history fold, so a record it decodes was not put
there by this gateway. `test_member_dashboard_routes.TestNothingHereWritesARecord`
asserts that absence by name, with a positive control so an empty module cannot pass
it.

What replaces the writers is the no-template successor: the agent composes a page from
a data-type catalog and the result is stored as an artifact of `kind="dashboard"`, so
there is no second per-crewmate template store to keep in step with it.

**Two readers outlive their writers, and both answer nothing.** `read` has no record
to find, because nothing writes one; `staged_preview` has no `preview.json` to find,
because nothing stages one. They are still here because their callers are — the member
dashboard's own page read and its `?preview=1` read — and those callers come out with
them in one step rather than being left calling a name that is gone. Unreachable
rather than absent is the deliberate intermediate state, not an oversight.

## Two versions never mix

Neither is called "the" version, because conflating them is what makes a cached page
wrong.

`template.version`
: The version of the template that was copied.

`instance_version`
: This copy's own counter.

A third number, the crew-log position the rendered VALUES were read at, belongs to the
data channel. The instance module neither reads nor stores it.

## Four states, derived

The state is DERIVED at read time rather than stored. A stored state would be a claim
about the registry made when the record was last written, and the registry moves on
its own: a built-in shipping a new version makes every copy of it stale without
touching one record, so a stored flag would say `live` forever.

| State | What it means | What the frame does |
|---|---|---|
| `empty` | no record, or no template adopted | the empty state, or the default page |
| `live` | the copy matches its source | render |
| `stale` | the copy is complete but cannot be compared to its source | render under a stale band |
| `error` | the copy does not parse | explain, never render |

`error` is decided FIRST and without consulting the registry: a copy that cannot be
parsed cannot be rendered whatever the registry says about its template. `stale`
covers two different facts that need the same answer from a reader -- the template is
no longer in the registry, and the template is now at a higher version than this copy.

`state_reason` carries one sentence saying why, because a surface that must explain an
empty or broken dashboard needs words rather than a blank frame.

## The default is resolved, never stored

`default_instance` reads `DEFAULT_TEMPLATE_ID` from the registry on demand and
persists nothing: `instance_version` stays 0 and the state stays `empty`, because
nothing was adopted and nothing was written. A registry that serves no such id answers
`None`, which is the ordinary state of a build that ships the loader without
templates, so it is logged and reported rather than raised.

## Registry rules

`catalog.py` serves only pages that SHIPPED with the product. A dashboard page runs its
own inline script against a crewmate's fold values -- a chart is script or it is
nothing -- inside a frame that may navigate itself, so a page that renders is a page
trusted with the task titles and summaries it is handed. The only provenance that
carries a human review is a directory in this repository, which is why
`RENDERABLE_SOURCES` holds one value.

A broken template is REPORTED, not raised: `list_templates` returns the entries it
could load and the directories it could not, because one unparsable directory must not
hide every good template behind it. The directory name is the id, and the manifest's
own `id` must agree with it: a directory holding a manifest that names a different id
is two names for one template, and a lookup by either name would find or miss it
depending on which name the caller happened to hold.

## The HTTP boundary

`GET /api/members/{slug}/dashboard?member=<name>` is the one route the frame reads.

`?member=` is REQUIRED. Slugification is lossy, so two crew names can reach one slug,
and an instance is one directory per slug -- so for a colliding slug the record
belongs to neither crewmate and the route refuses it. The exact name must derive the
slug, exist in config, and be the only name that derives it.

The read is OWNER-ONLY and an app token is denied outright. The fields this body
carries are work-ledger and crew-log data, and `work_ledger_board` answers a non-owner
`owner_only` for the same values, so arriving as rendered html does not make them a
wider audience's.

A crewmate that adopted nothing answers 200 with `state: "empty"`, never 404: having no
dashboard yet is the ordinary first state of every crewmate, and a 404 would make
"nothing adopted" and "no such member" one reading for the tab.

## The executed page comes from the catalog

The record is a file under the member's own space, which is WRITABLE. Gating the render
on the record's own `manifest.source` would ask that file to vouch for itself: anything
that could append a `<script>` to the stored page could leave the `builtin` label in
place beside it. The label is not evidence, so it cannot be the gate.

What is trusted is the repository. The record's `template_id` names a directory in it,
so the id is read from the record and the BYTES are read from the catalog. A stored
page may stay on disk -- it is what the crewmate copied -- but nothing executes it.
