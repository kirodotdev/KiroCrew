"""Values derived from the config, reused while the config files are unchanged.

``KiroCrewConfig.load()`` serves the parsed document from a stat-keyed cache, but
still rebuilds every section dataclass from it, with the GIL held, on each call.
A hot path that needs one or two fields (several times a turn, some of them on the
event loop) pays that whole rebuild every time. :func:`memoized` answers such a
read from the last one while the inputs it depends on are unchanged:

* the config files' stat fingerprint (``_config_fingerprint``: each path, device,
  inode, change time, modification time, size and mode, so an edit, a replace or a
  different data home misses);
* the config cache's invalidation generation (a ``save()`` or an explicit clear).

While ``KiroCrewConfig.load`` is not the loader the class was defined with (a test
double, which may hand back one shared object it then mutates), nothing is
remembered and every read calls it.

Only values derived from the config files may go through here: a value that also
reads the environment or the clock would be served stale. A derive that raises
is not remembered, so a caller's fail-closed fallback runs again on the next ask.
Neither is a value derived from a load that could not read a present file whole
(no content digest): that load fell back to defaults for a transient reason the
stat fingerprint does not see, and the loader's own cache declines it too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, TypeVar

if TYPE_CHECKING:
    from kiro_crew.config.loader_config import KiroCrewConfig

_T = TypeVar("_T")

#: ``name -> (signature, value)``. One entry per call site; every value is derived
#: from the process-wide config, never from a caller's own data.
_memo: dict[str, tuple[tuple, object]] = {}


def config_signature() -> tuple:
    """The inputs a config-derived value depends on, read without building the config."""
    from kiro_crew.config.loader_document import _config_fingerprint
    from kiro_crew.config.validation import _CONFIG_CACHE

    return (_config_fingerprint(), _CONFIG_CACHE.generation())


def _loader_is_original(load: Callable[[], object]) -> bool:
    """Whether *load* is ``KiroCrewConfig.load`` as the class defines it, still installed.

    A caller hands in the loader its own module binds, so a test that replaces that
    binding (or the class attribute) gets a loader this check refuses.
    """
    from kiro_crew.config.loader_config import KiroCrewConfig

    bound = KiroCrewConfig.__dict__.get("load")
    func = getattr(bound, "__func__", None)
    return (
        isinstance(bound, classmethod)
        and getattr(func, "__module__", None) == "kiro_crew.config.loader_config"
        and getattr(func, "__qualname__", None) == "KiroCrewConfig.load"
        and getattr(load, "__func__", None) is func
        and getattr(load, "__self__", None) is KiroCrewConfig
    )


def memoized(
    name: str, load: Callable[[], "KiroCrewConfig"], derive: Callable[["KiroCrewConfig"], _T]
) -> _T:
    """``derive(load())``, or its last result for *name* while :func:`config_signature`
    holds. *load* is the caller's own ``KiroCrewConfig.load``; any other loader is
    called every time and nothing is remembered.

    Exceptions from the signature, the load or *derive* propagate and leave no entry.
    """
    if not _loader_is_original(load):
        return derive(load())
    signature = config_signature()
    entry = _memo.get(name)
    if entry is not None and entry[0] == signature:
        return entry[1]  # type: ignore[return-value]
    config = load()
    value = derive(config)
    if getattr(config, "_content_digest", None) is not None:
        _memo[name] = (signature, value)
    return value


def forget(name: str | None = None) -> None:
    """Drop one entry, or every entry (for tests)."""
    if name is None:
        _memo.clear()
    else:
        _memo.pop(name, None)
