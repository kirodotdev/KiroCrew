"""Reading the Kiro Crew backend's per-boot authentication secret.

The backend writes `gateway-<port>.secret` into its run directory at startup.
The value is `os.urandom(16)`, generated fresh on every boot, and it is NOT
persisted anywhere else.

That single fact decides the shape of this module. A caller that caches the
value keeps working right up until the backend restarts, and then every request
fails with 403 for as long as the process lives. A 403 reads like a client
problem, so the actual cause, a stale secret, is not where anyone looks.

There is therefore no cache here, and no parameter to add one. The file read is
a few microseconds from the page cache; a caching layer would trade a real
outage for an unmeasurable saving.
"""

from __future__ import annotations

from pathlib import Path

HEADER = "X-Internal-Secret"


class BackendSecretUnavailable(RuntimeError):
    """The secret file is absent or empty.

    Distinct from an authentication failure on purpose. This means the backend
    has not finished booting, or the run directory is not shared between the
    processes, both of which are startup faults rather than request faults.
    """


def secret_path(run_dir: Path, backend_port: int) -> Path:
    return Path(run_dir) / f"gateway-{backend_port}.secret"


def read_boot_secret(run_dir: Path, backend_port: int) -> str:
    """Read the current secret from disk. Never cache the return value.

    Call this per request, or per attempt after a 403. Storing it in a module
    global, a client object, or a connection pool re-introduces exactly the
    failure this module is written to avoid.
    """
    path = secret_path(run_dir, backend_port)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise BackendSecretUnavailable(
            f"{path} does not exist. The backend writes it at startup, so this "
            "means the backend is not up yet or the run directory is not shared."
        ) from exc
    except OSError as exc:
        raise BackendSecretUnavailable(f"{path} could not be read: {exc}") from exc
    if not value:
        raise BackendSecretUnavailable(f"{path} is empty")
    return value


def auth_header(run_dir: Path, backend_port: int) -> dict[str, str]:
    """The one header the backend needs from a loopback caller."""
    return {HEADER: read_boot_secret(run_dir, backend_port)}
