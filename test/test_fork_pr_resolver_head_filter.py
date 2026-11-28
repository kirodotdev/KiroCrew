"""A fork-triggered workflow finds its pull request without walking every open one.

``workflow_run`` carries no pull-request number for a fork, so each of these
workflows resolves it from the run's head repository, branch and SHA. Listing
every open pull request to do that costs one request per hundred open pull
requests, in every workflow, on every fork head. The resolver asks for the
head's own branch first (``head=<owner>:<ref>``, the list endpoint's filter) and
walks the whole open set only when that answer holds no match.

The filter value is the fork's to choose, so what these tests pin is that it can
only narrow the list: the resolver step is run for real against a stub ``gh``
that answers the filtered query as GitHub does, and again against one that
ignores the filter and answers every query with the whole open set. Both runs
must end the same way.

The workflows are found by reading ``.github/workflows``, not listed here.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote

import pytest
import yaml
from ai_review_workflows_helpers import (
    WORKFLOWS,
    _bash,
    _fork_child_env,
    _proc_log,
    _script_file,
    _write_stub,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.testing.wait import default_timeout

REPO = "base-owner/base-repo"
WALK = f"repos/{REPO}/pulls?state=open&per_page=100"
#: The open-set list as the workflows spell it, before the shell expands it.
LIST_SOURCE = "repos/$REPO/pulls?state=open"
RATE_LIMIT_READ = "api -i rate_limit"

#: The event fields a resolver step reads, by the expression that supplies them.
EVENT_FIELDS = {
    "github.repository": "repo",
    "github.event.workflow_run.head_sha": "sha",
    "github.event.workflow_run.head_repository.full_name": "head_repo",
    "github.event.workflow_run.head_branch": "ref",
}


@dataclass(frozen=True)
class Resolver:
    workflow: str
    step: str
    env: dict[str, str]
    script: str

    def __str__(self) -> str:
        return self.workflow


def _resolvers() -> list[Resolver]:
    """Every fork-triggered step that can walk the whole open set, with no number to go on."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = doc.get(True) or doc.get("on") or {}
        if "workflow_run" not in triggers:
            continue
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                env = {k: str(v) for k, v in (step.get("env") or {}).items()}
                supplied = {v.strip("${} ") for v in env.values()}
                # A step handed a pull-request number resolves nothing.
                if LIST_SOURCE in step.get("run", "") and not any(
                    "pull_request.number" in v or "pull_requests" in v for v in supplied
                ):
                    found.append(Resolver(path.name, step["name"], env, step["run"]))
    return found


RESOLVERS = _resolvers()
by_resolver = pytest.mark.parametrize("resolver", RESOLVERS, ids=str)


def _pull(number: int, owner: str, name: str, ref: str, sha: str, label_owner: str = "") -> dict:
    return {
        "number": number,
        "state": "open",
        "head": {
            "label": f"{label_owner or owner}:{ref}",
            "ref": ref,
            "sha": sha,
            "repo": {"full_name": f"{owner}/{name}"},
        },
    }


@dataclass(frozen=True)
class Head:
    owner: str
    name: str
    ref: str
    sha: str

    @property
    def label(self) -> str:
        return f"{self.owner}:{self.ref}"


GH_STUB = r"""
printf '%s\n' "$*" >> "$STUB_DIR/gh.log"
if [ "$*" = "api -i rate_limit" ]; then
  printf 'HTTP/2.0 200 OK\n\n{"resources":{"core":{"remaining":4000,"reset":1}}}\n'
  exit 0
fi
case "${2-}" in
  *"pulls?state=open"*)
    if [ -n "${STUB_LIST_FAILS:-}" ]; then
      echo "gh: Bad Gateway (HTTP 502)" >&2
      exit 1
    fi
    case "$2" in
      *"&head="*) page="$STUB_DIR/filtered.json" ;;
      *) page="$STUB_DIR/walk.json" ;;
    esac
    expr=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--jq" ]; then expr="$2"; fi
      shift
    done
    if [ -n "$expr" ]; then jq -r "$expr" "$page"; else cat "$page"; fi
    exit 0
    ;;
esac
exit 0
"""


@dataclass
class Outcome:
    rc: int
    stdout: str
    outputs: str
    list_queries: list[str]
    later_calls: list[str]
    log: str

    def verdict(self) -> tuple[int, str, list[str]]:
        """How the step ended, apart from which list queries it sent."""
        said = "\n".join(
            line
            for line in self.stdout.splitlines()
            if "No open PR matched this head on" not in line
        )
        return (self.rc, said + "\n" + self.outputs, self.later_calls)

    def names(self, number: int) -> bool:
        text = "\n".join([self.stdout, self.outputs, *self.later_calls])
        return re.search(rf"(?<![0-9a-f]){number}(?![0-9a-f])", text) is not None


def _run(
    root: Path,
    resolver: Resolver,
    head: Head,
    pulls: list[dict],
    *,
    filter_honoured: bool,
    list_fails: bool = False,
) -> Outcome:
    bash = _bash()
    if bash is None:
        pytest.skip("the resolver step is exercised where bash is available")
    root.mkdir()
    stubs = root / "stubs"
    stubs.mkdir()
    _write_stub(stubs / "gh", GH_STUB)
    _write_stub(stubs / "sleep", "exit 0\n")
    filtered = [p for p in pulls if p["head"]["label"] == head.label] if filter_honoured else pulls
    (root / "walk.json").write_text(json.dumps(pulls), encoding="utf-8")
    (root / "filtered.json").write_text(json.dumps(filtered), encoding="utf-8")
    (root / "gh.log").write_text("", encoding="utf-8")
    event = {
        "repo": REPO,
        "sha": head.sha,
        "head_repo": f"{head.owner}/{head.name}",
        "ref": head.ref,
    }
    env = {
        name: event.get(EVENT_FIELDS.get(value.strip("${} "), ""), "")
        for name, value in resolver.env.items()
    }
    extra = {"STUB_DIR": str(root), "GITHUB_OUTPUT": str(root / "outputs"), **env}
    if list_fails:
        extra["STUB_LIST_FAILS"] = "1"
    done = subprocess.run(
        [bash, "-e", _script_file(root, resolver.script)],
        cwd=root,
        env=_fork_child_env(root, stubs, **extra),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
        check=False,
    )
    calls = (root / "gh.log").read_text(encoding="utf-8").splitlines()
    lists = [c for c in calls if "pulls?state=open" in c]
    outputs = root / "outputs"
    return Outcome(
        rc=done.returncode,
        stdout=done.stdout or "",
        outputs=outputs.read_text(encoding="utf-8") if outputs.exists() else "",
        list_queries=[c.split()[1] for c in lists],
        later_calls=[c for c in calls if c not in lists and c != RATE_LIMIT_READ],
        log=_proc_log(done),
    )


def _both(root: Path, resolver: Resolver, head: Head, pulls: list[dict]) -> tuple[Outcome, Outcome]:
    asked = _run(root / "asked", resolver, head, pulls, filter_honoured=True)
    walked = _run(root / "walked", resolver, head, pulls, filter_honoured=False)
    return asked, walked


def _head_param(query: str) -> list[tuple[str, str]]:
    return parse_qsl(query.partition("?")[2], keep_blank_values=True)


HEAD = Head("fork-owner", "base-repo", "fix/thing", "a" * 40)
OTHER_SHA = "b" * 40


def test_the_resolvers_are_found() -> None:
    # An empty roster would pass every test below without running anything.
    assert len({r.workflow for r in RESOLVERS}) >= 2
    assert all(r.script.count(LIST_SOURCE) == 1 for r in RESOLVERS)


@by_resolver
def test_one_page_answers_when_the_branch_has_its_pull_request(
    tmp_path: Path, resolver: Resolver
) -> None:
    crowd = [_pull(20000 + i, f"user{i}", "base-repo", f"b{i}", OTHER_SHA) for i in range(5)]
    pulls = [*crowd, _pull(31337, HEAD.owner, HEAD.name, HEAD.ref, HEAD.sha)]
    asked, walked = _both(tmp_path, resolver, HEAD, pulls)
    assert asked.names(31337), asked.log
    assert asked.verdict() == walked.verdict(), asked.log + walked.log
    assert len(asked.list_queries) == 1, asked.list_queries
    assert _head_param(asked.list_queries[0]) == [
        ("state", "open"),
        ("head", HEAD.label),
        ("per_page", "100"),
    ]


@by_resolver
def test_no_match_on_the_filtered_page_walks_the_open_set(
    tmp_path: Path, resolver: Resolver
) -> None:
    # The pull request's head label names another owner than its repository (a
    # renamed or transferred fork), so the filter does not return it.
    pulls = [_pull(31337, HEAD.owner, HEAD.name, HEAD.ref, HEAD.sha, label_owner="old-name")]
    asked, walked = _both(tmp_path, resolver, HEAD, pulls)
    assert asked.names(31337), asked.log
    assert asked.verdict() == walked.verdict(), asked.log + walked.log
    assert [("head" in dict(_head_param(q))) for q in asked.list_queries] == [True, False]
    assert asked.list_queries[1] == WALK


@by_resolver
def test_a_head_no_open_pull_request_carries_ends_as_the_walk_ends(
    tmp_path: Path, resolver: Resolver
) -> None:
    pulls = [_pull(31337, HEAD.owner, HEAD.name, HEAD.ref, OTHER_SHA)]
    asked, walked = _both(tmp_path, resolver, HEAD, pulls)
    assert not asked.names(31337), asked.log
    assert asked.verdict() == walked.verdict(), asked.log + walked.log


@by_resolver
def test_two_pull_requests_on_one_head_end_as_the_walk_ends(
    tmp_path: Path, resolver: Resolver
) -> None:
    pulls = [
        _pull(31337, HEAD.owner, HEAD.name, HEAD.ref, HEAD.sha),
        _pull(42424, HEAD.owner, HEAD.name, HEAD.ref, HEAD.sha),
    ]
    asked, walked = _both(tmp_path, resolver, HEAD, pulls)
    assert asked.verdict() == walked.verdict(), asked.log + walked.log
    assert len(asked.list_queries) == 1


@by_resolver
def test_a_failed_list_query_ends_the_step_with_nothing_resolved(
    tmp_path: Path, resolver: Resolver
) -> None:
    pulls = [_pull(31337, HEAD.owner, HEAD.name, HEAD.ref, HEAD.sha)]
    failed = _run(tmp_path / "failed", resolver, HEAD, pulls, filter_honoured=True, list_fails=True)
    assert failed.rc != 0, failed.log
    assert not failed.names(31337), failed.log
    # A failed query knows nothing, so it is never answered by asking the walk.
    assert all("head" in dict(_head_param(q)) for q in failed.list_queries), failed.list_queries


#: Characters a git ref may hold that mean something in a URL, a shell or jq.
HOSTILE_REFS = (
    "x&state=closed",
    "x&head=base-owner:main",
    "x#frag",
    "x%26per_page%3D1",
    "x+y=z",
    'quote"d',
    "single'd",
    "$(id)",
    "`id`",
    "a;b|c>d<e",
    "ünïcödé/分支",
    "main",
    "l" + "o" * 240 + "ng",
)


@by_resolver
@pytest.mark.parametrize("ref", HOSTILE_REFS, ids=range(len(HOSTILE_REFS)))
def test_a_hostile_branch_name_changes_only_which_page_is_asked_for(
    tmp_path: Path, resolver: Resolver, ref: str
) -> None:
    head = Head("fork-owner", "base-repo", ref, "c" * 40)
    pulls = [
        # Another fork's pull request on a ref of the same name, and the base
        # repository's own, are not this head's.
        _pull(11111, "other-owner", "base-repo", ref, head.sha),
        _pull(22222, "base-owner", "base-repo", ref, head.sha),
        _pull(31337, head.owner, head.name, ref, head.sha),
    ]
    asked, walked = _both(tmp_path, resolver, head, pulls)
    assert asked.verdict() == walked.verdict(), asked.log + walked.log
    assert asked.list_queries == [
        f"repos/{REPO}/pulls?state=open&head={quote(head.label, safe='')}&per_page=100"
    ]
    assert _head_param(asked.list_queries[0]) == [
        ("state", "open"),
        ("head", head.label),
        ("per_page", "100"),
    ]
    if resolver.script.count("@tsv") == 0 or "\\" not in ref:
        assert asked.names(31337) and not asked.names(11111), asked.log


_OWNERS = st.sampled_from(["fork-owner", "other-owner", "base-owner"])
_NAMES = st.sampled_from(["base-repo", "second-fork"])
_REFS = st.sampled_from(["fix/thing", "main", "x&state=closed", "fix/thing2"])
_SHAS = st.sampled_from(["a" * 40, "b" * 40])
_LABEL_OWNERS = st.sampled_from(["", "", "", "old-name", "other-owner"])
_PULLS = st.lists(st.tuples(_OWNERS, _NAMES, _REFS, _SHAS, _LABEL_OWNERS), max_size=6)
_CASE = itertools.count()


@settings(max_examples=30)
@given(
    which=st.integers(min_value=0, max_value=max(len(RESOLVERS) - 1, 0)),
    head=st.builds(Head, _OWNERS, _NAMES, _REFS, _SHAS),
    rows=_PULLS,
)
def test_asking_first_ends_as_the_walk_ends_on_any_open_set(
    tmp_path_factory: pytest.TempPathFactory, which: int, head: Head, rows: list[tuple]
) -> None:
    resolver = RESOLVERS[which]
    pulls = [_pull(30000 + i, *row) for i, row in enumerate(rows)]
    root = tmp_path_factory.mktemp(f"case{next(_CASE)}")
    asked, walked = _both(root, resolver, head, pulls)
    assert asked.verdict() == walked.verdict(), asked.log + walked.log
    assert "head" in dict(_head_param(asked.list_queries[0]))
    assert len(asked.list_queries) <= 2
