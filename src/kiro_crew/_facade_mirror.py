"""Keep a split module's patch seams whole: a write through its facade reaches every reader.

When a large module is split into owner modules and its original path stays as a
facade, a name that callers and tests patch through the facade is read in more than
one place: by the owner that defines it, and by every owner that imported it with
``from <owner> import name``. Each of those holds its own binding, so a patch applied
to one namespace -- the facade's, or the defining owner's -- leaves the other readers
running the original. In the unsplit module there was one namespace, and a patch of
``module.name`` reached every reader. Rewriting each moved read as ``facade.name``
would restore that by changing the moved code; this module restores it without
touching the moved code at all.

:func:`install` runs once, after every owner has been imported. It indexes each name
an owner binds -- name -> every owner whose namespace binds it, its *holders* -- and
:class:`MirroredModule`, installed as the facade's class, sends a write or delete of
an indexed name to every holder, and to the facade too when the facade binds that name
itself. A read of an indexed name the facade does not bind is answered from the first
holder, resolved from :data:`sys.modules` on each use, and ``dir()`` lists those names
beside the facade's own. ``monkeypatch.setattr``, ``mock.patch`` and their undo therefore
act on the split family as they acted on the one module.

Four properties make that hold, and each is deliberate:

* **The index is static.** It is computed once and frozen. ``mock.patch`` restores a
  name the facade does not hold by DELETING it and then writing the original back, so
  an index that asked "which owners hold this name right now?" would find none after
  the delete and restore only the first holder. A frozen index knows every holder for
  the life of the process.
* **One object per indexed name.** A name is indexed only when every holder, and the
  facade if it binds the name, hold the same object at install time -- which is what
  a name means when the code it serves was one namespace. A name bound to different
  objects is a coincidence of spelling, not one name, and forwarding a write to all of
  them would clobber the others; it is left out and listed in
  :attr:`MirrorIndex.excluded`, so a write to it touches the facade alone. The facade
  declares the names it expects excluded, and :func:`install` refuses an index whose
  exclusions differ, so an install that runs while an owner is patched directly (a
  reload of the facade under a patch) fails loudly instead of silently un-mirroring the
  name for the rest of the process. A RE-install -- the facade executed again in the same
  process -- re-syncs a name the family only imports: when the module it comes from was
  imported again, every holder takes the facade's fresh binding. A name the family
  defines (at the top level, or inside a module-level ``if``, ``try``, ``with`` or ``for``)
  is never re-synced, nor is a declared exclusion, so such a divergence still fails the
  install; and the re-sync writes happen only once the install is accepted.
* **A shared name is patched through the facade.** A write through the facade makes
  every holder equal. If one owner was patched directly first, the facade's undo then
  restores every holder to that owner's patched value, and the other holders keep it
  after the test: a leak. So a name more than one member binds is patched through the facade,
  never on one owner, and ``test_no_test_patches_a_shared_name_on_an_owner_directly`` in
  ``test/test_facade_mirror_guards.py`` refuses a direct owner patch of such a name in the
  test trees. A name one owner binds alone may be patched there.
* **Owners are resolved per use, by name.** The index stores dotted NAMES, never module
  objects, and looks each owner up in :data:`sys.modules`, so an owner that is purged and
  imported again is the one that is patched. A write or delete never imports: an owner
  missing from :data:`sys.modules` holds nothing to patch, and a teardown must not run an
  owner's body. (A forwarded name is the exception: it keeps the forwarding facade's
  import of its one owner.) A read imports on a miss, after the store, so a test that stubs
  ``importlib.import_module`` for its own reasons does not reroute every read of an
  indexed name (a forwarded name's read fetches its owner the way the forwarding facade
  did, ``forward_reads`` below). An owner
  still running its body (another thread is importing it) is waited for through
  ``importlib.import_module``, so neither path sees half a module.

A write that CREATES a name -- one neither the facade nor any owner binds, such as a
test shadowing a builtin with ``monkeypatch.setattr(facade, "open", fake,
raising=False)`` -- creates it on the facade and on every owner that does not bind it,
because in the unsplit module that global shadowed the builtin for all of the module's
code, and that code now lives in the owners. The facade records which owners the
creation wrote (under :data:`CREATED_ATTRIBUTE`): a later write of the name reaches
exactly those, and the delete that undoes it removes exactly those bindings, so its
undo leaves no residue and never touches an owner's own binding of the name, whatever
object that is. A name the facade binds at install time and the index does not hold
(code the facade keeps for itself, :attr:`MirrorIndex.facade_only`) is written and
deleted on the facade alone, a write that restores it after a delete included, since no
owner reads it; so is a module's own submodule, which the import system binds onto a
package facade.

A facade that already forwards a table of names to their owners (the
``_ReExportModule`` shape) keeps that table as *forwards*: a name in it is read from,
written to and deleted on its one owner, exactly as before, and listed by ``dir()``.
Legacy facades fetched that owner two ways, and *forward_reads* keeps the one the facade
had: ``"sys_modules_first"`` (the default) reads :data:`sys.modules` and imports only on
a miss, and ``"import_module"`` calls ``importlib.import_module`` on every read. And
a facade that adds owners beside its own code passes its *surface*, the names it bound
before the owners existed: an owner-bound name outside it -- a helper the split
introduced -- is not part of the facade at all. It is never served, listed or
forwarded, and a write of it through the facade follows the creation rule, which leaves
every owner's own binding alone; so a test cannot reach it through the facade by
accident, and an undo cannot delete it. One the facade binds itself (an owner helper the
facade imports for its own code) is facade-only: a write of it through the facade
reaches the facade's own reads and no owner's. A facade may also keep a module-level
``__getattr__`` (:pep:`562`) for names of its own. It answers before the mirror, so
:func:`install` refuses one that answers a name the mirror indexes or forwards.

Two rules bind the family. An owner never imports its facade: the facade imports the
owners, lowest layer first, and the owners import each other in that one direction. And
no member rebinds, at run time, a global that another member also binds (``global name;
name = ...``): such a write lands in one namespace alone, and no index can follow it. A
global only one owner binds may be rebound freely, but the facade's own code never
rebinds a name the mirror serves without the facade binding it: the write would shadow
the owners for every read through the facade.

Three limits are part of the contract:

* ``mock.patch(..., create=True)`` on a name the facade forwards cannot be undone: on
  exit ``mock`` deletes the name from every holder and, because ``create`` is true,
  never writes it back. ``test_no_test_patches_a_mirrored_facade_name_with_create_true``
  in ``test/test_facade_mirror_guards.py`` refuses such a patch anywhere in the test
  trees, for every facade this module serves.
* The same exit of a ``mock.patch`` on a forwarded name deletes it from every holder and
  then writes it back, one holder at a time, where the unsplit module's exit was one
  write. A background thread still reading the name at that moment can see it missing;
  stop such a thread before the patch exits.
* Purge the family together. A patch whose lifetime spans a purge of one of its owners
  leaves the purged copy patched, because the undo reaches the owner
  :data:`sys.modules` holds by then; and a facade that binds an owner's name for its own
  code keeps the purged owner's object until the facade is imported again.

A facade built on this module has this shape. The import loop and the class swap stay
in the facade's own text, because ``test/test_mirrored_owner_storage.py`` finds a module
that forwards attributes by its source, and its purge probe reads ``_EXPORTS``. The facade
binds ``_EXPORTS`` before the install, so the table is one of its own names::

    import importlib as _importlib
    import sys as _sys
    from typing import TYPE_CHECKING

    from kiro_crew import _facade_mirror

    #: Owner modules, lowest layer first: the first holder of a name answers its reads.
    _OWNERS = ("pkg.parts.low", "pkg.parts.high")
    for _owner in _OWNERS:
        _importlib.import_module(_owner)
    #: Name the facade serves but does not bind -> the owner that answers a read of it.
    _EXPORTS: dict[str, str] = {}
    _EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
    _sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
    __all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

    if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
        from pkg.parts.low import a, b  # noqa: F401

``test/test_facade_mirror_guards.py`` holds every facade it finds to that shape: one object
per indexed name, a ``TYPE_CHECKING`` manifest naming each owner-defined name the facade
forwards, no owner importing its facade, and every name the test trees patch through the
facade reaching every owner that binds it.
"""

from __future__ import annotations

import ast
import importlib
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Any, Literal

#: Where :func:`install` leaves the index on the facade, for :class:`MirroredModule` to
#: read. A dunder, so the facade's own name tables and star exports never meet it.
INDEX_ATTRIBUTE = "__facade_mirror__"

#: Where :class:`MirroredModule` records, on the facade, the owners each write that
#: CREATED a name was written to, so the delete that undoes it removes exactly those. An
#: empty entry marks a name of the facade's own that a delete removed.
CREATED_ATTRIBUTE = "__facade_mirror_created__"

_ABSENT = object()

#: Dotted names of the facades installed in this process. A second install of one is a
#: re-execution (a reload, or a purge and re-import), and re-syncs what changed outside it.
_INSTALLED: set[str] = set()


def _initializing(module: ModuleType) -> bool:
    """Whether *module* is still running its body, in this thread or another."""
    return bool(getattr(getattr(module, "__spec__", None), "_initializing", False))


def _loaded(dotted: str) -> ModuleType:
    """Return module *dotted*, read from :data:`sys.modules` and imported only on a miss.

    For reads only. :data:`sys.modules` is the one place a module is stored, so a purged
    and re-imported owner is seen at once, and a loaded module is returned without an
    import call, so a test that stubs ``importlib.import_module`` does not reach a read.
    A module still running its body is asked for through ``importlib.import_module``,
    which waits on the import lock while another thread finishes it.
    """
    module = sys.modules.get(dotted)
    if module is None or _initializing(module):
        return importlib.import_module(dotted)
    return module


def _present(dotted_names: Iterable[str]) -> list[ModuleType]:
    """The named modules that are loaded, in order; a write or delete never imports.

    A module still running its body is waited for through ``importlib.import_module``:
    it is in :data:`sys.modules` already, so that call waits on the import lock while
    another thread finishes it, instead of loading anything.
    """
    found = []
    for dotted in dotted_names:
        module = sys.modules.get(dotted)
        if module is not None and _initializing(module):
            module = importlib.import_module(dotted)
        if module is not None:
            found.append(module)
    return found


def _indexable(name: str, value: object) -> bool:
    """Whether a binding in an owner names something a caller could patch.

    Module dunders (``__name__``, ``__spec__``, ...) describe the module itself, and a
    ``__future__`` feature flag lands in every module that uses it while naming nothing.
    """
    if _is_dunder(name):
        return False
    return type(value).__module__ != "__future__"


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


@dataclass(frozen=True)
class MirrorIndex:
    """Where each name of one facade's family lives, computed once by :func:`install`."""

    #: Dotted name of the facade module.
    facade: str
    #: Owner modules, in the order the facade gave them (lowest layer first).
    owners: tuple[str, ...]
    #: Indexed name -> every owner whose namespace binds it, in owner order.
    holders: Mapping[str, tuple[str, ...]]
    #: Indexed name the facade does not bind -> the owner that answers a read of it.
    exports: Mapping[str, str]
    #: Names two members of the family bind to different objects; never forwarded.
    excluded: tuple[str, ...]
    #: Name -> the one owner it is read from, written to and deleted on (a legacy table).
    forwards: Mapping[str, str] = MappingProxyType({})
    #: How a read fetches a forwarded name's owner: ``"sys_modules_first"`` or
    #: ``"import_module"`` (see :func:`install`).
    forward_reads: str = "sys_modules_first"
    #: Names the facade binds at install that no owner shares: they stay on the facade.
    facade_only: frozenset[str] = frozenset()

    @property
    def served(self) -> dict[str, str]:
        """Every name a read through the facade answers that the facade does not bind."""
        return {**self.exports, **self.forwards}

    def read(self, name: str) -> Any:
        """Return *name* from the owner that answers it, for the facade's ``__getattr__``.

        A name outside :attr:`exports`, or one a patch has deleted from its owner, raises
        the ``AttributeError`` a plain module raises, naming the facade, so ``hasattr``
        and ``getattr(..., default)`` answer as they did before the split. An export's
        owner is read from :data:`sys.modules`; a forwarded name's is fetched the way
        :attr:`forward_reads` says, as the forwarding facade it comes from fetched it.
        """
        forward = self.forwards.get(name)
        owner = forward or self.exports.get(name)
        if owner is not None:
            every_read = forward is not None and self.forward_reads == "import_module"
            module = importlib.import_module(owner) if every_read else _loaded(owner)
            value = getattr(module, name, _ABSENT)
            if value is not _ABSENT:
                return value
        raise AttributeError(f"module {self.facade!r} has no attribute {name!r}")

    def members_binding(self, name: str) -> dict[str, object]:
        """Every loaded member of the family binding *name* -> what it binds.

        The facade counts as a member wherever it binds the name, a served name included:
        a binding there would shadow its owner for every read.
        """
        members = (self.facade, *self.holders.get(name, ()))
        if name in self.forwards:
            members = (self.facade, self.forwards[name])
        bound: dict[str, object] = {}
        for module in _present(members):
            value = vars(module).get(name, _ABSENT)
            if value is not _ABSENT:
                bound[module.__name__] = value
        return bound

    def divergent(self) -> tuple[str, ...]:
        """Indexed and forwarded names the family does not bind to one object now, sorted.

        Empty whenever no patch is active. An indexed name listed here is missing from a
        holder, bound to two objects (a write that bypassed the facade, or an owner
        rebinding its own global), or served yet bound on the facade too, where it shadows
        its owner; a forwarded name is missing from its owner or bound on the facade.
        """
        drifted: list[str] = []
        for name, holders in self.holders.items():
            bound = self.members_binding(name)
            values = list(bound.values())
            missing = any(holder not in bound for holder in holders)
            shadowed = name in self.exports and self.facade in bound
            if missing or shadowed or any(value is not values[0] for value in values[1:]):
                drifted.append(name)
        for name, owner in self.forwards.items():
            bound = self.members_binding(name)
            if self.facade in bound or (owner in sys.modules and owner not in bound):
                drifted.append(name)
        return tuple(sorted(drifted))


def install(
    facade: ModuleType,
    owners: Sequence[str],
    *,
    excluded: Iterable[str] = (),
    forwards: Mapping[str, str] | None = None,
    surface: Iterable[str] | None = None,
    forward_reads: Literal["sys_modules_first", "import_module"] = "sys_modules_first",
) -> MirrorIndex:
    """Index *owners* for *facade* and leave the index where :class:`MirroredModule` reads it.

    Every owner must already be imported: the index is a picture of the family as it
    stands, so an owner imported later would be missing from it for good. *excluded* is
    the set of names the facade expects to be bound to different objects across the
    family; an index whose exclusions differ is refused, because it means an owner is
    patched directly while the index is being taken. *forwards* is a legacy name -> owner
    table whose names keep single-owner forwarding, their owner fetched on a read as
    *forward_reads* says; *surface*, when given, limits the
    index to the names in it (see the module docstring). A module-level ``__getattr__``
    the facade keeps must decline every indexed and forwarded name, or reads and writes
    of that name would reach different places. Call this at the end of the facade, after
    its eager owner imports and before its class swap. Calling it again (a reload of the
    facade) replaces the index. A refused install changes nothing.
    """
    owner_names = tuple(owners)
    if not owner_names:
        raise ValueError(f"{facade.__name__}: a mirrored facade needs at least one owner")
    if len(set(owner_names)) != len(owner_names):
        raise ValueError(f"{facade.__name__}: an owner is listed twice in {owner_names!r}")
    if facade.__name__ in owner_names:
        raise ValueError(f"{facade.__name__} cannot be one of its own owners")
    if forward_reads not in ("sys_modules_first", "import_module"):
        raise ValueError(f"{facade.__name__}: unknown forward_reads {forward_reads!r}")
    unloaded = [name for name in owner_names if name not in sys.modules]
    if unloaded:
        raise RuntimeError(
            f"{facade.__name__}: import every owner before installing the mirror; "
            f"not yet imported: {unloaded}"
        )

    facade_namespace = vars(facade)
    created = facade_namespace.get(CREATED_ATTRIBUTE) or {}
    active = sorted(name for name, made in created.items() if made and name in facade_namespace)
    if active:
        raise RuntimeError(
            f"{facade.__name__}: {active} were created through the facade by a patch that "
            "is still active; undo it before importing the facade again, or the index would "
            "take the patch for a name of the family"
        )
    forwarded = dict(forwards or {})
    for name, owner in forwarded.items():
        if name in facade_namespace:
            raise ValueError(f"{facade.__name__}.{name} is forwarded to {owner} but bound here")
        if owner not in sys.modules or name not in vars(sys.modules[owner]):
            raise RuntimeError(f"{facade.__name__}: {owner} does not hold forwarded {name}")
    within = None if surface is None else frozenset(surface)
    holders_of: dict[str, list[str]] = {}
    objects_of: dict[str, list[object]] = {}
    for owner in owner_names:
        for name, value in vars(sys.modules[owner]).items():
            if not _indexable(name, value):
                continue
            if name in forwarded:
                if owner != forwarded[name]:
                    raise RuntimeError(
                        f"{facade.__name__}.{name} is forwarded to {forwarded[name]}, but owner "
                        f"{owner} binds it too, so a patch of it would miss {owner}; index the "
                        "name instead of forwarding it"
                    )
                continue
            if within is not None and name not in within:
                continue
            holders_of.setdefault(name, []).append(owner)
            objects_of.setdefault(name, []).append(value)

    declared = sorted(set(excluded))
    reinstall = facade.__name__ in _INSTALLED
    defined: set[str] | None = None
    resyncs: list[tuple[str, tuple[str, ...], object]] = []
    holders: dict[str, tuple[str, ...]] = {}
    exports: dict[str, str] = {}
    found_excluded: list[str] = []
    for name, holder_names in holders_of.items():
        objects = objects_of[name]
        if name in facade_namespace:
            objects = [*objects, facade_namespace[name]]
        if any(value is not objects[0] for value in objects[1:]):
            if reinstall and name not in declared and defined is None:
                defined = _family_definitions((facade, *map(sys.modules.get, owner_names)))
            if not reinstall or name in declared or name in (defined or ()):
                found_excluded.append(name)
                continue
            fresh = facade_namespace.get(name, objects[0])
            resyncs.append((name, tuple(holder_names), fresh))
        holders[name] = tuple(holder_names)
        if name not in facade_namespace:
            exports[name] = holder_names[0]

    if sorted(found_excluded) != declared:
        raise RuntimeError(
            f"{facade.__name__}: names bound to different objects across the family are "
            f"{sorted(found_excluded)}, but the facade declares {declared}. An owner patched "
            "directly while the facade is imported reads this way; otherwise update the "
            "declaration and its spec."
        )
    resolver = facade_namespace.get("__getattr__")
    if callable(resolver):
        answered = [
            name for name in sorted({*holders, *forwarded}) if _answers(facade, resolver, name)
        ]
        if answered:
            raise RuntimeError(
                f"{facade.__name__}: its own module-level __getattr__ answers {answered}, "
                "which the mirror also holds, so a read would come from the resolver while a "
                "write reaches the owners; make it decline those names"
            )

    for name, resynced, value in resyncs:
        _resync(name, resynced, value)
    index = MirrorIndex(
        facade=facade.__name__,
        owners=owner_names,
        holders=MappingProxyType(holders),
        exports=MappingProxyType(exports),
        excluded=tuple(declared),
        forwards=MappingProxyType(forwarded),
        forward_reads=forward_reads,
        facade_only=frozenset(
            name
            for name in facade_namespace
            if name not in holders and name not in declared and not _is_dunder(name)
        ),
    )
    facade_namespace[INDEX_ATTRIBUTE] = index
    _INSTALLED.add(facade.__name__)
    return index


def _answers(facade: ModuleType, resolver: Any, name: str) -> bool:
    """Whether a module-level ``__getattr__`` returns a value for *name*.

    A resolver that raises anything but ``AttributeError`` for a name the mirror holds
    would raise it for every ``hasattr`` of that name too, so the install stops on it.
    """
    try:
        resolver(name)
    except AttributeError:
        return False
    except Exception as error:
        raise RuntimeError(
            f"{facade.__name__}: its own module-level __getattr__ raised "
            f"{type(error).__name__} for {name!r}, which the mirror holds; a resolver "
            "declines a name by raising AttributeError"
        ) from error
    return True


def _module_level_statements(tree: ast.Module) -> list[ast.stmt]:
    """Every statement that runs in the module's own namespace.

    The top level, and the bodies of module-level ``if`` / ``try`` / ``with`` / ``for`` /
    ``while`` / ``match`` statements, but never a function's or a class's: a name bound
    there lives in that scope, not in the module.
    """
    collected: list[ast.stmt] = []
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop()
        collected.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            for child in getattr(node, field, None) or []:
                stack.extend([child] if isinstance(child, ast.stmt) else child.body)
    return collected


def _defined_names(tree: ast.Module) -> set[str]:
    """Every name *tree* defines in its module namespace: a ``def``, a ``class``, a
    ``type`` alias, an assignment, ``for`` or ``with`` target (tuples and starred targets
    included), a walrus in a module-level expression, and a ``match`` capture. An
    ``import`` binds a name too, but defines nothing."""
    names: set[str] = set()
    type_alias = getattr(ast, "TypeAlias", None)  # the ``type X = ...`` statement (3.12+)
    for node in _module_level_statements(tree):
        names |= _walrus_and_capture_names(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if type_alias is not None and isinstance(node, type_alias):
            names.add(node.name.id)
            continue
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets = [item.optional_vars for item in node.items if item.optional_vars]
        while targets:
            target = targets.pop()
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                targets.extend(target.elts)
            elif isinstance(target, ast.Starred):
                targets.append(target.value)
    return names


def _walrus_and_capture_names(statement: ast.stmt) -> set[str]:
    """The walrus targets in *statement*'s own expressions and the names its ``match``
    patterns capture, never looking into a nested function, class, lambda or statement:
    each of those is a scope of its own, or another module-level statement."""
    names: set[str] = set()
    stack: list[ast.AST] = list(ast.iter_child_nodes(statement))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.stmt, ast.Lambda)):
            continue
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _family_definitions(modules: Iterable[ModuleType | None]) -> set[str]:
    """Every name a member of the family defines in its module namespace, from its source.

    Read only on a re-install that finds a name bound to different objects: a name the
    family defines diverging is a patch the index must not paper over, while a name it
    merely imports diverges because the module it comes from was imported again.
    """
    names: set[str] = set()
    for module in modules:
        path = getattr(module, "__file__", None)
        if not path:
            continue
        with open(path, encoding="utf-8") as handle:
            names |= _defined_names(ast.parse(handle.read()))
    return names


def _resync(name: str, holder_names: Iterable[str], value: object) -> None:
    """Rebind imported *name* in every holder to *value*, the facade's fresh binding."""
    for holder in holder_names:
        setattr(sys.modules[holder], name, value)


def _index_of(facade: ModuleType) -> MirrorIndex | None:
    index = facade.__dict__.get(INDEX_ATTRIBUTE)
    return index if isinstance(index, MirrorIndex) else None


def _stays_on_facade(facade: ModuleType, index: MirrorIndex, name: str, value: object) -> bool:
    """Whether a write of unindexed *name* stays on the facade alone.

    A dunder describes the facade module itself; an excluded name means different things
    in different owners; a name in :attr:`MirrorIndex.facade_only` is the facade's own;
    and the import system binds a package's own submodule onto it, which is no owner's
    business.
    """
    if _is_dunder(name) or name in index.excluded or name in index.facade_only:
        return True
    return isinstance(value, ModuleType) and value.__name__ == f"{facade.__name__}.{name}"


def _created(facade: ModuleType) -> dict[str, tuple[str, ...]]:
    """The facade's record: created name -> the owners its creation wrote."""
    record: dict[str, tuple[str, ...]] = facade.__dict__.setdefault(CREATED_ATTRIBUTE, {})
    return record


def _creation_owners(
    facade: ModuleType, index: MirrorIndex, name: str, value: object
) -> tuple[str, ...]:
    """The owners a write of unindexed *name* reaches besides the facade.

    A name no member binds is being created: it lands on every owner that does not bind
    it, and the facade records which owners those were. A later write of it reaches the
    recorded owners, and nothing is inferred from which objects happen to be equal. A name
    the facade's own code binds after install is no creation: it stays here, and a delete
    of it records an empty owner list, so writing it back stays here as well.
    """
    if _stays_on_facade(facade, index, name, value):
        return ()
    record = _created(facade)
    if name in record:
        return record[name]
    if name in facade.__dict__:
        return ()  # bound here by the facade's own code after install: not a creation
    record[name] = tuple(m.__name__ for m in _present(index.owners) if name not in vars(m))
    return record[name]


class MirroredModule(ModuleType):
    """A facade's class: a write or delete through the facade reaches every owner reading it.

    An indexed name is written to every holder (and to the facade when it binds the name).
    A name the index does not hold follows the creation rule in the module docstring. A
    module whose class this is but that carries no index behaves as a plain module. Only
    dunders are defined here, because every attribute of the class is readable as an
    attribute of every facade.
    """

    def __getattr__(self, name: str) -> Any:
        """Read a name the facade does not bind from the owner that answers it.

        Python reaches this only after the module's own lookup failed, and that lookup
        includes a module-level ``__getattr__`` (:pep:`562`) the facade may keep, so a
        facade's own resolver answers first; :func:`install` holds it to declining every
        name the mirror holds.
        """
        index = _index_of(self)
        if index is None:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return index.read(name)

    def __dir__(self) -> list[str]:
        """The facade's own listing (its module-level ``__dir__`` when it keeps one)
        plus every name the mirror serves."""
        own = self.__dict__.get("__dir__")
        listed = set(own()) if callable(own) else set(self.__dict__)
        index = _index_of(self)
        served = set(index.served) if index is not None else set()
        return sorted(listed | served)

    def __setattr__(self, name: str, value: Any) -> None:
        index = _index_of(self)
        if index is None:
            super().__setattr__(name, value)
            return
        if name in index.forwards:
            setattr(_loaded(index.forwards[name]), name, value)
            return
        holders = index.holders.get(name)
        if holders is not None:
            if name not in index.exports:
                super().__setattr__(name, value)
            for module in _present(holders):
                setattr(module, name, value)
            return
        owners = _creation_owners(self, index, name, value)
        super().__setattr__(name, value)
        for module in _present(owners):
            setattr(module, name, value)

    def __delattr__(self, name: str) -> None:
        index = _index_of(self)
        if index is None:
            super().__delattr__(name)
            return
        if name in index.forwards:
            delattr(_loaded(index.forwards[name]), name)
            return
        holders = index.holders.get(name)
        if holders is not None:
            bound_here = name not in index.exports and name in self.__dict__
            held = [module for module in _present(holders) if name in vars(module)]
            if not bound_here and not held:
                raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
            if bound_here:
                super().__delattr__(name)
            for module in held:
                delattr(module, name)
            return
        if name not in self.__dict__:
            super().__delattr__(name)  # raises the plain module's AttributeError
        record = _created(self)
        owners = record.get(name, ())
        if owners:
            del record[name]  # the creation is undone; a later write creates afresh
        elif not _stays_on_facade(self, index, name, self.__dict__[name]):
            record[name] = ()  # the facade's own name: writing it back stays here too
        super().__delattr__(name)
        for module in _present(owners):
            if name in vars(module):
                delattr(module, name)
