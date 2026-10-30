"""A KEYRING: Kerberos cache that the agent sandbox hides is named, not silent.

The Linux sandbox starts each agent child in its own user namespace, where a
``KEYRING:persistent`` cache is a different, empty keyring. With no
``/tmp/krb5cc_<uid>`` file for :func:`resolve_krb5_ccname` to redirect to, the
spawn path logs one warning naming the remedy and ``kirocrew doctor`` prints an
advisory row in its Credentials section.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

import kiro_crew.env as env_mod
from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import access

# The reader splits KRB5_CONFIG on ':' as libkrb5 does on Linux, so a Windows
# drive-letter temp path cannot stand in for a config file here.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="krb5 config reader is Linux-only")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(env_mod.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(env_mod.os, "getuid", lambda: 4242, raising=False)
    monkeypatch.setattr(env_mod.os, "geteuid", lambda: 4242, raising=False)
    monkeypatch.setattr(env_mod, "_KRB5_KEYRING_CHECKED", False)


def _conf(tmp_path: Path, body: str, name: str = "krb5.conf") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


_AL2023_CONF = """\
includedir /etc/krb5.conf.d/
[logging]
    default = FILE:/var/log/krb5libs.log
[libdefaults]
    dns_lookup_realm = false
    default_ccache_name = KEYRING:persistent:%{uid}
"""


class TestKeyringCcache:
    def test_reads_default_from_libdefaults_and_expands_uid(self, linux, tmp_path) -> None:
        conf = _conf(tmp_path, _AL2023_CONF.replace("/etc/krb5.conf.d/", str(tmp_path / "none")))
        env = {"KRB5_CONFIG": str(conf)}
        assert env_mod.krb5_keyring_ccache(env) == "KEYRING:persistent:4242"

    def test_explicit_keyring_value_wins_over_conf(self, linux, tmp_path) -> None:
        conf = _conf(tmp_path, "[libdefaults]\n default_ccache_name = FILE:/x\n")
        env = {"KRB5_CONFIG": str(conf), "KRB5CCNAME": "KEYRING:persistent:4242"}
        assert env_mod.krb5_keyring_ccache(env) == "KEYRING:persistent:4242"

    def test_user_keyring_is_claimed_and_userid_token_expands(self, linux, tmp_path) -> None:
        conf = _conf(tmp_path, "[libdefaults]\n default_ccache_name = KEYRING:user:%{USERID}\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) == "KEYRING:user:4242"

    @pytest.mark.parametrize(
        "value", ["KEYRING:session:abc", "KEYRING:process:x", "KEYRING:krb5cc", "KEYRING:thread:t"]
    )
    def test_process_own_keyrings_are_not_claimed(self, linux, tmp_path, value) -> None:
        # Only the kinds the kernel registers per user namespace are flagged.
        conf = _conf(tmp_path, _AL2023_CONF)
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf), "KRB5CCNAME": value}) is None

    def test_tab_separated_include_is_followed(self, linux, tmp_path) -> None:
        inc = _conf(tmp_path, "[libdefaults]\n default_ccache_name = KEYRING:persistent:1\n", "i")
        conf = _conf(tmp_path, f"include\t{inc}\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) == "KEYRING:persistent:1"

    def test_indented_include_is_ignored_like_libkrb5(self, linux, tmp_path) -> None:
        inc = _conf(tmp_path, "[libdefaults]\n default_ccache_name = KEYRING:persistent:1\n", "i")
        conf = _conf(tmp_path, f"  include {inc}\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) is None

    def test_self_include_terminates(self, linux, tmp_path) -> None:
        conf = tmp_path / "krb5.conf"
        conf.write_text(f"include {conf}\n" * 500 + "[libdefaults]\n default_ccache_name = KCM:\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) is None

    @pytest.mark.parametrize("bare", ["KEYRING:persistent", "KEYRING:user"])
    def test_bare_per_namespace_keyring_is_claimed(self, linux, bare) -> None:
        assert env_mod.krb5_keyring_ccache({"KRB5CCNAME": bare}) == bare

    @pytest.mark.skipif(sys.platform == "win32", reason="mkfifo is POSIX-only")
    def test_fifo_in_config_does_not_block(self, linux, tmp_path) -> None:
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        conf = _conf(tmp_path, f"include {fifo}\n[libdefaults]\n default_ccache_name = KCM:\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": f"{fifo}:{conf}"}) is None

    @pytest.mark.parametrize("value", ["FILE:/tmp/krb5cc_4242", "KCM:", "DIR:/run/x"])
    def test_non_keyring_value_is_none(self, linux, tmp_path, value) -> None:
        conf = _conf(tmp_path, _AL2023_CONF)
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf), "KRB5CCNAME": value}) is None

    def test_default_outside_libdefaults_is_ignored(self, linux, tmp_path) -> None:
        conf = _conf(tmp_path, "[realms]\n default_ccache_name = KEYRING:persistent:1\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) is None

    def test_double_quoted_value_is_decoded(self, linux, tmp_path) -> None:
        conf = _conf(
            tmp_path, '[libdefaults]\n default_ccache_name = "KEYRING:persistent:%{uid}"\n'
        )
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) == "KEYRING:persistent:4242"

    @pytest.mark.parametrize(
        ("quoted", "decoded"),
        [
            (r'"a\"b" trailing', 'a"b'),
            (r'"a\\b"', "a\\b"),
            (r'"a\nb\tc\bd"', "a\nb\tc\bd"),
            (r'"a\qb"', "aqb"),
        ],
    )
    def test_quoted_value_escapes_follow_libkrb5(self, quoted, decoded) -> None:
        assert env_mod._krb5_quoted_value(quoted[1:]) == decoded

    def test_final_section_marker_is_a_section_header(self, linux, tmp_path) -> None:
        conf = _conf(tmp_path, "[libdefaults]*\n default_ccache_name = KEYRING:persistent:1\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) == "KEYRING:persistent:1"

    @pytest.mark.parametrize("name", ["default_ccache_name*", "default_ccache_name**"])
    def test_final_relation_marker_is_the_same_key(self, linux, tmp_path, name) -> None:
        body = f"[libdefaults]\n {name} = KEYRING:persistent:%{{uid}}\n"
        conf = _conf(tmp_path, body)
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) == "KEYRING:persistent:4242"

    def test_blank_inside_the_tag_is_not_the_key(self, linux, tmp_path) -> None:
        # libkrb5 rejects ``name * = value`` as a syntax error, so it is not the default.
        body = "[libdefaults]\n default_ccache_name * = KEYRING:persistent:%{uid}\n"
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(_conf(tmp_path, body))}) is None

    def test_final_marker_on_another_section_leaves_libdefaults(self, linux, tmp_path) -> None:
        body = "[libdefaults]\n[realms]*\n default_ccache_name = KEYRING:persistent:1\n"
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(_conf(tmp_path, body))}) is None

    def test_key_inside_a_subsection_is_not_the_default(self, linux, tmp_path) -> None:
        body = (
            "[libdefaults]\n"
            " EXAMPLE.COM = {\n"
            "  default_ccache_name = KEYRING:persistent:1\n"
            " }\n"
            " default_ccache_name = FILE:/tmp/cc\n"
        )
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(_conf(tmp_path, body))}) is None

    def test_top_level_default_after_a_subsection_is_found(self, linux, tmp_path) -> None:
        body = (
            "[libdefaults]\n"
            " EXAMPLE.COM = {\n"
            "  default_ccache_name = FILE:/tmp/cc\n"
            " }\n"
            " default_ccache_name = KEYRING:persistent:1\n"
        )
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(_conf(tmp_path, body))}) == (
            "KEYRING:persistent:1"
        )

    def test_missing_conf_is_none(self, linux, tmp_path) -> None:
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(tmp_path / "absent")}) is None

    def test_includedir_read_where_it_stands(self, linux, tmp_path) -> None:
        # The includedir line comes first, so its value wins, as in libkrb5.
        incdir = tmp_path / "krb5.conf.d"
        incdir.mkdir()
        _conf(incdir, "[libdefaults]\n default_ccache_name = FILE:/tmp/cc\n", "local.conf")
        _conf(incdir, "[libdefaults]\n default_ccache_name = KEYRING:x\n", "skip.rpmsave")
        conf = _conf(tmp_path, _AL2023_CONF.replace("/etc/krb5.conf.d/", str(incdir)))
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) is None

    def test_includedir_keyring_default_is_found(self, linux, tmp_path) -> None:
        incdir = tmp_path / "krb5.conf.d"
        incdir.mkdir()
        _conf(incdir, "[libdefaults]\n default_ccache_name = KEYRING:persistent:%{uid}\n", "cc")
        conf = _conf(tmp_path, f"includedir {incdir}\n[libdefaults]\n dns_lookup_kdc = true\n")
        assert env_mod.krb5_keyring_ccache({"KRB5_CONFIG": str(conf)}) == "KEYRING:persistent:4242"

    def test_off_linux_is_none(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(env_mod.platform_compat, "IS_LINUX", False)
        assert env_mod.krb5_keyring_ccache({"KRB5CCNAME": "KEYRING:persistent:1"}) is None


class TestSpawnWarning:
    def _no_file_cache(self, monkeypatch) -> None:
        monkeypatch.setattr(env_mod, "_reject_reason", lambda cache: "absent")

    def test_warns_once_when_left_on_keyring(self, linux, monkeypatch, tmp_path, caplog) -> None:
        self._no_file_cache(monkeypatch)
        conf = _conf(tmp_path, "[libdefaults]\n default_ccache_name = KEYRING:persistent:%{uid}\n")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.env"):
            for _ in range(3):
                env = {"KRB5_CONFIG": str(conf)}
                env_mod.resolve_krb5_ccname(env)
                assert "KRB5CCNAME" not in env
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "'KEYRING:persistent:4242'" in message
        assert "kinit -c FILE:/tmp/krb5cc_$(id -u)" in message

    def test_no_warning_when_file_cache_is_used(self, linux, monkeypatch, tmp_path, caplog) -> None:
        monkeypatch.setattr(env_mod, "_reject_reason", lambda cache: None)
        conf = _conf(tmp_path, "[libdefaults]\n default_ccache_name = KEYRING:persistent:%{uid}\n")
        env = {"KRB5_CONFIG": str(conf)}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.env"):
            env_mod.resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_4242"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_no_warning_without_keyring_default(self, linux, monkeypatch, tmp_path, caplog) -> None:
        self._no_file_cache(monkeypatch)
        conf = _conf(tmp_path, "[libdefaults]\n dns_lookup_kdc = true\n")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.env"):
            env_mod.resolve_krb5_ccname({"KRB5_CONFIG": str(conf)})
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_failing_config_read_never_breaks_the_spawn(self, linux, monkeypatch) -> None:
        self._no_file_cache(monkeypatch)

        def _boom(env):
            raise TypeError("unexpected")

        monkeypatch.setattr(env_mod, "krb5_keyring_ccache", _boom)
        env: dict[str, str] = {}
        env_mod.resolve_krb5_ccname(env)  # must not raise
        assert "KRB5CCNAME" not in env


class TestDoctorRow:
    def _environ(self, monkeypatch, tmp_path, conf_body: str) -> None:
        conf = _conf(tmp_path, conf_body)
        monkeypatch.delenv("KRB5CCNAME", raising=False)
        monkeypatch.setenv("KRB5_CONFIG", str(conf))

    def test_keyring_without_file_cache_is_advisory(
        self, linux, monkeypatch, tmp_path, capsys
    ) -> None:
        self._environ(
            monkeypatch, tmp_path, _AL2023_CONF.replace("/etc/krb5.conf.d/", str(tmp_path / "none"))
        )
        monkeypatch.setattr(env_mod, "_reject_reason", lambda cache: "absent")
        access._doctor_kerberos_ccache()
        out = capsys.readouterr().out
        assert "kerberos:" in out
        assert "KEYRING:persistent:4242" in out
        assert "kinit -c FILE:/tmp/krb5cc_$(id -u)" in out

    def test_keyring_with_file_cache_is_ok(self, linux, monkeypatch, tmp_path, capsys) -> None:
        self._environ(
            monkeypatch, tmp_path, _AL2023_CONF.replace("/etc/krb5.conf.d/", str(tmp_path / "none"))
        )
        monkeypatch.setattr(env_mod, "_reject_reason", lambda cache: None)
        access._doctor_kerberos_ccache()
        assert "✅ agents use the file cache '/tmp/krb5cc_4242'" in capsys.readouterr().out

    def test_a_failing_check_does_not_end_the_report(self, linux, monkeypatch, capsys) -> None:
        def _boom(environ):
            raise ValueError("bad")

        monkeypatch.setattr(env_mod, "krb5_keyring_ccache", _boom)
        access._doctor_kerberos_ccache()
        assert "could not check the ticket cache (ValueError)" in capsys.readouterr().out

    def test_silent_without_keyring(self, linux, monkeypatch, tmp_path, capsys) -> None:
        self._environ(monkeypatch, tmp_path, "[libdefaults]\n default_ccache_name = KCM:\n")
        access._doctor_kerberos_ccache()
        assert capsys.readouterr().out == ""

    def test_credentials_section_includes_row_without_an_issue(
        self, linux, monkeypatch, tmp_path, capsys
    ) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_doctor, "_credential_vendor_line", lambda: "")
        monkeypatch.setattr(cli_doctor, "_aws_profile_names", lambda: None)
        monkeypatch.setattr(cli_doctor, "_aws_auto_refreshes", lambda: None)
        self._environ(
            monkeypatch, tmp_path, _AL2023_CONF.replace("/etc/krb5.conf.d/", str(tmp_path / "none"))
        )
        monkeypatch.setattr(env_mod, "_reject_reason", lambda cache: "absent")
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        assert "kerberos:" in capsys.readouterr().out
        assert issues == []
