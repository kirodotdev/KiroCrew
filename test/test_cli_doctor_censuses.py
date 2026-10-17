"""``kirocrew doctor``'s cron health and its directory censuses.

The Cron Jobs section, the skill-view alias census of the Agents Directory
section and the read-only run-directory census.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import cli_doctor, cron


class TestCronHealth:
    """`kirocrew doctor` Cron Jobs section — auto-paused / errored jobs.

    Read-only by contract: the check reports and hints, it never resumes or
    triggers anything. The negative half of this suite (healthy store,
    user-paused job, missing file) is what stops the check crying wolf.
    """

    @staticmethod
    def _job(job_id: str, name: str, **over: object) -> dict:
        job = {
            "id": job_id,
            "name": name,
            "message": "do a thing",
            "schedule": {"kind": "every", "every_secs": 3600},
            "enabled": True,
            "user_paused": False,
            "auto_paused": False,
            "last_status": "ok",
        }
        job.update(over)
        return job

    def _write(self, tmp_path: Path, *jobs: dict) -> Path:
        path = tmp_path / "crons.json"
        path.write_text(json.dumps({"version": 2, "jobs": list(jobs)}), encoding="utf-8")
        return path

    def _run(self, monkeypatch, tmp_path: Path) -> list[str]:
        # The scan lives in cron.py (pause predicates: cron_service/store.py), so
        # the data home is patched THERE; doctor is only the presentation half.
        monkeypatch.setattr(cron, "config_dir", lambda: tmp_path)
        issues: list[str] = []
        cli_doctor._doctor_cron_health(issues)
        return issues

    # ── positive: the signals ARE reported ──

    def test_auto_paused_job_is_reported_with_resume_hint(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        self._write(
            tmp_path,
            self._job("j1", "nightly-sync", auto_paused=True, enabled=False, last_status="error"),
        )

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "Cron Jobs" in out
        assert "auto-paused" in out
        assert "'nightly-sync' ('j1')" in out
        assert "kirocrew cron resume <id>" in out
        assert issues == ["1 cron job(s) auto-paused"]

    def test_errored_job_is_reported_with_trigger_hint(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        self._write(tmp_path, self._job("j2", "pr-watch", last_status="error"))

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "errored:" in out
        assert "'pr-watch' ('j2')" in out
        assert "kirocrew cron trigger <id>" in out
        assert issues == ["1 cron job(s) last ran with an error"]

    def test_a_user_paused_at_job_with_a_stale_error_is_not_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """An explicitly paused ``at`` job must stay silent even carrying an error.

        The user pause is the later, more specific instruction, so it wins -- the
        contract this function's own docstring states. A stale ``last_status``
        from a run before the pause is not a reason to hand back a hint for a job
        the user switched off.

        Paired with the sibling below, which keeps a NON-paused errored at-job
        reported: neither test alone pins the distinction, because one mutation
        can only move one of the two outcomes.
        """
        self._write(
            tmp_path,
            self._job(
                "j-at",
                "one-off-import",
                schedule={"kind": "at", "at_ts": 1.0},
                enabled=False,
                user_paused=True,
                last_status="error",
            ),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert issues == []
        out = capsys.readouterr().out
        assert "errored:" not in out
        assert "'one-off-import' ('j-at')" not in out

    def test_a_non_paused_at_job_with_an_error_is_still_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The other half: silencing paused jobs must not silence live failures.

        Same ``at`` schedule and the same errored status as the sibling above --
        only the pause flag differs, so this is the assertion that catches a fix
        that simply stopped reporting at-jobs.
        """
        self._write(
            tmp_path,
            self._job(
                "j-at-live",
                "live-import",
                schedule={"kind": "at", "at_ts": 1.0},
                enabled=True,
                user_paused=False,
                last_status="error",
            ),
        )

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "errored:" in out
        assert "'live-import' ('j-at-live')" in out
        assert issues == ["1 cron job(s) last ran with an error"]

    def test_auto_paused_job_is_not_also_counted_as_errored(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A job only auto-pauses by failing repeatedly, so it carries
        # last_status="error" too. Reporting both would print contradictory
        # advice (resume vs. re-trigger) for one job.
        self._write(
            tmp_path,
            self._job("j3", "flaky", auto_paused=True, enabled=False, last_status="error"),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert issues == ["1 cron job(s) auto-paused"]
        assert "errored:" not in capsys.readouterr().out

    def test_job_list_is_capped_with_a_plus_n_more_tail(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A user with dozens of crons must not get a wall of text.
        jobs = [self._job(f"j{n}", f"job-{n}", auto_paused=True, enabled=False) for n in range(8)]
        self._write(tmp_path, *jobs)

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "+3 more" in out
        assert "'job-0' ('j0')" in out
        assert "'job-7' ('j7')" not in out, "beyond the cap must be summarised, not listed"
        assert issues == ["8 cron job(s) auto-paused"]

    # ── negative: healthy / deliberate state is NOT reported ──

    def test_healthy_store_is_silent(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._write(tmp_path, self._job("j4", "fine"), self._job("j5", "also-fine"))

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_user_paused_job_is_not_reported(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # user_paused is deliberately distinct from auto_paused: a job the user
        # paused on purpose is not a health signal, and neither is a stale
        # last_status left over from before they paused it.
        self._write(
            tmp_path,
            self._job("j6", "on-purpose", user_paused=True, enabled=False, last_status="error"),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_legacy_record_without_user_paused_key_is_not_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Records written before `user_paused` existed carry the reason only in
        # `enabled`; the deserializer derives user_paused from it, and so must this.
        job = self._job("j7", "legacy", enabled=False, last_status="error")
        del job["user_paused"]
        self._write(tmp_path, job)

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    # ── degradation: a broken or absent store must not fail doctor ──

    def test_missing_crons_file_is_silent(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # Every fresh install: no crons yet.
        assert not (tmp_path / "crons.json").exists()

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    @pytest.mark.parametrize(
        "body",
        ["not json at all", "", "[]", '{"jobs": "not-a-list"}', '{"jobs": [null, 3]}'],
        ids=["garbage", "empty", "top-level-list", "jobs-not-a-list", "jobs-of-scalars"],
    )
    def test_corrupt_crons_file_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys, body: str
    ) -> None:
        # The run on a host with a corrupt crons.json is exactly the run that
        # most needs doctor's other checks — it must not get a traceback. But it
        # must not be SILENT either: the scheduler can load no jobs from an
        # unreadable store, so every job has stopped, and reporting a clean bill
        # of health there is the silence this check exists to break.
        (tmp_path / "crons.json").write_text(body, encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_one_malformed_record_does_not_discard_the_rest(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        path = tmp_path / "crons.json"
        good = self._job("j8", "real-job", auto_paused=True, enabled=False)
        path.write_text(json.dumps({"jobs": ["junk", good]}), encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "'real-job' ('j8')" in capsys.readouterr().out
        assert issues == ["1 cron job(s) auto-paused"]

    def test_record_with_blank_id_and_name_still_renders(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # The `(unnamed)` / `no-id` label fallback, on a record the scheduler
        # CAN load: `_job_from_record` needs the keys present, not non-empty, so
        # blank strings still build a job and must still be nameable. A record
        # MISSING those keys is a different case -- unloadable, so it is skipped
        # and reported as a broken store instead (see the unloadable-record
        # test above); asserting a hint for it would encode that defect.
        self._write(tmp_path, self._job("", "", auto_paused=True, enabled=False))

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "(unnamed)" in out and "no-id" in out
        assert issues == ["1 cron job(s) auto-paused"]

    def test_an_auto_paused_job_the_user_also_paused_is_not_reported(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Both flags can be set at once, and the user pause wins.

        `_enable_job_locked` clears `auto_paused` only when ENABLING, so pausing
        an already-auto-paused job leaves `auto_paused` true and adds
        `user_paused`. Telling the user to resume a job they deliberately
        switched off would contradict the more specific instruction.
        """
        self._write(
            tmp_path,
            self._job(
                "j10",
                "off-on-purpose",
                auto_paused=True,
                user_paused=True,
                enabled=False,
                last_status="error",
            ),
        )

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_invalid_utf8_in_the_store_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # The store is bytes on disk and can hold invalid UTF-8. A
        # UnicodeDecodeError here would abort the whole doctor run; swallowing
        # it silently would instead hide that no job can load at all.
        (tmp_path / "crons.json").write_bytes(b'{"jobs": [{"id": "a", "name": "\xff\xfe"}]}')

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_deeply_nested_json_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # json.loads raises RecursionError on deeply nested input, and
        # RecursionError is a RuntimeError -- NOT a ValueError -- so it escapes
        # the decode-error tuple and would abort the whole doctor run. Caught, it
        # is still an unreadable store and must be reported rather than hidden.
        depth = 100_000
        (tmp_path / "crons.json").write_text("[" * depth + "]" * depth, encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_a_store_of_non_job_dicts_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # `{}` is a dict, so an isinstance-only shape check calls this store
        # readable -- but `_job_from_record` rejects it (KeyError: 'id'), so the
        # scheduler loads ZERO jobs from it. Entries were present and none is
        # loadable: that is the "parsed but nothing came out" fault this check
        # exists to surface, not an honestly empty store.
        (tmp_path / "crons.json").write_text('{"jobs": [{}]}', encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_an_unloadable_record_does_not_produce_a_bogus_resume_hint(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # `{"auto_paused": true}` carries no id/name/message, so the scheduler
        # rejects it and runs nothing -- but classifying it BEFORE checking
        # loadability puts it in the auto-paused bucket, so doctor advises
        # `cron resume` for a job that does not exist and the unloadable-store
        # report never fires. The store is the fault; the phantom job is not.
        (tmp_path / "crons.json").write_text('{"jobs": [{"auto_paused": true}]}', encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "could not be read" in out
        assert "resume" not in out
        assert issues == ["cron store unreadable"]

    def test_a_crons_json_directory_is_reported_not_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # `is_file()` is False for a DIRECTORY just as it is for a missing file,
        # so exempting on it silently classifies an unloadable store as the
        # fresh-install case. The scheduler can load nothing from a directory.
        (tmp_path / "crons.json").mkdir()

        issues = self._run(monkeypatch, tmp_path)

        assert "could not be read" in capsys.readouterr().out
        assert issues == ["cron store unreadable"]

    def test_a_readable_but_empty_store_stays_silent(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # The boundary that stops the unreadable-store report crying wolf: a
        # store that parses fine and simply holds no jobs is NOT a fault, and
        # must stay silent even though the scan returns nothing — exactly like
        # the missing-file case.
        (tmp_path / "crons.json").write_text('{"jobs": []}', encoding="utf-8")

        issues = self._run(monkeypatch, tmp_path)

        assert capsys.readouterr().out == ""
        assert issues == []

    def test_a_control_bearing_job_name_is_escaped(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A job name is free text an app or a hand-edit supplies, so it must not
        # be able to act on the terminal or spoof the surrounding report lines.
        self._write(
            tmp_path,
            self._job("j11", "evil\x1b[2Jname", auto_paused=True, enabled=False),
        )

        issues = self._run(monkeypatch, tmp_path)

        out = capsys.readouterr().out
        assert "\x1b" not in out, "raw escape from a job name reached the terminal"
        assert "\\x1b" in out, "the name is still shown, just escaped"
        assert issues == ["1 cron job(s) auto-paused"]

    def test_check_is_read_only(self, monkeypatch, tmp_path: Path) -> None:
        # The whole point: doctor diagnoses, it never resumes or triggers.
        path = self._write(
            tmp_path,
            self._job("j9", "paused-job", auto_paused=True, enabled=False, last_status="error"),
        )
        before = path.read_bytes()

        self._run(monkeypatch, tmp_path)

        assert path.read_bytes() == before, "doctor must not mutate crons.json"


class TestDoctorSkillViewCensus:
    """The Agents Directory section counts the ``kirocrew-skill-view-*`` aliases.

    The projection publishes one alias per distinct agent view into the shared
    kiro agents directory -- spawns of the same agent share one file -- and
    kiro-cli reads every file there on startup. Before
    the lease-based reclaim the directory grew without bound (28k files / 580 MB
    on one host; ``EMFILE`` on another), and the only way to see it was ``ls``.
    Doctor reports the census read-only: how many aliases exist, how many a
    lease record names, which share this gateway's reclaim covers, and a
    warning once the count is past the point where startup cost is measurable.
    """

    PREFIX = "kirocrew-skill-view-"

    @staticmethod
    def _home(tmp_path: Path) -> Path:
        # Spelled through ``.absolute().as_posix()`` like the publisher does, so
        # the test does not depend on how the platform absolutises a bare "/x".
        return tmp_path / "crew-home"

    @classmethod
    def _alias(cls, directory: Path, index: int, *, home: str | None) -> str:
        stem = f"{cls.PREFIX}{index:024x}"
        (directory / f"{stem}.json").write_text('{"name": "%s"}' % stem)
        if home is not None:
            metadata_dir = directory / ".kirocrew-skill-projection-metadata"
            metadata_dir.mkdir(exist_ok=True)
            (metadata_dir / f"{stem}.json").write_text(
                json.dumps({"x-kirocrew-managed": "skill-view", "x-kirocrew-home": home})
            )
        return stem

    def _own(self, tmp_path: Path, index: int) -> str:
        return self._alias(tmp_path, index, home=self._home(tmp_path).absolute().as_posix())

    @staticmethod
    def _lease(directory: Path, stems: list[str], name: str = "1-abc") -> None:
        lease_dir = directory / ".kirocrew-skill-projection-leases"
        lease_dir.mkdir(exist_ok=True)
        (lease_dir / f"{name}.json").write_text(json.dumps({"aliases": stems}))
        (lease_dir / f"{name}.hold").write_text("")

    def _run(self, tmp_path: Path, monkeypatch, capsys) -> str:
        from kiro_crew.acp import skill_projection

        monkeypatch.setattr(cli_doctor, "KIRO_AGENTS_DIR", tmp_path)
        # The census resolves the data home itself, spelled as the publisher
        # spells it, so the doctor cannot hand it a differently normalised id.
        monkeypatch.setattr(skill_projection, "data_home", lambda: self._home(tmp_path))
        cli_doctor._doctor_agents_janitor([], sweep_backups=False)
        return capsys.readouterr().out

    @staticmethod
    def _line(out: str) -> str:
        return out.split("skill views:", 1)[1]

    def test_an_empty_directory_reports_zero_aliases(self, tmp_path, monkeypatch, capsys):
        out = self._run(tmp_path, monkeypatch, capsys)
        assert "skill views: ✅ 0 kirocrew-skill-view-*.json alias(es)" in out

    def test_live_and_unreferenced_aliases_are_counted_separately(
        self, tmp_path, monkeypatch, capsys
    ):
        live = [self._own(tmp_path, i) for i in range(3)]
        for i in range(3, 8):
            self._own(tmp_path, i)
        self._lease(tmp_path, live)
        out = self._run(tmp_path, monkeypatch, capsys)
        assert "skill views: ✅ 8 kirocrew-skill-view-*.json alias(es)" in out
        assert "(3 named by a lease record, 5 not)" in out
        assert "⚠️" not in self._line(out)

    def test_the_threshold_is_the_real_one_and_exclusive(self, tmp_path, monkeypatch, capsys):
        # Exactly the production threshold is still green; one more warns. No
        # monkeypatched constant, so the 128 the docs claim is what is measured.
        limit = cli_doctor._SKILL_VIEW_BACKLOG_WARN
        assert limit == 128
        for i in range(limit):
            self._alias(tmp_path, i, home=None)
        assert "skill views: ✅" in self._run(tmp_path, monkeypatch, capsys)
        self._alias(tmp_path, limit, home=None)
        out = self._run(tmp_path, monkeypatch, capsys)
        assert "skill views: ⚠️" in out
        assert f"{limit + 1} kirocrew-skill-view-*.json alias(es)" in out

    def test_a_backlog_this_home_owns_names_its_share_and_the_remedy_is_a_move(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 5)
        for i in range(6):
            self._own(tmp_path, i)
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "⚠️" in line
        assert "reclaims a bounded number of the 6 this home owns" in line
        assert "gateway stopped" in line
        assert "move" in line
        # Advice the doctor prints is text an operator acts on: it must never
        # suggest deleting a file whose author the doctor cannot prove.
        assert "delete them" not in line
        assert "removed" not in line
        # No lease, no foreign home: neither caveat is printed.
        assert "Lease-named" not in line
        assert "another Kiro Crew home" not in line

    def test_a_backlog_another_home_owns_is_named_as_not_draining_here(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 3)
        for i in range(4):
            self._alias(tmp_path, i, home="/some/other/home")
        self._own(tmp_path, 4)
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "(0 named by a lease record, 5 not, 4 owned by another Kiro Crew home)" in line
        assert "reclaims a bounded number of the 1 this home owns" in line
        assert "The 4 another Kiro Crew home owns never drain here" in line
        # A second home shares this directory, so the remedy must stop BOTH
        # gateways, not only the one this doctor speaks for.
        assert "with every gateway that uses this agents directory stopped" in line
        assert "with the gateway stopped" not in line

    def test_another_homes_leased_aliases_are_not_called_held_or_crash_stale(
        self, tmp_path, monkeypatch, capsys
    ):
        # The other home's live sessions hold leases in this shared directory.
        # This gateway's reclaim refuses those aliases unconditionally, so they
        # are neither "kept while held" nor "reclaimed on the next spawn" here.
        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 2)
        theirs = [self._alias(tmp_path, i, home="/some/other/home") for i in range(3)]
        self._lease(tmp_path, theirs)
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "(3 named by a lease record, 0 not, 3 owned by another Kiro Crew home)" in line
        assert "crash-stale" not in line
        assert "The 3 another Kiro Crew home owns never drain here" in line

    def test_lease_named_aliases_are_described_as_held_not_as_never_draining(
        self, tmp_path, monkeypatch, capsys
    ):
        # A crash-stale but readable lease names aliases the next spawn will
        # reclaim; the census cannot tell it from a held one without probing
        # the lock, so the text says "while held", never "will not drain".
        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 2)
        stems = [self._own(tmp_path, i) for i in range(3)]
        self._lease(tmp_path, stems)
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "(3 named by a lease record, 0 not)" in line
        assert "This home's lease-named aliases are kept while their lease is held" in line
        assert "a crash-stale lease is reclaimed on the next spawn" in line
        assert "will not drain" not in line

    def test_an_unreadable_lease_record_withdraws_the_drain_promise(
        self, tmp_path, monkeypatch, capsys
    ):
        # The reclaim reads a malformed record as uncertainty and keeps EVERY
        # alias while it exists, so the census must say so even below the
        # backlog threshold -- and, below it, say nothing about startup cost.
        self._own(tmp_path, 1)
        self._own(tmp_path, 2)
        lease_dir = tmp_path / ".kirocrew-skill-projection-leases"
        lease_dir.mkdir()
        (lease_dir / "1-bad.json").write_text("{not json")
        (lease_dir / "1-big.json").write_text(json.dumps({"aliases": ["x" * 70000]}))
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "⚠️  2 kirocrew-skill-view-*.json alias(es)" in line
        assert "(0 named by a lease record, 2 not)" in line
        assert "2 lease record(s)" in line and "cannot be read" in line
        assert "slows every session start" not in line
        assert "reclaims a bounded number" not in line

    def test_an_unreadable_lease_above_the_threshold_denies_the_drain(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 1)
        for i in range(3):
            self._own(tmp_path, i)
        lease_dir = tmp_path / ".kirocrew-skill-projection-leases"
        lease_dir.mkdir()
        (lease_dir / "1-bad.json").write_text("?")
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "Nothing is reclaimed until the unreadable lease record(s) above are gone." in line
        assert "reclaims a bounded number" not in line
        assert "gateway stopped" in line

    def test_a_truncated_census_is_reported_as_floors_without_derived_counts(
        self, tmp_path, monkeypatch, capsys
    ):
        from kiro_crew.acp import skill_projection

        monkeypatch.setattr(skill_projection, "_CENSUS_MAX_ALIASES", 3)
        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 2)
        for i in range(5):
            self._own(tmp_path, i)
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "3+ kirocrew-skill-view-*.json alias(es) (0+ named by a lease record)" in line
        assert "floors: the census stopped at its retention bound" in line
        # `total - leased` is neither a floor nor a ceiling once a bound was hit,
        # so no derived number is printed or promised.
        assert " not" not in line.split(")", 1)[0]
        assert "Unscanned lease records leave reclaimability unknown" in line
        assert "of the 3" not in line
        assert "On every spawn the gateway reclaims" not in line

    def test_a_pathologically_nested_sidecar_or_lease_does_not_abort_the_doctor(
        self, tmp_path, monkeypatch, capsys
    ):
        # json.loads raises RecursionError (a RuntimeError) on nesting past the
        # interpreter limit; a hand-authored file in Kiro Crew's own hidden
        # directories must read as unreadable, never take the diagnostic down.
        deep = "[" * 100000 + "]" * 100000
        self._own(tmp_path, 1)
        stem = self._own(tmp_path, 2)
        (tmp_path / ".kirocrew-skill-projection-metadata" / f"{stem}.json").write_text(deep)
        lease_dir = tmp_path / ".kirocrew-skill-projection-leases"
        lease_dir.mkdir()
        (lease_dir / "1-deep.json").write_text(deep)
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        assert "2 kirocrew-skill-view-*.json alias(es)" in line
        assert "1 lease record(s)" in line and "cannot be read" in line

    def test_authored_specs_and_foreign_files_are_not_counted(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "kirocrew.json").write_text('{"name": "kirocrew"}')
        (tmp_path / "kirocrew-skill-view-notes.txt").write_text("x")
        (tmp_path / "kirocrew-skill-view-dir.json").mkdir()
        self._own(tmp_path, 1)
        out = self._run(tmp_path, monkeypatch, capsys)
        assert "skill views: ✅ 1 kirocrew-skill-view-*.json alias(es)" in out

    def test_the_census_never_deletes_anything(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 1)
        stems = [self._own(tmp_path, i) for i in range(4)]
        self._lease(tmp_path, stems[:1])
        (tmp_path / ".kirocrew-skill-projection-leases" / "2-bad.json").write_text("?")
        before = sorted(p.name for p in tmp_path.rglob("*"))
        self._run(tmp_path, monkeypatch, capsys)
        assert sorted(p.name for p in tmp_path.rglob("*")) == before

    def test_the_remedy_names_the_projection_directories_it_means(
        self, tmp_path, monkeypatch, capsys
    ):
        from kiro_crew.acp import skill_projection

        monkeypatch.setattr(cli_doctor, "_SKILL_VIEW_BACKLOG_WARN", 1)
        for i in range(2):
            self._own(tmp_path, i)
        (tmp_path / ".kirocrew-skill-projection-leases").mkdir()
        (tmp_path / ".kirocrew-skill-projection-leases" / "1-bad.json").write_text("?")
        line = self._line(self._run(tmp_path, monkeypatch, capsys))
        # The side files live under the Kiro Crew data home; the remedy names
        # those directories by their real path, and the legacy one an older
        # build left beside the aliases, which the reclaim still reads.
        metadata_dir = str(skill_projection._projection_metadata_dir())
        lease_dir = str(skill_projection._projection_lease_dir())
        assert f"{metadata_dir}/ directory out of the way" in line
        assert f"in {lease_dir}/ (or in the legacy" in line
        assert f"{skill_projection._PROJECTION_LEASE_DIR_NAME}/ beside the aliases" in line

    @staticmethod
    def _residue(out: str) -> str:
        return out.split("skill-view residue:", 1)[1]

    def test_a_clean_directory_reports_no_residue(self, tmp_path, monkeypatch, capsys):
        self._own(tmp_path, 0)
        line = self._residue(self._run(tmp_path, monkeypatch, capsys))
        assert line.startswith(" ✅ 0 ownership sidecar(s)")
        assert "0 leftover alias .lock file(s), 0 alias(es) rewritten" in line

    def test_residue_past_the_threshold_and_any_external_rewrite_warn(
        self, tmp_path, monkeypatch, capsys
    ):
        import hashlib

        from kiro_crew.doctor_checks import resources

        monkeypatch.setattr(resources, "_SKILL_VIEW_RESIDUE_WARN", 1)
        metadata_dir = tmp_path / ".kirocrew-skill-projection-metadata"
        metadata_dir.mkdir()
        for i in range(2):
            (metadata_dir / f"{self.PREFIX}{i:024x}.json").write_text("{}")
        out = self._run(tmp_path, monkeypatch, capsys)
        assert self._residue(out).startswith(" ⚠️  2 ownership sidecar(s)")
        assert "restart it once to drain the backlog" in out

        stem = self._own(tmp_path, 9)
        record = json.loads((metadata_dir / f"{stem}.json").read_text())
        record["x-kirocrew-alias-sha256"] = hashlib.sha256(b"what was published").hexdigest()
        (metadata_dir / f"{stem}.json").write_text(json.dumps(record))
        out = self._run(tmp_path, monkeypatch, capsys)
        assert "1 alias(es) rewritten by another program" in out
        assert "KIROCREW_NATIVE_SKILL_PROJECTION=0" in out

    def test_an_env_value_that_differs_across_one_agents_views_is_named(
        self, tmp_path, monkeypatch, capsys
    ):
        """Two views of one agent whose only difference is an env value: a launcher
        re-stamping a key the volatile set does not know. The line names it and
        the setting that makes it volatile; volatile keys are never named."""
        monkeypatch.delenv("KIROCREW_SKILL_VIEW_VOLATILE_ENV", raising=False)
        home = self._home(tmp_path).absolute().as_posix()
        metadata_dir = tmp_path / ".kirocrew-skill-projection-metadata"
        metadata_dir.mkdir()
        for i, launch in enumerate(("a", "b")):
            stem = f"{self.PREFIX}{i:024x}"
            view = {
                "name": stem,
                "mcpServers": {
                    "broker": {
                        "command": "b",
                        "env": {"LAUNCH_ID": launch, "AIM_CREDS_AGENT_INJECTION": launch},
                    }
                },
            }
            (tmp_path / f"{stem}.json").write_text(json.dumps(view))
            (metadata_dir / f"{stem}.json").write_text(
                json.dumps(
                    {
                        "x-kirocrew-managed": "skill-view",
                        "x-kirocrew-home": home,
                        "x-kirocrew-agent": "ops",
                        "x-kirocrew-source": "/agents/ops.json",
                    }
                )
            )
        out = self._residue(self._run(tmp_path, monkeypatch, capsys))
        assert "broker.LAUNCH_ID" in out

    def test_a_churning_env_label_is_rendered_escaped(self, tmp_path, monkeypatch, capsys):
        """Labels come from spec files another program wrote: a control sequence in
        a server or key name must reach the terminal escaped, never executed."""
        from kiro_crew.agent_sdk.drivers import acp as acp_driver

        monkeypatch.setattr(
            acp_driver, "skill_view_churning_env_keys", lambda _d: ["srv.\x1b]0;pwn\x07KEY"]
        )
        out = self._run(tmp_path, monkeypatch, capsys)
        assert "\x1b" not in out and "\x07" not in out
        assert "AIM_CREDS_AGENT_INJECTION" not in out.split("KIROCREW_SKILL_VIEW_VOLATILE_ENV")[0]
        assert "KIROCREW_SKILL_VIEW_VOLATILE_ENV" in out


class TestRunDirCensus:
    """The run-directory census is read-only, down to the workspace root itself.

    ``workspace_root()`` creates the tree it resolves, which is right for a
    gateway about to spawn into it and wrong for a doctor on a host where no
    gateway ever ran: the report would leave a workspace behind as its only
    trace. The doctor resolves without creating and says there is nothing yet.
    Its two figures come from the sweep's own rule over this home's pid ledger;
    no pid's liveness is probed.
    """

    @staticmethod
    def _run(monkeypatch, capsys, root: Path, retained=frozenset()) -> str:
        from kiro_crew import session_pid

        monkeypatch.setenv("KIROCREW_WORKSPACE", str(root))
        monkeypatch.setattr(session_pid, "retained_gateway_pids", lambda: frozenset(retained))
        cli_doctor._doctor_run_dirs()
        return capsys.readouterr().out

    @staticmethod
    def _marked(root: Path, name: str, marker: str) -> Path:
        from kiro_crew.session_work_dir import RUN_DIR_MARKER

        work_dir = root / name
        (work_dir / ".kiro" / "settings").mkdir(parents=True)
        (work_dir / ".kiro" / "settings" / "cli.json").write_text("{}", encoding="utf-8")
        (work_dir / RUN_DIR_MARKER).write_text(marker, encoding="ascii")
        return work_dir

    def test_a_workspace_root_that_does_not_exist_is_not_created(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        root = tmp_path / "never-ran" / "kirocrew-workspace"
        out = self._run(monkeypatch, capsys, root)
        assert not root.exists(), "the doctor created the workspace tree"
        assert not root.parent.exists()
        assert "run dirs:" in out and "no workspace root yet" in out

    def test_both_figures_print_on_one_line_and_nothing_is_removed(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from kiro_crew.session_work_dir import RUN_DIR_MARKER, data_home_id

        root = tmp_path / "ws"
        legacy = root / "subagent_deadbeef" / ".kiro" / "settings"
        legacy.mkdir(parents=True)
        (legacy / "cli.json").write_text("{}", encoding="utf-8")
        self._marked(root, "subagent_00000001", "f" * 24 + "\n12345")
        self._marked(root, "subagent_00000002", "garbled")
        self._marked(root, "subagent_00000003", f"{data_home_id()}\n12345")
        self._marked(root, "subagent_00000004", f"{data_home_id()}\n23456")
        before = sorted(p.name for p in root.rglob("*"))
        out = self._run(monkeypatch, capsys, root, retained={12345})
        shown = os.path.realpath(root)
        (line,) = [ln for ln in out.splitlines() if "run dirs:" in ln]
        assert line.startswith("  run dirs:    ⚠️ ")
        assert f"under {shown}: 1 run director(ies) carry no {RUN_DIR_MARKER} marker" in line
        assert "3 marked director(ies) this data home cannot reclaim" in line
        assert "no workspace root yet" not in out
        assert sorted(p.name for p in root.rglob("*")) == before

    def test_an_unmarked_backlog_names_the_remedy_and_the_cap_makes_a_floor(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from kiro_crew import session_work_dir

        root = tmp_path / "ws"
        for n in range(1, 4):
            (root / f"subagent_{n:08x}" / ".kiro" / "settings").mkdir(parents=True)
        out = self._run(monkeypatch, capsys, root)
        assert "✅ under" in out and "3 run director(ies) carry no" in out
        assert "With the gateway stopped" not in out
        monkeypatch.setattr(cli_doctor, "_RUN_DIR_BACKLOG_WARN", 2)
        out = self._run(monkeypatch, capsys, root)
        assert "⚠️ " in out and "With the gateway stopped, move directories matching" in out
        assert (
            "hold nothing beyond .kiro/settings/cli.json,"
            " .kiro/settings/.kirocrew-cli-settings.lock and an empty .kiro/agents"
        ) in out
        original = session_work_dir.count_run_dirs
        monkeypatch.setattr(
            session_work_dir,
            "count_run_dirs",
            lambda path, **kw: original(path, max_entries=2, **kw),
        )
        out = self._run(monkeypatch, capsys, root)
        assert "2+ run director(ies) carry no" in out and "0+ marked" in out

    @pytest.mark.parametrize("pinned", [True, False])
    def test_an_unmarked_memory_consolidation_backlog_is_counted_on_either_walk(
        self, pinned: bool, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The by-name walk never marks these folders, so the census is where they surface."""
        from kiro_crew import session_work_dir

        monkeypatch.setattr(session_work_dir.pinned_fs, "supports_pinned_walk", lambda: pinned)
        root = tmp_path / "ws"
        for n in range(1, 4):
            name = f"memory-consolidation_work_{n:032x}"
            (root / name / ".kiro" / "settings").mkdir(parents=True)
        before = sorted(p.name for p in root.rglob("*"))
        monkeypatch.setattr(cli_doctor, "_RUN_DIR_BACKLOG_WARN", 2)
        out = self._run(monkeypatch, capsys, root)
        (line,) = [ln for ln in out.splitlines() if "run dirs:" in ln]
        assert "⚠️ " in line and "3 run director(ies) carry no" in line
        assert "(left by a build that did not mark that kind)" in line
        assert "With the gateway stopped, move directories matching" in out
        assert session_work_dir.DERIVED_NAME_RE.pattern in out
        assert r"memory\-consolidation" in out
        assert sorted(p.name for p in root.rglob("*")) == before

    def test_a_root_with_nothing_the_sweep_cannot_reclaim_is_clean(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        from kiro_crew.session_work_dir import data_home_id

        root = tmp_path / "ws"
        self._marked(root, "subagent_00000001", f"{data_home_id()}\n12345")
        out = self._run(monkeypatch, capsys, root)
        assert "✅ no run directories left behind that the sweep cannot reclaim" in out

    def test_an_unreadable_ledger_skips_the_census(self, tmp_path: Path, monkeypatch, capsys):
        from kiro_crew import session_pid

        root = tmp_path / "ws"
        self._marked(root, "subagent_00000001", "garbled")
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(root))
        monkeypatch.setattr(
            session_pid,
            "retained_gateway_pids",
            lambda: (_ for _ in ()).throw(OSError("io")),
        )
        cli_doctor._doctor_run_dirs()
        out = capsys.readouterr().out
        assert "⚠️  the session pid ledger cannot be read; census skipped" in out

    def test_a_content_keep_is_reported_as_the_third_figure(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A permitted-marked folder the sweep keeps for its contents prints as *kept*."""
        from kiro_crew.session_work_dir import data_home_id

        root = tmp_path / "ws"
        kept = self._marked(root, "subagent_00000001", f"{data_home_id()}\n23456")
        (kept / "notes.txt").write_text("mine", encoding="utf-8")
        before = sorted(p.name for p in root.rglob("*"))
        out = self._run(monkeypatch, capsys, root)
        (line,) = [ln for ln in out.splitlines() if "run dirs:" in ln]
        assert line.startswith("  run dirs:    ⚠️ ")
        assert "1 marked director(ies) the sweep keeps for what they hold" in line
        assert "no run directories left behind" not in out
        assert sorted(p.name for p in root.rglob("*")) == before
