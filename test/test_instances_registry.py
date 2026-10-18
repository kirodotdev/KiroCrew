"""The instances registry and the injection-safe validators that guard it."""

from __future__ import annotations

import threading
import types
from pathlib import Path

import pytest


class TestValidation:
    @pytest.mark.parametrize("good", ["cd-1-alias", "user@host.example.com", "h_1"])
    def test_ssh_host_accept(self, good):
        from kiro_crew.instances.validation import validate_ssh_host

        assert validate_ssh_host(good) == good

    @pytest.mark.parametrize(
        "bad", ["-oProxyCommand=x", "a b", "a;b", "a$b", "a@b@c", "", "@h", "h@", "`x`"]
    )
    def test_ssh_host_reject(self, bad):
        from kiro_crew.instances.validation import SshValidationError, validate_ssh_host

        with pytest.raises(SshValidationError):
            validate_ssh_host(bad)

    def test_remote_bin(self):
        from kiro_crew.instances.validation import SshValidationError, validate_remote_bin

        assert validate_remote_bin("") == ""
        assert validate_remote_bin("~/.local/bin/kirocrew") == "~/.local/bin/kirocrew"
        for bad in ("$(x)", "a;b", "`x`", "-rf", 'a"b'):
            with pytest.raises(SshValidationError):
                validate_remote_bin(bad)


# ── registry ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def _reg(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry(path=tmp_path / "instances.json")

    def test_crud_and_collision(self, tmp_path):
        from kiro_crew.instances.registry import DuplicateInstanceError

        reg = self._reg(tmp_path)
        a = reg.add(name="Cloud Desktop 1", ssh_host="cd-1-alias")
        assert a.id == "cloud-desktop-1" and a.remote_port == 5476 and a.was_connected is False
        b = reg.add(name="Cloud Desktop 1", ssh_host="cd-2-alias")
        assert b.id == "cloud-desktop-1-2"
        with pytest.raises(DuplicateInstanceError):
            reg.add(name="x", ssh_host="h", instance_id="cloud-desktop-1")
        assert len(reg.list()) == 2

    def test_update_validation_and_hints(self, tmp_path):
        from kiro_crew.instances.registry import InstanceNotFoundError, InvalidInstanceError

        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        u = reg.update("cd-1", local_port=7778, was_connected=True)
        assert u.local_port == 7778 and u.was_connected is True
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", remote_port=70000)
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", id="nope")
        with pytest.raises(InstanceNotFoundError):
            reg.update("ghost", name="z")

    def test_update_mark_last_active_is_one_mutation(self, tmp_path):
        """update(mark_last_active=True) records the auto-revive target in the
        same read-modify-write as the field changes, and plain update leaves
        the recorded target alone."""
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        reg.add(name="CD2", ssh_host="cd-2", instance_id="cd-2")

        u = reg.update("cd-1", mark_last_active=True, local_port=7778, was_connected=True)
        assert u.local_port == 7778 and u.was_connected is True
        assert reg.get_last_active().id == "cd-1"

        # A plain update on another instance does not steal the target.
        reg.update("cd-2", local_port=7779)
        assert reg.get_last_active().id == "cd-1"

    def test_remove_clears_last_active_and_reload(self, tmp_path):
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        reg.update("cd-1", mark_last_active=True)
        assert reg.remove("cd-1") is True
        assert reg.remove("cd-1") is False
        assert reg.get_last_active() is None
        # fresh instance reads the same (empty) file
        assert self._reg(tmp_path).list() == []

    def test_no_credentials_persisted(self, tmp_path):
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        raw = (tmp_path / "instances.json").read_text(encoding="utf-8")
        assert "token" not in raw.lower()

    def test_env_home_path(self, tmp_path, monkeypatch):
        from kiro_crew.instances.registry import InstancesRegistry

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert InstancesRegistry().path == tmp_path / "instances.json"

    def test_two_registries_over_one_file_share_a_lock(self, tmp_path):
        """Objects over the same file hold one lock; different files hold two.

        Callers construct a registry per request, so a lock owned by the object
        serialises nothing between them.
        """
        from kiro_crew.instances.registry import InstancesRegistry

        first = self._reg(tmp_path)
        second = self._reg(tmp_path)
        assert first._lock is second._lock

        other = tmp_path / "elsewhere"
        other.mkdir()
        assert InstancesRegistry(other / "instances.json")._lock is not first._lock

    def test_a_second_writer_cannot_read_between_a_read_and_its_write(self, tmp_path):
        """An interleaved second registry must not drop the first's record.

        The writing thread is held inside its own write and the second thread is
        released to add a different instance. Both records have to survive: a
        reader admitted before the first write lands sees the pre-add document
        and persists it back without that record.

        Both waits are bounded, so the serialised case simply times out and
        proceeds rather than hanging.
        """
        from kiro_crew.instances.registry import InstancesRegistry

        path = tmp_path / "instances.json"
        writer = InstancesRegistry(path)
        intruder = InstancesRegistry(path)

        inside_write = threading.Event()
        intruder_done = threading.Event()
        real_write = type(writer)._write

        def holding_write(self, doc):
            inside_write.set()
            # flake-ok: pre-existing 2 s hold inside the write; on the locked path it expires and the write proceeds, moved verbatim
            intruder_done.wait(timeout=2.0)
            real_write(self, doc)

        writer._write = types.MethodType(holding_write, writer)

        def add_intruder():
            # flake-ok: pre-existing 2 s wait for the writer to enter its write; expiry proceeds instead of failing, moved verbatim
            inside_write.wait(timeout=2.0)
            intruder.add(name="Second", ssh_host="second-1", instance_id="second-1")
            intruder_done.set()

        thread = threading.Thread(target=add_intruder, daemon=True)
        thread.start()
        writer.add(name="First", ssh_host="first-1", instance_id="first-1")
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the second writer never finished"

        ids = sorted(inst.id for inst in InstancesRegistry(path).list())
        assert ids == ["first-1", "second-1"]


class TestValidatorsRejectEmbeddedNewlines:
    """Every anchored validator in this package must use ``\\Z``, not ``$``.

    Python's ``$`` also matches just BEFORE a trailing newline, so a value like
    ``"20h\\n"`` passes a ``$``-anchored check and then reaches an ssh/ssm
    argument list carrying an embedded newline. Every regex here guards a value
    that ends up on such a command line, so this is one bug class rather than one
    regex — a ratchet is the only thing that keeps a future edit from
    reintroducing it.
    """

    def test_no_anchored_pattern_uses_a_dollar_anchor(self):
        import re as _re

        offenders = []
        for mod in ("registry", "validation", "constants"):
            path = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "kiro_crew"
                / "instances"
                / f"{mod}.py"
            )
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # An anchored pattern literal that still ends a branch with `$`.
                if _re.search(r'r"\^[^"]*\$(?:\|\^\$)?"', stripped):
                    offenders.append(f"{mod}.py: {stripped}")
        assert not offenders, (
            "anchored with `$`, which also matches before a trailing newline; "
            "use `\\Z`:\n  " + "\n  ".join(offenders)
        )

    def test_a_trailing_newline_never_survives_validation(self, tmp_path):
        """Two safe answers, and every guard must give one of them.

        ``validation.py`` SANITIZES — it strips before matching and returns the
        cleaned value, so a newline cannot reach the argument list it guards. The
        registry and the ttl check REJECT, because they persist what they are
        given. What must not happen is a guard accepting the value and passing the
        newline through unchanged.
        """
        from kiro_crew.instances import validation
        from kiro_crew.instances.registry import (
            InstancesRegistry,
            InvalidInstanceError,
            validate_ttl,
        )

        for fn, dirty in (
            (validation.validate_ssh_host, "host\n"),
            (validation.validate_remote_bin, "/usr/bin/kirocrew\n"),
            (validation.validate_ssm_target, "i-0123456789abcdef0\n"),
            (validation.validate_ssm_run_as, "ec2-user\n"),
            (validation.validate_aws_profile, "Admin\n"),
            (validation.validate_aws_region, "us-west-2\n"),
        ):
            cleaned = fn(dirty)
            assert "\n" not in cleaned, f"{fn.__name__} passed a newline through"

        # The persisting layers refuse outright rather than silently rewriting.
        with pytest.raises(InvalidInstanceError):
            validate_ttl("20h\n")
        reg = InstancesRegistry(tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError):
            reg.add(name="CD", ssh_host="cd-1-alias\n", instance_id="cd-1")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", ttl="20h\n")


class TestSlugifyHashFallback:
    def test_non_ascii_names_derive_distinct_stable_ids(self) -> None:
        from kiro_crew.instances.registry import _ID_RE, _slugify

        chinese = _slugify("\u5f00\u53d1\u673a")
        arabic = _slugify("\u062e\u0627\u062f\u0645 \u0627\u0644\u062a\u0637\u0648\u064a\u0631")
        assert chinese.startswith("instance-")
        assert chinese != arabic
        assert chinese == _slugify("\u5f00\u53d1\u673a")
        assert _ID_RE.match(chinese)

    def test_ascii_names_are_unchanged(self) -> None:
        from kiro_crew.instances.registry import _slugify

        assert _slugify("Dev Box 2") == "dev-box-2"
