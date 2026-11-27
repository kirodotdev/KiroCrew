"""The read-only built-in workflow tier and its first script, ``ci-red-triage``.

A built-in is listed, runnable by slug and never writable or shadowable by a
user entry; a user definition with any slug keeps working; the library works
with the built-in directory missing. All against stub agent_fns.
"""

from __future__ import annotations

import configparser
import fnmatch
import json
import logging
from pathlib import Path

import pytest

from kiro_crew.testing.wait import async_wait_until
from kiro_crew.workflows import library as workflow_library
from kiro_crew.workflows.library import (
    BUILTIN_WORKFLOWS_DIR,
    BuiltinWorkflowError,
    WorkflowDefinitionLibrary,
    WorkflowSlugTakenError,
    command_ref,
    load_builtins,
)
from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore
from kiro_crew.workflows.validate import CORE_CTX_SURFACE, check_ctx_surface, validate

NOW = "2026-11-26T00:00:00Z"
TRIAGE = "ci-red-triage"
TRIAGE_SOURCE = (BUILTIN_WORKFLOWS_DIR / f"{TRIAGE}.py").read_text(encoding="utf-8")

USER_SOURCE = (
    'META = {"name": "mine", "description": "a user workflow"}\n'
    "async def workflow(ctx):\n"
    "    return await ctx.agent('hello')\n"
)

FAILURES = [
    {"check": "Backend Tests (3.12, 2)", "conclusion": "failure", "log_tail": "test_a failed"},
    {"check": "Frontend Tests (1)", "conclusion": "failure", "log_tail": "b.test.tsx failed"},
]


def _builtin_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    directory = tmp_path / "builtins"
    directory.mkdir()
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def _library(tmp_path: Path, builtin_dir: Path | None = None) -> WorkflowDefinitionLibrary:
    return WorkflowDefinitionLibrary(tmp_path / "home", builtin_dir=builtin_dir)


def _triage_agent(causes: dict[str, str], calls: list[str], *, failing: str = ""):
    """A stub agent_fn answering each ci-red-triage node by its label."""

    async def fn(prompt: str, opts: dict):
        label = str(opts.get("label") or "")
        calls.append(label)
        if label == "collect":
            return {"failures": FAILURES}
        kind, _, check = label.partition(":")
        if kind == "classify":
            if check == failing:
                raise RuntimeError("classify node died")
            return {"cause": causes[check], "evidence": f"probe for {check}"}
        if kind == "locate":
            return {"fix_pr": "#42001", "status": "green"}
        raise AssertionError(f"unexpected node {label}")

    return fn


async def _run_triage(agent_fn, **kwargs):
    return await WorkflowRunner(agent_fn=agent_fn, audit=lambda *a, **k: None).run(
        TRIAGE_SOURCE, run_id="r1", now=NOW, args={"pr": 1, "base": "main"}, **kwargs
    )


# --------------------------------------------------------------------------- #
# 1. Every shipped file is a valid workflow on the host surface
# --------------------------------------------------------------------------- #


def test_every_shipped_builtin_validates_and_uses_only_wired_ports() -> None:
    scripts = sorted(p for p in BUILTIN_WORKFLOWS_DIR.glob("*.py") if p.name != "__init__.py")
    assert scripts
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        result = validate(source)
        assert result.errors == [], path
        assert check_ctx_surface(source, CORE_CTX_SURFACE) == [], path
        assert (result.meta or {}).get("name") == path.stem
    # No shipped file is skipped as invalid: one record per script.
    assert [b["slug"] for b in load_builtins()] == [p.stem for p in scripts]


def test_builtin_scripts_are_declared_package_data() -> None:
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).resolve().parents[1] / "setup.cfg", encoding="utf-8")
    globs = [
        line.strip()
        for line in cfg.get("options.package_data", "kiro_crew").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    package_root = BUILTIN_WORKFLOWS_DIR.parent
    for path in BUILTIN_WORKFLOWS_DIR.glob("*.py"):
        rel = path.relative_to(package_root).as_posix()
        assert any(fnmatch.fnmatch(rel, g) for g in globs), rel


# --------------------------------------------------------------------------- #
# 2. ci-red-triage under the runner
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_triage_classifies_each_red_check_and_names_its_action() -> None:
    calls: list[str] = []
    causes = {FAILURES[0]["check"]: "main", FAILURES[1]["check"]: "flake"}

    res = await _run_triage(_triage_agent(causes, calls))

    assert res.ok, res.error
    per_check = {item["check"]: item for item in res.result["per_check"]}
    assert per_check[FAILURES[0]["check"]]["action"] == "wait #42001 then rebase"
    assert per_check[FAILURES[1]["check"]]["action"] == "rerun --failed"
    assert res.result["dropped"] == []
    # Locate runs only for a check that is red on main too.
    assert [c for c in calls if c.startswith("locate:")] == [f"locate:{FAILURES[0]['check']}"]


@pytest.mark.asyncio
async def test_a_failing_classify_node_drops_only_its_own_check() -> None:
    calls: list[str] = []
    causes = {FAILURES[0]["check"]: "pr", FAILURES[1]["check"]: "pr"}

    res = await _run_triage(_triage_agent(causes, calls, failing=FAILURES[1]["check"]))

    assert res.ok, res.error
    assert [item["check"] for item in res.result["per_check"]] == [FAILURES[0]["check"]]
    assert res.result["per_check"][0]["action"] == "fix-in-PR"
    assert res.result["dropped"] == [FAILURES[1]["check"]]


@pytest.mark.asyncio
async def test_a_chat_line_fills_pr_repo_dir_and_base_in_order() -> None:
    # `/workflow ci-red-triage 123 /home/me/repo release-1.2` arrives as input only.
    seen: list[tuple[str, str, object]] = []
    stub = _triage_agent({c["check"]: "pr" for c in FAILURES}, [])

    async def fn(prompt: str, opts: dict):
        seen.append((str(opts.get("label")), prompt, opts.get("cwd")))
        return await stub(prompt, opts)

    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        TRIAGE_SOURCE, run_id="r1", now=NOW, args={"input": "123 /home/me/repo release-1.2"}
    )

    assert res.ok, res.error
    assert "pr=123, repo_dir=/home/me/repo, base=release-1.2" in res.result["target"]
    assert {cwd for _, _, cwd in seen} == {"/home/me/repo"}
    classify_prompts = [p for label, p, _ in seen if label.startswith("classify:")]
    assert classify_prompts and all("origin/release-1.2" in p for p in classify_prompts)


@pytest.mark.parametrize(
    "line, repo_dir, base",
    [
        ('123 "/work/My Repo" release-1.2', "/work/My Repo", "release-1.2"),
        ("123 '/work/My Repo' release-1.2", "/work/My Repo", "release-1.2"),
        ('123 "/work/My Repo main', "/work/My Repo main", "main"),
        ("123 '/work/My Repo main", "/work/My Repo main", "main"),
    ],
)
@pytest.mark.asyncio
async def test_a_chat_line_keeps_quoted_repo_dir_together(line, repo_dir, base) -> None:
    seen: list[tuple[str, str, object]] = []
    stub = _triage_agent({c["check"]: "pr" for c in FAILURES}, [])

    async def fn(prompt: str, opts: dict):
        seen.append((str(opts.get("label")), prompt, opts.get("cwd")))
        return await stub(prompt, opts)

    res = await WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None).run(
        TRIAGE_SOURCE, run_id="r1", now=NOW, args={"input": line}
    )

    assert res.ok, res.error
    assert f"pr=123, repo_dir={repo_dir}" in res.result["target"]
    assert {cwd for _, _, cwd in seen} == {repo_dir}
    classify_prompts = [p for label, p, _ in seen if label.startswith("classify:")]
    assert classify_prompts and all(f"origin/{base}" in p for p in classify_prompts)


@pytest.mark.asyncio
async def test_a_locate_node_with_no_answer_drops_its_check() -> None:
    # A fix-PR search that never ran must not report "no open fix PR found".
    causes = {FAILURES[0]["check"]: "main", FAILURES[1]["check"]: "flake"}
    stub = _triage_agent(causes, [])

    async def fn(prompt: str, opts: dict):
        if str(opts.get("label")).startswith("locate:"):
            return None
        return await stub(prompt, opts)

    res = await _run_triage(fn)

    assert res.ok, res.error
    assert [item["check"] for item in res.result["per_check"]] == [FAILURES[1]["check"]]
    assert res.result["dropped"] == [FAILURES[0]["check"]]


@pytest.mark.asyncio
async def test_a_failed_collect_ends_the_run_with_nothing_classified() -> None:
    async def fn(prompt: str, opts: dict):
        raise RuntimeError("collect node died")

    res = await _run_triage(fn)

    assert res.ok, res.error
    assert res.result["per_check"] == []
    assert "could not collect" in res.result["error"]


@pytest.mark.asyncio
async def test_every_triage_node_prompt_carries_the_read_only_charter() -> None:
    prompts: list[str] = []
    causes = {FAILURES[0]["check"]: "main", FAILURES[1]["check"]: "flake"}
    stub = _triage_agent(causes, [])

    async def fn(prompt: str, opts: dict):
        prompts.append(prompt)
        return await stub(prompt, opts)

    res = await _run_triage(fn)

    assert res.ok, res.error
    assert prompts
    for prompt in prompts:
        for word in (
            "You are READ-ONLY",
            "never commit, push",
            "rerun",
            "comment",
            "UNTRUSTED data",
        ):
            assert word in prompt
        # Remove fenced data so even a duplicate log outside a fence fails.
        parts = prompt.split("<check>")
        unfenced = parts[0]
        for part in parts[1:]:
            data, closing, remainder = part.partition("</check>")
            unfenced += remainder if closing else data
        for failure in FAILURES:
            assert failure["log_tail"] not in unfenced


# --------------------------------------------------------------------------- #
# 3. The library tier: listed, runnable, read-only, unshadowable
# --------------------------------------------------------------------------- #


def test_builtin_is_listed_and_reached_by_slug_and_id(tmp_path) -> None:
    library = _library(tmp_path)

    listed = {d["id"]: d for d in library.list()}
    builtin = listed[f"builtin:{TRIAGE}"]
    assert builtin["builtin"] is True
    assert builtin["slug"] == TRIAGE
    assert command_ref(builtin) == TRIAGE
    assert library.get(TRIAGE)["id"] == f"builtin:{TRIAGE}"
    assert library.get(f"builtin:{TRIAGE}")["source"] == TRIAGE_SOURCE
    assert library.has_live(f"builtin:{TRIAGE}")
    assert [d["id"] for d in library.search("triage red ci checks")] == [f"builtin:{TRIAGE}"]


def test_builtin_revision_follows_its_source_hash(tmp_path) -> None:
    one = _builtin_dir(tmp_path, {"demo.py": USER_SOURCE})
    first = load_builtins(one)[0]
    (one / "demo.py").write_text(USER_SOURCE.replace("hello", "hi"), encoding="utf-8")
    second = load_builtins(one)[0]

    assert first["revision"] != second["revision"]
    assert first["revision"] == int(first["content_hash"][:7], 16)
    assert workflow_library.declared_lineage(
        {"adapted_from": f"builtin:demo@{first['revision']}"}
    ) == {"workflow_id": "builtin:demo", "revision": first["revision"]}


def test_update_and_remove_of_a_builtin_refuse_and_write_nothing(tmp_path) -> None:
    library = _library(tmp_path)
    builtin = library.get(TRIAGE)

    with pytest.raises(BuiltinWorkflowError, match="save a copy"):
        library.update(builtin["id"], source=USER_SOURCE, expected_revision=builtin["revision"])
    for ref in (TRIAGE, builtin["id"]):
        with pytest.raises(BuiltinWorkflowError, match="save a copy"):
            library.remove(ref)
    assert library.get(TRIAGE)["source"] == TRIAGE_SOURCE
    assert not list(library.library_dir.glob("*.json"))


def test_service_refuses_builtin_writes_with_a_save_a_copy_answer(tmp_path) -> None:
    svc = WorkflowService(
        sessions=object(),
        store=WorkflowRunStore(tmp_path / "runs"),
        definition_library=_library(tmp_path),
        pool_agents=False,
    )
    builtin = svc.get_definition(TRIAGE)

    updated = svc.update_definition(
        TRIAGE, source=USER_SOURCE, expected_revision=builtin["revision"]
    )
    removed = svc.remove_definition(TRIAGE)

    for out in (updated, removed):
        assert out["ok"] is False and out["builtin"] is True
        assert "save a copy" in out["error"]
    # A copy is the way to change it, and it records the built-in as its parent.
    copy = svc.save_definition(
        TRIAGE_SOURCE,
        name="my triage",
        derived_from={"workflow_id": builtin["id"], "revision": builtin["revision"]},
    )
    assert copy["ok"] is True, copy
    assert copy["definition"]["slug"] == "my-triage"


def test_a_user_save_never_takes_a_builtin_slug(tmp_path) -> None:
    library = _library(tmp_path)

    assert library.slug_taken(TRIAGE)
    with pytest.raises(WorkflowSlugTakenError):
        library.create(source=USER_SOURCE, name="x", slug=TRIAGE, exact_slug=True)
    suffixed = library.create(source=USER_SOURCE, name=TRIAGE)
    assert suffixed["slug"] == f"{TRIAGE}-2"
    other = library.create(source=USER_SOURCE, name="other")
    renamed = library.update(other["id"], source=USER_SOURCE, expected_revision=1, slug=TRIAGE)
    assert renamed["slug"] == f"{TRIAGE}-3"
    assert library.get(TRIAGE)["id"] == f"builtin:{TRIAGE}"


def test_an_existing_user_record_with_a_builtin_slug_keeps_working_by_id(tmp_path) -> None:
    # Saved before the built-in shipped (or placed by hand): the slug now reaches
    # the built-in, the record still runs, changes and removes by its own id.
    library = _library(tmp_path)
    library.library_dir.mkdir(parents=True)
    record = {
        "id": "wfd_mine",
        "slug": TRIAGE,
        "name": "mine",
        "format": "python",
        "revision": 1,
        "source": USER_SOURCE,
        "revisions": [{"revision": 1, "source": USER_SOURCE}],
        "updated_at": NOW,
        # A file claiming to be built in is an ordinary record.
        "builtin": True,
    }
    (library.library_dir / "wfd_mine.json").write_text(json.dumps(record), encoding="utf-8")

    assert library.get(TRIAGE)["id"] == f"builtin:{TRIAGE}"
    mine = library.get("wfd_mine")
    assert "builtin" not in mine
    assert mine["shadowed_refs"] == [TRIAGE]
    assert command_ref(mine) == "wfd_mine"
    assert library.update("wfd_mine", source=USER_SOURCE, expected_revision=1) is not None
    assert library.remove("wfd_mine") is not None
    assert library.get(TRIAGE)["id"] == f"builtin:{TRIAGE}"


def test_a_folder_record_with_a_builtin_id_is_skipped(tmp_path, caplog) -> None:
    # Copying a built-in's id (and revision) into a hand-placed file must not let
    # its own source run, list or count as lineage under the built-in's identity.
    library = _library(tmp_path)
    real = library.get(TRIAGE)
    library.library_dir.mkdir(parents=True)
    fake = {
        **{k: real[k] for k in ("id", "revision", "name", "format")},
        "slug": "fake-triage",
        "source": USER_SOURCE,
        "revisions": [{"revision": real["revision"], "source": USER_SOURCE}],
    }
    path = library.library_dir / "fake.json"
    path.write_text(json.dumps(fake), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=workflow_library.__name__):
        assert library.get("fake-triage") is None
        assert [d["source"] for d in library.list() if d["id"] == real["id"]] == [TRIAGE_SOURCE]
        lineage = [d for d in library.lineage_candidates() if d["id"] == real["id"]]
    assert [d["source"] for d in lineage] == [TRIAGE_SOURCE]
    assert str(path) in caplog.text


def test_a_tombstone_holding_a_builtin_slug_does_not_hide_the_builtin(tmp_path) -> None:
    builtin_dir = _builtin_dir(tmp_path, {})
    library = _library(tmp_path, builtin_dir)
    saved = library.create(source=USER_SOURCE, name="demo")
    library.remove(saved["id"])
    (builtin_dir / "demo.py").write_text(USER_SOURCE, encoding="utf-8")

    assert library.get("demo")["id"] == "builtin:demo"


def test_library_works_without_the_builtin_directory(tmp_path) -> None:
    library = _library(tmp_path, tmp_path / "missing")
    saved = library.create(source=USER_SOURCE, name="mine")

    assert [d["id"] for d in library.list()] == [saved["id"]]
    assert library.get(TRIAGE) is None
    assert library.create(source=USER_SOURCE, name=TRIAGE)["slug"] == TRIAGE


def test_a_malformed_builtin_is_skipped_and_the_rest_still_load(tmp_path, caplog) -> None:
    builtin_dir = _builtin_dir(
        tmp_path,
        {
            "good.py": USER_SOURCE,
            "broken.py": "import os\n",
            "Not_A_Slug.py": USER_SOURCE,
            "__init__.py": "",
        },
    )
    (builtin_dir / "binary.py").write_bytes(b"\xff\xfe\x00")

    with caplog.at_level(logging.WARNING, logger=workflow_library.__name__):
        loaded = load_builtins(builtin_dir)

    assert [b["id"] for b in loaded] == ["builtin:good"]
    assert "broken.py" in caplog.text and "binary.py" in caplog.text


# --------------------------------------------------------------------------- #
# Running by slug, and rerun replay
# --------------------------------------------------------------------------- #


class _FakeSessions:
    async def get_or_create(self, key, **kw):
        return object(), True, False

    def release(self, key, *, cleanup=False):
        pass


def _service(tmp_path, monkeypatch, agent_fn, builtin_dir=None) -> WorkflowService:
    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "build_agent_fn", lambda sessions, **kw: agent_fn)
    return WorkflowService(
        sessions=_FakeSessions(),
        store=WorkflowRunStore(tmp_path / "runs"),
        definition_library=_library(tmp_path, builtin_dir),
        pool_agents=False,
    )


async def _finished(svc: WorkflowService, run_id: str) -> dict:
    def _done():
        status = svc.status(run_id)
        return status if status and status["status"] != "running" else None

    return await async_wait_until(_done, describe=lambda: f"run {run_id}")


@pytest.mark.asyncio
async def test_builtin_runs_by_slug_and_rerun_replays_collect(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    causes = {FAILURES[0]["check"]: "pr", FAILURES[1]["check"]: "timeout"}
    svc = _service(tmp_path, monkeypatch, _triage_agent(causes, calls))

    started = await svc.start_definition(TRIAGE, args={"pr": 7})
    assert started.get("run_id"), started
    first = await _finished(svc, started["run_id"])
    assert first["status"] == "finished", first
    assert calls.count("collect") == 1
    assert svc.result(started["run_id"])["result"]["per_check"][1]["action"] == "raise timeout"

    calls.clear()
    rerun = await svc.rerun_subtree(started["run_id"], from_index=1)
    assert rerun.get("run_id"), rerun
    second = await _finished(svc, rerun["run_id"])

    assert second["status"] == "finished", second
    assert "collect" not in calls  # replayed from the first run's cache
    assert sorted(c for c in calls if c.startswith("classify:")) == sorted(
        f"classify:{f['check']}" for f in FAILURES
    )


@pytest.mark.asyncio
async def test_a_builtin_removed_between_list_and_run_answers_not_found(
    tmp_path, monkeypatch
) -> None:
    builtin_dir = _builtin_dir(tmp_path, {"demo.py": USER_SOURCE})
    svc = _service(tmp_path, monkeypatch, _triage_agent({}, []), builtin_dir)
    assert [d["id"] for d in svc.list_definitions()] == ["builtin:demo"]

    (builtin_dir / "demo.py").unlink()
    out = await svc.start_definition("demo")

    assert out.get("not_found") is True
