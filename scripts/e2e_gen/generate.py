"""Generate Playwright E2E specs by letting a local decision model find the path.

Usage::

    python scripts/e2e_gen/generate.py --cases scripts/e2e_gen/cases.jsonl
        [--model strands-decider-2b | --endpoint HOST:PORT] [--only ID ...]
        [--out website/playwright/generated] [--work <scratch dir>]

For each case (a goal in plain words plus a code-checked end state):

1. An isolated gateway boots from this checkout on the stub ACP backend
   (``spawn_feature_gateway``, as the E2E gate does), and a local decision model
   drives a browser toward the goal with JevOnly. The model only picks among
   actions the code built from the page, so every step is a real control.
2. A run that met its end state becomes ``<out>/<id>.spec.ts``: one Playwright
   step per action, addressed by role and accessible name, then the end state as
   an assertion.
3. Each generated spec is run once with the repo's own Playwright config against
   the same gateway. A spec that fails there is kept as ``<id>.spec.ts.failed``
   with the reason, never as a spec.

The output is a draft for review, not a test that ships itself: read each spec
(did the path exercise what the goal names, or a shortcut to the same page?)
before moving it into ``website/playwright/``. When an E2E spec fails because
the UI changed on purpose, regenerate it from its case and review the diff.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
WEBSITE = REPO / "website"
sys.path.insert(0, str(REPO / "src"))

#: First-run guide flags, the same ones website/playwright/auth.setup.ts sets.
ONBOARDED = {
    "mc-onboarded": "1",
    "mc-import-onboarded": "1",
    "mc-privacy-acked": "1",
    "mc-crewmates-onboarded": "1",
}

#: The probability at which the driver takes the goal as met, per model. JevOnly's
#: default (0.7) is calibrated on hosted Jev; Strands Decider reads "done" lower on
#: the same pages, and at 0.7 wanders into unrelated settings. A premature "done"
#: is caught by the case's code-checked end state.
DONE_THRESHOLD = {"strands-decider-2b": 0.55}

MODEL_READY_TIMEOUT_SECS = 3600

#: JevOnly describes an element as `role "accessible name" | detail | ...`.
_DESC = re.compile(r'^(?P<role>[a-z]+) "(?P<name>(?:[^"\\]|\\.)*)"')


def parse_action(desc: str) -> tuple[str, str] | None:
    """(role, accessible name) of a JevOnly action description, or None for a non-element action."""
    m = _DESC.match(str(desc).strip())
    return (m.group("role"), m.group("name")) if m else None


def trajectory(events: list[dict]) -> list[dict]:
    """The accepted actions of one run, in order, as {kind, role, name, value}.

    Consecutive repeats of the same click are folded: a model that clicked the nav
    item for the page it was already on contributes one step, not two.
    """
    steps: list[dict] = []
    pending: dict | None = None
    for event in events:
        if event.get("kind") == "act":
            pending = event
        elif event.get("kind") == "verify" and pending is not None:
            if event.get("accepted") and not event.get("undone"):
                desc = str(pending.get("desc") or "")
                kind = str(pending.get("action_kind") or "click")
                if desc.startswith("go back"):
                    steps.append({"kind": "back"})
                elif (parsed := parse_action(desc)) is not None:
                    step = {
                        "kind": kind,
                        "role": parsed[0],
                        "name": parsed[1],
                        "value": pending.get("value"),
                    }
                    if not (steps and steps[-1] == step and kind == "click"):
                        steps.append(step)
            pending = None
    return steps


def _js(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


#: A keyboard hint the page renders inside a control ("Schedule Alt + S"). JevOnly reads
#: it as part of the visible name; the accessible name Playwright matches leaves it out.
_SHORTCUT_HINT = re.compile(
    r"\s+(?:Alt|Ctrl|Control|Cmd|Command|Shift|Option|Meta)(?:\s*\+\s*\S+)+$"
)


def accessible_name(name: str) -> str:
    """The name a Playwright role locator should match for a JevOnly-read name."""
    return _SHORTCUT_HINT.sub("", name).strip() or name


def _locator(role: str, name: str) -> str:
    return f"page.getByRole({_js(role)}, {{ name: {_js(accessible_name(name))}, exact: true }})"


def render_spec(case: dict, path: str, steps: list[dict]) -> str:
    """A Playwright spec replaying *steps* from *path* and asserting the case's end state."""
    body = []
    if case.get("setup"):
        # The case's starting state, put on the server the same way the exploration had it.
        # Sent from the page, not Playwright's `request` fixture: the session cookie is bound to
        # the browser's own connection, and the API context's request fails that check.
        body.append("    await page.goto('/', { waitUntil: 'domcontentloaded' })")
        reqs = [
            {"method": str(r["method"]).upper(), "path": r["path"], "json": r.get("json")}
            for r in case["setup"]
        ]
        body.append(
            "    const setup = await page.evaluate(async (reqs) => {\n"
            "      const failed = []\n"
            "      for (const r of reqs) {\n"
            "        const init = { method: r.method, headers: { 'Content-Type': 'application/json' } }\n"
            "        if (r.json !== null) init.body = JSON.stringify(r.json)\n"
            "        const res = await fetch(r.path, init)\n"
            "        if (!res.ok) failed.push(`${r.method} ${r.path} -> ${res.status}`)\n"
            "      }\n"
            "      return failed\n"
            f"    }}, {json.dumps(reqs, ensure_ascii=False)})\n"
            "    expect(setup).toEqual([])"
        )
    body.append(f"    await page.goto({_js(path)}, {{ waitUntil: 'domcontentloaded' }})")
    for step in steps:
        if step["kind"] == "back":
            body.append("    await page.goBack()")
            continue
        loc = _locator(step["role"], step["name"])
        if step["kind"] in ("type", "fill") and step.get("value") is not None:
            body.append(f"    await {loc}.fill({_js(str(step['value']))})")
        elif step["kind"] in ("press_enter", "enter"):
            body.append(f"    await {loc}.press('Enter')")
        elif step["kind"] == "select" and step.get("value") is not None:
            body.append(f"    await {loc}.selectOption({{ label: {_js(str(step['value']))} }})")
        else:
            body.append(f"    await {loc}.click()")
    expect = case.get("expect") or {}
    if "url_contains" in expect:
        body.append(
            f"    await expect(page).toHaveURL(new RegExp({_js(re.escape(expect['url_contains']))}))"
        )
    if "text" in expect:
        body.append(
            f"    await expect(page.getByText({_js(expect['text'])}).first()).toBeVisible()"
        )
    if "selected" in expect:
        name = expect["selected"]
        body.append(
            "    await expect(\n"
            "      page\n"
            '        .locator(\'[aria-pressed="true"], [aria-selected="true"], [aria-checked="true"]\')\n'
            f"        .filter({{ hasText: {_js(name)} }})\n"
            "        .first(),\n"
            "    ).toBeVisible()"
        )
    lines = [
        "// Generated by scripts/e2e_gen/generate.py from its case of the same id; regenerate",
        "// rather than hand-edit when the flow changes on purpose, and review the diff.",
        "import { test, expect } from '@playwright/test'",
        "",
        f"test({_js(case['id'])}, async ({{ page }}) => {{",
        f"  // {case['goal']}",
        *[line[2:] for entry in body for line in entry.split("\n")],
        "})",
        "",
    ]
    return "\n".join(lines)


def start_path(start: str) -> str:
    """The path of a case's start URL, without the origin placeholder or a token."""
    path = start.replace("{BASE}", "") or "/"
    path = re.sub(r"[?&]token=[^&]*", "", path)
    return path or "/"


def _start_model(model_id: str) -> tuple[object, str]:
    """Start *model_id* under the current KIROCREW_HOME; (runtime, "host:port")."""
    from kiro_crew.decisions import local_models
    from kiro_crew.decisions.local_runtime import STATE_ERROR, STATE_RUNNING, free_port, get_runtime

    model = local_models.get(model_id)
    if model is None:
        raise SystemExit(f"unknown local model {model_id!r}")
    port = free_port(8100)
    runtime = get_runtime()
    runtime.activate(model, port)
    deadline = time.monotonic() + MODEL_READY_TIMEOUT_SECS
    last = ""
    while time.monotonic() < deadline:
        status = runtime.status()
        if status["state"] != last:
            print(f"[e2e-gen] model {model_id}: {status['state']}", flush=True)
            last = status["state"]
        if status["state"] == STATE_RUNNING:
            return runtime, f"127.0.0.1:{port}"
        if status["state"] == STATE_ERROR:
            raise SystemExit(f"model {model_id} failed to start: {status['error']}")
        time.sleep(2)
    raise SystemExit(f"model {model_id} was not ready after {MODEL_READY_TIMEOUT_SECS}s")


def _load_cases(path: Path, only: list[str]) -> list[dict]:
    cases = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        case = json.loads(line)
        if not case.get("expect"):
            raise SystemExit(f"case {case['id']}: a generated spec needs a code-checked `expect`")
        if not only or case["id"] in only:
            cases.append(case)
    return cases


def _explore(
    case: dict, work: Path, base: str, token: str, endpoint: str, jevonly: str, done: float | None
) -> Path:
    """Drive one case once in a fresh browser profile; its event log."""
    c = dict(case)
    path = start_path(c["start"])
    c["start"] = f"{base}{path}{'&' if '?' in path else '?'}token={token}"
    if done is not None:
        c.setdefault("thresholds", {}).setdefault("done", done)
    suite = work / f"{c['id']}.case.jsonl"
    suite.write_text(json.dumps(c) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        JEVONLY_LOCAL=endpoint,
        JEVONLY_INIT_LOCALSTORAGE=json.dumps(ONBOARDED),
        JEV_PROFILE_DIR=str(work / "profiles" / c["id"]),
    )
    with (work / "explore.log").open("ab") as log:
        subprocess.run(
            [jevonly, "qa", str(suite), "--out", str(work / "runs")],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return work / "runs" / f"{c['id']}-r1.jsonl"


def _run_setup(base: str, token: str, setup: list[dict]) -> None:
    """Send a case's setup requests to the gateway; any non-2xx answer stops the run."""
    import urllib.request

    for req in setup:
        sep = "&" if "?" in req["path"] else "?"
        data = json.dumps(req["json"]).encode("utf-8") if "json" in req else None
        request = urllib.request.Request(
            f"{base}{req['path']}{sep}token={token}",
            data=data,
            method=str(req["method"]).upper(),
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 - loopback gateway
            if not 200 <= resp.status < 300:
                raise SystemExit(f"setup {req['method']} {req['path']} answered {resp.status}")


def _validate(spec: Path, port: int, token: str, results: Path) -> tuple[bool, str]:
    """Run one spec with the repo's Playwright config; (passed, tail of the output)."""
    env = dict(os.environ)
    env.update(PLAYWRIGHT_BASE_URL=f"http://localhost:{port}", PLAYWRIGHT_TOKEN=token)
    rel = spec.relative_to(WEBSITE / "playwright")
    proc = subprocess.run(
        [
            "npx",
            "playwright",
            "test",
            "--project",
            "chromium",
            "--retries",
            "0",
            "--output",
            str(results),
            str(rel),
        ],
        cwd=WEBSITE,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=600,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def _emit(case: dict, log: Path, out: Path, gw, work: Path) -> str:
    """Write and validate the spec for one explored case; a one-line outcome."""
    events = (
        [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    end = next((e for e in reversed(events) if e.get("kind") == "end"), {})
    spec = out / f"{case['id']}.spec.ts"
    failed = spec.with_name(spec.name + ".failed")
    if not end.get("success"):
        return f"not generated: the model did not reach the end state ({end.get('stopped')})"
    spec.write_text(
        render_spec(case, start_path(case["start"]), trajectory(events)), encoding="utf-8"
    )
    ok, tail = _validate(spec, gw.port, gw.token, work / "playwright" / case["id"])
    if not ok and "[setup]" in tail and "Execution context was destroyed" in tail:
        # auth.setup.ts can lose a race with the dashboard's own post-sign-in
        # navigation; that is the harness, not the spec, so look once more.
        ok, tail = _validate(spec, gw.port, gw.token, work / "playwright" / case["id"])
    if ok:
        failed.unlink(missing_ok=True)
        return f"generated and passing: {spec.relative_to(REPO)}"
    failed.write_text(spec.read_text(encoding="utf-8") + "\n/* validation run:\n" + tail + "\n*/\n")
    spec.unlink()
    return f"generated but failed its own run: {failed.relative_to(REPO)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--cases", type=Path, default=HERE / "cases.jsonl")
    parser.add_argument("--only", nargs="*", default=[], help="generate only these case ids")
    parser.add_argument("--out", type=Path, default=WEBSITE / "playwright" / "generated")
    parser.add_argument("--work", type=Path, help="scratch directory for the exploration logs")
    parser.add_argument(
        "--model", default="strands-decider-2b", help="local preset, or the one --endpoint serves"
    )
    parser.add_argument("--endpoint", help="use an already-running System One server, HOST:PORT")
    parser.add_argument("--jevonly", default=shutil.which("jevonly") or "jevonly")
    args = parser.parse_args(argv)

    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    cases = _load_cases(args.cases, args.only)
    work = (args.work or Path(tempfile.mkdtemp(prefix="e2e-gen-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend.__file__)
    # spawn_feature_gateway isolates KIROCREW_HOME, not the workspace root, which
    # otherwise falls back to the host user's own workspace.
    os.environ["KIROCREW_WORKSPACE"] = str(work / "workspace")

    results: dict[str, str] = {}
    runtime = None
    endpoint = args.endpoint
    prior_home = os.environ.get("KIROCREW_HOME")
    try:
        if not endpoint:
            # The model outlives every per-case gateway, so it gets its own throwaway home.
            os.environ["KIROCREW_HOME"] = str(work / "model-home")
            runtime, endpoint = _start_model(args.model)
        for case in cases:
            # A fresh gateway per case: the dashboard's URL token can be exchanged only for five
            # minutes after it is minted, and setup, exploration and validation all use it.
            with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
                # localhost, not 127.0.0.1: the session cookie is per host spelling.
                base = f"http://localhost:{gw.port}"
                _run_setup(base, gw.token, case.get("setup") or [])
                log = _explore(
                    case,
                    work,
                    base,
                    gw.token,
                    endpoint,
                    args.jevonly,
                    DONE_THRESHOLD.get(args.model),
                )
                results[case["id"]] = _emit(case, log, out, gw, work)
    finally:
        if runtime is not None:
            runtime.deactivate(wait=True)  # type: ignore[attr-defined]
        if prior_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = prior_home
    for case_id, outcome in results.items():
        print(f"{case_id}: {outcome}")
    print(f"[e2e-gen] exploration logs: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
