"""Nothing host-derived is staged into a pod's ``.aws/sso/cache``.

A security review blocked the earlier shape: the carve-out that keeps the pod's
grant store WRITABLE also made the host's COPIED SSO bearer tokens readable to any
agent shell inside the pod. Live acceptance had already shown the copy was not
load-bearing -- sign-in comes from the runtime's own data store
(``_RUNTIME_AUTH_STORES``) -- so the copy is deleted rather than hidden. These
tests pin that the seeder creates the corridor and copies NOTHING into it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import pinned_fs
from kiro_crew.pod import runtime as rt

requires_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="the seeder builds the tree through pinned descriptors; refusal asserted separately",
)


def test_the_seeder_refuses_without_pinned_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform-honest contract, asserted on EVERY platform.

    Building the pod's credential tree by NAME would be exactly the symlink
    redirect the pinning exists to prevent, so a platform without those
    descriptors gets a refusal that aborts the boot -- not a degraded copy.
    """
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)

    with pytest.raises(rt.PodError, match="O_DIRECTORY/O_NOFOLLOW"):
        rt._seed_pod_os_home(os_home)


@requires_pinned_walk
def test_the_seeder_stages_nothing_from_the_host_sso_cache(tmp_path: Path, monkeypatch) -> None:
    """Red-first: the corridor must come up EMPTY, with the tree still built.

    Driven with a fixture host home that HAS an SSO cache holding a token-shaped
    file. Before the fix that file was copied in; now the directory exists (the pod
    child writes its grants there) and holds nothing.
    """
    host = tmp_path / "host-home"
    host_cache = host / ".aws" / "sso" / "cache"
    host_cache.mkdir(parents=True)
    (host_cache / "d1e5f0a3b2c4.json").write_text("host bearer token")
    (host_cache / "kiro-auth-token.json").write_text("host bearer token")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: host))

    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)
    rt._seed_pod_os_home(os_home)

    corridor = os_home / ".aws" / "sso" / "cache"
    assert corridor.is_dir(), "the pod's own kiro-cli needs this directory to exist"
    assert list(corridor.iterdir()) == [], "no host-derived file may be staged here"


@requires_pinned_walk
def test_the_corridor_is_owner_only(tmp_path: Path, monkeypatch) -> None:
    """Still 0o700 at every level -- the pod's grants land here."""
    host = tmp_path / "host-home"
    (host / ".aws" / "sso" / "cache").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: host))

    os_home = tmp_path / "pod" / "os-home"
    os_home.mkdir(parents=True)
    rt._seed_pod_os_home(os_home)

    assert oct((os_home / ".aws" / "sso" / "cache").stat().st_mode)[-3:] == "700"


def test_boot_flushes_stdout_before_exec() -> None:
    """The probe's own output must survive the exec into the gateway."""
    source = Path(rt.__file__).read_text()
    probe_at = source.index("_probe_pod_child_bootstrap(pod_env)")
    flush_at = source.index("sys.stdout.flush()", probe_at)
    gateway_at = source.index('argv = ["gateway"]', probe_at)
    assert probe_at < flush_at < gateway_at
