# E2E spec generator

```bash
python scripts/e2e_gen/generate.py --jevonly <path to jevonly> [--only <case id> ...]
```

Writes Playwright specs for the [E2E gate](e2e-gate.md) from plain-language
cases. A local decision model finds the path through the dashboard; what ships
is an ordinary deterministic spec, so the gate stays fast, free and repeatable.
The model is there to write tests nobody has written yet, not to run them.

## What it does

For each case in `scripts/e2e_gen/cases.jsonl` (a goal and a code-checked end
state):

1. Boots a fresh isolated gateway for the case with
   `kiro_crew.testing.harness.spawn_feature_gateway` (fixture `minimal`) on the
   packaged fake ACP backend, the same arrangement as the E2E gate. No model
   account, no login; the cases never send a chat. One gateway per case, because
   its URL token can only be exchanged for five minutes and the case's setup,
   exploration and validation all need it.
2. Starts the decision model once, as a gateway-managed local preset
   (`--model`, default `strands-decider-2b`) in its own throwaway home, or uses a
   System One server already on loopback (`--endpoint HOST:PORT`). Every call
   stays on 127.0.0.1.
3. Drives a browser toward the goal with
   [JevOnly](https://github.com/buluoray/JevOnly). The model only picks among
   actions the code built from the page, so each step is a real control with a
   role and an accessible name.
4. Turns a run that met its end state into `website/playwright/generated/<id>.spec.ts`:
   one step per accepted action (`getByRole(role, { name, exact: true })`, with
   repeated clicks folded), then the end state as an assertion.
5. Runs that spec once with `website/playwright.config.ts` against the same
   gateway. A spec that fails there is kept only as `<id>.spec.ts.failed`, with
   the output, for a person to look at.

## Cases

One JSON object per line: `id`, `start` (`{BASE}/path`), `goal`, `expect`, and
optionally `setup`, `max_steps` and `irreversible`. `expect` is required, because
it becomes the spec's assertion:

| `expect` key | Spec assertion |
|---|---|
| `url_contains` | `expect(page).toHaveURL(...)` |
| `text` | the text is visible |
| `selected` | a control with that text reports `aria-pressed`, `aria-selected` or `aria-checked` true |

`setup` is a list of `{method, path, json}` requests that put the server in the
case's starting state, sent before the exploration and again at the start of the
generated spec. A case whose end state may already hold needs one: the mode
cases set the opposite mode first, or "switch to Dark" passes on arrival and the
spec records no Dark click. The spec sends them with `fetch` from the page, not
Playwright's `request` fixture: the session cookie is bound to the browser's own
connection, and the fixture's request is refused as an IP mismatch.

Write the end state so only the path the goal names can reach it. A goal like
"use the settings search to open Shortcuts" with `url_contains: /settings/shortcuts`
is satisfied just as well by clicking Shortcuts in the sidebar, and the model
took that shortcut, so the generated spec would not test search at all.

## Reviewing a generated spec

A generated spec is a draft. Before moving it into `website/playwright/`:

- Read the steps: did the path exercise what the goal names, or a shortcut to the
  same page?
- Check the end state holds the behaviour, not only the page it lands on.
- Fold it into an existing spec file for the same surface when one exists.
- A spec with `setup` changes server state other specs share. Locally the suite
  runs in parallel, so put such specs for one setting in one file under
  `test.describe.configure({ mode: 'serial' })`; CI runs one worker.

## When an E2E spec fails

Decide first whether the UI change was intended. A failure on an unintended
change is the regression the gate exists to catch: fix the product. When the
change was intended, regenerate that spec from its case
(`--only <id>`) and review the diff in the same PR as the UI change, so the
reviewer sees the new path next to the change that caused it.

## Things that bite

- **Host spelling.** The browser uses `localhost`, not `127.0.0.1`: the session
  cookie is per host, and a mixed spelling loses the session.
- **First-run screens.** The exploration browser gets the same localStorage
  flags as `website/playwright/auth.setup.ts`.
- **Done threshold.** JevOnly's default (0.7) is calibrated on hosted Jev;
  Strands Decider reads "done" lower and at 0.7 wanders on, so the generator sets
  0.55 for it. The code-checked end state is what decides whether a spec is
  written.
- **Speed.** On CPU each model call takes seconds and grows with the size of the
  page; a navigation case explores in about a minute, a settings change in three
  to five. The generated spec then runs in seconds.
