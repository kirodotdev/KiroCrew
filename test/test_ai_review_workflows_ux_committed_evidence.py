"""Review-workflow tests for the screenshot evidence a PR commits to its tree: what the
UX and design lanes admit through the committed-evidence script, and what it refuses.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    ATTACHMENT_SCRIPT,
    ATTACHMENT_SOURCE_LINE,
    DESIGN_EVIDENCE_LANES,
    DESIGN_EVIDENCE_STEP,
    DESIGN_LANES,
    FORK_ATTACHMENT_STEP,
    ROOT,
    UX_REVIEW_STEP,
    UxEvidenceGateHarness,
    _flat,
    _step,
    _step_env,
    _step_script,
    _workflow,
)


class TestUxReviewReadsTheScreenshotsBlindFirstCommittedEvidence(UxEvidenceGateHarness):
    """The evidence a PR commits to its tree reaches the reviewer only through the
    committed-evidence script's mode, type and budget checks.
    """

    # --- Evidence a contributor with no write access COMMITS -----------------
    # `gh pr create|edit --attach` uploads through an endpoint that answers READ
    # and TRIAGE permission with a 404 (cli/cli#14302), so a fork contributor
    # has no CLI path to a description attachment at all. The committed
    # convention is theirs, and the fork lane -- which never checks the fork
    # head out -- has to read those blobs out of the object store for the
    # reviewer, or their UI change is reviewed with no evidence.

    COMMITTED_SCRIPT = ".github/scripts/pr-committed-evidence.sh"
    COMMITTED_SOURCE_LINE = '. "$GITHUB_WORKSPACE/.github/scripts/pr-committed-evidence.sh"'

    def _repo_committing_evidence(self, tmp_path: Path) -> tuple[Path, str]:
        """A repository whose head changes a UI file and commits its evidence
        under both committed-screenshot conventions, plus the things that must
        NOT reach the reviewer: a text file, and a tracked SYMLINK pointing out
        of the tree (mode 120000), which is how a PR would otherwise aim the
        reviewer at an arbitrary file on the runner."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "README").write_text("base\n")
        self._git(repo, "add", "README")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")
        (repo / "website").mkdir()
        (repo / "website" / "App.tsx").write_text("x")
        shots = repo / "temp-screenshots" / "topic"
        shots.mkdir(parents=True)
        # An author-named file: the copy handed to the reviewer must not carry
        # this name, which would prime it with the author's own vocabulary.
        (shots / "pinned-turn-chip.png").write_bytes(self.PNG)
        (shots / "walkthrough.webm").write_bytes(self.WEBM)
        (shots / "notes.txt").write_bytes(self.TEXT)
        legacy = repo / ".github" / "screenshots" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / "old.png").write_bytes(self.PNG)
        # `git add -f`: both directories are gitignored in this repository, and
        # that is exactly the command the fallback documents.
        self._git(repo, "add", "-fA")
        # A symlink entry written through the index, so the fixture does not
        # depend on the platform's symlink support.
        target = tmp_path / "symlink-target"
        target.write_text("../../../../etc/passwd")
        oid = self._git(repo, "hash-object", "-w", "--", str(target))
        self._git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            "120000",
            oid,
            "temp-screenshots/topic/secrets.png",
        )
        self._git(repo, "commit", "-qm", "head")
        return repo, base

    def test_the_fork_lane_reads_the_media_a_no_write_access_contributor_commits(
        self, tmp_path: Path
    ) -> None:
        """Execute the ACTUAL fork evidence step against a PR that commits its
        screenshots. Each image reaches the reviewer as an index-named copy
        (never the author's filename), the recording is listed, the map records
        the repository path as the origin, and the text file is skipped by its
        bytes. Without this the fork lane reviewed a UI change with nothing to
        look at whenever the author could not attach -- which is every
        contributor whose permission is READ."""
        repo, base = self._repo_committing_evidence(tmp_path)
        shot_list, shot_map, clip_list, _output, attach_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body="No attachments here.\n",
            fixtures={},
            lane="fork-ux-review.yml",
        )
        kept = sorted(p.name for p in attach_dir.iterdir())
        assert kept == ["attachment-01.png", "attachment-02.png"], kept
        assert [Path(line).name for line in shot_list.splitlines()] == kept
        # The origin is the repository path; the copy's name carries only the
        # order and the format.
        origins = dict(line.split("\t") for line in shot_map.splitlines())
        assert set(origins) == set(kept)
        assert sorted(origins.values()) == [
            ".github/screenshots/legacy/old.png",
            "temp-screenshots/topic/pinned-turn-chip.png",
        ]
        assert "pinned-turn-chip" not in shot_list
        assert clip_list.splitlines() == ["temp-screenshots/topic/walkthrough.webm"]
        assert "notes.txt" not in shot_list and "notes.txt" not in clip_list
        assert "Committed evidence:" in self._evidence_stdout
        assert "2 image(s) kept" in self._evidence_stdout

    def test_a_committed_symlink_is_refused_by_its_mode_before_its_bytes_are_read(
        self, tmp_path: Path
    ) -> None:
        """A tracked symlink under temp-screenshots/ is how a fork PR would aim
        the reviewer at an arbitrary path. The tree entry's MODE refuses it, so
        the decision never depends on what the link resolves to -- and because
        the bytes come from `git cat-file`, the link is never created on disk in
        the first place."""
        repo, base = self._repo_committing_evidence(tmp_path)
        shot_list, shot_map, _clips, _output, _attach_dir = self._run_evidence_gate(
            repo, base, tmp_path, body="", fixtures={}, lane="fork-ux-review.yml"
        )
        assert "secrets.png" not in shot_list and "secrets.png" not in shot_map
        assert "etc/passwd" not in shot_list and "etc/passwd" not in shot_map
        assert (
            "::warning::SKIPPED (not a regular file at HEAD, mode 120000): "
            "temp-screenshots/topic/secrets.png" in self._evidence_stdout
        ), self._evidence_stdout
        # Nothing was materialized from the fork's tree: the step wrote only
        # its own index-named copies.
        assert not (repo / "temp-screenshots" / "topic" / "secrets.png").is_symlink()

    def test_rejected_committed_media_still_consumes_the_blob_read_budget(
        self, tmp_path: Path
    ) -> None:
        """Rejected bytes cannot make fork-controlled object reads unbounded."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "README").write_text("base\n")
        self._git(repo, "add", "README")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")
        evidence = repo / "temp-screenshots"
        evidence.mkdir()
        for name in ("a.png", "b.png", "c.png"):
            (evidence / name).write_text("not an image\n")
        self._git(repo, "add", "-fA")
        self._git(repo, "commit", "-qm", "head")

        self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body="",
            fixtures={},
            max_shots="1",
            max_clips="1",
            lane="fork-ux-review.yml",
        )

        assert self._evidence_stdout.count("SKIPPED (mime text/plain)") == 2
        assert (
            "TRUNCATED: more than 2 pieces of evidence; not read: " "temp-screenshots/c.png"
        ) in self._evidence_stdout
        assert (
            "Committed evidence: 3 media path(s) added or changed under "
            "temp-screenshots/ or .github/screenshots/, 0 image(s) kept, 2 skipped."
        ) in self._evidence_stdout

    def test_a_description_attachment_is_never_displaced_by_a_committed_file(
        self, tmp_path: Path
    ) -> None:
        """Attachments are the normal home for evidence, so they are read FIRST
        and numbered first: the two sources share one MAX_SHOTS cap, and a
        committed file must never push a downloaded attachment out of it."""
        repo, base = self._repo_committing_evidence(tmp_path)
        url = "https://github.com/user-attachments/assets/0f3b2c1a-1111-4bcd-9e8f-0123456789ab"
        shot_list, shot_map, _clips, _output, _attach_dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body=f"![after]({url})\n",
            fixtures={url: self.JPEG},
            max_shots="1",
            lane="fork-ux-review.yml",
        )
        copies = [
            Path(line).name for line in shot_list.splitlines() if not line.startswith("TRUNC")
        ]
        assert copies == ["attachment-01.jpg"]
        origins = dict(line.split("\t") for line in shot_map.splitlines())
        assert origins["attachment-01.jpg"] == url
        # The committed images did not fit under the cap, and the list says so
        # rather than dropping them silently.
        assert "TRUNCATED" in shot_list

    def test_the_committed_script_runs_under_the_bash_3_of_macos(self) -> None:
        """The macOS shard runs the fork evidence step under /bin/bash 3.2,
        which has no `${var,,}` and aborts the sourced script on it with `bad
        substitution` -- taking the whole evidence step down. The extension
        allowlist lower-cases through `tr`, the way ux-review.yml does."""
        script = (ROOT / self.COMMITTED_SCRIPT).read_text(encoding="utf-8")
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(
            r"\$\{[A-Za-z_]+(,,|\^\^)\}", code
        ), "case-folding parameter expansion is bash 4 only; lower-case with tr"
        assert "LC_ALL=C tr '[:upper:]' '[:lower:]'" in code

    def test_a_glob_metacharacter_in_a_committed_name_names_that_one_entry(
        self, tmp_path: Path
    ) -> None:
        """`git ls-tree` takes a pathspec while `git cat-file` takes an exact
        path, so the mode and size gates must be pinned to the same tree entry
        `cat-file` reads: with `:(literal)` a `*`, a `[`, a `!` or a `)` in a
        committed name is that name, not a pattern. Four legitimate images
        under such names all reach the reviewer, none is dropped as `mode
        unknown`, and each origin is the exact repository path. The entries
        are written through the index, so the test does not depend on the
        platform's filesystem permitting these characters."""
        repo, base = self._repo_committing_evidence(tmp_path)
        hostile = ["!a.png", "*.png", "chip[1].png", "a)b:c?.png"]
        blob = tmp_path / "hostile-blob"
        blob.write_bytes(self.PNG)
        oid = self._git(repo, "hash-object", "-w", "--", str(blob))
        for name in hostile:
            self._git(
                repo,
                # These names are invalid WIN32 paths, and git's Windows build
                # refuses them in the index under `core.protectNTFS`, which is
                # on by default there. The entry is what this test needs, not a
                # file: a fork on Linux can commit such a name, so CI must be
                # able to build the tree that proves the reader handles it.
                "-c",
                "core.protectNTFS=false",
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                oid,
                f"temp-screenshots/topic/{name}",
            )
        self._git(repo, "commit", "-qm", "hostile names")
        script = (ROOT / self.COMMITTED_SCRIPT).read_text(encoding="utf-8")
        assert 'git ls-tree -l "$HEAD_SHA" -- ":(literal)$path"' in script

        shot_list, shot_map, _clips, _output, attach_dir = self._run_evidence_gate(
            repo, base, tmp_path, body="", fixtures={}, lane="fork-ux-review.yml"
        )
        origins = dict(line.split("\t") for line in shot_map.splitlines())
        for name in hostile:
            assert f"temp-screenshots/topic/{name}" in origins.values(), (
                name,
                self._evidence_stdout,
            )
        assert "mode unknown" not in self._evidence_stdout, self._evidence_stdout
        assert len(shot_list.splitlines()) == 2 + len(hostile)
        assert len(list(attach_dir.iterdir())) == 2 + len(hostile)

    def test_a_committed_name_with_a_control_character_reaches_no_sink(
        self, tmp_path: Path
    ) -> None:
        """Git permits a newline, a tab or a carriage return in a tracked
        filename, and the fork controls the names it commits. The map and the
        clip list are presented to the reviewer as files the workflow wrote, so
        a newline inside a recorded path forges extra records in them, and the
        same byte in a `::warning::` line forges a workflow command. Such a name
        is refused before the first line that interpolates it: neither data
        file gains a record, the log shows it only `%q`-quoted, and the valid
        media beside it are still read."""
        repo, base = self._repo_committing_evidence(tmp_path)
        forged = [
            ("temp-screenshots/topic/a\nforged.gif", self.GIF),
            ("temp-screenshots/topic/c\tforged.png", self.PNG),
            ("temp-screenshots/topic/e\rforged.png", self.PNG),
        ]
        for path, payload in forged:
            blob = tmp_path / f"blob-{len(path)}"
            blob.write_bytes(payload)
            oid = self._git(repo, "hash-object", "-w", "--", str(blob))
            # `core.protectNTFS` (default on in git for Windows) refuses a
            # control character in an index entry; a fork on Linux can commit
            # one, which is the whole point of the guard under test.
            self._git(
                repo,
                "-c",
                "core.protectNTFS=false",
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                oid,
                path,
            )
        self._git(repo, "commit", "-qm", "forged names")

        shot_list, shot_map, clip_list, _output, _attach_dir = self._run_evidence_gate(
            repo, base, tmp_path, body="", fixtures={}, lane="fork-ux-review.yml"
        )
        # The clip list holds exactly the one legitimate recording: no line
        # from the GIF's name, split or whole.
        assert clip_list.splitlines() == ["temp-screenshots/topic/walkthrough.webm"], clip_list
        # Every map record is one name, one tab, one origin; the tab-bearing
        # name would have made a third column and the newline a fifth row.
        rows = shot_map.splitlines()
        assert len(rows) == 2 and all(row.count("\t") == 1 for row in rows), shot_map
        assert "forged" not in shot_map and "forged" not in shot_list
        # No raw control character from a path reached the log, and the
        # refusal names each file quoted.
        stdout = self._evidence_stdout
        assert "\nforged" not in stdout and "\tforged" not in stdout
        assert "\rforged" not in stdout
        assert (
            "::warning::SKIPPED (path contains a control character): "
            "$'temp-screenshots/topic/a\\nforged.gif'"
        ) in stdout, stdout
        assert "$'temp-screenshots/topic/c\\tforged.png'" in stdout
        assert "$'temp-screenshots/topic/e\\rforged.png'" in stdout
        assert "7 media path(s) added" in stdout and "2 image(s) kept, 4 skipped." in stdout, stdout

    def test_a_committed_format_the_reviewer_cannot_read_is_named_not_dropped(
        self, tmp_path: Path
    ) -> None:
        """A committed file the script declines has to reach its author.

        An author who commits `after.svg` has followed the instruction to commit
        evidence, and the reviewer cannot read SVG. Declining it in silence
        leaves them with a summary saying nothing was found and a lane blocking
        them for supplying nothing, which is the worst of both. So a name in an
        unreadable MEDIA format is counted and named with its reason, while a
        sidecar that was never evidence (a provenance JSON, a README, a
        dotfile) stays silent -- otherwise every run warns about files nobody
        offered as a screenshot. A refused name must also cost no object read:
        it is declined before the blob is materialized, so it cannot consume the
        read budget.
        """
        repo, base = self._repo_committing_evidence(tmp_path)
        extra = {
            "temp-screenshots/topic/after.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            "temp-screenshots/topic/After.BMP": b"BM\x00\x00\x00\x00",
            "temp-screenshots/topic/provenance.json": b'{"ok": true}\n',
            "temp-screenshots/topic/README": b"why these files exist\n",
            # A hidden PARENT directory must not hide real media: the sort is on
            # the basename, not the path.
            "temp-screenshots/.hidden/shot.png": self.PNG,
        }
        for path, payload in extra.items():
            blob = tmp_path / f"extra-{len(path)}-{path.rsplit('/', 1)[-1]}"
            blob.write_bytes(payload)
            oid = self._git(repo, "hash-object", "-w", "--", str(blob))
            self._git(repo, "update-index", "--add", "--cacheinfo", "100644", oid, path)
        self._git(repo, "commit", "-qm", "unreadable formats beside sidecars")

        shot_list, shot_map, _clips, _output, _attach_dir = self._run_evidence_gate(
            repo, base, tmp_path, body="", fixtures={}, lane="fork-ux-review.yml"
        )
        stdout = self._evidence_stdout
        # Named, with the reason the author needs to act on.
        assert (
            "::warning::SKIPPED (SVG is opened as markup, not pixels; export the "
            "image as PNG): temp-screenshots/topic/after.svg" in stdout
        ), stdout
        assert (
            "::warning::SKIPPED (not a format the reviewer reads; commit PNG, JPEG, "
            "WebP, GIF, MP4, MOV or WebM): temp-screenshots/topic/After.BMP" in stdout
        ), stdout
        # Sidecars were never offered as screenshots, so they stay silent.
        for quiet in ("provenance.json", "README", "notes.txt"):
            assert quiet not in stdout, quiet
        # The hidden parent directory did not hide its media.
        assert "temp-screenshots/.hidden/shot.png" in shot_map

    def test_a_refused_committed_format_consumes_no_read_budget(self, tmp_path: Path) -> None:
        """The refusal happens before the blob is materialized, so an unreadable
        name cannot push a real image out of the shared cap. Under a cap of one
        image the PNG is still kept, and the `.svg` is never reported as
        TRUNCATED -- it was declined, not crowded out."""
        repo, base = self._repo_committing_evidence(tmp_path)
        blob = tmp_path / "svg-blob"
        blob.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        oid = self._git(repo, "hash-object", "-w", "--", str(blob))
        # Sorts before `pinned-turn-chip.png`, so it is listed first and would
        # take the slot if it were read.
        self._git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            "100644",
            oid,
            "temp-screenshots/topic/aaa.svg",
        )
        self._git(repo, "commit", "-qm", "an svg ahead of the real media")

        shot_list, _map, _clips, _output, _dir = self._run_evidence_gate(
            repo,
            base,
            tmp_path,
            body="",
            fixtures={},
            max_shots="1",
            max_clips="1",
            lane="fork-ux-review.yml",
        )
        assert "aaa.svg" not in shot_list
        assert "not read: temp-screenshots/topic/aaa.svg" not in self._evidence_stdout
        kept = [ln for ln in shot_list.splitlines() if not ln.startswith("TRUNC")]
        assert len(kept) == 1, shot_list

    def test_a_screenshot_that_was_only_moved_is_named_as_the_base_not_admitted(
        self, tmp_path: Path
    ) -> None:
        """A `git mv` of a screenshot the base already holds is a rename to
        git, and rename detection is on by default, so a listing filtered to
        additions and modifications never sees the destination: the moved file
        is neither counted nor warned about, and the author is told nothing was
        found. The listing here admits every status a path at HEAD can carry and
        decides the rename by its status: bytes already on the base show the
        base's rendering, not this revision's, so an unchanged move is refused
        by name with both paths and counted, while a move whose bytes changed is
        a modified file under a new name and is read like any other. The
        summary counts the moved file and says what it was."""
        repo = tmp_path / "repo"
        shots = repo / "temp-screenshots" / "topic"
        shots.mkdir(parents=True)
        self._git(repo, "init", "-q")
        # Two screenshots on the BASE: one the head moves untouched, one it
        # moves and re-captures. The second is long enough that a change to
        # its tail leaves git a high similarity score: a rename, not A + D.
        (shots / "same.png").write_bytes(self.PNG)
        (shots / "before.png").write_bytes(self.PNG + bytes(range(256)) * 16)
        self._git(repo, "add", "-fA")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")
        (repo / "website").mkdir()
        (repo / "website" / "App.tsx").write_text("x")
        panel = repo / "temp-screenshots" / "panel"
        panel.mkdir()
        self._git(repo, "mv", "temp-screenshots/topic/same.png", "temp-screenshots/panel/same.png")
        self._git(
            repo, "mv", "temp-screenshots/topic/before.png", "temp-screenshots/panel/after.png"
        )
        (panel / "after.png").write_bytes(self.PNG + bytes(range(256)) * 15 + b"Z" * 256)
        (panel / "fresh.png").write_bytes(self.JPEG)
        self._git(repo, "add", "-fA")
        self._git(repo, "commit", "-qm", "head")
        listing = self._git(
            repo, "diff", "-M", "--name-status", "--diff-filter=d", f"{base}...HEAD"
        )
        assert "R100\ttemp-screenshots/topic/same.png\ttemp-screenshots/panel/same.png" in listing
        assert "\ttemp-screenshots/topic/before.png\ttemp-screenshots/panel/after.png" in listing
        assert "R100\ttemp-screenshots/topic/before.png" not in listing, listing

        shot_list, shot_map, _clips, _output, _dir = self._run_evidence_gate(
            repo, base, tmp_path, body="", fixtures={}, lane="fork-ux-review.yml"
        )
        stdout = self._evidence_stdout
        origins = dict(line.split("\t") for line in shot_map.splitlines())
        # The re-captured file and the new file reach the reviewer; the moved
        # file does not, under either of its names.
        assert sorted(origins.values()) == [
            "temp-screenshots/panel/after.png",
            "temp-screenshots/panel/fresh.png",
        ], shot_map
        assert "same.png" not in shot_list and "same.png" not in shot_map
        assert (
            "::warning::SKIPPED (moved from temp-screenshots/topic/same.png with no change "
            "in its bytes, so it shows the base, not this revision; capture the screenshot "
            "again): temp-screenshots/panel/same.png"
        ) in stdout, stdout
        assert "not read: temp-screenshots/panel/same.png" not in stdout
        assert (
            "Committed evidence: 3 media path(s) added or changed under temp-screenshots/ "
            "or .github/screenshots/, 2 image(s) kept, 1 skipped. 1 of the skipped were moved "
            "from the base without a change in bytes and show the base, not this revision."
        ) in stdout, stdout
        # The listing itself is the contract: every status but a deletion
        # supplies a candidate, rename detection is asked for rather than
        # inherited from the runner's config, and the status letter reaches
        # the loop so the rename is decided there.
        script = (ROOT / self.COMMITTED_SCRIPT).read_text(encoding="utf-8")
        assert "git diff -z -M --name-status --diff-filter=d" in script
        assert "--diff-filter=AM" not in script
        assert "R100|C100)" in script

    def test_a_failed_enumeration_fails_the_step_instead_of_reading_as_no_media(
        self, tmp_path: Path
    ) -> None:
        """`git diff` failing is not "this PR commits no media": an empty
        listing would make the design lane report evidence missing that the
        author supplied. The step fails with an error annotation naming the
        re-run, the same contract the attachment script applies to a
        description it could not read, and the summary line that means "the
        enumeration ran" is not printed."""
        repo, _base = self._repo_committing_evidence(tmp_path)
        missing = "0123456789abcdef0123456789abcdef01234567"
        _shots, _map, _clips, output, _dir = self._run_evidence_gate(
            repo,
            missing,
            tmp_path,
            body="",
            fixtures={},
            lane="fork-ux-review.yml",
            expect_failure=True,
        )
        stdout = self._evidence_stdout
        assert (
            f"::error::Could not enumerate the media committed between {missing} and HEAD "
            "(git diff failed, see above), so the committed evidence cannot be collected; "
            "re-run the workflow."
        ) in stdout, stdout
        assert "Committed evidence:" not in stdout, stdout
        # `exit 1` from a SOURCED script skips the rest of the caller's step,
        # so the output the fork lanes turn into a FAILED check-run has to be
        # written before leaving. Without it the step is red while the
        # check-run resolves NEUTRAL, which PR readiness scores as a pass.
        assert "unfetched=true" in output, output

    def test_both_fork_lanes_read_the_committed_media_from_the_object_store(self) -> None:
        """Static contract for the fallback. Both fork lanes source the shared
        committed-evidence script AFTER the attachment script, pass it the
        pinned head, and read blobs with `git cat-file` -- never by checking the
        fork head out. The UX prompt tells the reviewer a committed image counts
        the same as an attached one, because this script puts it in front of
        the reviewer."""
        script = (ROOT / self.COMMITTED_SCRIPT).read_text(encoding="utf-8")
        assert 'git cat-file blob "$HEAD_SHA:$path"' in script
        assert 'mime="$(file --mime-type -b -- "$tmp")"' in script
        # Mode-gated before any read, with the permanent-history blob cap.
        assert "100644|100755) ;;" in script
        assert 'if [ "${size:-0}" -gt 10485760 ]; then' in script
        assert "over the 10 MB ceiling" in script
        assert "checkout" in script, "the script says why it reads objects, not files"
        for lane, step in (
            ("fork-ux-review.yml", FORK_ATTACHMENT_STEP),
            ("fork-design-review.yml", "Collect rendered evidence"),
        ):
            step_script = _step_script(_workflow(lane), step)
            assert ATTACHMENT_SOURCE_LINE in step_script
            assert self.COMMITTED_SOURCE_LINE in step_script
            assert step_script.index(ATTACHMENT_SOURCE_LINE) < step_script.index(
                self.COMMITTED_SOURCE_LINE
            ), "attachments are read first, so the shared cap cannot drop one"
            env = _step_env(lane, step)
            assert env["HEAD_SHA"] == "${{ steps.pr.outputs.head_sha }}"
            assert env["BASE_SHA"] == "${{ steps.pr.outputs.base_sha }}"
        fork_prompt = _flat(_step("fork-ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "A committed image counts the same" in fork_prompt
        assert "not materialized here" not in fork_prompt
        assert "a committed-only image cannot close it" not in fork_prompt
        design = _flat(_workflow("fork-design-review.yml"))
        assert "committed images this revision adds or changes: not checked" not in design

    def test_the_same_repo_design_lane_sources_the_shared_script_and_keeps_no_loop_of_its_own(
        self,
    ) -> None:
        """The same-repo design lane once re-spelled the committed-media
        collection inline: its own symlink gate, its own mime table, no size
        cap, no control-character guard, no skip counting -- and a listing by
        `--diff-filter=AM` that lost a renamed screenshot, beside a shipped
        script whose header says that is the shape that loses one. The rule
        this pins is structural: there is ONE admission contract, the shared
        script, and the same-repo lane sources it AFTER the attachment script
        like the fork lanes do. Nothing of the inline copy may return -- not
        the AM listing, not a `file` call on a working-tree path, not a
        `-L` test, not a mime table -- and the mime table exists in exactly
        two files under .github/: the two evidence scripts. The two design
        lanes' evidence-writing blocks are pinned byte-identical, the way
        their calibration blocks are, so the report cannot drift by lane."""
        raw = _workflow("design-review.yml")
        script = _step_script(raw, DESIGN_EVIDENCE_STEP)
        assert ATTACHMENT_SOURCE_LINE in script
        assert self.COMMITTED_SOURCE_LINE in script
        assert script.index(ATTACHMENT_SOURCE_LINE) < script.index(self.COMMITTED_SOURCE_LINE)
        # The inline copy, gone in every one of its parts.
        assert "--diff-filter=AM" not in raw
        assert "--diff-filter" not in script
        assert 'file --mime-type -b -- "$path"' not in script
        assert '[ -L "$path" ]' not in script and "[ ! -f " not in script
        assert 'committed=""' not in script
        assert "image/webp" not in raw and "video/quicktime" not in raw
        # The one mime table lives in the two scripts and nowhere else.
        tables = sorted(
            str(p.relative_to(ROOT)).replace(os.sep, "/")
            for p in (ROOT / ".github").rglob("*")
            if p.is_file() and "image/webp" in p.read_text(encoding="utf-8", errors="ignore")
        )
        assert tables == [ATTACHMENT_SCRIPT, self.COMMITTED_SCRIPT], tables
        # The blobs are read at the pull request's head, listed against its
        # base -- the same inputs the fork lanes hand the script.
        env = _step_env("design-review.yml", DESIGN_EVIDENCE_STEP)
        assert env["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
        assert env["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
        assert env["NAME_STEM"] == "attachment"
        # The report the prompt reads still says the committed media was
        # typed by its bytes, and it is the same report in both design lanes.
        blocks = {}
        for lane in DESIGN_EVIDENCE_LANES:
            lane_script = _step_script(_workflow(lane), DESIGN_EVIDENCE_STEP)
            start = lane_script.index('attached_images="$n"')
            end = lane_script.index('echo "Rendered evidence:')
            blocks[lane] = lane_script[start:end]
            assert "read from the object store and typed by their bytes: $committed_kept" in (
                blocks[lane]
            ), lane
            assert "typed by their bytes: $((clips - attached_clips))" in blocks[lane], lane
        assert (
            blocks["design-review.yml"] == blocks["fork-design-review.yml"]
        ), "the two design lanes' evidence blocks drifted apart"

    def _repo_renaming_its_evidence(self, tmp_path: Path) -> tuple[Path, str]:
        """The rename case the inline `--diff-filter=AM` listing lost, beside
        the things the fenced fix refuses. The base tracks two screenshots; the
        head `git mv`s one untouched and re-captures the other under a new
        name (a rename to git, with a high similarity score), adds a fresh
        image whose NAME says PNG but whose BYTES are JPEG, commits a text file
        under an image name, a recording, and a tracked SYMLINK written through
        the index (mode 120000) pointing out of the tree."""
        repo = tmp_path / "repo"
        shots = repo / "temp-screenshots" / "topic"
        shots.mkdir(parents=True)
        self._git(repo, "init", "-q")
        (shots / "same.png").write_bytes(self.PNG)
        (shots / "before.png").write_bytes(self.PNG + bytes(range(256)) * 16)
        self._git(repo, "add", "-fA")
        self._git(repo, "commit", "-qm", "base")
        base = self._git(repo, "rev-parse", "HEAD")
        (repo / "website").mkdir()
        (repo / "website" / "App.tsx").write_text("x")
        panel = repo / "temp-screenshots" / "panel"
        panel.mkdir()
        self._git(repo, "mv", "temp-screenshots/topic/same.png", "temp-screenshots/panel/same.png")
        self._git(
            repo, "mv", "temp-screenshots/topic/before.png", "temp-screenshots/panel/after.png"
        )
        (panel / "after.png").write_bytes(self.PNG + bytes(range(256)) * 15 + b"Z" * 256)
        (panel / "fresh.png").write_bytes(self.JPEG)
        (panel / "notes.png").write_bytes(self.TEXT)
        (panel / "walkthrough.webm").write_bytes(self.WEBM)
        self._git(repo, "add", "-fA")
        target = tmp_path / "symlink-target"
        target.write_text("../../../../etc/passwd")
        oid = self._git(repo, "hash-object", "-w", "--", str(target))
        self._git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            "120000",
            oid,
            "temp-screenshots/panel/secrets.png",
        )
        self._git(repo, "commit", "-qm", "head")
        return repo, base

    @pytest.mark.parametrize("lane", DESIGN_EVIDENCE_LANES)
    def test_both_design_lanes_admit_a_renamed_screenshot_and_refuse_by_mode_and_bytes(
        self, tmp_path: Path, lane: str
    ) -> None:
        """Execute the ACTUAL "Collect rendered evidence" step of each design
        lane against a PR that renames its screenshots. First the fixture is
        shown to be the losing case: `git diff --name-only --diff-filter=AM`
        does not list the re-captured file's destination. Then the step
        proves it reaches the reviewer anyway -- the evidence file counts it,
        the map names its repository path -- while the unchanged move is
        refused by name as the base's rendering, the symlink is refused by its
        MODE before any bytes are read, the text file under an image name is
        refused by its BYTES, the JPEG under a `.png` name is kept under the
        extension its bytes earn, and the recording is listed. Every count in
        the file matches what the lists hold. That is the fenced guarantee --
        no path is called rendered evidence without being established as a
        regular, byte-typed media file -- now held by execution rather than by
        the text of an inline loop, and held identically by both lanes."""
        repo, base = self._repo_renaming_its_evidence(tmp_path)
        lost = self._git(repo, "diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD")
        assert "temp-screenshots/panel/after.png" not in lost.splitlines(), lost
        assert "temp-screenshots/panel/fresh.png" in lost.splitlines(), lost

        shot_list, shot_map, clip_list, _output, dest_dir = self._run_evidence_gate(
            repo, base, tmp_path, body="No attachments.\n", fixtures={}, lane=lane
        )
        stdout = self._evidence_stdout
        evidence = self._evidence_file.read_text(encoding="utf-8")
        origins = dict(line.split("\t") for line in shot_map.splitlines())
        assert sorted(origins.values()) == [
            "temp-screenshots/panel/after.png",
            "temp-screenshots/panel/fresh.png",
        ], shot_map
        # Index-named copies, extension from the bytes: the `.png` holding
        # JPEG is a .jpg to the reviewer.
        assert sorted(origins) == ["attachment-01.png", "attachment-02.jpg"], shot_map
        assert sorted(p.name for p in dest_dir.iterdir()) == sorted(origins)
        assert clip_list.splitlines() == ["temp-screenshots/panel/walkthrough.webm"]
        # The refusals, each by the gate that decides it.
        assert (
            "::warning::SKIPPED (moved from temp-screenshots/topic/same.png with no change "
            "in its bytes, so it shows the base, not this revision; capture the screenshot "
            "again): temp-screenshots/panel/same.png"
        ) in stdout, stdout
        assert (
            "::warning::SKIPPED (not a regular file at HEAD, mode 120000): "
            "temp-screenshots/panel/secrets.png"
        ) in stdout, stdout
        assert "::warning::SKIPPED (mime text/plain): temp-screenshots/panel/notes.png" in stdout
        assert not (repo / "temp-screenshots" / "panel" / "secrets.png").is_symlink()
        # The file the prompt calls the predicate: counts that match the
        # lists, the admitted copies, the recording, and none of the refused.
        assert "attachments downloaded and typed as images: 0" in evidence
        assert "attachments downloaded and typed as recordings: 0" in evidence
        assert (
            "committed images this revision adds or changes, read from the object store "
            "and typed by their bytes: 2"
        ) in evidence, evidence
        assert (
            "committed recordings this revision adds or changes, read from the object store "
            "and typed by their bytes: 1"
        ) in evidence, evidence
        assert "(presence unconfirmed, not absent): 0" in evidence
        for copy in shot_list.splitlines():
            assert copy in evidence, (copy, evidence)
        assert "temp-screenshots/panel/walkthrough.webm" in evidence
        for refused in ("same.png", "secrets.png", "etc/passwd", "notes.png", "before.png"):
            assert refused not in evidence, (refused, evidence)
        assert "(none)" not in evidence
        assert (
            "Committed evidence: 6 media path(s) added or changed under temp-screenshots/ or "
            ".github/screenshots/, 2 image(s) kept, 3 skipped. 1 of the skipped were moved from "
            "the base without a change in bytes and show the base, not this revision."
        ) in stdout, stdout
        assert "Rendered evidence: 2 image(s) (2 committed), 1 recording(s) (1 committed)" in (
            stdout
        ), stdout

    @pytest.mark.parametrize("lane", DESIGN_EVIDENCE_LANES)
    def test_a_design_lane_attachment_is_counted_apart_from_the_committed_media(
        self, tmp_path: Path, lane: str
    ) -> None:
        """One attachment downloads and two images are committed: the file
        reports one attached image and two committed ones, the attachment is
        numbered first (the cap must never drop it for a committed file), and
        the summary line's totals are the sums."""
        repo, base = self._repo_committing_evidence(tmp_path)
        url = "https://github.com/user-attachments/assets/0f3b2c1a-1111-4bcd-9e8f-0123456789ab"
        shot_list, shot_map, _clips, _output, _dir = self._run_evidence_gate(
            repo, base, tmp_path, body=f"![after]({url})\n", fixtures={url: self.JPEG}, lane=lane
        )
        evidence = self._evidence_file.read_text(encoding="utf-8")
        origins = dict(line.split("\t") for line in shot_map.splitlines())
        assert origins["attachment-01.jpg"] == url
        assert sorted(origins) == ["attachment-01.jpg", "attachment-02.png", "attachment-03.png"]
        assert "attachments downloaded and typed as images: 1" in evidence
        assert "attachments downloaded and typed as recordings: 0" in evidence
        assert (
            "committed images this revision adds or changes, read from the object store "
            "and typed by their bytes: 2"
        ) in evidence, evidence
        assert (
            "committed recordings this revision adds or changes, read from the object store "
            "and typed by their bytes: 1"
        ) in evidence, evidence
        assert len(shot_list.splitlines()) == 3
        assert "Rendered evidence: 3 image(s) (2 committed), 1 recording(s) (1 committed)" in (
            self._evidence_stdout
        ), self._evidence_stdout

    def test_neither_fork_lane_tells_its_reviewer_to_discount_a_committed_image(self) -> None:
        """The evidence steps hand the reviewer committed media as bytes out of
        the object store, so the trigger that decides "cannot evaluate" has to
        count that media as evidence. A trigger that still says a committed
        image is not evidence on a fork pull request, or that a gap is a
        control no ATTACHED screenshot shows, instructs the reviewer to block
        an author for the very evidence the workflow just gave it. The negative
        assertions name the stale shapes; the positive ones name the rule the
        trigger states instead. The design trigger is checked in BOTH design
        lanes because their calibration blocks are pinned identical, so its
        wording has to be true of a same-repo and a fork pull request alike --
        which it is only as the one read the shared script performs: bytes out
        of the object store at the head SHA, never off a working tree. A
        per-lane split ("in a same-repo checkout as the file at HEAD") names a
        read the script does not perform. The CANNOT-EVALUATE contract itself is
        untouched: the point is which evidence counts, not when the lane may
        refuse."""
        for lane in DESIGN_LANES:
            design_prompt = _flat(_step(lane, "Design review (Opus 5.5)")["with"]["prompt"])
            assert "THIS checkout can render" not in design_prompt, lane
            assert "a committed image is not evidence here" not in design_prompt, lane
            assert "in a same-repo checkout as the file at HEAD" not in design_prompt, lane
            assert "no committed image or recording it names" in design_prompt, lane
            assert "counts the same as an attachment" in design_prompt, lane
            assert (
                "read as bytes out of the object store at the head SHA, never off a "
                "working tree, on a same-repo and a fork pull request alike" in design_prompt
            ), lane
            assert "a committed path the list does not name was not admitted" in design_prompt, lane
            # The lane still refuses when the list names nothing, and still
            # says a URL the workflow could not download is not evidence.
            assert "CANNOT EVALUATE -- REQUIRED EVIDENCE MISSING" in design_prompt, lane
            assert "a URL that did not download is not evidence" in design_prompt, lane
        ux_prompt = _flat(_step("fork-ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "attached screenshot" not in ux_prompt
        assert "a control the attachments show" not in ux_prompt
        assert "a control no supplied screenshot (attached or committed) shows is" in ux_prompt
        assert "a control or state no supplied screenshot shows" in ux_prompt
        assert "user-visible control in no supplied screenshot" in ux_prompt
        # The same-repo lane's own wording is the model: it never had a
        # checkout problem, and it says "supplied" for the same reason.
        same_repo = _flat(_step("ux-review.yml", UX_REVIEW_STEP)["with"]["prompt"])
        assert "a control or state no supplied screenshot" in same_repo
