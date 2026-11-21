"""The sensitive-path gates answer exactly as their plainest form does.

The plainest form of a decision resolves the anchor roots for every list it matches
(the gate's own rows, the installed apps' credential masks, the keystone artifact
parents) and compares each candidate against every target. The gates resolve the roots
once per decision (``_DecisionRoots``) and look the candidate's own folders up in the
target set (``_under_a_target``, ``_holds_a_target``). Neither may change an answer.

The proof is differential. A seeded script builds a small world on disk (two homes and
a link to one of them, override roots, a workspace with links into and out of the
protected folders, loops and dangling links) and then alternates changes to that world
with gate calls: the home moves, an override root appears or is repointed, a protected
folder becomes a link, the app-declared rows and the static list change, the resolver
stalls, faults or fails, the clock moves. The script runs twice from a rebuilt world,
once with the reference pieces below in place of the three new ones and once without,
and every answer must be equal, refusal text included.

The reference is that plainest form: ``_RootsNotKept`` makes every list resolve the
roots again, and the two ``_scan_*`` functions compare a candidate against every member.
The deviations at the foot are the negative controls: each replaces one piece with a
plausible wrong one and the same comparison must report it.

The resolver child is replaced by an in-process double that reads the real filesystem,
so no interpreter is started; the protected rows are read from the gate at run time and
never restated here.
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import security
from kiro_crew.security._child_realpath import realpaths_sharing_folders
from kiro_crew.subprocess_pool import SubprocessPoolTimeout

paths = security.paths
SEP = os.sep

#: Enough scripts that every deviation below is caught by at least one of them.
SEEDS = tuple(range(8))
#: World changes per script; two to five gate calls follow each.
ROUNDS = 70


# --------------------------------------------------------------------------- reference


def _scan_under(cand_cf: str, targets: set[str]) -> bool:
    return any(cand_cf == t or cand_cf.startswith(t + SEP) for t in targets)


def _scan_holds(dir_cf: str, targets: set[str]) -> bool:
    prefix = dir_cf + SEP
    return any(t == dir_cf or t.startswith(prefix) for t in targets)


class _RootsNotKept:
    """A decision's roots holder that holds nothing, so every list resolves them."""

    def __init__(self, *, inline: bool) -> None:
        self.inline = inline

    roots = property(lambda self: None, lambda self, value: None)


def _reference() -> dict[str, Any]:
    return {
        "_DecisionRoots": _RootsNotKept,
        "_under_a_target": _scan_under,
        "_holds_a_target": _scan_holds,
    }


# --------------------------------------------------------------------------- the doubles


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _InlinePool:
    """Stands in for the resolver pool's thread side: the job runs on the caller."""

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 -- handed to the waiter, as a pool does
            future.set_exception(exc)
        return future


class _Resolver:
    """The resolver child, in this interpreter: real answers, and the world's faults.

    A fault applies to a request that names a path under ``under`` (as spelled or as
    resolved) and only inside a bounded call, as in production: the inline gates wait
    on the filesystem and are never refused.
    """

    def __init__(self) -> None:
        self.mode: str | None = None
        self.under = ""

    def _touches(self, path: str) -> bool:
        if self.mode is None or paths._outside_bounded_call():
            return False
        spelled = os.path.normpath(paths._absolutized(path))
        try:
            real = os.path.realpath(spelled)
        except (OSError, ValueError):
            real = spelled
        return any(p == self.under or p.startswith(self.under + SEP) for p in (spelled, real))

    def _refuse(self, asked: list[str]) -> None:
        hit = next((p for p in asked if self._touches(p)), None)
        if hit is None:
            return
        if self.mode == "wedged":
            raise SubprocessPoolTimeout("the mount did not answer", child_syscall=None)
        if self.mode == "starved":
            raise SubprocessPoolTimeout("the child was off the CPU", child_syscall=b"running")
        if self.mode == "saturated":
            raise TimeoutError("no child took the request")
        if self.mode == "faulted":
            raise paths.PathResolutionStalled(hit, paths._stall_prefix(hit))

    def spellings(self, expanded: str) -> set[str]:
        self._refuse([expanded])
        if self.mode == "failing" and self._touches(expanded):
            return set()
        return paths._resolved_spellings_inline(paths._absolutized(expanded))

    def realpaths(self, asked: list[str]) -> list[str | None]:
        self._refuse(asked)
        answers = realpaths_sharing_folders([paths._absolutized(p) for p in asked])
        if self.mode == "failing":
            return [None if self._touches(p) else a for p, a in zip(asked, answers)]
        return answers


def _reset_gate_state() -> None:
    paths._home_targets_cache.clear()
    paths._home_targets_pin_state.clear()
    paths._path_resolve_degraded.clear()
    paths._path_resolve_load_probes.clear()
    paths._path_resolve_thread_waits.clear()
    paths._APP_READONLY_ROWS.reset()
    paths._APP_CREDENTIAL_MASK_ROWS.reset()


# --------------------------------------------------------------------------- the world


def _link(target: str, link: str) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pass  # the spot is taken, or sits under a file: the script goes on without it


def _unmake(path: str) -> None:
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            os.rmdir(path)
    except OSError:
        pass  # absent, or a folder with children: left as it is


class _World:
    """One seeded script: the folders, the knobs, and the gate calls between changes."""

    def __init__(self, root: Path, seed: int, patch: pytest.MonkeyPatch) -> None:
        self.rng = random.Random(seed)
        self.patch = patch
        self.clock = _Clock()
        self.resolver = _Resolver()
        self.answers: list[Any] = []
        self.asked: list[str] = []
        self.root = str(root)
        self.homes = [self._dir("homes", name) for name in ("a", "b")]
        self.home_link = os.path.join(self.root, "homes", "link")
        self.root_dirs = [self._dir("roots", name) for name in ("r0", "r1")]
        self.root_link = os.path.join(self.root, "roots", "l0")
        self.vaults = [self._dir("vault", name) for name in ("v0", "v1", "v2")]
        self.ws = self._dir("ws")
        self._dir("ws", "d0", "d1")
        # The protected rows, read from the gate as it stands and sampled: what they
        # are is the gate's business, this only needs some of each kind.
        rng = self.rng
        self.static_rows = list(paths._SENSITIVE_HOME_DIRS)
        self.write_rows = list(paths._WRITE_PROTECTED_HOME_PATHS)
        self.rule_leaves = list(paths._KIRO_PERMISSION_RULE_LEAVES)
        self.suffixes = list(paths._KEYSTONE_ARTIFACT_SUFFIXES)
        crew = list(paths._CREW_HOME_PREFIXES)
        self.entries = (
            rng.sample(sorted(set(self.static_rows)), 9)
            + rng.sample(sorted(set(self.write_rows)), 4)
            + rng.sample(sorted(set(paths._KEYSTONE_ARTIFACT_PARENTS)), 3)
        )
        self.made_up = [f"{prefix}/made-up-{i}" for i, prefix in enumerate(crew)] + [
            "made-up-top",
            ".Made-Up/Nested",
            "made up/with space/leaf",
        ]
        self.override_envs = [env for _field, env in paths._OVERRIDE_ROOT_ENVS] + list(
            paths._ADAPTER_ROOT_ENVS
        )
        self.set_roots: dict[str, str] = {}
        self.mask_rows = paths._AppMaskRows((), ())
        self.readonly_rows: tuple[str, ...] = ()
        self.rule_rows: list[str] = []
        self.listing_stalled = False
        for home in self.homes:
            for row in self.entries + self.made_up:
                self._make(os.path.join(home, *row.split("/")), rng.choice("dflx"))
        _link(self.homes[0], self.home_link)
        _link(self.root_dirs[0], self.root_link)
        some_dir = os.path.join(self.homes[0], *self.entries[0].split("/"))
        _link(some_dir, os.path.join(self.ws, "into0"))
        _link(os.path.join("..", "homes", "b", *self.entries[1].split("/")), self._ws("into1"))
        _link(self._ws("loop-b"), self._ws("loop-a"))
        _link(self._ws("loop-a"), self._ws("loop-b"))
        _link(self._ws("nowhere"), self._ws("dangling"))
        _link("..", self._ws("up"))
        _link(self.home_link, self._ws("homelink"))
        _link(self._ws("d0"), os.path.join(some_dir, "out"))
        for env in self.override_envs:
            patch.delenv(env, raising=False)
        self._set_home(self.home_link)
        patch.chdir(self.ws)

    # -- building blocks

    def _dir(self, *parts: str) -> str:
        path = os.path.join(self.root, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def _ws(self, name: str) -> str:
        return os.path.join(self.ws, name)

    def _make(self, path: str, kind: str) -> None:
        """*kind*: ``d`` a folder, ``f`` a file, ``l`` a link out to a vault, ``x`` nothing."""
        try:
            if kind == "d":
                os.makedirs(path, exist_ok=True)
            elif kind == "f":
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if not os.path.lexists(path):
                    Path(path).write_text("k", encoding="utf-8")
            elif kind == "l":
                os.makedirs(os.path.dirname(path), exist_ok=True)
                _link(self.rng.choice(self.vaults), path)
        except OSError:
            pass  # a parent is a file or a link to one: this row stays unmade

    def _set_home(self, home: str) -> None:
        self.home = home
        self.patch.setenv("HOME", home)
        self.patch.setenv("USERPROFILE", home)

    # -- changes to the world

    def change(self) -> None:
        rng = self.rng
        kind = rng.choice(
            "home home relink root root reroot masks masks readonly rules static "
            "swap swap fault fault listing clock clock clock wslink".split()
        )
        if kind == "home":
            self._set_home(
                rng.choice([self.home_link, *self.homes, os.path.join(self.root, "homes", "none")])
            )
        elif kind == "relink":
            _unmake(self.home_link)
            _link(rng.choice(self.homes), self.home_link)
        elif kind == "root":
            env = rng.choice(self.override_envs)
            value = rng.choice(
                [
                    None,
                    *self.root_dirs,
                    self.root_link,
                    os.path.join(self.root, "roots", "none"),
                    os.path.join(self.homes[0], *self.made_up[0].split("/")[:-1]),
                    "roots-as-spelled",
                ]
            )
            if value is None:
                self.set_roots.pop(env, None)
                self.patch.delenv(env, raising=False)
            else:
                self.set_roots[env] = value
                self.patch.setenv(env, value)
        elif kind == "reroot":
            _unmake(self.root_link)
            _link(rng.choice(self.root_dirs), self.root_link)
        elif kind == "masks":
            rows = rng.sample(self.made_up + self.entries, rng.randint(0, 3))
            parents = sorted({row.rsplit("/", 1)[0] for row in rows if "/" in row})
            self.mask_rows = paths._AppMaskRows(tuple(sorted(rows)), tuple(parents))
        elif kind == "readonly":
            self.readonly_rows = tuple(rng.sample(self.made_up, rng.randint(0, 2)))
        elif kind == "rules":
            self.rule_rows = rng.sample(self.rule_leaves, rng.randint(0, len(self.rule_leaves)))
        elif kind == "static":
            added = rng.sample(self.made_up, rng.randint(0, 2))
            self.patch.setattr(paths, "_SENSITIVE_HOME_DIRS", self.static_rows + added)
        elif kind == "swap":
            path = os.path.join(rng.choice(self.homes), *rng.choice(self.entries).split("/"))
            _unmake(path)
            self._make(path, rng.choice("dflx"))
        elif kind == "fault":
            self.resolver.mode = rng.choice(
                [None] * 7 + ["wedged", "starved", "saturated", "faulted", "failing"]
            )
            self.resolver.under = os.path.join(
                self.root, *rng.choice([(), ("homes",), ("roots",), ("vault",), ("ws",)])
            )
        elif kind == "listing":
            self.listing_stalled = rng.random() < 0.3
        elif kind == "clock":
            self.clock.now += rng.choice([0.01, 0.05, 0.3, 1.5, 35.0, 2000.0])
        elif kind == "wslink":
            _unmake(self._ws("into0"))
            target = os.path.join(rng.choice(self.homes), *rng.choice(self.entries).split("/"))
            _link(target, self._ws("into0"))

    # -- listers the gate reads its app-declared rows from

    def list_masks(self) -> Any:
        if self.listing_stalled:
            raise paths.PathResolutionStalled(self.home, paths._stall_prefix(self.home))
        return self.mask_rows

    def list_readonly(self) -> tuple[str, ...]:
        if self.listing_stalled:
            raise paths.PathResolutionStalled(self.home, paths._stall_prefix(self.home))
        return self.readonly_rows

    # -- a path worth asking about

    def spot(self) -> tuple[str, str | None]:
        rng = self.rng
        bases = [self.home, self.home_link, *self.homes, self.ws, *self.vaults]
        bases += sorted(self.set_roots.values())
        base = rng.choice(bases)
        if rng.random() < 0.3:
            try:
                base = os.path.realpath(base)
            except (OSError, ValueError):
                pass
        rows = self.entries + self.made_up + list(self.mask_rows.rows) + list(self.readonly_rows)
        row = rng.choice(rows + self.rule_rows)
        segs = row.split("/")
        if len(segs) > 1 and rng.random() < 0.3:
            segs = segs[1:]  # the tail an override root is joined with
        path = os.path.join(base, *segs)
        base_dir: str | None = None
        if rng.random() < 0.15:
            # A near miss of a row the gate holds now: it shares the spelling and names
            # neither the row nor a folder above it.
            held = self.entries[:9] + list(self.mask_rows.rows)
            path = os.path.join(rng.choice([self.home, *self.homes]), *rng.choice(held).split("/"))
            return (path + "x" if rng.random() < 0.5 else path[: -rng.randint(1, 2)]), None
        shape = rng.randrange(28)
        if shape == 0:
            path = os.path.join(path, "child")
        elif shape == 1:
            path = os.path.join(path, "deep", "er", "x.txt")
        elif shape == 2:
            path += "x"  # a sibling that only shares the spelling
        elif shape == 3:
            path += rng.choice(self.suffixes)
        elif shape == 4:
            path = os.path.join(path, "pub" + rng.choice(self.suffixes))
        elif shape == 5:
            path = os.path.join(path, ("pub" + rng.choice(self.suffixes)).upper())
        elif shape == 6:
            path = os.path.dirname(path)
        elif shape == 7:
            path = os.path.dirname(os.path.dirname(path))
        elif shape == 8:
            path = path.upper()
        elif shape == 9:
            path = path.swapcase()
        elif shape == 10:
            path = os.path.join(base, *[s.upper() for s in segs], "Child")
        elif shape == 11:
            path += SEP
        elif shape == 12:
            path = os.path.join(path, "..", os.path.basename(path), "k")
        elif shape == 13:
            path = os.path.join(path, ".")
        elif shape == 14:
            path = base + SEP + SEP + SEP.join(segs)
        elif shape == 15:
            path = "~/" + row
        elif shape == 16:
            path = "$HOME/" + row + "/child"
        elif shape in (17, 18):
            try:
                path = os.path.relpath(path, self.ws)
            except ValueError:
                pass  # another drive: asked as spelled
            base_dir = self.ws if shape == 17 else None
        elif shape == 19:
            link = rng.choice(["into0", "into1", "loop-a", "dangling", "homelink", "up"])
            path = os.path.join(self.ws, link, rng.choice(["", "child", row]))
        elif shape == 20:
            path = os.path.join(self.ws, "up", "homes", rng.choice("ab"), *segs)
        elif shape == 21:
            path = rng.choice(["", SEP, self.root, os.path.dirname(self.root), "d0", ".."])
        elif shape == 22:
            path, base_dir = os.path.join("d0", "..", "into0", "child"), self.ws
        elif shape in (23, 26):
            path = path[: len(path) - rng.randint(1, 3)]  # shares a spelling, names no folder
        elif shape in (24, 25):
            path = os.path.join(self.ws, "d0", "d1", segs[-1] + rng.choice(["", ".tmp", ".md"]))
        return path, base_dir

    # -- asking

    def ask(self, loop: asyncio.AbstractEventLoop) -> None:
        rng = self.rng
        path, base_dir = self.spot()
        made_up = rng.sample(self.made_up + self.entries, 3)
        try:
            real = os.path.realpath(os.path.join(base_dir or self.ws, os.path.expanduser(path)))
        except (OSError, ValueError):
            real = os.path.abspath(path)
        gates: dict[str, Callable[[], Any]] = {
            "read": lambda: security.sensitive_path_refusal(path, base_dir),
            "write": lambda: security.sensitive_write_path_refusal(path, base_dir),
            "resolved": lambda: security.is_sensitive_resolved_path(real),
            "canonical": lambda: security.is_sensitive_canonical_path(real),
            "contains": lambda: security.path_contains_sensitive(path, base_dir),
            "contains-resolved": lambda: security.path_contains_sensitive(real, pre_resolved=True),
            "rows": lambda: paths._matches_gate_rows(path, made_up, base_dir),
            "rows-strict": lambda: paths._matches_gate_rows(path, made_up, base_dir, strict=True),
            "one-list": lambda: paths._path_in_home_dirs(path, made_up, base_dir),
            "artifact": lambda: paths._is_keystone_publish_artifact(path, base_dir),
        }
        name = rng.choice(sorted(gates))
        where = rng.choice(["loop", "loop", "thread", "judgment"])

        def answer() -> Any:
            try:
                return gates[name]()
            except Exception as exc:  # noqa: BLE001 -- what a gate raises is its answer here
                return f"raised {type(exc).__name__}"

        if where == "loop":

            async def on_loop() -> Any:
                return answer()

            got = loop.run_until_complete(on_loop())
        elif where == "judgment":
            with paths.bounded_judgment():
                got = answer()
        else:
            got = answer()
        self.asked.append(f"{name} on {where}: {path!r} base={base_dir!r} real={real!r}")
        self.answers.append(got)


def _run_script(
    world_dir: Path, seed: int, replaced: dict[str, Any], *, rounds: int = ROUNDS
) -> _World:
    """Build the world for *seed*, run its script with *replaced* in the gate, tear it down."""
    root = world_dir / "world"
    with pytest.MonkeyPatch.context() as patch:
        world = _World(root, seed, patch)
        patch.setattr(paths, "_path_resolve_clock", world.clock)
        patch.setattr(paths, "_app_rows_clock", world.clock)
        patch.setattr(paths, "time", SimpleNamespace(monotonic=world.clock))
        patch.setattr(paths, "path_resolve_executor", lambda: _InlinePool())
        patch.setattr(paths, "_resolved_spellings", world.resolver.spellings)
        patch.setattr(paths, "_realpaths_or_none", world.resolver.realpaths)
        patch.setattr(paths, "_list_app_credential_mask_rows", world.list_masks)
        patch.setattr(paths, "_list_app_readonly_dir_rows", world.list_readonly)
        patch.setattr(paths, "_permission_rule_rows", lambda: list(world.rule_rows))
        for name, value in replaced.items():
            patch.setattr(paths, name, value)
        _reset_gate_state()
        loop = asyncio.new_event_loop()
        try:
            for _ in range(rounds):
                world.change()
                for _ in range(world.rng.randint(2, 5)):
                    world.ask(loop)
        finally:
            loop.close()
            _reset_gate_state()
            patch.chdir(world_dir)
            shutil.rmtree(root, ignore_errors=True)
    return world


#: The reference run of each script, which is the same every time it is made.
_REFERENCE_RUNS: dict[int, _World] = {}


def _differences(world_dir: Path, seed: int, replaced: dict[str, Any]) -> list[str]:
    """Where the gate with *replaced* in it answers differently from the reference."""
    reference = _REFERENCE_RUNS.get(seed)
    if reference is None:
        reference = _REFERENCE_RUNS[seed] = _run_script(world_dir, seed, _reference())
    subject = _run_script(world_dir, seed, replaced)
    assert subject.asked == reference.asked, "the two runs did not ask the same questions"
    return [
        f"seed {seed} call {i}: {asked}\n  reference: {was!r}\n  now:       {now!r}"
        for i, (asked, was, now) in enumerate(
            zip(reference.asked, reference.answers, subject.answers)
        )
        if was != now
    ]


@pytest.fixture(scope="module")
def world_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One folder for every world of this module, so each script spells the same paths."""
    return tmp_path_factory.mktemp("gate-worlds")


# --------------------------------------------------------------------------- the proof


@pytest.mark.parametrize("seed", SEEDS)
def test_every_gate_answers_as_the_reference_does(world_dir: Path, seed: int) -> None:
    differences = _differences(world_dir, seed, {})
    assert not differences, "\n".join(differences[:5])


def test_the_scripts_reach_every_kind_of_answer(world_dir: Path) -> None:
    # A comparison of two runs proves nothing about a kind of answer neither run gave.
    seen: set[str] = set()
    for seed in SEEDS:
        for answer in _run_script(world_dir, seed, {}).answers:
            if isinstance(answer, str):
                unverifiable = security.is_unverifiable_path_refusal(answer)
                seen.add("stall" if unverifiable else answer.split(":")[0])
            else:
                seen.add(repr(answer))
    assert {
        "True",
        "False",
        "None",
        "stall",
        "Blocked",
        "raised PathResolutionStalled",
    } <= seen, seen


def _segments(rng: random.Random) -> str:
    words = ["a", "b", "A", "ab", "", ".", "a b", "ä"]
    return SEP.join(rng.choice(words) for _ in range(rng.randint(0, 5)))


def _lookup_disagreements(
    under: Callable[[str, set[str]], bool], holds: Callable[[str, set[str]], bool]
) -> int:
    """How often *under* and *holds* differ from a comparison against every member.

    Over spellings no real target set holds as well: an empty member, a member that is
    one separator, doubled and trailing separators.
    """
    rng = random.Random(39162)
    wrong = 0
    for _ in range(4000):
        members = {_segments(rng).casefold() for _ in range(rng.randint(0, 6))}
        built = paths._BuiltTargets(members)
        for _ in range(6):
            cand = _segments(rng).casefold()
            if rng.random() < 0.5 and members:
                cand = rng.choice(sorted(members)) + rng.choice(["", SEP, SEP + "x", "x"])
            folder = cand.rstrip(SEP)
            wrong += under(cand, built) is not _scan_under(cand, members)
            wrong += holds(folder, built) is not _scan_holds(folder, members)
            wrong += holds(folder, members) is not _scan_holds(folder, members)
    return wrong


def test_the_lookups_agree_with_a_comparison_against_every_member() -> None:
    assert _lookup_disagreements(paths._under_a_target, paths._holds_a_target) == 0


def test_a_decision_resolves_its_roots_once(world_dir: Path) -> None:
    """One resolution per decision whatever the number of lists, and several without the holder."""

    def resolutions(replaced: dict[str, Any]) -> dict[str, int]:
        counted: dict[str, int] = {}
        real = paths._resolve_root_anchors
        calls = [0]

        def counting(logical_home: str) -> Any:
            calls[0] += 1
            return real(logical_home)

        root = world_dir / "world"
        with pytest.MonkeyPatch.context() as patch:
            world = _World(root, 0, patch)
            world.mask_rows = paths._AppMaskRows((world.made_up[0],), (world.made_up[2],))
            world.readonly_rows = (world.made_up[3],)
            world.rule_rows = list(world.rule_leaves)
            patch.setattr(paths, "path_resolve_executor", lambda: _InlinePool())
            patch.setattr(paths, "_resolved_spellings", world.resolver.spellings)
            patch.setattr(paths, "_realpaths_or_none", world.resolver.realpaths)
            patch.setattr(paths, "_list_app_credential_mask_rows", world.list_masks)
            patch.setattr(paths, "_list_app_readonly_dir_rows", world.list_readonly)
            patch.setattr(paths, "_permission_rule_rows", lambda: list(world.rule_rows))
            patch.setattr(paths, "time", SimpleNamespace(monotonic=world.clock))
            patch.setattr(paths, "_resolve_root_anchors", counting)
            for name, value in replaced.items():
                patch.setattr(paths, name, value)
            _reset_gate_state()
            plain = os.path.join(world.ws, "d0", "notes.tmp")
            gates: dict[str, Callable[[], Any]] = {
                "read": lambda: security.sensitive_path_refusal(plain),
                "write": lambda: security.sensitive_write_path_refusal(plain),
                "resolved": lambda: security.is_sensitive_resolved_path(plain),
                "contains": lambda: security.path_contains_sensitive(plain, pre_resolved=True),
            }
            loop = asyncio.new_event_loop()
            try:
                for name, gate in gates.items():

                    async def on_loop(gate: Callable[[], Any] = gate) -> Any:
                        return gate()

                    ask = (
                        (lambda: loop.run_until_complete(on_loop()))
                        if name in ("read", "write")
                        else gate
                    )
                    assert not ask(), name  # the first call builds the target sets
                    calls[0] = 0
                    assert not ask(), name
                    counted[name] = calls[0]
            finally:
                loop.close()
                _reset_gate_state()
                patch.chdir(world_dir)
                shutil.rmtree(root, ignore_errors=True)
        return counted

    assert resolutions({}) == {"read": 1, "write": 1, "resolved": 1, "contains": 1}
    without = resolutions({"_DecisionRoots": _RootsNotKept})
    assert all(without[name] > 1 for name in without), without


# --------------------------------------------------------------------------- deviations


def _roots_kept_across_decisions() -> dict[str, Any]:
    kept: dict[bool, Any] = {}

    class Kept:
        def __init__(self, *, inline: bool) -> None:
            self.inline = inline

        roots = property(
            lambda self: kept.get(self.inline),
            lambda self, value: kept.__setitem__(self.inline, value),
        )

    return {"_DecisionRoots": Kept}


def _roots_served_through_a_stall() -> dict[str, Any]:
    real, last = paths._resolved_root_key, []

    def lenient() -> Any:
        try:
            roots = real()
        except paths.PathResolutionStalled:
            if not last:
                raise
            return last[-1]
        last.append(roots)
        return roots

    return {"_resolved_root_key": lenient}


def _one_target_set_for_a_whole_decision() -> dict[str, Any]:
    real, first = paths._home_dir_targets, {}

    def shared(home_dirs: list[str], *, inline: bool = False, decision: Any = None) -> set[str]:
        if decision is None:
            return real(home_dirs, inline=inline)
        if id(decision) not in first:
            first[id(decision)] = (decision, real(home_dirs, inline=inline, decision=decision))
        return first[id(decision)][1]

    return {"_home_dir_targets": shared}


def _a_stalled_listing_reads_as_no_rows() -> dict[str, Any]:
    real = paths._app_credential_mask_rows

    def lenient(*, inline: bool = False) -> Any:
        try:
            return real(inline=inline)
        except paths.PathResolutionStalled:
            return paths._AppMaskRows((), ())

    return {"_app_credential_mask_rows": lenient}


def _folders_above_kept_from_the_first_set(patch: pytest.MonkeyPatch) -> None:
    real, first = paths._BuiltTargets.folders_above, []

    def kept(self: Any) -> frozenset[str]:
        if not first:
            first.append(real(self))
        return first[0]

    patch.setattr(paths._BuiltTargets, "folders_above", kept)


#: Lookups that are wrong in one way each, as ``(under, holds)`` with the gate's own
#: function where the deviation is in the other.
WRONG_LOOKUPS: dict[str, tuple[Any, Any]] = {
    "under: no separator after the member": (
        lambda c, ts: any(c.startswith(t) for t in ts),
        None,
    ),
    "under: the member itself is not matched": (
        lambda c, ts: any(c.startswith(t + SEP) for t in ts),
        None,
    ),
    "under: only the folder the path sits in is looked up": (
        lambda c, ts: c in ts or os.path.dirname(c) in ts,
        None,
    ),
    "under: the first folder is never looked up": (
        lambda c, ts: c in ts or any(c[:i] in ts for i in range(1, len(c)) if c[i] == SEP),
        None,
    ),
    "holds: the member itself is not matched": (
        None,
        lambda d, ts: any(t.startswith(d + SEP) for t in ts),
    ),
    "holds: only the folder a member sits in is known": (
        None,
        lambda d, ts: d in ts or d in {os.path.dirname(t) for t in ts},
    ),
    "holds: no separator after the folder": (
        None,
        lambda d, ts: any(t.startswith(d) for t in ts),
    ),
}
#: The wrong lookups a real target set cannot tell: it has no member this short.
ONLY_TOLD_BY_SPELLINGS = {"under: the first folder is never looked up"}


def _with_lookups(name: str) -> dict[str, Any]:
    under, holds = WRONG_LOOKUPS[name]
    replaced = {"_under_a_target": under, "_holds_a_target": holds}
    return {key: value for key, value in replaced.items() if value is not None}


DEVIATIONS: dict[str, Callable[[], dict[str, Any]]] = {
    "the roots outlive the decision": _roots_kept_across_decisions,
    "the last roots are served through a stall": _roots_served_through_a_stall,
    "one target set serves every list of a decision": _one_target_set_for_a_whole_decision,
    "a stalled listing reads as no rows": _a_stalled_listing_reads_as_no_rows,
    **{
        name: (lambda name=name: _with_lookups(name))
        for name in sorted(set(WRONG_LOOKUPS) - ONLY_TOLD_BY_SPELLINGS)
    },
}


@pytest.mark.parametrize("name", sorted(WRONG_LOOKUPS))
def test_the_member_comparison_reports_a_wrong_lookup(name: str) -> None:
    under, holds = WRONG_LOOKUPS[name]
    assert _lookup_disagreements(under or paths._under_a_target, holds or paths._holds_a_target)


@pytest.mark.parametrize("deviation", sorted(DEVIATIONS))
def test_the_comparison_reports_a_deviation(world_dir: Path, deviation: str) -> None:
    for seed in SEEDS:
        if _differences(world_dir, seed, DEVIATIONS[deviation]()):
            return
    pytest.fail(f"no script told the gate with this deviation from the reference: {deviation}")


def test_the_comparison_reports_folders_kept_from_another_set(
    world_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folders_above_kept_from_the_first_set(monkeypatch)
    assert any(_differences(world_dir, seed, {}) for seed in SEEDS)
