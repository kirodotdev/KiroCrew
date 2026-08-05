"""Config validation + the validated-data cache for KiroCrew.

Extracted from ``config/loader.py`` so the schema-driven validation logic and
the process-global validated-config cache live apart from the config DTO
definitions and the loader's read/merge orchestration.

What lives here:
- Schema-introspection helpers (``_is_sensitive_path``, ``_mask_value``,
  ``_apply_field_default``, …) used while validating.
- ``validate_config_data()`` — runs jsonschema validation, logs human-readable
  warnings, and removes invalid values in-place so the loader falls back to
  field defaults. Never raises.
- ``ConfigCache`` — a small wrapper around the process-global validated-data
  cache, with an explicit ``clear()`` (replacing the bare module globals that
  were impossible to reset/inject in tests).

What deliberately stays in ``config/loader.py``:
- ``_config_fingerprint()`` — it reads ``config_path()`` / ``config_local_path()``,
  which the test suite patches via ``kiro_crew.config.loader.config_path``; keeping
  it there preserves that monkeypatch seam. The loader computes the fingerprint
  and passes it in, so this module never resolves config paths itself.

Logging note: config-validation warnings ("unrecognized top-level keys",
"deprecated field", "enum violation", …) are emitted on the
``kiro_crew.config.loader`` logger — not this module's ``__name__`` logger — to
preserve the loader's observable warning channel (asserted by the test suite).
All names here are re-exported from ``config/loader.py`` for backward
compatibility.
"""

from __future__ import annotations

import copy
import logging
import threading

try:
    import jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAS_JSONSCHEMA = False

# Config-validation warnings are part of the loader's observable contract, so
# they are emitted on the loader's logger channel (see module docstring).
logger = logging.getLogger("kiro_crew.config.loader")


# ---------------------------------------------------------------------------
# Schema-introspection helpers
# ---------------------------------------------------------------------------


def _lookup_schema_node(schema: dict, dot_path: str) -> dict | None:
    """Walk the JSON Schema tree to find the node for a dot-separated path."""
    parts = dot_path.split(".")
    node = schema
    for part in parts:
        props = node.get("properties", {})
        if part in props:
            node = props[part]
        else:
            return None
    return node


def _is_sensitive_path(schema: dict, dot_path: str) -> bool:
    """Return True if the field at *dot_path* is marked sensitive."""
    node = _lookup_schema_node(schema, dot_path)
    if node is None:
        return False
    return node.get("x-meta", {}).get("sensitive", False)


def _is_deprecated_path(schema: dict, dot_path: str) -> bool:
    """Return True if the field at *dot_path* is marked deprecated."""
    node = _lookup_schema_node(schema, dot_path)
    if node is None:
        return False
    return node.get("x-meta", {}).get("deprecated", False)


def _get_help_text(schema: dict, dot_path: str) -> str:
    """Return the help text for the field at *dot_path*."""
    node = _lookup_schema_node(schema, dot_path)
    if node is None:
        return ""
    return node.get("x-meta", {}).get("help", "")


def _mask_value(value: object, sensitive: bool) -> str:
    """Return a display string for a value, masking if sensitive."""
    if sensitive:
        return '"***"'
    return repr(value)


def _dot_path_from_json_path(path: list) -> str:
    """Convert a jsonschema error path (deque of keys) to a dot-separated string."""
    return ".".join(str(p) for p in path)


def _actual_type_name(value: object) -> str:
    """Return a human-readable type name for a JSON value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _apply_field_default(data: dict, dot_path: str) -> None:
    """Remove the invalid value at *dot_path* so the loader falls back to defaults.

    Only handles top-level and one-level nested paths (e.g. ``agent.provider``).
    """
    parts = dot_path.split(".")
    if len(parts) == 1:
        data.pop(parts[0], None)
    elif len(parts) == 2:
        section = data.get(parts[0])
        if isinstance(section, dict):
            section.pop(parts[1], None)


def _coerce_legacy_numeric_values(data: dict, schema: dict) -> None:
    """Normalize legacy numeric strings before JSON Schema validation.

    Older config writers persisted some numeric fields as strings. The loader
    still accepts those values, so validation must not discard them before the
    field-level compatibility parsers can run. Malformed and non-finite values
    remain unchanged and are handled by the normal validation/loader fallback.
    """
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return
    for key, node in properties.items():
        if key not in data or not isinstance(node, dict):
            continue
        value = data[key]
        raw_type = node.get("type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if "integer" in types:
            if isinstance(value, str):
                try:
                    data[key] = int(value)
                except (TypeError, ValueError, OverflowError):
                    pass
            elif isinstance(value, float) and value.is_integer():
                data[key] = int(value)
        elif "number" in types and isinstance(value, str):
            try:
                numeric_value = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if numeric_value == numeric_value and numeric_value not in (
                float("inf"),
                float("-inf"),
            ):
                data[key] = numeric_value
        elif isinstance(value, dict):
            _coerce_legacy_numeric_values(value, node)


# ---------------------------------------------------------------------------
# Validated-data cache
#
# KiroCrewConfig.load() is called on per-message / per-request hot paths (skill
# triggering, context build, dashboard handlers). The expensive part is NOT the
# dataclass construction — it is reading config.json (+ config.local.json),
# json.loads, _deep_merge, and the full jsonschema.validate against the whole
# config schema. We cache ONLY that validated, merged dict, keyed on a cheap
# fingerprint (path + st_mtime_ns + st_size + st_mode) of both files, computed by
# the loader. On a cache hit load() still builds fresh dataclasses from a deep
# copy, so the 100+ callers that mutate the returned config in place (settings
# handlers, the write-back migration) never corrupt the shared cache.
# mtime-keying (not a blind TTL) means a runtime edit — e.g. via the dashboard
# settings handler that calls save() — is reflected on the very next load();
# save() also invalidates eagerly.
# ---------------------------------------------------------------------------


class ConfigCache:
    """Thread-safe cache of one validated, merged config dict keyed on a fingerprint.

    Replaces the bare ``_CONFIG_CACHE`` module global so the cache can be reset
    deterministically in tests via :meth:`clear`. The loader owns fingerprint
    computation and passes it in; this object never stats the config files.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (fingerprint, deep-copyable validated data dict)
        self._entry: tuple[tuple, dict] | None = None

    def get(self, fingerprint: tuple) -> dict | None:
        """Return a deep copy of the cached dict if *fingerprint* matches, else None.

        The deep copy is mandatory: load() and its callers mutate the returned
        structure (the write-back migration adds a default agent; settings
        handlers assign nested fields), so the cached original must never be
        handed out.
        """
        with self._lock:
            if self._entry is not None and self._entry[0] == fingerprint:
                return copy.deepcopy(self._entry[1])
        return None

    def store(self, data: dict, fingerprint: tuple) -> None:
        """Cache a deep copy of *data* under *fingerprint*.

        *fingerprint* MUST be the one captured BEFORE the files were read (by
        ``load()``), not a fresh stat. If a write lands between the read and this
        store, *fingerprint* describes the pre-write file, so it won't match the
        post-write on-disk stat — the next ``load()`` misses and re-reads rather
        than serving the stale content we just read. Re-statting here instead
        would cache old content under the new file's fingerprint (a read->store
        TOCTOU) and serve it as a false hit until the file changed again.
        """
        with self._lock:
            self._entry = (fingerprint, copy.deepcopy(data))

    def clear(self) -> None:
        """Drop the cached validated config (called after save()/write-back)."""
        with self._lock:
            self._entry = None


# Process-global cache instance.
_CONFIG_CACHE = ConfigCache()
# Back-compat alias only: the cache lock used to be a module-level global of this
# name. Exposed so any lingering `kiro_crew.config.loader._CONFIG_CACHE_LOCK`
# reference keeps resolving. Do NOT acquire this externally — all locking is
# internal to ConfigCache; this alias can be dropped once nothing references the
# old module-level name.
_CONFIG_CACHE_LOCK = _CONFIG_CACHE._lock


def validate_config_data(data: dict) -> dict:
    """Validate *data* against the config JSON Schema.

    Logs warnings for any issues found and mutates *data* in-place to
    remove invalid values (so the loader falls back to field defaults).
    Always returns *data* — never raises.
    """
    if not _HAS_JSONSCHEMA:
        return data

    # circular import: schema.py imports KiroCrewConfig from config.loader, which
    # re-exports this module — importing schema at module level here would close
    # a config.loader -> validation -> schema -> loader cycle at import time.
    from kiro_crew.config.loader import CONFIG_RESERVED_TOP_KEYS
    from kiro_crew.config.schema import JSON_SCHEMA, SCHEMA_REGISTRY

    # 1. Detect unrecognized top-level keys. The schema registry models only the
    # config's *sections*, so the keys save() stamps itself are not in it and
    # must be excluded — otherwise every load of a config KiroCrew has ever
    # saved warns about KiroCrew's own bookkeeping.
    known_top_keys = {e.path for e in SCHEMA_REGISTRY if "." not in e.path and e.path != "*"}
    unknown = sorted(set(data.keys()) - known_top_keys - CONFIG_RESERVED_TOP_KEYS)
    if unknown:
        logger.warning("Config: unrecognized top-level keys: %s", ", ".join(unknown))

    # 2. Detect deprecated fields and log warnings
    for entry in SCHEMA_REGISTRY:
        if not entry.deprecated:
            continue
        parts = entry.path.split(".")
        # Check if the deprecated key is present in data
        node = data
        found = True
        for p in parts:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                found = False
                break
        if found:
            logger.warning(
                "Config: deprecated field '%s': %s",
                entry.path,
                entry.help,
            )

    # 3. Normalize case-insensitive enum fields before validation
    agent = data.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("log_level"), str):
        agent["log_level"] = agent["log_level"].upper()

    # 4. Preserve numeric values written by older config writers.
    _coerce_legacy_numeric_values(data, JSON_SCHEMA)

    # 5. Run jsonschema validation
    try:
        jsonschema.validate(data, JSON_SCHEMA)
    except jsonschema.ValidationError:
        # Collect all errors (including nested ones)
        validator_cls = jsonschema.validators.validator_for(JSON_SCHEMA)
        validator = validator_cls(JSON_SCHEMA)
        for err in validator.iter_errors(data):
            dot_path = _dot_path_from_json_path(err.absolute_path)
            if not dot_path:
                # Root-level schema error — skip
                continue

            sensitive = _is_sensitive_path(JSON_SCHEMA, dot_path)
            value = err.instance
            display_val = _mask_value(value, sensitive)

            # Determine error type
            if err.validator == "enum":
                allowed = err.schema.get("enum", [])
                logger.warning(
                    "Config: enum violation at '%s': " "allowed values %s, got %s; using default",
                    dot_path,
                    allowed,
                    display_val,
                )
                _apply_field_default(data, dot_path)
            elif err.validator == "type":
                expected = err.schema.get("type", "unknown")
                actual = _actual_type_name(value)
                logger.warning(
                    "Config: type mismatch at '%s': "
                    "expected %s, got %s (value: %s); using default",
                    dot_path,
                    expected,
                    actual,
                    display_val,
                )
                _apply_field_default(data, dot_path)
            else:
                # Generic validation error
                logger.warning(
                    "Config: validation error at '%s': %s; using default",
                    dot_path,
                    err.message,
                )
                _apply_field_default(data, dot_path)

    return data
