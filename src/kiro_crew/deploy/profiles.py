"""deploy-web profile control plane — multi-profile registry + AWS config writes.

The registry (``profiles.json`` in the app data dir) is the app's own view of
which AWS profiles the user deploys with: name, region, last-verified account,
and which one is the default. Profile *names* are the only credential-adjacent
data stored — never keys, never tokens.

Security boundary (mirrors ``kiro_crew.metrics.profile_install``, the
precedent for explicit named-profile installs):

* Reads: profile *names* come from ``aws configure list-profiles`` (CLI output,
  names only). We never open ``~/.aws/config`` or ``~/.aws/credentials``.
* Writes: only via ``aws configure set <key> <value> --profile <name>`` with an
  allowlisted key set (``region``, ``credential_process``) — the AWS CLI's own
  writer, which routes these keys to the *config* file. Keys that would land in
  the credentials file (``aws_access_key_id`` etc.) are not in the allowlist and
  can never be written. All values are schema-validated before reaching argv.
* Deletes: registry-only. We never remove blocks from ``~/.aws/config``.

TODO(sanctioned-writer): once the ``is_sanctioned_credential_writer`` registry
merges, register this module there too.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import AWS_PROFILE_FIRST_CHARS, AWS_PROFILE_NAME_RE
from kiro_crew.deploy import engine
from kiro_crew.platform_compat import file_lock, open_lock_file
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import FieldSpec, ValidationError, validate_field

logger = logging.getLogger(__name__)

# Legacy paths (read-only migration sources) — derived from config_dir() so
# pods/tests with KIROCREW_HOME override isolate correctly.


def _legacy_app_dir() -> Path:
    return config_dir() / "apps" / "deploy-web"


def _legacy_data_dir() -> Path:
    return _legacy_app_dir() / "data"


# Canonical paths (via config_dir() so pods/tests isolate correctly)
def _data_dir() -> Path:
    return config_dir() / "deploy"


def _registry_path() -> Path:
    return _data_dir() / "profiles.json"


def _legacy_registry_path() -> Path:
    return _legacy_data_dir() / "profiles.json"


def _legacy_config_path() -> Path:
    return _legacy_data_dir() / "config.json"


DEFAULT_REGION = engine.DEFAULT_REGION

_MAX_PROFILES = 50
_NOTE_MAX = 256

# Same shapes handlers.py enforces for the legacy single-profile config — these
# values flow into subprocess argv (--profile/--region) on every aws call.
# The profile shape ('+' admitted for IAM Identity Center derived names;
# first char excludes '-' so a name is never option-shaped; \Z rejects trailing
# newlines) is constants.AWS_PROFILE_NAME_RE — the single source of truth,
# aliased rather than re-spelled here. Also used by
# discover_aws_profiles() to filter `aws configure list-profiles` lines
# (stripped before matching, so the anchor is behavior-neutral there).
_PROFILE_RE = AWS_PROFILE_NAME_RE
PROFILE_SPEC = FieldSpec(name="profile", type=str, max_len=128, pattern=_PROFILE_RE)
# What a STORED entry's name may look like: the union of every writer's own
# charset. The deploy routes write PROFILE_SPEC names; the AWS control app's
# register route admits ``@`` and ``=`` as well (``aws_consent._PROFILE_RE``,
# the legacy continuation set the AWS CLI itself accepts). Both are inert in
# the remediation command once ``shlex.join`` quotes them. Checking a stored
# name against the narrower request spec would drop a name a shipped writer
# registered, which is a data-loss shape, not a refusal. Composed from
# AWS_PROFILE_FIRST_CHARS per the contract in constants.py.
_STORED_PROFILE_RE = re.compile(rf"^[{AWS_PROFILE_FIRST_CHARS}][{AWS_PROFILE_FIRST_CHARS}@=-]{{0,127}}\Z")
STORED_PROFILE_SPEC = FieldSpec(name="profile", type=str, max_len=128, pattern=_STORED_PROFILE_RE)
# Multi-segment to admit GovCloud (us-gov-west-1) alongside standard regions;
# max_len=32 keeps the backtracking surface negligible.
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")
REGION_SPEC = FieldSpec(name="region", type=str, max_len=32, pattern=_REGION_RE)
# Inputs to the credential_process line (iam-identity-style profiles). Both are
# LLM/user-influenceable and flow into `aws configure set` argv.
_ACCOUNT_RE = re.compile(r"^\d{12}$")
ACCOUNT_SPEC = FieldSpec(name="account", type=str, max_len=12, pattern=_ACCOUNT_RE)
_ROLE_RE = re.compile(r"^[A-Za-z0-9+=,.@_-]+$")
ROLE_SPEC = FieldSpec(name="role", type=str, max_len=64, pattern=_ROLE_RE)

#: The only ``aws configure set`` keys this module may write. Both route to the
#: shared *config* file; credential-material keys are deliberately absent.
_WRITABLE_KEYS = ("region", "credential_process")


# --- registry ---------------------------------------------------------------

@contextlib.contextmanager
def locked_registry() -> Generator[dict[str, Any], None, None]:
    """Atomic read-modify-write on the profile registry under LOCK_EX.

    Usage::

        with locked_registry() as reg:
            reg["profiles"].append(new_entry)
            # save happens automatically on clean exit

    The lock is held for the entire context — concurrent callers block until
    release. This prevents interleaved load/mutate/save from losing writes.
    """
    _data_dir().mkdir(parents=True, exist_ok=True)
    lock_path = _registry_path().with_suffix(".lock")
    # required=True: a lost profile write is silent data loss, so refuse to
    # proceed without cross-process exclusion. flock_compat is a Windows no-op,
    # so this uses platform_compat's real msvcrt lock.
    # open_lock_file opens writable and never truncates; its docstring carries
    # the Windows rationale.
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True):
            reg = load_registry()
            yield reg
            # Persist on clean exit (exceptions skip save, lock still releases)
            tmp_fd = tempfile.NamedTemporaryFile(
                mode="w", dir=str(_data_dir()), suffix=".json.tmp",
                delete=False, encoding="utf-8",
            )
            try:
                tmp_fd.write(json.dumps(reg, indent=2))
                tmp_fd.flush()
                os.fsync(tmp_fd.fileno())
                tmp_fd.close()
                replace_with_retry(tmp_fd.name, _registry_path())
            except BaseException:
                tmp_fd.close()
                try:
                    os.unlink(tmp_fd.name)
                except OSError:
                    pass
                raise


def make_entry(name: str, region: str, *, account: str = "", verified_at: str = "",
               note: str = "") -> dict[str, str]:
    return {"name": name, "region": region or DEFAULT_REGION,
            "account": account, "verified_at": verified_at, "note": note[:_NOTE_MAX]}


def _well_formed_entry(raw: Any) -> bool:
    """True when a stored registry entry's ``name`` and ``region`` pass the
    field specs its writers enforce (``STORED_PROFILE_SPEC`` / ``REGION_SPEC``).

    The registry file is agent-writable, and a stored entry's name and region
    flow into ``aws`` argv and into the copyable remediation command exactly
    like a request's ``profile`` does -- but ``_resolve_profile`` validates only
    the request parameter, and a request that names no profile is served the
    registry default unvalidated. Checking here on load is what makes "a deploy
    only ever runs with a registered profile" also mean a well-formed one. An
    empty region is admitted: ``make_entry`` fills in the default.

    The stored value itself must pass, not a cleaned copy of it:
    ``validate_field`` sanitizes a string (control characters, NUL, padding)
    before matching the pattern and returns the cleaned form, while the entry
    keeps the raw one. A raw ``"p\\x00"`` that validates as ``"p"`` would still
    be the string handed to ``aws`` argv, where an embedded NUL raises out of
    ``subprocess`` as a 500. So an entry is well formed only when validation
    returns it unchanged.
    """
    if not isinstance(raw, dict):
        return False
    name = raw.get("name")
    region = raw.get("region", "")
    if not isinstance(name, str) or not name or not isinstance(region, str):
        logger.warning("deploy profile registry: dropping malformed entry %r", str(name)[:40])
        return False
    try:
        clean_name = validate_field(name, STORED_PROFILE_SPEC)
        clean_region = validate_field(region, REGION_SPEC) if region else ""
    except ValidationError:
        clean_name, clean_region = "", None
    if clean_name != name or clean_region != region:
        logger.warning("deploy profile registry: dropping malformed entry %r", name[:40])
        return False
    return True


def _parse_registry(raw: Any) -> dict[str, Any]:
    """Build the v2 registry from a parsed ``profiles.json``-shaped document.

    Entries that fail :func:`_well_formed_entry` are left out. The default is
    then settled by WHY it is missing: a default that names no entry at all (a
    stale pointer) falls back to the first profile, as it always has; a default
    whose entry was present but dropped as malformed becomes ``""``. Falling
    back there would silently point a deploy that names no profile at a
    different account and region, with no error -- a 400 "not configured" is
    the honest answer, and the warning log names the dropped entry.

    This is a LOAD-time guarantee only: this function never substitutes a
    profile. A later registry write runs under its own rules -- the AWS
    control app's unregister route, for one, fills an empty default with the
    first kept profile -- so the cleared default can be re-pointed by an
    explicit profile edit afterwards.
    """
    entries = [p for p in raw.get("profiles", []) if isinstance(p, dict)]
    profiles = [
        make_entry(str(p.get("name", "")), str(p.get("region", "")),
                   account=str(p.get("account", "")),
                   verified_at=str(p.get("verified_at", "")),
                   note=str(p.get("note", "")))
        for p in entries if _well_formed_entry(p)
    ][:_MAX_PROFILES]
    default = str(raw.get("default", ""))
    if default and not any(p["name"] == default for p in profiles):
        # str(): the stored name may be a bare number (``123456789012``), which
        # the loader has always normalized through ``str`` and which fails
        # _well_formed_entry as a non-string; comparing the raw value would miss
        # it and fall back to another profile, i.e. another account.
        default_was_dropped = any(str(p.get("name", "")) == default for p in entries)
        default = "" if default_was_dropped else (profiles[0]["name"] if profiles else "")
    return {"version": 2, "profiles": profiles, "default": default}


def load_registry() -> dict[str, Any]:
    """Load the profile registry, migrating the legacy v1 single-profile config.

    v1 shape (``config.json``): ``{"profile": "x", "region": "r"}``.
    v2 shape (``profiles.json``): ``{"version": 2, "profiles": [...], "default": "x"}``.
    Migration is read-through: a v1 config becomes a one-entry registry; the
    legacy file is left in place (GET /config keeps serving the default entry).

    Valid JSON of the WRONG SHAPE degrades exactly like unparseable JSON. This
    file is agent-writable, so ``[]``, ``"x"`` or ``{"profiles": 5}`` are all
    reachable contents, and each of them would otherwise escape as an ``AttributeError``
    or ``TypeError`` from ``raw.get`` / the comprehension -- past the caller and
    out as an HTTP 500 on every route that reads the registry. Catching them
    here routes a mis-shaped file into the fallback chain the function already
    implements (try the legacy registry, then the v1 config, then an empty
    registry) rather than inventing a second recovery, and it fixes every reader
    at once instead of asking each call site to guard a shape it did not parse.
    """
    try:
        return _parse_registry(json.loads(_registry_path().read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    # Legacy app-dir registry migration (apps/deploy-web/data/profiles.json)
    try:
        return _parse_registry(json.loads(_legacy_registry_path().read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    # v1 single-profile migration path (apps/deploy-web/data/config.json)
    try:
        legacy = json.loads(_legacy_config_path().read_text(encoding="utf-8"))
        if _well_formed_entry({"name": legacy.get("profile", ""),
                               "region": legacy.get("region", "")}):
            name = str(legacy["profile"])
            return {"version": 2, "default": name,
                    "profiles": [make_entry(name, str(legacy.get("region", "")))]}
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return {"version": 2, "profiles": [], "default": ""}


def save_registry(reg: dict[str, Any]) -> dict[str, Any]:
    _data_dir().mkdir(parents=True, exist_ok=True)
    lock_path = _registry_path().with_suffix(".lock")
    # open_lock_file: writable but non-truncating; see locked_registry above.
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True):
            tmp_fd = tempfile.NamedTemporaryFile(
                mode="w", dir=str(_data_dir()), suffix=".json.tmp",
                delete=False, encoding="utf-8",
            )
            try:
                tmp_fd.write(json.dumps(reg, indent=2))
                tmp_fd.flush()
                os.fsync(tmp_fd.fileno())
                tmp_fd.close()
                replace_with_retry(tmp_fd.name, _registry_path())
            except BaseException:
                tmp_fd.close()
                try:
                    os.unlink(tmp_fd.name)
                except OSError:
                    pass
                raise
    return reg


def get_entry(reg: dict[str, Any], name: str) -> dict[str, str] | None:
    for p in reg["profiles"]:
        if p["name"] == name:
            return p
    return None


def resolve_profile(requested: str = "") -> tuple[str, str] | None:
    """Resolve a deploy-time profile choice to ``(name, region)``.

    ``requested`` must already be schema-validated by the caller. An empty
    request resolves to the registry default. Unknown names resolve to ``None``
    (callers 400) — a deploy may only ever run with a *registered* profile.
    """
    reg = load_registry()
    name = requested or reg["default"]
    if not name:
        return None
    entry = get_entry(reg, name)
    if entry is None:
        return None
    return entry["name"], entry["region"] or DEFAULT_REGION


# --- discovery (read-only, names only) --------------------------------------

def discover_aws_profiles() -> list[str] | None:
    """Profile names known to the AWS CLI (``aws configure list-profiles``).

    Names only — the CLI enumerates sections itself; we never read the files.

    ``None`` means could not ask, and it is deliberately not ``[]``. "Asked, and
    there are none" is a fact about the machine, "could not ask" is a fact about
    this process, and a caller handed the same empty list for both reports the
    second as the first. That is not a corner case: ``configure list-profiles``
    arrived in AWS CLI v2 and exits non-zero on v1, so an ordinary host with
    profiles configured takes the failing branch, and a route that compares a
    requested name against an empty set then answers that the operator's own
    profiles are not on this machine. ``cli_doctor._aws_profile_names`` draws the
    same line for the doctor report, for the same reason.

    Windows is the other ``None``: the sandboxed subprocess backend deploy needs
    is POSIX-only (fail-loud policy), so discovery cannot run there rather than
    running and finding nothing. A caller with something more useful to say about
    the platform than about the failure branches on ``os.name`` itself, which is
    how the aws-control listing gets to name WSL.
    """
    if os.name == "nt":
        logger.debug("deploy-web: profile discovery unsupported on Windows")
        return None
    rc, out, err = engine.run_aws(["configure", "list-profiles"], "")
    if rc != 0:
        logger.debug("deploy-web: list-profiles failed: %s", (err or "")[:200])
        return None
    names: list[str] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line and _PROFILE_RE.match(line):
            names.append(line)
    return names[:200]


# --- writes (allowlisted `aws configure set` only) ---------------------------

def _configure_set(key: str, value: str, profile: str) -> str | None:
    """Run ``aws configure set`` for an allowlisted key. Returns error or None."""
    if key not in _WRITABLE_KEYS:  # defense in depth — callers only pass these
        return f"refusing to write non-allowlisted key '{key}'"
    rc, _out, err = engine.run_aws(["configure", "set", key, value], profile)
    if rc != 0:
        raw = (err or f"aws configure set {key} failed").strip()[:200]
        raw, _ = redact_credentials(raw)
        raw, _ = redact_exfiltration_urls(raw)
        return raw
    return None


def create_aws_profile(name: str, region: str, *, account: str = "",
                       role: str = "") -> str | None:
    """Create/update an AWS *config* profile block via the CLI's own writer.

    With ``account``+``role`` this is an iam-identity-style profile whose
    ``credential_process`` mints short-lived creds from the ambient IAM
    session (``aws sts assume-role --role-arn arn:aws:iam::<account>:role/<role>``,
    session name ``kirocrew-deploy``) — the profile block itself contains no
    secret material. Without them, only ``region`` is written (the profile's
    credentials come from however the user otherwise configures it).
    Returns an error string, or None on success.
    """
    # Reject Windows BEFORE any AWS call — engine.run_aws uses
    # subprocess preexec_fn (unsupported on nt); reaching it mid-way could
    # leave a partially-written profile (region set, credentials not).
    if os.name == "nt":
        return "deploy features are not supported on Windows"

    try:
        name = validate_field(name, PROFILE_SPEC)
        region = validate_field(region, REGION_SPEC)
        if account or role:
            account = validate_field(account, ACCOUNT_SPEC)
            role = validate_field(role, ROLE_SPEC)
    except ValidationError as e:
        return f"invalid profile input: {e}"

    # Defense in depth: validate_field passes "" through (spec is not
    # required + pattern check is skipped on empty), and engine._aws() omits
    # --profile for an empty name — an empty name here would rewrite the
    # user's DEFAULT AWS profile via ``aws configure set``. Never allow it.
    if not name:
        return "profile name must not be empty"

    if err := _configure_set("region", region, name):
        return err
    if account and role:
        # AWS credential_process contract: the command must print JSON that
        # includes "Version": 1. Use a single python3 -c wrapper that invokes
        # aws via subprocess (argv list, no shell pipe) so there is only ONE
        # level of quoting -- outer single quotes for shlex, double quotes
        # inside. Nested bash -c quoting is fragile and breaks silently.
        if os.name == "nt":
            return (
                "credential_process is not supported on Windows. "
                "Please configure AWS credentials manually via `aws configure` "
                "or environment variables."
            )
        python_bin = sys.executable or "python3"
        # Deliberately a bare "aws" (NOT the deploy engine's absolute resolver):
        # this command is persisted into ~/.aws/config and later run by
        # whichever AWS CLI/SDK reads that profile — possibly a different user
        # shell, possibly long after the CLI was reinstalled elsewhere. An
        # absolute path resolved in the gateway's environment today can be
        # wrong or stale in that consumer's environment, and would turn a
        # PATH-configurable lookup into a frozen location. The consumer
        # resolves "aws" against its own PATH.
        script = (
            "import json,subprocess;"
            'r=subprocess.run(["aws","sts","assume-role","--role-arn",'
            f'"arn:aws:iam::{account}:role/{role}",'
            '"--role-session-name","kirocrew-deploy",'
            '"--output","json"],capture_output=True,text=True,check=True);'
            'c=json.loads(r.stdout)["Credentials"];'
            'print(json.dumps({"Version":1,'
            '"AccessKeyId":c["AccessKeyId"],'
            '"SecretAccessKey":c["SecretAccessKey"],'
            '"SessionToken":c["SessionToken"],'
            '"Expiration":c["Expiration"]}))'
        )
        cred_proc = f"{python_bin} -c '{script}'"
        if err := _configure_set("credential_process", cred_proc, name):
            return err
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
