# Worktree verification recipes

These traces prove a Kiro Crew change against an isolated gateway running the
changed worktree. They do not touch the live gateway or its data home. Run them
from the worktree root after the normal build and test gates pass.

## Command availability

The pod lifecycle, packaged scenarios, diagnostic commands, and pod-e2e harness
are on `main` today:

- `kirocrew pod scenarios [--json]`
- `kirocrew pod up/down/ls/status/logs/prune`
- `src/kiro_crew/apps/builtins/dev_fleet/skills/pod-e2e/scripts/pod-e2e.sh`

`kirocrew pod api` is not on `main` yet. It arrives with PR #8218. This guide is
sequenced after #8218: do not merge it first, and update recipes 1 and 3 in the
same review round if #8218's interface changes. Recipes that use `pod api`
deliberately fail their preflight until that command is installed:

```bash
kirocrew pod --help | grep -qw api || {
  echo "This recipe requires kirocrew pod api from PR #8218" >&2
  exit 1
}
```

`pod api` prints a stable JSON object with `name`, `method`, `path`, `status`,
`ok`, and `body`. It permits `GET` and `HEAD` by default; `POST`, `PUT`, `PATCH`,
and `DELETE` require `--allow-write`. It mints the selected pod's dashboard token
internally, so do not add a `token` query parameter.

Only three packaged scenarios exist: `empty`, `minimal`, and `rich`.
`kirocrew pod scenarios` prints each name plus its description. Use the smallest
one that establishes the state under test. The fixture job names asserted below
are owned by [`minimal/crons.json`](../../src/kiro_crew/tests_fixtures/minimal/crons.json).
Ten specialized payloads remain deferred until a verification recipe
demonstrates that one is needed; do not invent a fourth scenario in a feature
branch.

## Find the verification surface without searching the repository

Start at [`../feature-map/README.md`](../feature-map/README.md). Its row for a
user-facing feature gives the page, backend handler, and HTTP endpoint. That is
the trace from the product surface to the assertion target; do not rediscover it
with a repository-wide search.

For example, the **Schedule** row maps the schedule page to
`handlers/cron.py` and `GET /api/crons`. The backend recipe below therefore
asserts that endpoint against the `minimal` scenario, which contains one active
and one paused cron.

## Recipe 1: prove a backend change

This concrete trace proves the Schedule read path. Change only `PATH` and the
`jq` assertion when the feature-map row for your change names a different
endpoint.

```bash
set -euo pipefail
WT="$(basename "$PWD")"
PATH_TO_ASSERT=/api/crons

kirocrew pod scenarios
kirocrew pod --help | grep -qw api || {
  echo "This recipe requires kirocrew pod api from PR #8218" >&2
  exit 1
}

HANDLE="$(kirocrew pod up "$WT" --seed minimal --json)"
trap 'kirocrew pod down "$WT"' EXIT
printf '%s\n' "$HANDLE" | jq -e --arg wt "$WT" '
  .name == $wt and
  .status == "up" and
  (.base_url | startswith("http://127.0.0.1:"))
' >/dev/null

RESPONSE="$(kirocrew pod api "$WT" GET "$PATH_TO_ASSERT")"
printf '%s\n' "$RESPONSE" | jq -e '
  .status == 200 and
  .ok == true and
  ([.body.jobs[].name] | sort) ==
    (["daily minimal fixture ping", "paused weekly recap"] | sort)
' >/dev/null

kirocrew pod down "$WT"
trap - EXIT
```

This asserts all of the following:

1. the isolated gateway started from this worktree and returned its loopback
   `base_url`;
2. the feature-map endpoint answered HTTP 200 through `pod api`;
3. the handler read the seeded pod home rather than an empty or live data home;
4. `pod down` reclaimed the isolated home.

A successful boot without the `jq` API assertion is not backend verification.

## Recipe 2: prove a frontend change

The [pod-e2e skill](../../src/kiro_crew/apps/builtins/dev_fleet/skills/pod-e2e/SKILL.md)
owns the harness flags, manifest syntax, authenticated Playwright context, and
artifact contract. Follow that skill for harness details. This recipe adds the
worktree-specific proof delta: a seeded boot, a passing verdict assertion, and
non-empty screenshot evidence. Use `--no-suppress-first-run` only when the
changed surface includes onboarding or another first-run overlay.

```bash
set -euo pipefail
WT="$(basename "$PWD")"
HARNESS="$PWD/src/kiro_crew/apps/builtins/dev_fleet/skills/pod-e2e/scripts/pod-e2e.sh"
ARTIFACT_DIR="$HOME/.kirocrew-pods/.e2e-artifacts/$WT"

HANDLE="$(kirocrew pod up "$WT" --seed rich --json)"
trap 'kirocrew pod down "$WT"' EXIT
printf '%s\n' "$HANDLE" | jq -e '
  .status == "up" and (.base_url | startswith("http://127.0.0.1:"))
' >/dev/null

bash "$HARNESS" "$WT" --fe-only --no-suppress-first-run

jq -se '
  any(.[]; .phase == "smoke" and .status == "pass") and
  all(.[]; .status == "pass")
' "$ARTIFACT_DIR/verdict.jsonl" >/dev/null
test -s "$ARTIFACT_DIR/fe-smoke.png"

kirocrew pod down "$WT"
trap - EXIT
printf 'Evidence: %s\n' "$ARTIFACT_DIR/fe-smoke.png"
```

The built-in smoke phase proves the authenticated SPA shell, not the changed
feature. A feature-specific frontend change also needs the branch-local
Playwright assertion and screenshot described by the pod-e2e skill. Inspect the
resulting image before using it as PR evidence; a green verdict with a stale or
unrelated frame is not proof.

## Recipe 3: drive an agent inside the pod

The existing routes and payloads are:

| Operation | Route | Payload |
|---|---|---|
| Create | `POST /api/session-control/create` | optional `title`, `agent`, `folder_id` |
| Send | `POST /api/session-control/send` | required `target`, `message` |
| Read | `GET /api/session-control/read` | query `target`; optional integer `limit`, `since` |
| Stop | `POST /api/session-control/stop` | required `target` |
| Close | `POST /api/session-control/close` | required `target` |

The create response returns the new session key as `body.target`. Send returns
`body.started`; read returns `body.running`, `body.next_since`, and
`body.messages`.

**This recipe is not executable through PR #8218 as currently implemented.**
The session-control routes require a validated `X-Internal-Secret` and identify
the caller from `X-Session-Key`. PR #8218's `pod api` sends only a dashboard
query token and has no caller-session option. A live probe returns HTTP 403 with
`code: internal_secret_required`; even adding internal authentication alone
would leave create without a caller workspace. Do not claim that an agent was
driven through `pod api` until both requirements have a supported interface.

The intended trace below records the exact routes and bodies, but the first
request is expected to fail under the current #8218 implementation. It is kept
here as the acceptance trace for closing that compatibility gap, not as a green
recipe:

```bash
set -euo pipefail
WT="$(basename "$PWD")"

CREATED="$(kirocrew pod api "$WT" POST /api/session-control/create \
  --data '{"title":"cron verification agent"}' --allow-write)"
TARGET="$(printf '%s\n' "$CREATED" | jq -er \
  '.status == 200 and .body.ok == true and .body.target')"

SENT="$(kirocrew pod api "$WT" POST /api/session-control/send \
  --data "$(jq -nc --arg target "$TARGET" \
    --arg message 'list my cron jobs' '{target:$target,message:$message}')" \
  --allow-write)"
printf '%s\n' "$SENT" | jq -e \
  '.status == 200 and .body.ok == true and (.body.started | type == "boolean")' \
  >/dev/null

while :; do
  READ="$(kirocrew pod api "$WT" GET \
    "/api/session-control/read?target=$TARGET&limit=20")"
  printf '%s\n' "$READ" | jq -e '.status == 200 and .body.ok == true' >/dev/null
  [ "$(printf '%s\n' "$READ" | jq -r '.body.running')" = false ] && break
  sleep 1
done
printf '%s\n' "$READ" | jq -e '
  any(.body.messages[];
      .role == "assistant" and
      (.content | ascii_downcase | contains("cron")))
' >/dev/null

kirocrew pod api "$WT" POST /api/session-control/stop \
  --data "$(jq -nc --arg target "$TARGET" '{target:$target}')" \
  --allow-write >/dev/null
kirocrew pod api "$WT" POST /api/session-control/close \
  --data "$(jq -nc --arg target "$TARGET" '{target:$target}')" \
  --allow-write >/dev/null
```

The acceptance assertion is not merely that send returned 200: read must show
an assistant reply about cron jobs, and close must archive the temporary
session. `stop` is a safe no-op if the reply already finished.

## Recipe 4: diagnose and reclaim

Use the non-destructive views first:

```bash
set -euo pipefail
WT="$(basename "$PWD")"

kirocrew pod ls
STATUS="$(kirocrew pod status "$WT" --json)"
printf '%s\n' "$STATUS" | jq -e --arg wt "$WT" \
  '.name == $wt and .status == "up" and (.health == 200 or .health == 401 or .health == 403)' \
  >/dev/null
kirocrew pod logs "$WT" --lines 100
kirocrew pod prune --dry-run --json | jq .
```

`pod ls` distinguishes running pods from orphaned pod homes. `pod status`
asserts that this named pod is active and that its own gateway answered the
health probe. `pod logs` shows the last 100 service lines. `pod prune --dry-run`
classifies bulk-reclaim candidates without deleting anything.

Reclaim one known pod with the same stop-drain-delete-verify path used by every
recipe:

```bash
kirocrew pod down "$WT"
```

After reviewing the dry-run, reclaim orphaned homes older than the default three
days with:

```bash
kirocrew pod prune
```

Use `kirocrew pod prune --all` only when every reported orphan should be
removed regardless of age. Reclamation is destructive to the isolated pod home,
so keep a fresh crash home until its logs and sessions are no longer needed.
