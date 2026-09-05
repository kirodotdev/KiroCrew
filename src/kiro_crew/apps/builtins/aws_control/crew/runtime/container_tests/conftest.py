"""Reach the container package the way the image does: by its build context.

The subject of this suite is imported as top-level ``container.*``, because that
is what it is inside the image -- ``/app`` is on ``sys.path`` and the package
sits at ``/app/container``. Eight modules import ``container.common``
absolutely, and the supervisor hands a
``from container.backup.sidecar import run_sidecar`` string to a child
interpreter, so that name is part of the image's contract rather than an
artifact of where the source used to live. Rewriting the imports to this
repository's package path would break the image at runtime, silently in the
supervisor's case.

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

import os
import sys
from pathlib import Path

_BUILD_CONTEXT = Path(__file__).resolve().parents[1]

# Not collected on a non-POSIX host. This suite's SUBJECT is the source of a Linux
# container image, built by the deploy driver and run on Fargate -- not part of the
# application that installs on a user's machine. It depends on POSIX primitives in at
# least three places that are not incidental: the sidecar takes an ``flock``, the
# supervisor forks and signals a process group, and the layout tests assert
# container filesystem paths. Running it on Windows measures nothing about the only
# platform the image runs on, and it failed 28 tests there for exactly that reason.
#
# Marking individual tests was tried first and is the wrong shape: three separate
# POSIX dependencies meant the list was already incomplete, and the next test to
# touch the backup path would redden Windows again without changing anything real.
# ``.github/workflows/cross-platform.yml`` already excludes this same tree from the
# portability audit, with the same reasoning spelled out there; this keeps the two
# consistent instead of having one gate believe the tree is cross-platform while the
# other knows it is not.
if os.name != "posix":  # pragma: no cover - the excluded platform
    collect_ignore_glob = ["test_*.py"]

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
