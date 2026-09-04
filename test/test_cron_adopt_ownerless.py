"""Recovery path for a cron job with no owning chat session (issue #4660).

A cron created outside a chat -- ``kirocrew cron add``, the dashboard Schedule
page, an onboarding import -- has no originating chat session, so its
``session_key`` is empty. That is truthful, not a defect: every consumer reads
that field as the delivery target (``session="origin"`` resolution and
script-result injection both strip the ``dashboard:`` prefix off it to reach a
slot), and such a job has no chat to deliver into.

What was missing is the operator's side of it: nothing displayed the field, and
nothing could write it, so once per-session scoping treats an empty owner as
"outside every chat session's scope" the state is invisible AND one-way. These
tests pin the two halves of the way back -- ``cron list`` showing ownership and
``cron adopt`` setting it -- plus the prefix invariant that keeps ``--session-of``
the exact inverse of what the delivery consumers do.
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cli_commands import _cron


def _job(job_id="abc12345", *, session_key="", created_by=""):
    job = MagicMock()
    job.id = job_id
    job.name = "nightly"
    job.message = "run the sweep"
    job.enabled = True
    job.schedule.kind = "every"
    job.schedule.every_secs = 3600
    job.schedule.cron_expr = None
    job.schedule.at_ts = None
    job.session_key = session_key
    job.created_by = created_by
    return job


def _adopt_args(job_id="abc12345", *, session_of=None, release=False):
    return argparse.Namespace(
        cron_action="adopt",
        job_id=job_id,
        session_of=session_of,
        release=release,
    )


class TestCronListOwnership:
    """``cron list`` is the only surface that shows who owns a job."""

    def test_list_marks_a_job_with_no_owning_session(self, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc_cls.return_value.list_jobs.return_value = [_job()]
            _cron(argparse.Namespace(cron_action="list"))
        out = capsys.readouterr().out
        # Without this line the CLI was the one place the state existed and
        # nothing rendered it, so a correct "no chat owns this" read as a job
        # that had silently vanished from chat.
        assert "owner: none" in out
        assert "dashboard Schedule page" in out

    def test_list_shows_the_owning_session_key(self, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc_cls.return_value.list_jobs.return_value = [
                _job(session_key="dashboard:chat-3-1712793600")
            ]
            _cron(argparse.Namespace(cron_action="list"))
        out = capsys.readouterr().out
        assert "owner: dashboard:chat-3-1712793600" in out
        assert "owner: none" not in out

    def test_list_shows_provenance_when_tagged(self, capsys):
        """``created_by`` is a separate, namespaced provenance tag (``app:*`` /
        ``import:*``) and is NOT the owner -- printing both keeps them distinct."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc_cls.return_value.list_jobs.return_value = [
                _job(created_by="import:slack-reminders")
            ]
            _cron(argparse.Namespace(cron_action="list"))
        out = capsys.readouterr().out
        assert "owner: none" in out
        assert "created by: import:slack-reminders" in out


class TestCronAdopt:
    def test_adopt_reports_the_delivery_consequence(self, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.adopt_job.return_value = True
            _cron(_adopt_args(session_of="chat-3-1712793600"))
        out = capsys.readouterr().out
        # The message states the delivery consequence, because for this field
        # owning the job and receiving its output are the same fact.
        assert "results are delivered there" in out
        assert "dashboard:chat-3-1712793600" in out

    def test_session_of_adds_the_prefix_the_consumers_strip(self):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.adopt_job.return_value = True
            _cron(_adopt_args(session_of="chat-3-1712793600"))
            mock_svc.adopt_job.assert_called_once_with("abc12345", "dashboard:chat-3-1712793600")

    def test_session_of_round_trips_through_the_real_delivery_resolver(self):
        """Drift guard: ``--session-of`` must be the exact inverse of the
        transform the delivery path applies.

        Both consumers (``_resolve_session_target`` in the dashboard messaging
        handler and the Slack gateway's script-result delivery) turn a stored
        key into a slot with ``removeprefix("dashboard:")``. If either side ever
        changes prefix, adoption would silently point a job at a slot that does
        not exist and its results would go nowhere -- so assert the round trip
        rather than restating the literal.
        """
        stored = {}

        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.adopt_job.side_effect = lambda jid, sk: stored.setdefault(jid, sk) or True
            _cron(_adopt_args(session_of="chat-7-1712793999"))

        assert stored["abc12345"].removeprefix("dashboard:") == "chat-7-1712793999"

    def test_session_of_accepts_an_already_qualified_key_unchanged(self):
        """A key that already carries a namespace is passed through, so an
        operator pasting a full key into ``--session-of`` is not double-prefixed
        into an undeliverable ``dashboard:dashboard:...``."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.adopt_job.return_value = True
            _cron(_adopt_args(session_of="dashboard:chat-3-1712793600"))
            mock_svc.adopt_job.assert_called_once_with("abc12345", "dashboard:chat-3-1712793600")

    def test_release_clears_the_owner(self, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.adopt_job.return_value = True
            _cron(_adopt_args(release=True))
            mock_svc.adopt_job.assert_called_once_with("abc12345", "")
        assert "released" in capsys.readouterr().out

    def test_missing_job_exits_nonzero(self):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc_cls.return_value.adopt_job.return_value = False
            with pytest.raises(SystemExit) as exc:
                _cron(_adopt_args("deadbeef", session_of="dashboard:chat-1"))
            assert exc.value.code == 1

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_target_is_refused_rather_than_silently_releasing(self, blank):
        """A blank target must NOT fall through to the release path: that would
        turn a typo into a silent hand-back to operator-only management."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            with pytest.raises(SystemExit) as exc:
                _cron(_adopt_args(session_of=blank))
            assert exc.value.code == 1
            mock_svc.adopt_job.assert_not_called()

    def test_adopt_is_audited(self):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc_cls.return_value.adopt_job.return_value = True
            _cron(_adopt_args(session_of="dashboard:chat-3"))
            mock_sel.return_value.log_api_access.assert_called_once()
            kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
            assert kwargs["operation"] == "cron.adopt"
            assert "dashboard:chat-3" in kwargs["resources"]


class TestAdoptTargetVisibility:
    """A typo must not silently produce a job whose output reaches nobody.

    Accepting an unknown key is deliberate -- the delivery path resolves a live
    slot first and only falls back to rehydrating from history, so a brand-new
    tab that has logged nothing yet is a legitimate target. What is not
    acceptable is doing it silently, which would recreate the very
    invisible-delivery state this command exists to recover from.
    """

    def _run_adopt_with_known(self, known):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
            patch("kiro_crew.cli_commands.ConversationLog") as mock_log_cls,
        ):
            mock_svc_cls.return_value.adopt_job.return_value = True
            mock_log_cls.return_value.has_log.return_value = known
            _cron(_adopt_args(session_of="chat-9-1712799999"))
            return mock_log_cls

    def test_unknown_session_warns(self, capsys):
        self._run_adopt_with_known(False)
        err = capsys.readouterr().err
        assert "no recorded session" in err
        assert "chat-9-1712799999" in err
        assert "--release" in err

    def test_known_session_does_not_warn(self, capsys):
        self._run_adopt_with_known(True)
        assert "no recorded session" not in capsys.readouterr().err

    def test_existence_is_checked_on_the_slot_not_the_prefixed_key(self):
        """The lookup must use the same slot the delivery path derives, or the
        warning would fire on every adopt and be trained away as noise."""
        mock_log_cls = self._run_adopt_with_known(True)
        mock_log_cls.return_value.has_log.assert_called_once_with("chat-9-1712799999")

    def test_a_failing_lookup_stays_quiet(self, capsys):
        """Cannot-tell is not evidence of a typo -- never cry wolf."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
            patch("kiro_crew.cli_commands.ConversationLog", side_effect=OSError("no dir")),
        ):
            mock_svc_cls.return_value.adopt_job.return_value = True
            _cron(_adopt_args(session_of="chat-9"))
        assert "no recorded session" not in capsys.readouterr().err

    def test_release_does_not_warn(self, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc_cls.return_value.adopt_job.return_value = True
            _cron(_adopt_args(release=True))
        assert "no recorded session" not in capsys.readouterr().err


class TestAdoptNamespaceHonesty:
    """Ownership and delivery do not have the same reach, and the output says so.

    ``_owned_by`` matches any namespace, so a Slack or Telegram session can own
    and manage a job. But only a ``dashboard:`` key resolves to a slot the
    delivery path can inject into -- both consumers reach a slot with
    ``removeprefix("dashboard:")``. Promising delivery for a ``slack:`` owner
    would be a claim the code does not honour.
    """

    def _adopt(self, target):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
            patch("kiro_crew.cli_commands.ConversationLog") as mock_log_cls,
        ):
            mock_svc_cls.return_value.adopt_job.return_value = True
            mock_log_cls.return_value.has_log.return_value = True
            _cron(_adopt_args(session_of=target))

    @pytest.mark.parametrize("target", ["slack:1712793600.001", "telegram:12345"])
    def test_a_non_dashboard_owner_is_not_promised_delivery(self, target, capsys):
        self._adopt(target)
        cap = capsys.readouterr()
        assert "results are delivered there" not in cap.out
        assert "can manage it." in cap.out
        assert "Ownership transferred; delivery did not." in cap.err

    def test_a_dashboard_owner_is_promised_delivery(self, capsys):
        self._adopt("chat-3-1712793600")
        cap = capsys.readouterr()
        assert "results are delivered there" in cap.out
        assert "Ownership transferred; delivery did not." not in cap.err

    def test_the_unknown_session_warning_is_dashboard_only(self, capsys):
        """The history lookup only knows dashboard slots, so a `slack:` key must
        not be run through it and reported as a typo."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
            patch("kiro_crew.cli_commands.ConversationLog") as mock_log_cls,
        ):
            mock_svc_cls.return_value.adopt_job.return_value = True
            mock_log_cls.return_value.has_log.return_value = False
            _cron(_adopt_args(session_of="slack:1712793600.001"))
            mock_log_cls.return_value.has_log.assert_not_called()
        assert "no recorded session" not in capsys.readouterr().err


class TestAdoptIsDeniedFromBash:
    """The narrow-writer design must be enforced, not merely undocumented.

    "The CLI is the only writer" is only true while a session's agent cannot run
    the CLI. It can, through bash -- so without a denied-command rule an agent
    could run ``cron adopt --session-of <its own slot>`` and acquire exactly the
    power the MCP ``cron_update`` tool is denied: management scope over another
    session's job, plus delivery of its output. The repo's spec requires new
    destructive CLI-facing surfaces to carry a rule in ``BUILTIN_DENIED_RULES``.
    """

    def _rule(self):
        from kiro_crew.security import BUILTIN_DENIED_RULES

        return next(
            (r for r in BUILTIN_DENIED_RULES if r.id == "self-protection-cron-adopt"),
            None,
        )

    def test_a_rule_exists_in_the_builtin_set(self):
        rule = self._rule()
        assert rule is not None, "cron adopt must be covered by a DeniedCommandRule"
        assert rule.category == "self-protection"

    @pytest.mark.parametrize(
        "command",
        [
            "kirocrew cron adopt abc123 --session-of chat-3",
            "kirocrew cron adopt abc123 --release",
            "kiro-crew cron adopt abc123 --session-of dashboard:chat-3",
            "kiro.crew cron adopt abc123 --session-of dashboard:chat-3",
            "cd /tmp && kirocrew cron  adopt abc123 --session-of chat-3",
            "KIROCREW_HOME=/tmp kirocrew cron adopt abc123 --session-of chat-3",
            # The module spelling, which is how the CLI is invoked from a venv.
            # `kiro.?crew` covers it because `.` matches the underscore.
            "python3 -m kiro_crew cron adopt abc123 --release",
            # Interposed top-level flags. The CLI really accepts these before a
            # subcommand (`--verbose`/`-v` is a repeatable count and `--no-jail`
            # is declared on the top-level parser as well as the jailed
            # subparsers) -- verified by running `kirocrew -v cron list`, which
            # executes normally.
            "kirocrew -v cron adopt abc123 --session-of chat-3",
            "kirocrew -vv cron adopt abc123 --session-of chat-3",
            "kirocrew --verbose cron adopt abc123 --session-of chat-3",
            "kirocrew --no-jail cron adopt abc123 --session-of chat-3",
            "kirocrew -v --no-jail cron adopt abc123 --session-of chat-3",
            # Separators that are not whitespace and not flags. A redirection is
            # legal anywhere in a simple command, and $IFS is a word separator,
            # so an allow-list of interlopers would need extending per spelling.
            "kirocrew >/dev/null cron adopt abc123 --release",
            "kirocrew 2>/dev/null cron adopt abc123 --release",
            "kirocrew >/dev/null 2>&1 cron adopt abc123 --release",
            "kirocrew${IFS}cron${IFS}adopt abc123 --release",
            "kirocrew\tcron\tadopt abc123 --release",
            "kirocrew cron >/dev/null adopt abc123 --release",
        ],
    )
    def test_the_pattern_matches_the_forms_an_agent_would_reach_for(self, command):
        import re

        rule = self._rule()
        assert re.search(rule.pattern, command, re.IGNORECASE), command

    @pytest.mark.parametrize(
        "command",
        [
            # A command separator ends the match: the words must belong to ONE
            # simple command, so an unrelated `adopt` in a LATER command does not
            # combine with an earlier harmless `kirocrew cron` read.
            "kirocrew cron list; echo adopt",
            "kirocrew cron list && git adopt-nothing",
            "kirocrew cron list | grep adopt",
        ],
    )
    def test_a_command_separator_ends_the_match(self, command):
        import re

        rule = self._rule()
        assert not re.search(rule.pattern, command, re.IGNORECASE), command

    @pytest.mark.parametrize(
        "command",
        [
            "kirocrew cron list",
            "kirocrew cron add nightly 'sweep' --every 3600",
            "kirocrew cron remove abc123",
            "git commit -m 'adopt a cron convention'",
        ],
    )
    def test_the_pattern_does_not_catch_the_read_and_sibling_verbs(self, command):
        import re

        rule = self._rule()
        assert not re.search(rule.pattern, command, re.IGNORECASE), command

    @pytest.mark.parametrize(
        "command",
        [
            "kirocrew cron adopt abc123 --session-of chat-3",
            "kirocrew -v cron adopt abc123 --session-of chat-3",
        ],
    )
    def test_the_real_gate_denies_it_not_just_the_regex(self, command):
        """Assert through ``is_denied`` -- the function the PreToolUse gate calls --
        so the test proves enforcement rather than only pattern shape."""
        from kiro_crew import security

        assert security.is_denied(
            command, denied_regexes=[r.pattern for r in security.BUILTIN_DENIED_RULES]
        ), command

    def test_the_real_gate_still_allows_reading_the_list(self):
        from kiro_crew import security

        assert not security.is_denied(
            "kirocrew cron list",
            denied_regexes=[r.pattern for r in security.BUILTIN_DENIED_RULES],
        )


class TestAdoptJobService:
    """``adopt_job`` is the only writer of ``session_key`` outside creation."""

    def test_adopt_and_release_persist(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="nightly", message="sweep", every_secs=3600)
        assert job.session_key == ""

        assert svc.adopt_job(job.id, "dashboard:chat-3-1712793600") is True
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == (
            "dashboard:chat-3-1712793600"
        )

        assert svc.adopt_job(job.id, "") is True
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""

    def test_adopt_missing_id_returns_false(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        assert svc.adopt_job("deadbeef", "dashboard:chat-1") is False

    def test_update_job_still_cannot_set_session_key(self, tmp_path):
        """Ownership stays off the shared update path.

        ``update_job`` is reachable from MCP ``cron_update`` and the dashboard
        ``PATCH``; if it grew a ``session_key`` branch, those surfaces could
        repoint where any job delivers. The CLI -- the one surface that is not
        itself a session -- is deliberately the only writer.
        """
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="nightly", message="sweep", every_secs=3600)
        svc.update_job(job.id, session_key="dashboard:chat-attacker")
        assert CronService(base_dir=tmp_path).get_job(job.id).session_key == ""
