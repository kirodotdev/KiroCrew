"""Unit tests for the code-enforced two-stage review driver (gap A + phase switch)."""
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sage_lib import results
from sage_lib import review_driver as D  # noqa: N812
from sage_lib import store


class TestHostQualifiedIdentity(unittest.TestCase):
    """Change ids and reviewed keys are the persisted run-scoping keys: a GitHub
    Enterprise host must qualify them (or two hosts' runs collide in storage),
    while github.com keys stay byte-identical so old records resolve."""

    GHE_CFG = {"github_hosts": ["github.com", "acme.ghe.com"]}

    def test_github_keys_unchanged(self):
        self.assertEqual(D.change_id_for("https://github.com/o/r/pull/7"), "GH-o-r-7")
        self.assertEqual(D.reviewed_key_for("https://github.com/o/r/pull/7"),
                         "github.com/o/r#7")

    def test_ghe_keys_carry_the_host(self):
        with mock.patch.object(D.pipeline.adapters.store, "read_config_quiet",
                               return_value=self.GHE_CFG):
            self.assertEqual(D.change_id_for("https://acme.ghe.com/o/r/pull/7"),
                             "GH-acme.ghe.com-o-r-7")
            self.assertEqual(D.reviewed_key_for("https://acme.ghe.com/o/r/pull/7"),
                             "acme.ghe.com/o/r#7")

    def test_fetch_and_post_prompts_route_to_the_ghe_api(self):
        with mock.patch.object(D.pipeline.adapters.store, "read_config_quiet",
                               return_value=self.GHE_CFG):
            fetch = D._fetch_instruction("https://acme.ghe.com/o/r/pull/7")
            post = D.build_post_task("https://acme.ghe.com/o/r/pull/7")
            gh_fetch = D._fetch_instruction("https://github.com/o/r/pull/7")
            gh_post = D.build_post_task("https://github.com/o/r/pull/7")
        self.assertIn("--hostname acme.ghe.com", fetch)
        self.assertIn("--hostname acme.ghe.com", post)
        # github.com prompts pin their host EXPLICITLY too — omission would
        # let the worker's gh CLI default host decide the instance.
        self.assertIn("--hostname github.com", gh_fetch)
        self.assertIn("--hostname github.com", gh_post)

    def test_a_bare_change_token_keeps_the_legacy_path(self):
        # "CR-1" names no host, so there is no GitHub instance to cross to —
        # builders keep producing default-instruction prompts for it (the
        # driver's legacy flow relies on this). Only a link that NAMES a host
        # must resolve-or-refuse.
        self.assertIn("CR-1", D.build_post_task("CR-1"))
        self.assertNotIn("--hostname", D.build_post_task("CR-1"))
        self.assertNotIn("--hostname", D.build_review_task("CR-1"))

    def test_prompt_builders_fail_closed_on_an_unresolvable_host(self):
        """The leak guard: when the link's host no longer revalidates (e.g. the
        GHE host was removed from `github_hosts` between run start and this
        build), every prompt builder must REFUSE — a prompt whose `gh api`
        calls silently default to public github.com would fetch from, or post
        an internal enterprise draft onto, a public same-slug PR."""
        link = "https://acme.ghe.com/o/r/pull/7"
        with mock.patch.object(D.pipeline.adapters.store, "read_config_quiet",
                               return_value={"github_hosts": ["github.com"]}):
            for builder in (D.build_post_task, D._fetch_instruction,
                            D.build_review_task, D.build_review_followup_task):
                with self.assertRaises(D.pipeline.adapters.AdapterError):
                    builder(link)
                # A scheme is a named host too, even when it cannot be parsed.
                with self.assertRaises(D.pipeline.adapters.AdapterError):
                    builder("https://[::1")

    def test_non_github_prompts_always_carry_hostname(self):
        """The property that prevents the cross-host leak: for a non-github.com
        link there is no outcome where a prompt exists WITHOUT `--hostname` —
        either the host revalidates and the flag is embedded, or the builder
        raises (asserted above). "Resolved as github.com" and "failed to
        resolve" must never look identical."""
        with mock.patch.object(D.pipeline.adapters.store, "read_config_quiet",
                               return_value=self.GHE_CFG):
            for builder in (D.build_post_task, D._fetch_instruction,
                            D.build_review_task, D.build_review_followup_task):
                prompt = builder("https://acme.ghe.com/o/r/pull/7")
                self.assertIn("--hostname acme.ghe.com", prompt,
                              f"{builder.__name__} lost the hostname routing")
                gh = builder("https://github.com/o/r/pull/7")
                self.assertIn("--hostname github.com", gh,
                              f"{builder.__name__} left github.com unpinned")


def _confirmed(_link, _units):
    """Stand in for the GitHub read-back: this delivery really happened.

    `post_ok` means DELIVERED, so a test about what follows a delivery must supply
    the confirmation it has no pull request to obtain.
    """
    return True


class TestReviewDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)
        self.calls = []
        self.lock = threading.Lock()
        self.archived = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_dispatch(self, verdict="PASS", write_records=True, max_seen=None,
                       coverage_complete=True):
        """A dispatch fn modeling the SINGLE-PASS reviewer:
        - a REVIEW session writes the COMPLETE record in one turn (phase1 verdict +
          one finding + counts + deep_reviewed=true + the coverage signal);
        - a POSTER session publishes the driver-built pending comments;
        - a COVERAGE FOLLOW-UP session (dispatched only when the first pass set
          coverage_complete=false) marks coverage complete.
        The pool's send() returns when the worker's turn ends; this fake models
        that by writing the record and returning synchronously.
        """
        def dispatch(task, timeout=0):
            with self.lock:
                self.calls.append(task)
                self._concurrent = getattr(self, "_concurrent", 0) + 1
                if max_seen is not None:
                    max_seen[0] = max(max_seen[0], self._concurrent)
            m = re.search(r"CR-\d+", task)
            is_review = "SINGLE thorough pass" in task
            is_followup = "INCOMPLETE file coverage" in task
            is_poster = "pre-redacted DRAFT review comments" in task
            if write_records and m:
                cid = m.group(0)
                if is_poster:
                    rec = results.read_result(cid, self.root) or {}
                    pending = rec.get("pending_comments", []) or []
                    rec["posted_comments"] = len(pending)   # posts each verbatim
                    rec["design_comment_posted"] = any(
                        e.get("kind") == "design" for e in pending)
                    results.write_result(rec, self.root)
                elif is_review:
                    results.write_result({
                        "schema": "code-review-sage-result", "version": 1, "change_id": cid,
                        "platform": "github", "repo_identity": "github.com/o/r", "revision": "1",
                        "phase1": {"gate_verdict": verdict, "design_risk": "low", "criticality": "low"},
                        "blast_radius": {"rating": "SMALL", "signals": {}},
                        "counts": {"red": 0, "yellow": 1},
                        "findings": [{"dimension": "correctness", "severity": "yellow",
                                      "file": "f", "line": 1, "snippet": "x",
                                      "observation": "o", "consequence": "c", "suggestion": "s"}],
                        "deep_reviewed": True, "title": cid,
                        "files_covered": ["f"], "coverage_complete": coverage_complete,
                    }, self.root)
                elif is_followup:
                    rec = results.read_result(cid, self.root) or {}
                    rec["coverage_complete"] = True
                    rec["deep_reviewed"] = True
                    results.write_result(rec, self.root)
            with self.lock:
                self._concurrent -= 1
            return {"ok": True, "output": "done", "error": ""}
        return dispatch

    def _archiver(self, html, root=None):
        self.archived.append(html)
        return "sage-report-test"

    def test_single_pass_reviews_all_changes(self):
        out = D.run_review(["CR-1", "CR-2", "CR-3"], dispatch=self._fake_dispatch(verdict="PASS"), confirm=_confirmed,
                           archiver=self._archiver, root=self.root, post=True)
        self.assertEqual(out["changes"], 3)
        self.assertEqual(out["gate_spawns"], 3)
        self.assertEqual(out["deep_spawns"], 3)
        self.assertEqual(out["deep_reviewed"], 3)
        self.assertEqual(out["design_blocked"], 0)
        # ONE review pass + poster per change (coverage complete -> no follow-up)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(out["deep_rounds"], 3)           # one review pass per change
        self.assertEqual(out["report"]["total"], 3)
        # report archived as a per-run artifact, then records cleaned
        self.assertEqual(out["report_slug"], "sage-report-test")
        self.assertEqual(out["results_cleaned"], 3)
        self.assertEqual(results.list_results(self.root), [])

    def test_concerns_still_proceeds_to_phase2(self):
        out = D.run_review(["CR-5"], dispatch=self._fake_dispatch(verdict="CONCERNS"),
                           generate_report=False, root=self.root, post=True)
        self.assertEqual(out["deep_spawns"], 1)
        self.assertEqual(out["deep_reviewed"], 1)

    def test_block_still_runs_phase2(self):
        # A design BLOCK does not skip Phase 2 — the full code review still runs so
        # the author sees design + code issues in one pass. BLOCK only informs the
        # ship decision.
        out = D.run_review(["CR-1", "CR-2"], dispatch=self._fake_dispatch(verdict="BLOCK"),
                           archiver=self._archiver, root=self.root, post=True)
        self.assertEqual(out["gate_spawns"], 2)
        self.assertEqual(out["deep_spawns"], 2)            # BLOCK proceeds to Phase 2
        self.assertEqual(out["phase2_skipped_on_block"], 0)
        self.assertEqual(out["design_blocked"], 2)         # BLOCK verdicts, still deep-reviewed
        self.assertEqual(out["deep_reviewed"], 2)
        # every reviewed change gets the always-on ship-readiness (design) comment
        self.assertEqual(out["design_comments_posted"], 2)
        self.assertEqual(out["report"]["total"], 2)

    def test_archive_failure_keeps_records(self):
        out = D.run_review(["CR-1", "CR-2"], dispatch=self._fake_dispatch(verdict="PASS"),
                           archiver=lambda html, root=None: None, root=self.root, post=True)
        self.assertIn("archive_error", out)
        self.assertNotIn("results_cleaned", out)
        self.assertEqual(len(results.list_results(self.root)), 2)   # records preserved

    def test_no_record_marks_failed(self):
        # dispatch that completes but writes no record -> review produced nothing usable
        def no_record(task, timeout=0):
            return {"ok": True, "output": "", "error": ""}
        out = D.run_review(["CR-7"], dispatch=no_record, generate_report=False, root=self.root, post=True)
        self.assertEqual(out["deep_reviewed"], 0)
        # The residual "turn completed, nothing written" case keeps this value;
        # environment failures carry their own discriminated reasons instead.
        self.assertEqual(out["per_change"][0]["skipped_reason"], "no_review_recorded")
        self.assertFalse(out["per_change"][0]["result_recorded"])

    def test_incomplete_record_is_discriminated_from_no_record(self):
        # A worker that writes a record but never marks the review complete is a
        # DIFFERENT detectable cause than one that writes nothing — the two must
        # not collapse into one reason (that ambiguity is what made the failure
        # untriageable).
        errors: dict = {}

        def prog(cid, phase, extra=None):
            if phase == "failed":
                errors[cid] = (extra or {}).get("error", "")

        def partial(task, timeout=0):
            results.write_result({
                "schema": "code-review-sage-result", "version": 1, "change_id": "CR-8",
                "platform": "github", "repo_identity": "github.com/o/r", "revision": "1",
                "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
                "blast_radius": {"rating": "SMALL", "signals": {}},
                "counts": {"red": 0, "yellow": 0}, "findings": [],
                "deep_reviewed": False, "title": "CR-8",
                "files_covered": [], "coverage_complete": True,
            }, self.root)
            return {"ok": True, "output": "", "error": ""}

        out = D.run_review(["CR-8"], dispatch=partial, generate_report=False,
                           root=self.root, post=True, progress=prog)
        rec = out["per_change"][0]
        self.assertEqual(rec["skipped_reason"], "review_record_incomplete")
        self.assertTrue(rec["result_recorded"])
        self.assertIn("never completed", errors["CR-8"])
        self.assertNotIn("no result record", errors["CR-8"])

    def test_preflight_failure_fails_fast_and_never_dispatches(self):
        # A failed runtime preflight must produce its own discriminated reason,
        # name the missing runtime in the progress error, and return BEFORE any
        # reviewer session is dispatched.
        errors: dict = {}

        def prog(cid, phase, extra=None):
            errors[cid] = (phase, (extra or {}).get("error", ""))

        out = D.run_review(
            ["CR-1", "CR-2"], dispatch=self._fake_dispatch(),
            generate_report=False, root=self.root, post=True, progress=prog,
            preflight=lambda: "the reviewer cannot run: no kiro-cli executable "
                              "was found on this host")
        self.assertEqual(self.calls, [])            # nothing was dispatched
        self.assertFalse(out["ok"])
        self.assertIn("kiro-cli", out["error"])
        self.assertEqual(out["changes"], 2)
        self.assertEqual(out["result_records"], 0)
        for rec in out["per_change"]:
            self.assertEqual(rec["skipped_reason"], "runtime_unavailable")
            self.assertIn("kiro-cli", rec["deep_error"])
        self.assertEqual(errors["CR-1"][0], "failed")
        self.assertIn("kiro-cli", errors["CR-1"][1])

    def test_passing_preflight_runs_the_review(self):
        # "" means the runtime is available — the run proceeds normally.
        out = D.run_review(["CR-1"], dispatch=self._fake_dispatch(),
                           generate_report=False, root=self.root, post=True,
                           preflight=lambda: "")
        self.assertTrue(out["ok"])
        self.assertEqual(out["deep_reviewed"], 1)
        self.assertGreater(len(self.calls), 0)

    def test_each_task_is_single_change(self):
        D.run_review(["CR-1", "CR-2"], dispatch=self._fake_dispatch(), archiver=self._archiver,
                     root=self.root, post=True)
        for task in self.calls:
            self.assertIn("EXACTLY ONE change", task)

    def test_concurrency_capped(self):
        max_seen = [0]
        D.run_review(["CR-1", "CR-2", "CR-3", "CR-4", "CR-5"],
                     dispatch=self._fake_dispatch(max_seen=max_seen), archiver=self._archiver,
                     concurrency=2, root=self.root, post=True)
        self.assertLessEqual(max_seen[0], 2)

    def test_review_failure_surfaced(self):
        def failing(task, timeout=0):
            return {"ok": False, "output": "", "error": "boom"}
        out = D.run_review(["CR-9"], dispatch=failing, generate_report=False, root=self.root, post=True)
        self.assertEqual(len(out["failures"]), 1)
        self.assertEqual(out["deep_reviewed"], 0)
        self.assertEqual(out["per_change"][0]["skipped_reason"], "review_failed")

    def test_empty_change_set(self):
        out = D.run_review([], dispatch=self._fake_dispatch(), root=self.root, post=True)
        self.assertFalse(out["ok"])

    # ── The per-change progress entry must name its cause as a TOKEN ──
    # Every failure path below already puts the token on its per_change RECORD
    # (`skipped_reason`), but the record is driver-internal: what the dashboard
    # reads is the `progress` entry, which carried prose only. So the cause had to
    # be recognized by its wording, and rewording any message below silently
    # reverted the card to untranslated pass-through with no test going red.
    # These assert the token travels with the sentence on the entry itself, and
    # that it AGREES with the record on the same path.

    def _failed_entry(self, cid: str, **kw) -> dict:
        """Drive ``run_review`` and return the `failed` progress entry for ``cid``."""
        entries: dict = {}

        def prog(c, phase, extra=None):
            if phase == "failed":
                entries[c] = dict(extra or {})

        out = D.run_review([cid], generate_report=False, root=self.root,
                           post=True, progress=prog, **kw)
        self.out = out
        return entries.get(cid, {})

    def test_progress_entry_names_no_review_recorded(self):
        entry = self._failed_entry(
            "CR-7", dispatch=lambda task, timeout=0: {"ok": True, "output": "", "error": ""})
        self.assertEqual(entry.get("reason"), "no_review_recorded")
        # The sentence is unchanged -- the token is carried BESIDE it.
        self.assertEqual(entry.get("error"), "review produced no result record")
        self.assertEqual(self.out["per_change"][0]["skipped_reason"], entry.get("reason"))

    def test_progress_entry_names_review_record_incomplete(self):
        def partial(task, timeout=0):
            results.write_result({
                "schema": "code-review-sage-result", "version": 1, "change_id": "CR-8",
                "platform": "github", "repo_identity": "github.com/o/r", "revision": "1",
                "phase1": {"gate_verdict": "PASS", "design_risk": "low",
                           "criticality": "low"},
                "blast_radius": {"rating": "SMALL", "signals": {}},
                "counts": {"red": 0, "yellow": 0}, "findings": [],
                "deep_reviewed": False, "title": "CR-8",
                "files_covered": [], "coverage_complete": True,
            }, self.root)
            return {"ok": True, "output": "", "error": ""}

        entry = self._failed_entry("CR-8", dispatch=partial)
        self.assertEqual(entry.get("reason"), "review_record_incomplete")
        self.assertIn("never completed", entry.get("error", ""))
        self.assertEqual(self.out["per_change"][0]["skipped_reason"], entry.get("reason"))

    def test_progress_entry_names_review_failed(self):
        entry = self._failed_entry(
            "CR-9",
            dispatch=lambda task, timeout=0: {"ok": False, "output": "", "error": "boom"})
        self.assertEqual(entry.get("reason"), "review_failed")
        # The spawn's own text, which for this cause is the information: it can be
        # a missing-agent-spec message carrying its own repair command, so the
        # sentence must survive alongside the (generic) token.
        self.assertEqual(entry.get("error"), "boom")
        self.assertEqual(self.out["per_change"][0]["skipped_reason"], entry.get("reason"))

    def test_progress_entry_names_runtime_unavailable(self):
        entries: dict = {}

        def prog(cid, phase, extra=None):
            if phase == "failed":
                entries[cid] = dict(extra or {})

        out = D.run_review(
            ["CR-1", "CR-2"], dispatch=self._fake_dispatch(),
            generate_report=False, root=self.root, post=True, progress=prog,
            preflight=lambda: "the reviewer cannot run: no kiro-cli executable "
                              "was found on this host")
        # Every change of a preflight-failed run, not just the first.
        for cid, rec in zip(("CR-1", "CR-2"), out["per_change"]):
            self.assertEqual(entries[cid].get("reason"), "runtime_unavailable")
            self.assertIn("kiro-cli", entries[cid].get("error", ""))
            self.assertEqual(rec["skipped_reason"], entries[cid].get("reason"))

    def test_progress_entry_names_a_refused_host_as_review_failed(self):
        # `build_review_task` fails CLOSED when the link's host no longer
        # revalidates. Patched rather than reached through a crafted URL so the
        # test pins THIS site's payload, not the host-allowlist rules.
        def refuse(link):
            raise D.pipeline.adapters.AdapterError("host is not allowed")

        with mock.patch.object(D, "build_review_task", refuse):
            entry = self._failed_entry("CR-6", dispatch=self._fake_dispatch())
        self.assertEqual(entry.get("reason"), "review_failed")
        self.assertIn("refusing to review", entry.get("error", ""))
        self.assertEqual(self.out["per_change"][0]["skipped_reason"], entry.get("reason"))

    def test_review_task_covers_design_and_is_single_pass(self):
        task = D.build_review_task("https://github.com/o/r/pull/7")
        self.assertIn("ISOLATED", task)
        self.assertIn("SINGLE thorough pass", task)
        self.assertIn("DESIGN dimension", task)
        self.assertIn("spawn further subagents", task)
        self.assertIn("github.com/o/r/pull/7", task)

    def test_review_task_has_inline_learning(self):
        task = D.build_review_task("CR-8")
        self.assertIn("ISOLATED", task)
        self.assertIn("INLINE miss-analysis", task)
        self.assertIn("is_fix", task)
        self.assertIn("Do NOT spawn further subagents", task)
        self.assertIn("CR-8", task)

    def test_review_task_keeps_code_reviewer_checks_first_class(self):
        # The code-reviewer's strong checks must be explicit in the single-pass
        # prompt (the 9 dimensions + fidelity + security threat chain).
        task = D.build_review_task("CR-9")
        self.assertIn("9 code-level dimensions", task)
        self.assertIn("description<->diff fidelity", task)
        self.assertIn("threat chain", task)

    def test_progress_reports_phase_transitions(self):
        seen = []
        plock = threading.Lock()

        def prog(cid, phase, extra=None):
            with plock:
                seen.append((cid, phase))
        D.run_review(["CR-1"], dispatch=self._fake_dispatch(verdict="PASS"),
                     generate_report=False, root=self.root, progress=prog, post=True)
        phases = [p for (c, p) in seen if c == "CR-1"]
        self.assertEqual(phases[0], "queued")     # marked queued upfront
        self.assertIn("reviewing", phases)        # single review pass in progress
        self.assertEqual(phases[-1], "done")      # terminal

    def test_progress_block_still_reviews(self):
        seen = []

        def prog(cid, phase, extra=None):
            seen.append(phase)
        D.run_review(["CR-2"], dispatch=self._fake_dispatch(verdict="BLOCK"),
                     generate_report=False, root=self.root, progress=prog, post=True)
        self.assertIn("reviewing", seen)          # BLOCK is still fully reviewed
        self.assertNotIn("blocked", seen)         # no design-block short-circuit
        self.assertNotIn("gating", seen)          # no gate phase
        self.assertEqual(seen[-1], "done")

    def test_review_task_blast_radius_never_blocks(self):
        # The design dimension must BLOCK only on genuine design defects — a large
        # blast radius alone is not a BLOCK (it raises review depth).
        task = D.build_review_task("CR-9")
        self.assertIn("BLOCK is ONLY for a genuine DESIGN defect", task)
        self.assertIn("NEVER on its own a BLOCK", task)

    def test_progress_done_reports_posted_comments(self):
        seen = {}

        def prog(cid, phase, extra=None):
            if phase == "done":
                seen[cid] = extra or {}
        out = D.run_review(["CR-1"], dispatch=self._fake_dispatch(verdict="PASS"),
                           generate_report=False, root=self.root, progress=prog, post=True)
        self.assertEqual(seen["CR-1"]["posted"], 2)       # 1 finding + always-on ship comment
        self.assertEqual(seen["CR-1"]["expected"], 2)     # red0 + yellow1 + 1 ship comment
        self.assertEqual(out["per_change"][0]["posted_comments"], 2)

    def test_progress_done_flags_unposted_findings(self):
        # A review that records findings but whose poster posts nothing -> driver
        # reports posted=0 with expected>0 so the UI can flag "findings not posted".
        def review_no_post(task, timeout=0):
            m = re.search(r"CR-\d+", task)
            assert m is not None
            cid = m.group(0)
            if "SINGLE thorough pass" in task:
                results.write_result({
                    "schema": "code-review-sage-result", "version": 1, "change_id": cid,
                    "platform": "github", "repo_identity": "x", "revision": "1",
                    "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
                    "blast_radius": {"rating": "SMALL", "signals": {}},
                    "counts": {"red": 1, "yellow": 0},
                    "findings": [{"dimension": "correctness", "severity": "red",
                                  "file": "f", "line": 1, "snippet": "x",
                                  "observation": "o", "consequence": "c", "suggestion": "s"}],
                    "deep_reviewed": True, "title": cid,
                    "files_covered": ["f"], "coverage_complete": True,
                }, self.root)
            elif "pre-redacted DRAFT review comments" in task:
                rec = results.read_result(cid, self.root) or {}
                rec["posted_comments"] = 0   # nothing posted despite a finding
                results.write_result(rec, self.root)
            return {"ok": True, "output": "", "error": ""}

        seen = {}

        def prog(cid, phase, extra=None):
            if phase == "done":
                seen[cid] = extra or {}
        D.run_review(["CR-2"], dispatch=review_no_post, generate_report=False,
                     root=self.root, progress=prog, post=True)
        self.assertEqual(seen["CR-2"]["posted"], 0)
        self.assertEqual(seen["CR-2"]["expected"], 2)     # red1 + 1 ship comment; UI flags mismatch

    def test_archive_report_posts_and_returns_slug(self):
        calls = []
        orig = D._api_request

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path))
            if method == "POST":
                return {"slug": "sage-report-xyz"}
            return {"artifacts": []}      # GET (prune) — nothing to prune
        D._api_request = fake_api
        try:
            slug = D._archive_report("<b>report</b>")
        finally:
            D._api_request = orig
        self.assertEqual(slug, "sage-report-xyz")
        self.assertTrue(any(m == "POST" for m, _ in calls))

    def test_archive_report_returns_none_on_post_error(self):
        orig = D._api_request
        D._api_request = lambda method, path, body=None, timeout=30: {"error": "boom"}
        try:
            self.assertIsNone(D._archive_report("<b>x</b>"))
        finally:
            D._api_request = orig

    def test_redact_is_noop_without_security_lib(self):
        # In standalone/test context kiro_crew.security isn't importable, so the
        # redact helpers are None and _redact passes text through unchanged.
        self.assertEqual(D._redact("plain text"), "plain text")

    def test_resolve_concurrency_auto_matches_pool_cap(self):
        # Auto (0/None) defaults to the worker pool's concurrency cap — pool
        # workers aren't /api/spawn sub-agents, so the gateway cap does not apply.
        self.assertEqual(D._resolve_concurrency(0), D.review_pool.MAX_CONCURRENT)
        self.assertEqual(D._resolve_concurrency(None), D.review_pool.MAX_CONCURRENT)
        self.assertEqual(D.review_pool.MAX_CONCURRENT, 5)   # max 5 concurrent reviews

    def test_resolve_concurrency_explicit_wins(self):
        self.assertEqual(D._resolve_concurrency(7), 7)
        self.assertEqual(D._resolve_concurrency(1), 1)

    def test_coverage_backstop_runs_one_followup(self):
        # First pass reports incomplete coverage -> the driver runs EXACTLY ONE
        # targeted follow-up (not a blanket loop), then posts. deep_rounds == 2.
        disp = self._fake_dispatch(verdict="PASS", coverage_complete=False)
        out = D.run_review(["CR-1"], dispatch=disp, generate_report=False, root=self.root, post=True)
        self.assertEqual(out["per_change"][0]["deep_rounds"], 2)
        self.assertEqual(len(self.calls), 3)   # review + one follow-up + poster
        self.assertTrue(any("INCOMPLETE file coverage" in c for c in self.calls))

    def test_coverage_complete_skips_followup(self):
        disp = self._fake_dispatch(verdict="PASS", coverage_complete=True)
        out = D.run_review(["CR-1"], dispatch=disp, generate_report=False, root=self.root, post=True)
        self.assertEqual(out["per_change"][0]["deep_rounds"], 1)
        self.assertEqual(len(self.calls), 2)   # review + poster only
        self.assertFalse(any("INCOMPLETE file coverage" in c for c in self.calls))


class TestWorkerPromptScriptPaths(unittest.TestCase):
    """Guard: every script path the worker prompts instruct must point at a script
    that actually ships in the app. This catches a rename (e.g. the ``lib`` ->
    ``sage_lib`` move) that misses a prompt string — which would make the worker
    run a non-existent path and silently produce no verdict."""

    def test_prompts_reference_existing_script_paths(self):
        app_root = Path(__file__).resolve().parents[1]
        prompts = [D.build_review_task("CR-12345678"),
                   D.build_review_followup_task("CR-12345678")]
        refs = set()
        for p in prompts:
            refs.update(re.findall(r"(sage_lib/[\w./-]+\.py)", p))
        self.assertTrue(refs, "expected the worker prompts to reference a script")
        for rel in sorted(refs):
            self.assertTrue((app_root / rel).is_file(),
                            f"worker prompt references a missing path: {rel}")


class TestWorkerPromptInterpreter(unittest.TestCase):
    """The worker runs the app's scripts through a shell on an unknown host, so the
    prompts must name an interpreter that exists there. ``python3`` does not on
    Windows — the name is a Microsoft Store app-execution alias, not an
    interpreter — and the failure is silent: the command runs no Python, no result
    record is written, and the review ends with no verdict."""

    LINK = "https://github.com/o/r/pull/12345678"

    def _prompts(self):
        return [D.build_review_task(self.LINK), D.build_review_followup_task(self.LINK)]

    def test_no_prompt_names_a_bare_interpreter(self):
        # Both needles are anchored on the opening backtick of an inline code
        # span: python_command() legitimately embeds the absolute
        # sys.executable, which can itself end in "python3" (issue #8205), so
        # an unanchored needle would fire on the correct path. Unbackticked
        # prose is covered only by the span test below
        # (test_every_script_command_carries_the_resolved_interpreter).
        for p in self._prompts():
            self.assertNotIn("`python3 ", p)
            self.assertNotIn("`python ", p)

    def test_every_script_command_carries_the_resolved_interpreter(self):
        py = D.python_command()
        for p in self._prompts():
            for cmd in re.findall(r"`([^`]*sage_lib/[\w./-]+\.py[^`]*)`", p):
                self.assertTrue(cmd.startswith(py + " "),
                                f"script command does not name the interpreter: {cmd}")

    def test_prompt_states_the_interpreter_for_the_skill_commands(self):
        # The shipped skills write their commands as `<python> ...`; the prompt is
        # the only place that can tell the worker what to substitute.
        for p in self._prompts():
            self.assertIn("<python>", p)
            self.assertIn(D.python_command(), p)

    def test_resolved_interpreter_is_an_absolute_path(self):
        self.assertTrue(Path(D.python_command()).is_absolute())

    def test_the_interpreter_is_never_taken_from_a_worker_writable_path(self):
        """``store.app_root()`` is under ``KIROCREW_HOME``, which the review worker
        writes into and which a prompt injection therefore controls. Resolving the
        interpreter through it would let a planted ``.venv/Scripts/python.exe`` be
        executed by the NEXT review. Any reintroduction of that lookup fails here.
        """
        with mock.patch.object(
            D.store,
            "app_root",
            side_effect=AssertionError("python_command must not consult app_root"),
        ):
            self.assertEqual(D.python_command(), sys.executable)


class TestInterpreterIsHandedOverRaw(unittest.TestCase):
    """The prompts do NOT shell-quote the interpreter, and that is deliberate.

    Quoting needs the worker's shell, which is not pinned: a Windows session may
    get PowerShell or cmd, and a form valid in one is a syntax error in the
    other. Three separate defects came out of guessing (``$`` expanding inside a
    double-quoted PowerShell string, ``\\`` consumed as an escape by a POSIX
    shell, and the PowerShell call operator breaking cmd), so the responsibility
    moved to the worker, which knows its own shell.
    """

    LINK = "https://github.com/o/r/pull/12345678"

    def test_the_resolved_path_is_not_quoted_or_escaped(self):
        # Injected at the seam the interpreter now comes from: the gateway's own
        # ``sys.executable``. Whatever it contains -- a space, a ``$``, a backslash
        # run -- must arrive verbatim, since each of those was a separate defect.
        for exe in (r"C:\Program Files\Py\python.exe",
                    r"C:\tools\$python v2\python.exe",
                    "/home/u/my py/bin/python3",
                    r"/home/u/kiro\home/bin/python3"):
            with mock.patch.object(D.sys, "executable", exe):
                self.assertEqual(D.python_command(), exe)

    def test_the_prompt_tells_the_worker_to_quote_for_its_own_shell(self):
        # Without this instruction a space-containing path -- ordinary on Windows,
        # where profiles are named "First Last" -- would be split by the shell and
        # the command would run nothing, which is the failure this PR removes.
        for prompt in (D.build_review_task(self.LINK),
                       D.build_review_followup_task(self.LINK)):
            self.assertIn("quote it as YOUR shell requires", prompt)


class TestShippedSkillsNameNoBareInterpreter(unittest.TestCase):
    """The skills ship in the wheel and the worker is told to load them, so a
    ``python3`` command left in one reaches the worker exactly as a prompt string
    would — the prompt guard above cannot see it."""

    def test_no_skill_command_names_a_bare_interpreter(self):
        # Matches an interpreter name followed by a script path ANYWHERE in the
        # line, not just at its start: the one surviving `python3` in these files
        # was inside a `>` blockquote, which a line-anchored pattern cannot see.
        # The lookbehind keeps `<python> foo.py` (the placeholder) from matching.
        bare = re.compile(r"(?<![<\w])python3?\s+\S*\.py\b")
        skills = Path(__file__).resolve().parents[1] / "skills"
        found = list(skills.glob("*/SKILL.md"))
        self.assertTrue(found, "expected the app to ship skills")
        for md in found:
            for i, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
                self.assertNotRegex(line, bare,
                                    f"{md.name}:{i} names a bare interpreter")


class TestDeterministicPosting(unittest.TestCase):
    """CR-comment redaction is deterministic: the deep worker RECORDS findings (it
    never posts), the driver builds the Python-redacted bodies, and a separate
    poster publishes them verbatim — so no LLM free-text reaches the CR surface."""

    def test_review_prompt_records_only_and_never_posts(self):
        p = D.build_review_task("https://github.com/o/r/pull/12345678")
        self.assertIn("RECORD ONLY", p)                  # writes findings, no posting
        self.assertIn("do NOT post", p)
        self.assertIn("MUST NOT call any comment tool", p)
        self.assertNotIn("CRAddComment", p)              # reviewer never posts

    def test_post_task_posts_prebuilt_redacted_bodies_verbatim(self):
        p = D.build_post_task("https://github.com/o/r/pull/12345678")
        self.assertIn("pending_comments", p)             # reads driver-built bodies
        self.assertIn("VERBATIM", p)                     # posts exactly, no compose
        self.assertIn("already redacted in Python", p)   # redaction is deterministic
        self.assertIn("posted_comments", p)              # records count
        self.assertIn("design_comment_posted", p)


class TestChangeIdAndFetch(unittest.TestCase):
    """The driver's change-id derivation is platform-aware + filesystem-safe, and
    the gate/deep prompts fetch with the right per-platform mechanism."""

    def test_cid_non_pr_fallback_safe(self):
        # A non-PR link isn't a valid change; _cid returns a filesystem-safe stem.
        self.assertNotIn("/", D._cid("https://github.com/o/r/pull/123"))

    def test_cid_github_pr(self):
        cid = D._cid("https://github.com/kiro-team/kiro-cli/pull/3361")
        self.assertEqual(cid, "GH-kiro_team-kiro_cli-3361")

    def test_cid_matches_adapter_recorded_id(self):
        # The driver-read id MUST equal the id the worker's adapter records, or the
        # written record and the driver's read would hit different files.
        link = "https://github.com/o/r/pull/7"
        target = D.pipeline.adapters.parse_github_payload(
            {"number": 7, "body": "x", "files": [{"path": "a", "diff": "d"}]}, link=link)
        self.assertEqual(D._cid(link), target.change_id)

    def test_cid_fallback_is_filesystem_safe(self):
        cid = D._cid("https://example.com/weird/thing")
        self.assertNotIn("/", cid)
        self.assertNotIn(":", cid)

    def test_review_prompt_github_fetch(self):
        p = D.build_review_task("https://github.com/o/r/pull/5")
        self.assertIn("gh api", p)
        self.assertIn("--payload-file", p)
        self.assertNotIn("ReadInternalWebsites", p)

    def test_review_and_followup_prompts_use_gh(self):
        gh = "https://github.com/o/r/pull/5"
        self.assertIn("gh api", D.build_review_task(gh))
        self.assertIn("gh api", D.build_review_followup_task(gh))


class TestGithubPosting(unittest.TestCase):
    """GitHub draft posting = ONE PENDING (unsubmitted) review, never auto-submitted;
    the envelope is Python-built + redacted and the poster posts it verbatim."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_github_poster_prompt_is_pending_and_never_submits(self):
        p = D.build_post_task("https://github.com/o/r/pull/5")
        self.assertIn("PENDING", p)
        self.assertIn("NO `event` key", p)             # unsubmitted review
        self.assertIn("gh api", p)
        self.assertIn("--method POST", p)
        self.assertIn("github_review_payload", p)      # uses the Python-built envelope
        self.assertIn("MUST NOT", p)                   # submit/approve prohibition
        self.assertIn("VERBATIM", p)
        self.assertIn("already redacted in Python", p)
        # Re-review safety: clear only a prior SAGE pending review (watermark-gated),
        # never a human's, so a second run doesn't 422 on "one pending review per PR".
        self.assertIn('state==\"PENDING\"', p)
        self.assertIn("[code-review-sage]", p)

    def test_github_poster_prompt_is_pending_review(self):
        p = D.build_post_task("https://github.com/o/r/pull/5")
        self.assertIn("github_review_payload", p)
        self.assertIn("gh api", p)
        self.assertIn("PENDING", p)
        self.assertNotIn("CRAddComment", p)

    def test_github_run_attaches_review_payload_and_posts(self):
        link = "https://github.com/o/r/pull/5"
        cid = D._cid(link)   # GH-o-r-5

        def dispatch(task, timeout=0):
            if "SINGLE thorough pass" in task:
                results.write_result({
                    "schema": "code-review-sage-result", "version": 1, "change_id": cid,
                    "platform": "github", "repo_identity": "github.com/o/r",
                    "revision": "sha123",
                    "phase1": {"gate_verdict": "PASS", "design_risk": "low",
                               "criticality": "low"},
                    "blast_radius": {"rating": "SMALL", "signals": {}},
                    "counts": {"red": 1, "yellow": 0},
                    "findings": [{"dimension": "correctness", "severity": "red",
                                  "file": "src/a.rs", "line": 5, "snippet": "x",
                                  "observation": "o", "consequence": "c",
                                  "suggestion": "s"}],
                    "deep_reviewed": True, "title": cid,
                    "files_covered": ["src/a.rs"], "coverage_complete": True,
                }, self.root)
            elif "pre-redacted DRAFT review comments" in task:
                rec = results.read_result(cid, self.root) or {}
                pay = rec.get("github_review_payload") or {}
                rec["posted_comments"] = (len(pay.get("comments", []))
                                          + (1 if pay.get("body") else 0))
                rec["design_comment_posted"] = bool(pay.get("body"))
                results.write_result(rec, self.root)
            return {"ok": True, "output": "", "error": ""}

        out = D.run_review([link], dispatch=dispatch, generate_report=False, root=self.root, post=True)
        rec = results.read_result(cid, self.root)
        pay = rec["github_review_payload"]
        self.assertNotIn("event", pay)                 # PENDING (unsubmitted)
        self.assertEqual(pay["commit_id"], "sha123")   # anchored to head SHA
        self.assertEqual(len(pay["comments"]), 1)
        self.assertEqual(pay["comments"][0]["path"], "src/a.rs")
        self.assertEqual(pay["comments"][0]["side"], "RIGHT")
        self.assertEqual(rec["posted_comments"], 2)    # 1 finding + ship-readiness body
        self.assertEqual(out["per_change"][0]["posted_comments"], 2)

    def test_post_recorded_reports_a_record_with_no_revision(self):
        """An unanchorable record fails its own post, not the batch.

        `build_github_review_payload` refuses a record with no `revision` because
        GitHub would anchor the draft to the current head. The driver must turn that
        into a post failure -- the run reports it, the findings stay on disk for a
        retry after the record is repaired, and no draft is created.
        """
        root = Path(tempfile.mkdtemp())
        run_id = "r1"
        cid = "GH-acme-repo-1"
        rec = {
            "schema": "code-review-sage/result", "version": 1, "change_id": cid,
            "platform": "github", "repo_identity": "acme/repo",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low",
                       "criticality": "low"},
            "findings": [], "counts": {"red": 0, "yellow": 0},
            # No `revision` -- contract-valid, unanchorable.
        }
        results.write_result(rec, root, run_id)

        posted: list = []

        def _dispatch(*a, **k):
            posted.append(a)
            return {"ok": True}

        out = D.post_recorded(
            cid, "https://github.com/acme/repo/pull/1",
            dispatch=_dispatch, run_id=run_id, root=root, keys=None,
        )
        self.assertFalse(out["post_ok"])
        self.assertIn("commit_id", out["post_error"])
        self.assertEqual(out["posted_comments"], 0)
        self.assertEqual(out["expected_units"], 0)
        # Nothing was handed to the poster.
        self.assertEqual(posted, [])
        # The refusal is durable on the record, so a run can report it.
        after = results.read_result(cid, root, run_id) or {}
        self.assertFalse(after.get("post_ok"))
        self.assertIn("commit_id", after.get("post_error", ""))
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
