"""Reach the container package the way the image does: by its build context.

The subject of this suite is imported as top-level ``container.*``, because that
is what it is inside the image -- ``/app`` is on ``sys.path`` and the package
sits at ``/app/container``. Modules import ``container.common`` absolutely, so
that name is part of the image's contract rather than an artifact of where the
source sits in the image. Rewriting the imports to this repository's package
path would break the image at runtime.

``crew/runtime/`` therefore has no ``__init__.py``: it is a docker build
context, not a python package, and
``test_spawn_audit.py::test_container_image_assets_are_not_imported`` pins that
so the gateway can never import this tree by package path. Putting the build
context on ``sys.path`` here is what lets the tests resolve ``container`` while
that stays true.

pytest's prepend import mode happens to insert this same directory (it is the
first ancestor without an ``__init__.py``), so this file is belt and braces --
but the import root is a fact about the image, not about a pytest setting, and
it should be stated somewhere that survives a change to either.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_BUILD_CONTEXT = Path(__file__).resolve().parents[1]
_HERE = Path(__file__).resolve().parent

# The one lane whose entire purpose is to run this suite sets this. Everywhere else
# -- a developer's laptop, the application's own CI shards -- leaves it unset, and
# the skips below stay skips.
#
# It exists because a skip is indistinguishable from a pass in every report anyone
# reads, and the skips below are wide: a missing image dependency takes the WHOLE
# suite out of collection, so 236 tests can be entirely absent from a run that
# reports success. Installing those dependencies in a dedicated job is not enough on
# its own, because it fixes the environment and not the mechanism: a dependency
# rename, an extras split or a resolver change puts that lane back to reporting
# success while collecting nothing, with no signal anywhere.
#
# So the lane that installs them also declares that it MUST be able to run them, and
# under that declaration every reason this file would decline to collect becomes a
# hard error instead. The rule is one-directional on purpose: this variable can only
# turn a skip into a failure, never a failure into a skip, so setting it can hide
# nothing.
_REQUIRED_ENV = "CREW_CONTAINER_TESTS_REQUIRED"
_REQUIRED_RAW = os.environ.get(_REQUIRED_ENV)
# Parsed strictly, not with ``bool(os.environ.get(...))``, which reads "0" and "false" as
# ON because any non-empty string is truthy. A lane that set it to "0" believing it had
# turned the requirement off would get the requirement, and a lane that set it to
# "true" expecting a boolean would get it for the wrong reason. An unrecognised value is
# REFUSED rather than guessed at, the same call ``parse_route_prefix`` makes about a bare
# word: this decides whether a suite is allowed to skip, so reading a typo as either
# answer is worse than saying the value is not a boolean.
if _REQUIRED_RAW is None or _REQUIRED_RAW.strip() == "":
    _REQUIRED = False
elif _REQUIRED_RAW.strip().lower() in ("1", "true", "yes", "on"):
    _REQUIRED = True
elif _REQUIRED_RAW.strip().lower() in ("0", "false", "no", "off"):
    _REQUIRED = False
else:
    raise RuntimeError(
        f"{_REQUIRED_ENV} must be a boolean (1/0, true/false), got {_REQUIRED_RAW!r}. It "
        "decides whether this suite may skip, so an unrecognised value is refused rather "
        "than read as either answer."
    )

# Floor on how many tests the suite must yield, checked only under _REQUIRED_ENV.
#
# Read off a real collection (327 items). The margin is 2, not a comfortable ten per
# cent, and the tightness IS the feature: the smallest module here contributes 3
# tests, so a floor of 325 is tripped by losing even the smallest one, which a looser
# floor would wave through. The per-module check below catches a module that stops
# being collected at all; this catches the subtler shape, a module still collected
# but yielding fewer tests than it holds -- a parametrize source that silently
# empties, a decorator that swallows its function, an import guard that turns a class
# into nothing.
#
# When the suite grows, raise it. It may be LOWERED only alongside a deliberate
# deletion of tests, in the same commit, and never to make a red lane green: a floor
# edited down to meet the measurement measures nothing.
_MIN_COLLECTED = 325

# Not collected on a non-POSIX host. This suite's SUBJECT is the source of a Linux
# container image, built by the deploy driver and run on Fargate -- not part of the
# application that installs on a user's machine. It depends on POSIX primitives that
# are not incidental: the supervisor forks and signals a process group, and the
# layout tests assert container filesystem paths. Running it on Windows measures
# nothing about the only platform the image runs on, and it failed there for exactly
# that reason.
#
# Marking individual tests was tried first and is the wrong shape: the POSIX
# dependencies are spread across the suite, so the list was already incomplete and
# the next test to touch a fork/signal path would redden Windows again without
# changing anything real. Skipping the whole tree on a non-POSIX host is the honest
# unit.
#
# The suite is ALSO not collected when the image's own runtime dependencies are not
# importable. ``container/requirements.txt`` (fastapi, uvicorn, httpx, boto3) is
# marked "Container runtime only. These must NOT become dependencies of the Kiro
# Crew app itself" -- the app's backend hooks are imported into every owner's
# gateway process, and a web framework there buys nothing. So the application's own
# CI environment installs ``-e .[voice,desktop] --group dev`` and does NOT carry
# these, yet ``testpaths`` in ``setup.cfg`` walks ``src/kiro_crew/apps/builtins``
# and reaches this tree: four modules ``import httpx`` (the front's backend client)
# at module top level, which is a collection-time ``ModuleNotFoundError`` that
# cascades every backend shard. The image build context and any developer machine
# that installs ``requirements-dev.txt`` DO carry them, so the suite still runs
# where their subject can -- this only declines to run them where the deliberately
# absent deps make the subject unimportable, rather than forcing the app env to
# grow a dependency the runtime contract forbids.
#
# Only the deps imported AT MODULE TOP LEVEL by the test modules gate collection:
# ``httpx`` (four modules), ``fastapi`` and ``uvicorn`` (``test_front_proxy``).
# ``boto3`` is deliberately NOT in this list -- ``front/transcript.py`` imports it
# lazily inside a function ("keep the package importable without AWS"), so it is
# never touched at collection time and a venv without it (the app's own dev env is
# one) collects and runs this suite fine. Listing it here would skip the suite on
# every such env for a dependency that never blocks import.
_IMAGE_COLLECT_TIME_DEPS = ("fastapi", "httpx", "uvicorn")
_missing_image_deps = [
    name for name in _IMAGE_COLLECT_TIME_DEPS if importlib.util.find_spec(name) is None
]

if os.name != "posix":  # pragma: no cover - the excluded platform
    _declined: str | None = f"the host is not POSIX (os.name is {os.name!r})"
elif _missing_image_deps:  # pragma: no cover - the app CI env without image deps
    _declined = "these image runtime dependencies are not importable: " + ", ".join(
        _missing_image_deps
    )
else:
    _declined = None

if _declined is not None:  # pragma: no cover - decided by the host, not by a branch
    if _REQUIRED:
        raise RuntimeError(
            f"{_REQUIRED_ENV} is set, so this environment declared that it must run "
            f"the crew container suite, but it cannot: {_declined}. Refusing to skip. "
            "A skip here would take the whole suite out of collection while the run "
            "still reports success. Install the image's runtime dependencies "
            "(container/requirements.txt) on a POSIX host, or unset "
            f"{_REQUIRED_ENV} if this environment is not meant to run them."
        )
    collect_ignore_glob = ["test_*.py"]


def _modules_that_define_tests() -> set[str]:
    """Names of the ``test_*.py`` files beside this one that define a test function.

    Read from the source with ``ast``, never imported: this runs while deciding
    whether collection was complete, and importing a module to find out would either
    duplicate collection or hide the very import failure being looked for.

    The filter matters because a file matching ``test_*.py`` is not necessarily a
    test module. ``test_supervisor_fakes.py`` is named that way to sit inside one
    track's ownership and deliberately defines no test function, so requiring every
    ``test_*.py`` to yield an item fails on the tree as it stands. Asking the source
    what it defines keeps the check exact and self-maintaining: add a test to that
    helper and it starts being required, with nothing to remember.
    """
    named: set[str] = set()
    for path in _HERE.glob("test_*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unparseable is pytest's error
            named.add(path.name)
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test"
            ):
                named.add(path.name)
                break
    return named


def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Under ``CREW_CONTAINER_TESTS_REQUIRED``, refuse a collection that came up short.

    The import-time gate above proves the suite CAN be collected. This proves it WAS.
    They are different failures, and only the second catches a module that quietly
    stops yielding tests while every dependency is still importable.

    Two checks, and the first is the one that cannot rot: the set of modules that
    must yield tests is read off the filesystem, so it needs no maintenance and
    cannot disagree with the tree. A module that defines tests and contributed no
    collected item is an error whatever the reason. Deleting a test file legitimately
    removes it from both sides and stays silent, which is why this check can be exact
    rather than a floor. The count floor then covers what a presence check cannot
    see.

    Items outside this directory are ignored, so a wider run that happens to include
    this suite is not judged by it.
    """
    if not _REQUIRED:
        return
    mine = [item for item in items if getattr(item, "path", None) is not None]
    mine = [item for item in mine if item.path.parent == _HERE]
    collected = {item.path.name for item in mine}
    uncollected = sorted(_modules_that_define_tests() - collected)
    if uncollected:
        raise pytest.UsageError(
            f"{_REQUIRED_ENV} is set and these modules define tests but contributed "
            f"no collected test: {', '.join(uncollected)}. A module that collects "
            "nothing is reported as neither a pass nor a failure, so this is an error "
            "rather than a silence."
        )
    if len(mine) < _MIN_COLLECTED:
        raise pytest.UsageError(
            f"{_REQUIRED_ENV} is set and the crew container suite collected "
            f"{len(mine)} tests, below its floor of {_MIN_COLLECTED}. Every module "
            "that defines tests yielded at least one, so tests went missing inside "
            "one of them. Find them rather than lowering the floor; lower it only in "
            "the same commit as a deliberate deletion."
        )


# APPEND, never insert(0), and note the directory beside this one is named
# ``container_tests`` rather than ``tests``. Both facts exist for the same reason.
#
# ``crew/runtime/`` deliberately has no ``__init__.py`` (see above), so pytest's
# prepend mode inserts THIS directory at sys.path[0] and names the suite by its own
# folder. Called ``tests``, that made it the TOP-LEVEL package ``tests`` for the
# whole process, and several builtin apps import their own fixtures under exactly
# that name (``from tests.fixtures import ...``) -- so ours won the name and theirs
# failed to import. It surfaced only on Windows, whose shard split happened to put
# both suites in one process, which means the collision was latent on every
# platform and observable on one. Every other app's test package sits inside an
# unbroken ``__init__.py`` chain and is therefore never top-level; this tree is the
# only one that breaks the chain, and it breaks it on purpose, so it is the one
# that has to carry a name nothing else claims.
#
# Appending is then belt and braces for what this is actually for: the container's
# own package is ``container``, which no other suite defines, so it resolves from
# anywhere on the path and needs no precedence.
if str(_BUILD_CONTEXT) not in sys.path:
    sys.path.append(str(_BUILD_CONTEXT))
