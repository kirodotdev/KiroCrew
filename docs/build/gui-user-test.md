# GUI user test (agentic, pixel-only)

`.github/workflows/gui-user-test.yml` runs an LLM as a first-time user of the
dashboard: it sees only screenshots and acts only through the mouse and keyboard,
against a real Chromium window on a private Xvfb display and a real gateway seeded
from a fixture. It exists for the defects the DOM-level lanes cannot see -- a control
the DOM calls visible that a person cannot click, a theme switch that changed a class
name and not the pixels, a horizontal scrollbar, a spinner that never stops. Tracking
issue: [#9578](https://github.com/kirodotdev/KiroCrew/issues/9578).

The lane runs on the nightly schedule and on `workflow_dispatch` only; its job status is
the verdict. It is not a pull-request check: a PR lane would run the PR's own harness
with an assumed cloud role, and any write collaborator can open a same-repo PR, which
makes "code write" reach "cloud credential" however the job is gated. The PR-advisory
shape is phase 2 of the tracking issue and takes the `workflow_run` form the
`fork-*-review.yml` lanes use -- harness from the trusted base, the PR-built gateway and
browser under a separate unprivileged user -- so the code under test can be the PR's
while the code holding credentials is not. `pr-readiness.yml` does not read this lane.

## Pieces

| Path | Role |
|---|---|
| `.github/workflows/gui-user-test.yml` | Triggers, boot, run, artifact, PR comment, nightly issue, lane status. |
| `scripts/gui-user-test/boot.sh` | Xvfb -> `seed_home.py` -> `python -m kiro_crew gateway --test-mode --approval yolo --no-crons` on the packaged fake ACP backend -> Chromium at the dashboard URL. Writes `target.env` (origin + one-time token, mode 0600) and `pids`. |
| `scripts/gui-user-test/seed_home.py` | Copies a fixture into `$KIROCREW_HOME` through `kiro_crew.seed` and adds `config.agents.<slug>` for each `--member` so the Crew Members page has a roster. |
| `scripts/gui-user-test/teardown.sh` | Kills the three process groups and removes the scratch home and browser profile. |
| `test/gui_user/harness.py` | The screenshot -> Bedrock Messages API -> action loop with the step, time and budget gates. |
| `test/gui_user/x11.py` | Screenshots (Pillow `ImageGrab`) and input (`xdotool`); coordinate scaling, key aliases and argv building are pure and unit-tested. |
| `test/gui_user/scenarios.py` + `scenarios/*.yaml` | The scenario DSL (including the `FEATURES` registry) and the shipped scenarios. |
| `test/gui_user/report.py` | Renders `summary.json` into `verdict.md`, the PR comment and the nightly issue, all grouped by feature; renders `features.md` from the scenario directory. |
| [`test/gui_user/FEATURES.md`](../../test/gui_user/FEATURES.md) + `features.json` | The scenario backlog: every user-visible feature as one record (feature slug, user story, start URL, seed, runnable tier, priority). `features.json` is the source of truth; `FEATURES.md` is rendered from it by `features_catalog.py` (`--write` / `--check`), which also validates every record and refuses cross-slug duplicates. |

The unit tests under `test/gui_user/` run in the ordinary Backend Tests shards; they
need no display and never call Bedrock.

## How a run works

1. The runner installs `xvfb`, `xdotool`, `x11-utils`, the backend (`pip install -e .`
   plus `boto3`), builds the frontend and stages it into `src/kiro_crew/static/dist`
   exactly as the E2E job does, and lifts Ubuntu's AppArmor user-namespace restriction
   (`sysctl kernel.apparmor_restrict_unprivileged_userns=0`, as the namespace-sandbox
   test job does) so the gateway's sandbox can actually run the fake backend -- without
   it every chat turn renders a sandbox error, and the tester reports that as the UI.
2. `boot.sh` starts Xvfb `:99` at 1600x1000, seeds a throwaway `KIROCREW_HOME`, starts
   the gateway with `KIROCREW_KIRO_BIN` pointed at
   `kiro_crew.testing.fake_acp_backend` (so chat replies without kiro-cli or a
   login), reads the `KIROCREW_READY:{port, token}` line, opens Chromium
   (`--no-sandbox --test-type`, full-screen window, omnibox kept) at
   `http://127.0.0.1:<port>/?token=...`, focuses the window.
3. `harness.py` navigates to each scenario's `start_url` through the omnibox (the
   token has become the `mc_token` cookie by then), takes a screenshot, and loops:
   the model returns one action, the harness executes it, waits about a second, and
   returns a fresh screenshot as the tool result. The conversation keeps the newest
   three screenshots; older ones become a one-line placeholder.
4. The model ends with a `VERDICT: PASS|FAIL` block listing each expectation as
   `MET` / `NOT MET` plus any UI defects it noticed. A scenario that fails is retried
   once; `max_steps` / `max_seconds` from the YAML and the run's `--budget-usd` are
   hard stops.
5. Everything lands in the `gui-user-test-<run id>` artifact: `results/<scenario>/attempt-N/NN-<action>.png`,
   `steps.jsonl` (every action with parameters and the screenshot it produced),
   `summary.json`, `verdict.md`, `features.md` (the feature catalog joined with this
   run's verdicts), plus `gateway.log` / `gateway.err` / `chrome.log` /
   `xvfb.log` with the one-time token scrubbed.

### The model and the tool shape

The harness speaks the Bedrock Messages API through `boto3` in `us-west-2` under an OIDC
role trusted for schedule/dispatch: a lane-scoped `GUI_TEST_BEDROCK_ROLE_ARN` when
configured, else the shared scheduled-workflow fallback `SHIP_REPORT_BEDROCK_ROLE_ARN`
(`bedrock:InvokeModel` only, all Anthropic model ids -- the same fallback
`fix-loop-analysis.yml` and `issue-triage.yml` use). The review lanes'
`AWS_BEDROCK_ROLE_ARN` is trusted for `pull_request` events only and is not used here.
It first offers
the native computer-use tool (`computer_20251124` under the
`computer-use-2025-11-24` beta); if the model or endpoint rejects the beta with a 400
it switches, for the rest of the run, to a plain tool-use loop with one custom tool
per action (`screenshot`, `left_click`, `double_click`, `right_click`, `mouse_move`,
`left_click_drag`, `type`, `key`, `scroll`, `wait`) and the screenshot returned as an
image block inside `tool_result`. Both shapes drive the same `x11.perform`, so the
logs and the scenarios are identical either way. `--tool-mode native|custom` pins one.

The model id is a workflow parameter (`inputs.model`, default
`us.anthropic.claude-sonnet-4-6`), never a default in code.

## Adding a scenario

Create `test/gui_user/scenarios/<name>.yaml`; the file stem must equal `name`:

```yaml
name: settings-theme-toggle
tier: smoke                 # smoke = runs on PRs and nightly; nightly = nightly only
feature: settings           # product area -- a key of scenarios.FEATURES (see below)
user_story: >-              # one sentence a person can read: who wants what, and why
  As a user, I want to switch the dashboard theme in Settings, so that the app
  matches my environment and the change is visible at once.
docs_url: docs/system-specs/modules/themes.md   # optional: where the feature is specified
summary: Switch the dashboard theme in Settings and confirm the colours change
preconditions:
  seed: rich                # KIROCREW_HOME fixture (kirocrew gateway --seed NAME)
  members: []               # crew member slugs boot.sh adds to config.agents
  start_url: /settings      # path the harness navigates to first (no query string)
steps:
  - You are on the Settings page. Take note of the current background colour.
  - Find the theme control and pick a different theme than the one selected.
expectations:
  - The page background colour is clearly different from the first screenshot.
max_steps: 12               # actions before the scenario FAILS (ceiling 40)
max_seconds: 300            # wall clock before the scenario FAILS (ceiling 900)
```

Write `steps` as you would brief a human tester -- what to look for, not where to
click -- and `expectations` as things that are true or false on the final screen. Keep
a scenario to one flow; the cheapest scenario is the one that needs the fewest
screenshots. `test_scenarios_and_report.py` loads every shipped file, so a malformed
scenario fails the unit tests before it costs a model call.

### `feature` and `user_story`: the scenario directory is also the feature catalog

`feature` and `user_story` are required and are never shown to the model. They exist
for the readers of the report: the verdict table, the step summary and the nightly
issue are grouped by feature, each row carries the user story, and
`report.py --format features` renders **`features.md`** -- one section per feature
listing its user stories with the latest verdict, followed by the features that have no
scenario yet. The workflow uploads `results/features.md` with the artifact and appends
it to the run's step summary, so "what does the product do, and is it healthy" is
answered from any run page without opening the YAML.

- `feature` is a slug from the closed registry `scenarios.FEATURES`, one per product
  area, in report order: `chat`, `side-panel`, `terminal`, `sidebar`, `navigation`,
  `topbar`, `search`, `members`, `capabilities`, `connections`, `memory`, `knowledge`,
  `artifacts`, `files`, `browser-panel`, `apps`, `task-runner`, `worlds`, `dev-fleet`,
  `schedule`, `api`, `webhooks`, `channels`, `voice`, `notifications`, `computer-use`,
  `instances`, `remote-instances`, `popout`, `auth`, `onboarding`, `settings`, `themes`,
  `security`, `developer`. A closed list, not a free-form slug, so a typo cannot split
  one feature into two report groups. To add a product area, add `slug: "Human title"`
  to `FEATURES` in the order you want it reported, mirror it in
  `features_catalog.FEATURE_TITLES` (a unit test holds the two equal) and mention it in
  the list above; a scenario naming an unknown feature is rejected at load time.
- `user_story` is one sentence of at most 300 characters, in the user's voice: `As a
  <who>, I want <what>, so that <why>`, or a plain use case when the persona adds
  nothing. Say what the user is trying to achieve, not which control they press --
  that is what `steps` are for. The shipped scenarios all start with `As a`, and the
  unit tests hold them to it.
- `docs_url` is optional: an `https://` URL or a repo path under `docs/` ending in
  `.md` (an anchor is allowed). It becomes the **Docs** link in `features.md`.

Scenario names should start with the feature they belong to where that reads
naturally (`settings-theme-toggle`, `members-dm-hello`) so the artifact directory
sorts the same way the report groups.

### Keeping a scenario true as the product moves

A step that names a control by its exact label goes stale when the label changes.
When a surface is mid-transition, describe it by what does not change: the Feature
Previews card for Crew Members was relabelled from "Crew Members and Crew Mode" to
"Crew Members" as Crew Mode retired (#9519), so `members-dm-hello` asks for the card
"whose title starts with `Crew Members`" and names both readings, and finds the page by
its rail label, which was the same on both sides of the change. When a scenario does
need to move with the product, change the YAML in the same PR as the UI and re-run it
on demand (below) before merging.

`boot.sh` seeds one home per run from `GUI_SEED` (default `rich`) with `GUI_MEMBERS`
(default `nova-sky`); a scenario's `preconditions.seed` / `members` document what it
needs and must agree with that boot, because the target is booted once per run.

## Running it

- **On demand**: `gh workflow run gui-user-test.yml --ref main -f tier=nightly`
  (also `-f scenario=<name>`, `-f model=<id>`, `-f budget_usd=<n>`). The job status is
  the verdict. A dispatch on a non-default branch only works if the role's trust policy
  covers that ref; the shared fallback role trusts `main`, so a branch dispatch fails at
  `configure-aws-credentials` -- verify branch changes through the nightly after merge
  or through a lane-scoped role that trusts the branch.
- **On a PR**: not yet -- see the note at the top and phase 2 of the tracking issue.
- **Nightly**: `20 9 * * *` UTC on `main`, full tier. A non-PASS night opens or
  updates the single open issue labelled `gui-test-report`.

### Locally

The harness needs an X display, `xdotool`, a Chromium, non-interactive `sudo` (the boot
installs a managed browser policy under `/etc/opt/chrome/policies/managed/` and its
Chromium equivalents, and refuses to launch the browser unpinned without it;
`teardown.sh` removes exactly those files again), and AWS credentials for a Bedrock
role. On a machine with those:

```bash
sudo apt-get install -y xvfb xdotool x11-utils           # Debian/Ubuntu
pip install -e . "boto3>=1.34,<2"
(cd website && npm ci && npm run build) && rm -rf src/kiro_crew/static/dist && cp -R website/dist src/kiro_crew/static/dist
export GUI_OUT="$(mktemp -d)"
bash scripts/gui-user-test/boot.sh                       # Xvfb :99, gateway, Chromium
. "$GUI_OUT/target.env"
python test/gui_user/harness.py --out "$GUI_OUT/results" --base-url "$GUI_BASE_URL" \
  --model us.anthropic.claude-sonnet-4-6 --tier smoke --budget-usd 2
bash scripts/gui-user-test/teardown.sh
```

`--dry-run` skips the model entirely and only navigates + screenshots each scenario's
start page -- the cheapest check that the target booted and the display works. To
watch the run, point a VNC server at `:99` (`x11vnc -display :99`). Never point
`--display` at your own desktop: the backend refuses `:0` / `:1` and any display whose
owning server is not a virtual one.

## Cost and limits

- A 1280x800 screenshot is about 1 365 input tokens (width x height / 750). With three
  screenshots kept, a step costs roughly 6-8k input and ~150 output tokens; a
  10-step scenario on a Sonnet-class model is about $0.25-0.40 and two to four
  minutes. The nightly tier (three scenarios, one retry each in the worst case) is
  about $1-2.50; the PR smoke pair about $0.60. The run stops at `--budget-usd`
  (dispatch default $3, nightly $4, PR $2) and marks the remaining scenarios
  `SKIPPED`.
- Pixel tests are stochastic. One retry absorbs a mis-click; a scenario that flips
  night to night is a scenario problem (vague step, timing) before it is a product
  problem. Read `steps.jsonl` and the numbered screenshots: they show exactly where
  the model looked and what it did.
- The model never sees the DOM, so it cannot assert what a human cannot see either.
  Use the Playwright E2E suite for exact text and state; use this lane for "does it
  look and behave right to a person".
- Actions the native tool can emit but the backend refuses (`zoom`, `hold_key`,
  mouse down/up as separate events) come back as structured tool errors; the model
  recovers with the supported vocabulary.

## Security boundary

The display is a private Xvfb (`-nolisten tcp`). `x11.verify_virtual_display` refuses
`:0` / `:1`, any remote host, and -- by reading the display's `/tmp/.X<N>-lock` pid and
that process's name -- any display not served by a memory-only X server (`Xvfb`,
`Xdcv`, `Xvnc`, `Xephyr`, `Xdummy`), so a real seat on `:2` is refused too. Model text
reaches a comment or issue only through `report.neutralize` inside a fenced block
(fence delimiters defanged, `@` mentions broken, control characters dropped,
length capped). Three more controls bound what the model can reach through the browser:
`x11.check_key_allowed` refuses every modifier chord that leaves the page (`ctrl+o`,
`ctrl+t`, `ctrl+u`, `ctrl+shift+i`, `F12`, `alt+…`, `super`) and keeps only editing and
in-page chords plus `ctrl+l`; `boot.sh` installs a managed browser policy that pins the
browser to the loopback gateway origin (`URLBlocklist: *`, `URLAllowlist: <origin>`, so
`file://` and every other host are refused by the browser itself, plus no file dialogs,
downloads, devtools, extensions or extra profiles); and the workflow assumes the AWS
role only AFTER the gateway and browser are up, so neither process ever inherits the
credentials -- only the harness step runs with them. The model gets screenshot,
click, type, key, scroll and wait -- no shell, no file access, no clipboard -- and
typed text is capped at 400 characters. The system prompt declares on-screen text to
be data, never instructions. The target holds nothing worth stealing: a seeded
fixture, the fake backend, and a one-time token for a gateway that dies with the job
(scrubbed from the uploaded logs). The checkout uses `persist-credentials: false`,
and the Bedrock role is the review lanes' existing least-privilege role. Fork pull
requests never run the lane.
