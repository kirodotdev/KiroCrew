# Route access

## Overview

An HTTP route the gateway registers can carry a label: what it does (an **action
name**, such as `widgets.reset`) and how long its request and response shape is
promised (a **tier**). This spec says where a label is recorded and how to pick its
two parts.

**No route is labelled yet.** The table is empty, and every route registered today is
listed in a baseline instead. What exists is the mechanism: a new route must come
with a label, and the baseline may only shrink.

The labels are a record. No request reads them, and they do not decide which caller
may call a route: each route's own checks do that, unchanged.

The tier names here belong to this table. They do not override the status markings of
the Crew API RFC (`docs/request-for-change/rfc-crew-api.md`) or of its OpenAPI files:
for a `/v1` route those stay the record of what is promised.

| Concern | Where |
|---|---|
| The descriptor and the route table | `kiro_crew.dashboard.route_access` |
| The test that holds registered routes against the table | `test/test_route_access_labels.py` |
| Routes that have no entry yet | `.github/route-access-baseline.txt` |
| The review rule for a new entry | `a-route-declares-its-action-and-tier` in `AUTOSDE.yaml` |

## What a label is

A label is a `RouteAccess(action, tier)`, an entry in `ROUTES` in `route_access`
keyed by `(METHOD, path pattern)`. The pattern is the route's canonical path: the path
as registered, with any regex dropped from a `{name:regex}` segment. For example, an
invented route registered as `/api/widgets/{name:.+}` is keyed as
`/api/widgets/{name}`.

Registering a GET route registers a HEAD route beside it. That HEAD route takes the
GET route's entry and needs none of its own.

The table sits apart from route registration for two reasons. One handler can serve
two routes, so a label on the handler could not tell them apart. And registration runs
on the gateway boot path, where a label must add no work.

## Picking an action name

- Shape: `<area>.<verb>`, lower case, parts joined by dots: `widgets.read`,
  `widgets.reset`. `ACTION_NAME_RE` is the exact form.
- `<area>` is the feature a user knows, not the file the handler lives in.
- The verb says what the handler does. A handler that writes a file, starts a job
  or changes state is not a `.read`, whatever its HTTP method.
- When the handler already passes an operation name to the audit log, reuse its
  verb.

## Picking a tier

| Tier | Constant | Promise |
|---|---|---|
| `stable` | `STABLE` | Request and response shapes only gain fields |
| `preview` | `PREVIEW` | The shape is nearly settled; small changes are possible |
| `unstable` | `UNSTABLE` | None. The shape changes whenever the dashboard needs it to |

A new route is `unstable` unless a reviewed contract says otherwise. A `stable` or
`preview` route has a contract test for its request and response. A `/v1` route's
tier must agree with what the Crew API RFC and its OpenAPI file say about that route.

## The baseline

`.github/route-access-baseline.txt` lists the routes that had no entry when the
table arrived, one `<METHOD> <path pattern>` per line, sorted by path and then by
method. A HEAD route beside a GET route of the same path has no line.

The baseline only shrinks:

- A new route gets an entry in `ROUTES`. Adding a baseline line for it is a
  review finding (`a-ratchet-may-only-tighten` lists the file).
- A listed route that gains an entry loses its line in the same change;
  `test_a_labelled_route_has_no_baseline_row` fails otherwise.
- A listed route that is removed loses its line too.

## What the test checks

`test/test_route_access_labels.py` builds the gateway's route table the way
`test_dashboard_route_table.py` does and fails when:

- a registered route has neither an entry nor a baseline line;
- an entry names no registered route (a typo, or a route that moved);
- a baseline line names a route that has an entry;
- the baseline is unsorted, repeats a line or holds a malformed one;
- an entry's action name does not match `ACTION_NAME_RE`, or its tier is not one of
  the three.

The test sees the routes a default startup registers. A route an installed app
registers at run time, or one registered only in development mode, is not in that
table. Nor is a route that a distribution adds through
`DashboardContributor.contribute_routes`: the rule covers the routes core registers,
so such a route needs no label and has no baseline line.

## Worked example

This example is invented: there is no "widgets" feature and no such route. It shows
a change that adds a read route and a write route.

```python
# kiro_crew/dashboard/routes/widgets.py
app.router.add_get("/api/widgets", handlers.api_widgets_list)
app.router.add_post("/api/widgets/{widget_id}/reset", handlers.api_widget_reset)

# kiro_crew/dashboard/route_access.py, in ROUTES
("GET", "/api/widgets"): RouteAccess("widgets.read", UNSTABLE),
("POST", "/api/widgets/{widget_id}/reset"): RouteAccess("widgets.reset", UNSTABLE),
```

## Checklist

- [ ] Each new route has a `RouteAccess` entry; the baseline did not grow.
- [ ] The action name says what the handler does; a GET that writes is not a `.read`.
- [ ] The tier is `unstable` unless a contract was reviewed and a contract test is in
      the change.
- [ ] A baselined route that gained an entry, or was removed, lost its baseline line.
