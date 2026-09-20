"""Tests for the Knowledge Library retrieval eval harness (``bench kb-retrieval``).

Covers the pure metric (``mrr_at_k``), the golden-set model's refusals, the
end-to-end deterministic run against a real ``KnowledgeStore`` + ``HybridRetriever``
via the toy embedder, per-class and abstention scoring, cross-process determinism,
and the CLI dispatch (toy path + the --real-embedder refusal when the model is
absent).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from kiro_crew.cli_bench import bench_cmd
from kiro_crew.eval.bench.kb_retrieval import (
    KB_QUERY_CLASSES,
    KBGoldenSet,
    KBGoldenSetError,
    average_precision_at_k,
    default_golden_set_path,
    format_kb_report,
    mrr_at_k,
    run_kb_retrieval,
    v1_golden_set_path,
)


class _Args:
    """Stand-in for the argparse namespace the dispatch receives."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


# -- mrr_at_k -----------------------------------------------------------------


class TestMrrAtK:
    def test_first_rank_is_one(self) -> None:
        assert mrr_at_k(["a", "b", "c"], ["a"], 3) == 1.0

    def test_second_rank_is_half(self) -> None:
        assert mrr_at_k(["x", "a", "c"], ["a"], 3) == 0.5

    def test_third_rank_is_third(self) -> None:
        assert mrr_at_k(["x", "y", "a"], ["a"], 3) == pytest.approx(1 / 3)

    def test_outside_window_is_zero(self) -> None:
        assert mrr_at_k(["x", "y", "z", "a"], ["a"], 3) == 0.0

    def test_no_gold_is_zero(self) -> None:
        assert mrr_at_k(["a", "b"], [], 3) == 0.0

    def test_first_of_multiple_gold_counts(self) -> None:
        # Reciprocal of the FIRST gold hit, regardless of how many gold exist.
        assert mrr_at_k(["x", "g2", "g1"], ["g1", "g2"], 5) == 0.5


# -- average_precision_at_k ---------------------------------------------------


class TestAveragePrecisionAtK:
    def test_perfect_ranking_is_one(self) -> None:
        assert average_precision_at_k(["g1", "g2", "z"], ["g1", "g2"], 3) == 1.0

    def test_single_gold_at_first_rank_is_one(self) -> None:
        assert average_precision_at_k(["g1", "z", "y"], ["g1"], 3) == 1.0

    def test_single_gold_matches_mrr(self) -> None:
        """With exactly one gold doc, average precision degenerates to 1/rank."""
        for ranked in (["g", "x", "y"], ["x", "g", "y"], ["x", "y", "g"]):
            assert average_precision_at_k(ranked, ["g"], 3) == pytest.approx(
                mrr_at_k(ranked, ["g"], 3)
            )

    def test_no_hits_is_zero(self) -> None:
        assert average_precision_at_k(["x", "y", "z"], ["g1"], 3) == 0.0

    def test_no_gold_is_zero(self) -> None:
        assert average_precision_at_k(["a", "b"], [], 3) == 0.0

    def test_second_gold_placement_changes_the_score(self) -> None:
        """The property that motivates the metric: MRR cannot see this, AP can.

        Both rankings put the first gold doc at rank 1, so MRR scores them
        identically. Average precision separates them because the second gold
        doc sits at rank 2 in one and rank 3 in the other.
        """
        early = average_precision_at_k(["g1", "g2", "z"], ["g1", "g2"], 3)
        late = average_precision_at_k(["g1", "z", "g2"], ["g1", "g2"], 3)
        assert mrr_at_k(["g1", "g2", "z"], ["g1", "g2"], 3) == mrr_at_k(
            ["g1", "z", "g2"], ["g1", "g2"], 3
        )
        assert early > late
        # (1/1 + 2/2) / 2 = 1.0 vs (1/1 + 2/3) / 2 = 0.8333
        assert early == 1.0
        assert late == pytest.approx((1.0 + 2 / 3) / 2)

    def test_gold_outside_window_is_not_counted(self) -> None:
        assert average_precision_at_k(["g1", "z", "y", "g2"], ["g1", "g2"], 3) == pytest.approx(
            1.0 / 2
        )

    def test_denominator_is_truncated_at_k(self) -> None:
        """More gold than fits in the window must still be able to reach 1.0.

        Mirrors :func:`ndcg_at_k`'s truncated ideal ranking: with three gold docs
        scored at k=2, a textbook ``len(gold)`` denominator would cap this run at
        0.667 for a reason that is arithmetic rather than a ranking fault.
        """
        assert average_precision_at_k(["g1", "g2", "g3"], ["g1", "g2", "g3"], 2) == 1.0

    def test_repeated_gold_doc_counts_once(self) -> None:
        """A duplicated result must not inflate the score above 1.0."""
        assert average_precision_at_k(["g1", "g1", "g1"], ["g1", "g2"], 3) == pytest.approx(0.5)

    def test_never_exceeds_one(self) -> None:
        assert average_precision_at_k(["g1", "g1", "g2"], ["g1", "g2"], 3) <= 1.0


# -- golden set model ---------------------------------------------------------


def _write_golden(tmp_path: Path, docs: list[dict], queries: list[dict]) -> Path:
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"name": "t", "docs": docs, "queries": queries}))
    return p


def _gold_label_digest(gs: KBGoldenSet) -> str:
    """SHA-256 over the label-bearing fields of every query, and nothing else.

    Covers query id, class and the set of gold doc ids -- the three fields that
    decide what the ruler scores as correct. Deliberately excludes the question
    text, the documents and the file bytes, so rewording a question, fixing a
    typo in a document or reformatting the JSON leaves the digest unchanged.
    Rows and gold ids are sorted before hashing, so reordering queries or gold
    ids is not a label change either.
    """
    rows = sorted([q.id, q.query_class, sorted(q.gold_doc_ids)] for q in gs.queries)
    payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


#: The gold labels of v2 at ``label_revision`` 1. Changing any gold label under
#: this revision fails ``test_v2_declares_the_label_revision_its_corrections_make``.
V2_LABEL_DIGEST_AT_REVISION_1 = "f43387ab3152a16ad3de3168a6616456787747d3bae8be369ad9d8a18f4fe864"


class TestGoldenSetV2:
    """The enlarged set exists to DISCRIMINATE, and that is a property of its
    shape, not of its size.

    v1 scores 1.000 recall on every class with both a keyword-only and a semantic
    retriever, so on that corpus the ruler can confirm the harness ran and nothing
    else. Size is not the reason: each of v1's gold documents is the only one in
    that corpus using its topic's vocabulary, so any ranker matching a single term
    wins. These tests pin the properties that make v2 different, because they are
    exactly what a later "just add a few more docs" edit would erode.
    """

    def _v2(self) -> KBGoldenSet:
        # The default IS v2 now; going through the default keeps this class
        # covering whatever a no-argument run actually measures.
        return KBGoldenSet.from_json(default_golden_set_path())

    def test_v2_loads_and_validates(self) -> None:
        gs = self._v2()
        assert gs.name == "kb_golden_v2"
        for q in gs.queries:
            assert q.query_class in KB_QUERY_CLASSES

    def test_v2_is_larger_than_v1_on_both_axes(self) -> None:
        """Corpus size sets the floor for how selective a cut-off can be.

        With v1's 18 documents, a top-3 cut-off admits a sixth of the corpus, so a
        near-random ranker scores well. Pinning growth on BOTH axes keeps the
        denominator meaningful and keeps per-class means off single samples.
        """
        v1 = KBGoldenSet.from_json(v1_golden_set_path())
        v2 = self._v2()
        assert len(v2.docs) > 3 * len(v1.docs) // 2
        assert len(v2.queries) > len(v1.queries)

    def test_every_class_has_enough_queries_to_average(self) -> None:
        """v1's abstention_rate was reported over n=1 — a coin flip printed as a
        rate. A class mean needs a population, so every class carries several."""
        gs = self._v2()
        counts = Counter(q.query_class for q in gs.queries)
        assert set(counts) == set(KB_QUERY_CLASSES), "every class must be exercised"
        thin = {cls: n for cls, n in counts.items() if n < 4}
        assert not thin, f"classes with too few queries to average: {thin}"

    def test_audited_labels_demand_only_the_documents_needed(self) -> None:
        """Two labels a blind re-labelling audit overturned, pinned.

        Three annotators on separate model families relabelled the set from an
        anonymized packet and agreed with each other AGAINST the file on exactly
        these two. Both had the same shape -- a document written to answer the
        question on its own, labelled as though it needed a partner -- and both
        biased ``recall_all``/``recall_micro`` downward, so the ruler charged the
        retriever for a labelling error.

        Pinned by id rather than by count: a future corpus edit that re-adds a
        redundant gold doc here is the regression, and a count assertion would
        pass as long as some other query lost one.
        """
        by_id = {q.id: q for q in self._v2().queries}
        # d-vpn-access states "requires the corporate VPN in addition to hardware
        # MFA. Both are required together", so it answers the question alone.
        assert by_id["q-multi-5"].gold_doc_ids == ("d-vpn-access",)
        # Corroboration is not requirement: the question asks whether MFA IS
        # required, which the baseline answers outright. The two audits confirm
        # it without being needed to answer it.
        assert by_id["q-reinforcement-1"].gold_doc_ids == ("d-mfa-baseline",)

    def test_v2_declares_the_label_revision_its_corrections_make(self) -> None:
        """The two audit corrections above change v2's labels under an unchanged
        name, so the set must say so itself: a print scored against the original
        labels and one scored against these are not comparable, and the name is
        the only thing both prints share.

        What is enforced: the revision is pinned at 1 AND a digest of every gold
        label (query id, class, gold doc ids) is pinned beside it, so a further
        gold-label edit that lands without a bump fails here mechanically. What
        is not: a bumped revision with unchanged labels passes once the pins are
        moved; the bump-on-edit rule is checked in one direction only.
        """
        gs = self._v2()
        assert gs.label_revision == 1
        digest = _gold_label_digest(gs)
        assert digest == V2_LABEL_DIGEST_AT_REVISION_1, (
            f"v2's gold labels differ from the set pinned at label_revision 1 "
            f"(digest {digest}). If a gold label changed on purpose, bump "
            f"'label_revision' in the golden JSON and re-pin the digest constant "
            f"for the new revision to {digest!r}; if no label was meant to "
            "change, revert the label edit."
        )

    def test_label_digest_moves_when_any_gold_label_changes(self) -> None:
        """The guard above is only a guard if an unbumped label edit trips it.

        Swap one gold doc id on a query that is NOT one of the two id-pinned
        corrections, leave ``label_revision`` at 1, and the digest must differ;
        reordering queries or rewording a question must leave it unchanged.
        """
        gs = self._v2()
        base = _gold_label_digest(gs)
        target = next(
            q
            for q in gs.queries
            if q.gold_doc_ids and q.id not in {"q-multi-5", "q-reinforcement-1"}
        )
        other_doc = next(d.id for d in gs.docs if d.id not in target.gold_doc_ids)
        edited = dataclasses.replace(target, gold_doc_ids=(other_doc, *target.gold_doc_ids[1:]))
        relabelled = dataclasses.replace(
            gs, queries=tuple(edited if q.id == target.id else q for q in gs.queries)
        )
        assert relabelled.label_revision == 1
        assert _gold_label_digest(relabelled) != base
        reordered = dataclasses.replace(gs, queries=tuple(reversed(gs.queries)))
        assert _gold_label_digest(reordered) == base
        reworded = dataclasses.replace(
            gs,
            queries=tuple(
                dataclasses.replace(q, question=q.question + " (reworded)") for q in gs.queries
            ),
        )
        assert _gold_label_digest(reworded) == base

    def test_multi_hop_queries_all_require_more_than_one_document(self) -> None:
        """A single-gold query in ``multi_hop`` inflates the class it sits in.

        ``multi_hop`` exists to measure whether retrieval can assemble an answer
        that no one document carries. A member needing only one document scores
        1.0 on ``recall_all`` for free and raises the class mean without any
        multi-hop retrieval happening, which is how a wrong label hid inside a
        plausible-looking number.
        """
        for q in self._v2().queries:
            if q.query_class == "multi_hop":
                assert len(q.gold_doc_ids) > 1, (
                    f"{q.id} is class 'multi_hop' with {len(q.gold_doc_ids)} gold doc(s); "
                    "a one-document answer belongs in a single-document class"
                )

    def test_every_answerable_query_faces_a_competing_distractor(self) -> None:
        """THE discriminating property, and the one worth a test.

        A query only separates a good retriever from a bad one when some non-gold
        document also looks like an answer. So for every answerable query, some
        non-gold doc must share at least two content words with the question. If
        this ever fails, the set has drifted back to v1's shape — findable only by
        noticing every score is 1.000, which is how the weakness survived v1.
        """
        gs = self._v2()
        stop = {
            "what",
            "which",
            "who",
            "when",
            "does",
            "did",
            "the",
            "a",
            "an",
            "is",
            "are",
            "was",
            "were",
            "for",
            "to",
            "of",
            "in",
            "on",
            "at",
            "by",
            "and",
            "or",
            "how",
            "many",
            "much",
            "long",
            "do",
            "can",
            "it",
            "that",
            "this",
            "there",
            "any",
            "with",
            "from",
            "into",
            "than",
            "then",
            "outside",
            "before",
            "after",
            "actually",
            "currently",
            "today",
            "still",
            "be",
        }

        def terms(text: str) -> set[str]:
            words = re.findall(r"[a-z0-9][a-z0-9-]+", text.lower())
            return {w for w in words if w not in stop and len(w) > 2}

        by_id = {d.id: d for d in gs.docs}
        undefended = []
        for q in gs.queries:
            if q.is_abstention:
                continue
            qterms = terms(q.question)
            competitors = [
                d.id
                for d in gs.docs
                if d.id not in q.gold_doc_ids and len(qterms & terms(f"{d.title} {d.content}")) >= 2
            ]
            if not competitors:
                undefended.append(q.id)
        assert not undefended, (
            "these queries have no competing non-gold document, so they cannot "
            f"discriminate between retrievers: {undefended}"
        )
        assert by_id, "corpus must be non-empty for the check above to mean anything"

    def test_v2_preserves_supersession_chronology(self) -> None:
        """Same invariant the v1 test pins, restated for every chain v2 adds.

        ``KnowledgeStore.add_item`` timestamps in insertion order, so a superseded,
        retracted or never-adopted document must be inserted BEFORE the document
        that overrides it. Otherwise a freshness-aware ranker is rewarded for
        surfacing stale evidence, and the correction / retraction / time_bound
        classes would silently measure the opposite of what they claim.
        """
        ids = [d.id for d in self._v2().docs]

        def precedes(earlier: str, later: str) -> None:
            assert ids.index(earlier) < ids.index(later), f"{earlier} must precede {later}"

        precedes("d-cache-ttl-draft", "d-cache-ttl-current")
        precedes("d-flag-checkout-plan", "d-flag-checkout-retracted")
        precedes("d-region-wiki", "d-region-adr")
        precedes("d-api-deprecation-2024", "d-api-deprecation-2025")
        precedes("d-mfa-sms-draft", "d-mfa-baseline")
        precedes("d-mfa-baseline", "d-mfa-audit-q3")
        precedes("d-mfa-audit-q3", "d-mfa-audit-q4")
        # The retention chain is three deep: 30d -> 60d interim -> 90d current.
        precedes("d-retention-2023", "d-retention-2024-interim")
        precedes("d-retention-2024-interim", "d-retention-2025")
        # A never-adopted hypothetical must not outrank the process in force.
        precedes("d-deploy-payments-cd-hypothetical", "d-deploy-payments")
        precedes("d-deploy-orders-nocanary-proposal", "d-deploy-orders")

    def test_abstention_queries_sit_inside_covered_topics(self) -> None:
        """An abstention query is only hard when the corpus ALMOST answers it.

        "parental leave in Brazil" is easy to abstain on -- no document is close.
        The interesting ones name a topic the corpus does cover and ask for a
        detail it withholds (a staging TTL, a per-service budget), so a retriever
        with no score floor confidently returns the neighbouring document. That is
        what keeps abstention_rate an honest measurement rather than a freebie.
        """
        gs = self._v2()
        abstentions = [q for q in gs.queries if q.is_abstention]
        assert len(abstentions) >= 5

        def terms(text: str) -> set[str]:
            return set(re.findall(r"[a-z0-9][a-z0-9-]+", text.lower()))

        corpus = terms(" ".join(f"{d.title} {d.content}" for d in gs.docs))
        near_misses = [
            q.id
            for q in abstentions
            if len([t for t in terms(q.question) if t in corpus and len(t) > 4]) >= 3
        ]
        assert len(near_misses) >= 4, (
            "too few abstention queries overlap covered topics; a corpus-distant "
            f"question is a freebie, not a test: {near_misses}"
        )


class TestGoldenSet:
    def test_shipped_v1_loads_and_validates(self) -> None:
        # v1 ships alongside the default so a v1-labelled report stays reproducible,
        # which means it must still parse and validate. Named explicitly rather than
        # via the default: following the default here would point this test at v2 and
        # leave v1 uncovered.
        gs = KBGoldenSet.from_json(v1_golden_set_path())
        assert gs.docs and gs.queries
        for q in gs.queries:
            assert q.query_class in KB_QUERY_CLASSES

    def test_shipped_v1_carries_no_label_revision(self) -> None:
        # v1's labels are the author's originals; the field is absent, which must
        # parse as "unrevised" rather than crash or default to a number the file
        # never declared.
        assert KBGoldenSet.from_json(v1_golden_set_path()).label_revision is None

    def test_label_revision_absent_means_unrevised(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            [{"id": "d1", "title": "t", "content": "c"}],
            [{"id": "q1", "class": "clean_fact", "question": "q", "gold_doc_ids": ["d1"]}],
        )
        assert KBGoldenSet.from_json(p).label_revision is None

    @pytest.mark.parametrize("bad", [0, -1, True, "1", 1.0])
    def test_label_revision_must_be_a_positive_integer(self, tmp_path: Path, bad: object) -> None:
        # 0 would spell "unrevised" a second way; a bool is an int subclass that
        # json.loads produces from `true`; a string or float cannot be ordered
        # against an integer revision unambiguously. All refuse rather than print.
        p = tmp_path / "g.json"
        p.write_text(
            json.dumps(
                {
                    "name": "t",
                    "label_revision": bad,
                    "docs": [{"id": "d1", "title": "t", "content": "c"}],
                    "queries": [
                        {"id": "q1", "class": "clean_fact", "question": "q", "gold_doc_ids": ["d1"]}
                    ],
                }
            )
        )
        with pytest.raises(KBGoldenSetError, match="label_revision"):
            KBGoldenSet.from_json(p)

    def test_shipped_v1_preserves_supersession_chronology(self) -> None:
        # KnowledgeStore.add_item timestamps in insertion order. Older/draft evidence
        # must therefore precede its superseding/authoritative documents, or future
        # freshness-aware ranking would reward stale evidence.
        ids = [d.id for d in KBGoldenSet.from_json(v1_golden_set_path()).docs]
        assert ids.index("d-feature-flag-announcement") < ids.index("d-feature-flag-retracted")
        assert (
            ids.index("d-security-mfa-proposal")
            < ids.index("d-security-mfa")
            < ids.index("d-security-mfa-audit")
        )

    def test_the_default_golden_set_is_v2(self) -> None:
        """Pins the switch itself.

        A no-argument ``bench kb-retrieval`` must measure the set that can
        discriminate. If this ever reads v1 again, every reported class score
        returns to 1.000 and the ruler goes back to proving only that it ran.
        """
        assert default_golden_set_path().name == "kb_golden_v2.json"
        assert default_golden_set_path().is_file()
        # Both sets stay packaged: the default moved, v1 was not deleted.
        assert v1_golden_set_path().is_file()
        assert KBGoldenSet.from_json(default_golden_set_path()).name == "kb_golden_v2"

    def test_missing_file_refuses(self, tmp_path: Path) -> None:
        # Match the application-owned prefix, not platform-specific errno text
        # (POSIX says "No such file"; Windows says "cannot find the file").
        with pytest.raises(KBGoldenSetError, match="refusing to read the golden set"):
            KBGoldenSet.from_json(tmp_path / "nope.json")

    def test_bad_json_refuses(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        with pytest.raises(KBGoldenSetError, match="not valid JSON"):
            KBGoldenSet.from_json(p)

    def test_control_bearing_filename_is_escaped_in_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.eval.bench.kb_retrieval as kbr

        malicious = tmp_path / "bad\x1b]0;title\x07.json"
        monkeypatch.setattr(kbr, "read_text_nofollow", lambda *_a, **_kw: "{")
        with pytest.raises(KBGoldenSetError) as excinfo:
            KBGoldenSet.from_json(malicious)
        message = str(excinfo.value)
        assert "\x1b" not in message
        assert "\\x1b" in message
        assert "\\x07" in message

    def test_dangling_gold_ref_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[
                {"id": "q1", "class": "clean_fact", "question": "?", "gold_doc_ids": ["MISSING"]}
            ],
        )
        with pytest.raises(KBGoldenSetError, match="undefined gold docs"):
            KBGoldenSet.from_json(p)

    def test_duplicate_doc_id_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[
                {"id": "d1", "title": "t", "content": "c"},
                {"id": "d1", "title": "t2", "content": "c2"},
            ],
            queries=[{"id": "q1", "class": "clean_fact", "question": "?", "gold_doc_ids": ["d1"]}],
        )
        with pytest.raises(KBGoldenSetError, match="duplicate doc ids"):
            KBGoldenSet.from_json(p)

    def test_unknown_class_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[{"id": "q1", "class": "not_a_class", "question": "?", "gold_doc_ids": ["d1"]}],
        )
        with pytest.raises(KBGoldenSetError, match="unknown class"):
            KBGoldenSet.from_json(p)

    def test_no_queries_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(tmp_path, docs=[{"id": "d1", "title": "t", "content": "c"}], queries=[])
        with pytest.raises(KBGoldenSetError, match="no queries"):
            KBGoldenSet.from_json(p)

    def test_abstention_query_is_flagged(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[
                {"id": "q1", "class": "abstention", "question": "?", "gold_doc_ids": []},
                # validate() refuses an all-abstention set (answerable metrics
                # would be fabricated 0.0s), so keep one answerable query.
                {"id": "q2", "class": "clean_fact", "question": "??", "gold_doc_ids": ["d1"]},
            ],
        )
        gs = KBGoldenSet.from_json(p)
        assert gs.queries[0].is_abstention is True
        assert gs.queries[1].is_abstention is False

    def test_non_dict_json_refuses(self, tmp_path: Path) -> None:
        # Valid JSON that is not an object (list/scalar/null) must refuse cleanly,
        # not crash with AttributeError on .get().
        for payload in ("[]", "null", "42", '"a string"'):
            p = tmp_path / "scalar.json"
            p.write_text(payload)
            with pytest.raises(KBGoldenSetError, match="must be a JSON object"):
                KBGoldenSet.from_json(p)

    def test_lone_surrogate_name_refuses_before_report_output(self, tmp_path: Path) -> None:
        payload = {
            "name": "\ud800",
            "docs": [{"id": "d1", "title": "t", "content": "c"}],
            "queries": [
                {
                    "id": "q1",
                    "class": "clean_fact",
                    "question": "?",
                    "gold_doc_ids": ["d1"],
                }
            ],
        }
        p = tmp_path / "surrogate-name.json"
        # ensure_ascii=True emits valid ASCII JSON whose decoded string contains
        # the lone surrogate, reproducing the later print() UnicodeEncodeError.
        p.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(KBGoldenSetError, match="name.*valid UTF-8"):
            KBGoldenSet.from_json(p)

    def test_terminal_escape_name_refuses_before_report_output(self, tmp_path: Path) -> None:
        payload = {
            "name": "\x1b]0;changed-title\x07",
            "docs": [{"id": "d1", "title": "t", "content": "c"}],
            "queries": [
                {
                    "id": "q1",
                    "class": "clean_fact",
                    "question": "?",
                    "gold_doc_ids": ["d1"],
                }
            ],
        }
        p = tmp_path / "control-name.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(KBGoldenSetError, match="name.*printable text"):
            KBGoldenSet.from_json(p)

    def test_null_docs_refuses(self, tmp_path: Path) -> None:
        # A present-but-null collection would crash the tuple comprehension with
        # TypeError; it must refuse as the documented error instead.
        p = tmp_path / "nulldocs.json"
        p.write_text('{"docs": null, "queries": []}')
        with pytest.raises(KBGoldenSetError, match="'docs' must be a list"):
            KBGoldenSet.from_json(p)

    def test_class_gold_mismatch_refuses(self, tmp_path: Path) -> None:
        # An answerable class with no gold, or abstention WITH gold, would land in
        # the wrong aggregate bucket and report a meaningless metric.
        no_gold = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[{"id": "q1", "class": "clean_fact", "question": "?", "gold_doc_ids": []}],
        )
        with pytest.raises(KBGoldenSetError, match="has no gold docs"):
            KBGoldenSet.from_json(no_gold)
        abst_with_gold = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[{"id": "q1", "class": "abstention", "question": "?", "gold_doc_ids": ["d1"]}],
        )
        with pytest.raises(KBGoldenSetError, match="abstention.*but has gold docs"):
            KBGoldenSet.from_json(abst_with_gold)

    def test_sensitive_path_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The golden path is argv-supplied and agent-reachable, so it must go
        # through the sensitive-path gate. Inject the verdict on the classifier the
        # guard consults, rather than relocating HOME/USERPROFILE -- moving the real
        # protection boundary is a test side effect and does not exercise the gate
        # against the operator's actual home.
        import kiro_crew.security as security

        secret = tmp_path / "looks_ok.json"
        secret.write_text('{"docs": [], "queries": []}')
        real_is_sensitive = security.is_sensitive_path
        monkeypatch.setattr(
            security,
            "is_sensitive_path",
            lambda s: str(secret) in s or real_is_sensitive(s),
        )
        with pytest.raises(KBGoldenSetError, match="protected location"):
            KBGoldenSet.from_json(secret)


# -- end-to-end run against the real store + retriever (toy embedder) ---------


class TestRunKbRetrieval:
    def test_shipped_set_runs_and_scores(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        assert len(report.results) == len(gs.queries)
        head = report.headline(3)
        for key in ("recall_any@3", "mrr@3", "ndcg@3"):
            assert 0.0 <= head[key] <= 1.0

    def test_toy_embedder_finds_clean_fact(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        clean = [r for r in report.results if r.query_class == "clean_fact"]
        assert clean
        assert any(r.recall_any.get(5, 0.0) == 1.0 for r in clean)

    def test_abstention_scored_separately(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        abst = [r for r in report.results if r.is_abstention]
        assert abst
        for r in abst:
            assert r.abstained in (0.0, 1.0)
        by_class = report.by_class(3)
        if "abstention" in by_class:
            assert "abstention_rate" in by_class["abstention"]
            assert "recall_any" not in by_class["abstention"]

    def test_deterministic_across_runs(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        h1 = run_kb_retrieval(gs).headline(3)
        h2 = run_kb_retrieval(gs).headline(3)
        assert h1 == h2

    def test_keyword_only_mode_runs(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs, use_embeddings=False)
        assert len(report.results) == len(gs.queries)

    def test_format_report_mentions_classes(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        text = format_kb_report(run_kb_retrieval(gs), k=3)
        assert "KB retrieval eval" in text
        assert "HEADLINE" in text
        assert "clean_fact" in text

    def test_report_header_names_the_label_revision(self) -> None:
        """The header is the one guard against differencing incomparable prints,
        so it must carry the labels' revision, not just the corpus name: v2's
        gold labels change under the unchanged name ``kb_golden_v2``, and a
        print that shows only the name cannot say which labels it scored."""
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs, use_embeddings=False)
        assert report.label_revision == 1
        text = format_kb_report(report, k=3)
        assert text.splitlines()[0] == "KB retrieval eval: kb_golden_v2 (label revision 1)"

    def test_report_header_marks_unrevised_labels_explicitly(self) -> None:
        """A set without the field prints an explicit marker, not the bare name,
        so an unrevised print is distinguishable both from a revised one and
        from an archived print that predates the field."""
        gs = KBGoldenSet.from_json(v1_golden_set_path())
        report = run_kb_retrieval(gs, use_embeddings=False)
        assert report.label_revision is None
        text = format_kb_report(report, k=3)
        first = text.splitlines()[0]
        assert first == "KB retrieval eval: kb_golden_v1 (unrevised labels)"
        assert "label revision" not in first
        # The rest of the report is unaffected by the missing field.
        assert "HEADLINE @3" in text
        assert "clean_fact" in text

    def test_report_header_differs_between_revisions_of_one_name(self) -> None:
        """Two reports on the same corpus name but different label revisions must
        print different identity lines -- that difference IS the guard."""
        from kiro_crew.eval.bench.kb_retrieval import KBRetrievalReport

        a = KBRetrievalReport(golden_set="same", embedder_id="e", k_values=(3,))
        b = KBRetrievalReport(golden_set="same", embedder_id="e", k_values=(3,), label_revision=1)
        c = KBRetrievalReport(golden_set="same", embedder_id="e", k_values=(3,), label_revision=2)
        idents = {a.golden_set_identity, b.golden_set_identity, c.golden_set_identity}
        assert len(idents) == 3
        assert a.golden_set_identity == "same (unrevised labels)"
        assert b.golden_set_identity == "same (label revision 1)"
        assert c.golden_set_identity == "same (label revision 2)"

    def test_non_default_k_is_computed_not_zero(self) -> None:
        # A cut-off outside DEFAULT_KB_K_VALUES must be explicitly computed, or the
        # headline defaults it to 0.0 -- a false-zero benchmark result. Passing
        # k_values including the requested k is what the CLI does; verify the dict
        # actually carries the key so headline(k) is real.
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs, k_values=(1, 2, 3, 5, 10))
        assert 2 in report.k_values
        for r in report.results:
            assert 2 in r.recall_any  # key present -> headline(2) is measured, not 0.0
        # headline(2) is real; it must NOT silently return 0.0.
        assert report.headline(2)  # does not raise

    def test_uncomputed_k_fails_loud(self) -> None:
        # The old .get(k, 0.0) default silently fabricated a 0.0 for an uncomputed
        # cut-off; now it must raise rather than report a fake low score.
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)  # default k_values = (1, 3, 5, 10)
        with pytest.raises(KBGoldenSetError, match="was not computed"):
            report.headline(2)
        with pytest.raises(KBGoldenSetError, match="was not computed"):
            report.by_class(2)

    def test_empty_embedding_refuses_when_enabled(self) -> None:
        # With embeddings enabled, an embedder that returns empty must fail closed
        # -- otherwise a --real-embedder run silently reports keyword-only metrics
        # under a semantic embedder label. The guard now covers both doc ingest and
        # query search (one fail-closed wrapper). The refusal path must ALSO close
        # the store and remove its temp dir (an open SQLite handle breaks
        # TemporaryDirectory.cleanup() on Windows -- WinError 32).
        import glob
        import tempfile

        gs = KBGoldenSet.from_json(default_golden_set_path())
        before = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        with pytest.raises(KBGoldenSetError, match="empty vector"):
            run_kb_retrieval(gs, embed_fn=lambda _t: [], embedder_id="fake")
        after = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        assert after <= before  # refusal path left no temp dir behind
        # And keyword-only mode (embeddings disabled) runs fine with no embedder.
        report = run_kb_retrieval(gs, embed_fn=lambda _t: [], use_embeddings=False)
        assert len(report.results) == len(gs.queries)

    def test_keyword_only_run_has_keyword_only_identity(self) -> None:
        # A --no-embeddings run must NOT carry a semantic (or toy) embedder label:
        # the printed identity has to reflect that the vector leg was off.
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs, embedder_id="qwen3-embedding:0.6b", use_embeddings=False)
        assert "keyword-only" in report.embedder_id.lower()
        assert "qwen" not in report.embedder_id.lower()
        assert "keyword-only" in format_kb_report(report).lower()

    def test_temp_dir_cleaned_up_after_run(self) -> None:
        # A completed run must close the store and remove its temp dir (the store
        # holds an open SQLite handle; on Windows an un-closed handle would make
        # TemporaryDirectory.cleanup() raise). Assert no kb_eval_* dir lingers.
        import glob
        import tempfile

        gs = KBGoldenSet.from_json(default_golden_set_path())
        before = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        run_kb_retrieval(gs)
        after = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        assert after <= before  # no new kb_eval_ temp dir left behind


# -- CLI dispatch -------------------------------------------------------------


class TestCli:
    def test_toy_path_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "KB retrieval eval" in out
        assert "toy" in out.lower()

    def test_missing_golden_refuses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=str(tmp_path / "nope.json"),
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert "refusing to run" in capsys.readouterr().out

    def test_real_embedder_refuses_when_warmup_times_out(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.knowledge.embedder as emb

        timeouts: list[float | None] = []

        def _wait_ready(self: object, timeout: float | None = None) -> bool:
            timeouts.append(timeout)
            return False

        monkeypatch.setattr(emb.InProcessEmbedder, "wait_ready", _wait_ready)
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=True,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert timeouts == [120.0]
        assert "did not become ready within 120 seconds" in capsys.readouterr().out

    def test_real_embedder_waits_and_reports_actual_model_identity(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cold or custom embedding model is warmed and labeled truthfully.

        The CLI must use the explicit blocking readiness seam instead of the
        non-blocking availability probe, then report the model actually serving
        the run rather than a hardcoded Qwen3 label.
        """
        import kiro_crew.knowledge.embedder as emb
        from kiro_crew.eval.bench.toy_embedder import toy_embed_fn

        fake_model = "custom-embedding:test-9b"
        deterministic = toy_embed_fn()
        timeouts: list[float | None] = []

        def _wait_ready(self: object, timeout: float | None = None) -> bool:
            timeouts.append(timeout)
            return True

        monkeypatch.setattr(emb.InProcessEmbedder, "wait_ready", _wait_ready)
        monkeypatch.setattr(
            emb.InProcessEmbedder,
            "is_available",
            lambda self: pytest.fail("the non-blocking probe must not gate a one-shot run"),
        )
        monkeypatch.setattr(emb.InProcessEmbedder, "model", property(lambda self: fake_model))
        monkeypatch.setattr(emb.InProcessEmbedder, "embed", lambda self, text: deterministic(text))
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=True,
                no_embeddings=False,
            )
        )
        assert rc == 0
        assert timeouts == [120.0]
        out = capsys.readouterr().out
        assert fake_model in out
        assert "qwen" not in out.lower()


class TestRound7Fixes:
    """Regression tests for KB retrieval edge cases."""

    def test_huge_k_does_not_crash(self) -> None:
        """An attacker-sized -k must not overflow the SQL LIMIT arithmetic.

        Regression: ``limit = max(k_values)`` flowed an unbounded cut-off into
        the store's SQL LIMIT, crashing with an uncaught OverflowError for
        e.g. ``-k 4611686018427387904``. The retrieval depth is now clamped to
        the corpus size (a deeper LIMIT cannot change any ranking).
        """
        gs = KBGoldenSet.from_json(default_golden_set_path())
        huge = 2**62
        report = run_kb_retrieval(gs, k_values=(3, huge))
        # The requested cut-off is still computed (scoring is over the ranked
        # list, which is at most corpus-sized) -- never silently dropped.
        assert huge in report.k_values
        head = report.headline(3)
        assert 0.0 <= head["recall_any@3"] <= 1.0

    def test_recall_micro_reported(self) -> None:
        """recall_micro must appear per query, in by_class, headline, and text."""
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        for r in report.results:
            assert set(r.recall_micro.keys()) == set(report.k_values)
        head = report.headline(3)
        assert "recall_micro@3" in head
        by_class = report.by_class(3)
        answerable = [c for c in by_class if c != "abstention"]
        assert answerable and all("recall_micro" in by_class[c] for c in answerable)
        assert "recall_micro" in format_kb_report(report, k=3)

    def test_recall_micro_is_fractional_for_partial_hit(self) -> None:
        """A two-gold query with one hit scores 0.5 micro, not the 1/0 of any/all."""
        from kiro_crew.eval.bench.kb_retrieval import KBQueryResult, KBRetrievalReport
        from kiro_crew.eval.bench.retrieval import recall_micro_at_k

        ranked, gold = ("g1", "z1", "z2"), ("g1", "g2")
        micro = recall_micro_at_k(ranked, gold, 3)
        assert micro == 0.5  # the scorer itself is fractional
        r = KBQueryResult(
            query_id="q-x",
            query_class="multi_hop",
            is_abstention=False,
            recall_any={3: 1.0},
            recall_all={3: 0.0},
            recall_micro={3: micro},
            ndcg={3: 0.5},
            mrr={3: 1.0},
            avg_precision={3: 0.5},
        )
        report = KBRetrievalReport(golden_set="t", embedder_id="toy", k_values=(3,))
        report.results.append(r)
        head = report.headline(3)
        assert head["recall_micro@3"] == 0.5
        assert head["recall_any@3"] == 1.0
        assert head["recall_all@3"] == 0.0

    def test_store_error_is_a_refusal_not_a_traceback(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sqlite failure (e.g. full temp volume) exits 1 with a refusal message.

        Regression: ``run_kb_retrieval``'s store build could raise
        ``sqlite3.OperationalError`` past the CLI dispatch, printing an uncaught
        traceback instead of the deliberate-refusal message every other failure
        path in ``_kb_retrieval`` produces.
        """
        import kiro_crew.eval.bench.kb_retrieval as kbr
        from kiro_crew._sqlite_compat import sqlite3

        def _boom(*_a: object, **_k: object) -> None:
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(kbr, "run_kb_retrieval", _boom)
        # Route the CLI through the patched module attribute.
        import kiro_crew.cli_bench as cb

        rc = cb.bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "refusing to run" in out
        assert "disk is full" in out

    def test_invalid_utf8_golden_is_a_refusal_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A golden file with broken UTF-8 exits 1 with a refusal message.

        Regression: ``KBGoldenSet.from_json`` decodes the file as UTF-8; invalid
        bytes raise ``UnicodeDecodeError``, which escaped the CLI's refusal
        handler and printed an uncaught traceback instead of the deliberate
        ``refusing to run`` message every other malformed input produces.
        """
        bad = tmp_path / "bad-golden.json"
        bad.write_bytes(b'{"name": "x", "docs": [], "queries": [\xff\xfe]}')
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=str(bad),
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert "refusing to run" in capsys.readouterr().out

    def test_all_abstention_golden_refuses(self, tmp_path: Path) -> None:
        """A golden set with only abstention queries must refuse, not report 0.0s.

        Regression: ``headline()`` averages the answerable population; with zero
        answerable queries ``_mean([])`` returns 0.0 for all five answerable
        metrics, fabricating scores for a population that was never measured
        (the same invariant ``_require_k`` enforces for uncomputed cut-offs:
        an unmeasurable metric must be absent or refused, never 0.0).
        """
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[
                {"id": "q1", "class": "abstention", "question": "?", "gold_doc_ids": []},
                {"id": "q2", "class": "abstention", "question": "??", "gold_doc_ids": []},
            ],
        )
        with pytest.raises(KBGoldenSetError, match="no answerable queries"):
            KBGoldenSet.from_json(p)

    def test_deeply_nested_golden_is_a_refusal_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A golden file of deeply nested JSON exits 1 with a refusal message.

        Regression: ``json.loads`` raises ``RecursionError`` on ~10k nested
        arrays; it escaped ``from_json``'s JSONDecodeError conversion and the
        CLI's ``(KBGoldenSetError, UnicodeError)`` refusal handlers, printing
        an uncaught traceback. ``from_json`` now converts it at the source so
        every caller refuses cleanly, whatever parse-time class the payload
        provokes.
        """
        deep = tmp_path / "deep-golden.json"
        deep.write_text("[" * 10000 + "]" * 10000)
        with pytest.raises(KBGoldenSetError, match="not valid JSON|too deeply nested"):
            KBGoldenSet.from_json(deep)
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=str(deep),
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert "refusing to run" in capsys.readouterr().out

    def test_nonregular_golden_refuses_without_reading(self) -> None:
        """A non-regular golden path (device/FIFO) refuses without consuming it.

        Regression: ``--golden /dev/zero`` reached an unbounded ``read()`` and
        exhausted memory. The shared reader opens nonblocking, classifies the SAME
        descriptor with fstat, and refuses before reading any bytes.
        """
        if not Path("/dev/zero").exists():
            pytest.skip("no /dev/zero on this platform")
        with pytest.raises(KBGoldenSetError, match="not a regular file"):
            KBGoldenSet.from_json("/dev/zero")

    def test_oversized_golden_refuses(self, tmp_path: Path) -> None:
        """A golden file over the size cap refuses instead of being slurped.

        Uses a sparse file so the test costs no real disk; the refusal must
        come from the stat-based size check, before the read.
        """
        big = tmp_path / "big-golden.json"
        with open(big, "wb") as fh:
            fh.seek(64 * 1024 * 1024)  # 64 MiB, over the 16 MiB cap
            fh.write(b"\0")
        with pytest.raises(KBGoldenSetError, match="too large"):
            KBGoldenSet.from_json(big)
