# Memory benchmarks (LongMemEval, LoCoMo)

`kirocrew bench` measures the Kiro Crew memory layer against two published benchmarks.
It exists to answer one question the existing `kirocrew eval` cannot: **did this
change make the agent's memory better or worse?**

## Why not extend `kirocrew eval`

`kirocrew eval` runs a handful of hand-written scenarios carrying substring
assertions, in a single pass. Three properties make it unable to answer that question, and none of
them are fixed by adding scenarios:

* **No repetitions and no seed.** `temperature`, `top_p` and `seed` are not
  threaded through the provider stack at all — the only sampling-adjacent knob is
  `reasoning_effort`. Every score is one draw from a distribution.
* **Binary assertions.** The smallest observable effect is one flipped
  assertion, a coarse step that sits inside the noise of a single draw.
* **No baseline comparison.** Two runs produce two numbers with no way to tell a
  real change from a different sample.

`bench` splits the measurement so that the useful half is not exposed to any of
this.

## The two rulers

### Retrieval — deterministic, cheap, the default instrument

Ingests each benchmark instance's haystack into its own real `VectorMemoryStore`,
runs every question through the real `search_episodic`, and scores whether the gold
evidence surfaced. The embedder is local and the ranker is deterministic, so **the
delta between two commits is exact** — one pass, no repetitions, no confidence
interval to argue about.

Reported at session and turn level, because they fail differently: session recall
asks whether the right conversation was reachable, turn recall whether the right
utterance ranked. Diversity reranking routinely improves one while degrading the
other, and a blended number would hide that.

Four metrics per cut-off. `recall_all@k` (every gold item inside the window) is the
headline, because surfacing three of four required sessions does not let a model
answer a multi-hop question — scoring that 0.75 overstates the memory layer's
usefulness. `recall_any`, `recall_micro` and `ndcg` are reported alongside it.

### Answers — the datasets' official metrics

* **LoCoMo: token-F1 per category, no LLM.** Ported faithfully from
  `task_eval/evaluation.py`, including the Porter stemming that upstream applies to
  both sides. Category 5 (adversarial) is not F1 at all — it scores 1 if the output
  contains `no information available` or `not mentioned`. Category 1 (multi-hop)
  splits *both* sides on `,` and takes mean-of-max, which is recall-shaped: missing
  a sub-answer costs, a spurious extra one does not. Category 3 pre-truncates gold
  at `;`. **This is the only end-to-end score obtainable without an external API
  key**, which makes it the default.
* **LongMemEval: LLM judge, optional.** Five distinct prompts (four non-abstention
  templates covering six question types, plus an abstention template selected by
  `_abs` in the question id), `temperature=0`, `max_tokens=10`, and a label rule
  that is literally `'yes' in reply.lower()`. When no judge is wired, items are
  reported as **unscored** — never as zeros. An unscored item carries
  `score=None` so an aggregator that forgets to filter raises rather than
  publishing a depressed accuracy.

One deliberate divergence: upstream stems with nltk's `PorterStemmer`, which
defaults to `NLTK_EXTENSIONS` (an irregular-form table, and words of length ≤ 2 left
alone). nltk is not a Kiro Crew dependency, so the port uses `snowballstemmer`'s
original Porter algorithm, already a hard dependency. They agree on the large
majority of English tokens but are not bit-identical, so a LoCoMo number from this
harness can differ from a published one in the third decimal.

## What the first run found

Running the retrieval ruler over LoCoMo surfaced two compounding defects in
episodic ranking. Both are in the production read path, not in the harness.

**1. The recency decay has a 23-day half-life.** `search_episodic` scores
`cosine * (0.7 + 0.3 * importance) * exp(-rate * days_old)`, where `rate` is
`vector_memory._DEFAULT_DECAY_RATE` (0.03/day) unless `memory.decay_rates` overrides
it for a tag; overrides are clamped to `_DECAY_RATE_MIN`..`_DECAY_RATE_MAX`. A decay
rate of
0.03/day means `ln2 / 0.03 ≈ 23` days: a memory three weeks old is scored half as
relevant as an identical one from today. Across three months the factor is
`exp(-0.03 * 90) ≈ 0.067`, a 15× penalty — far outside the realistic dynamic range
of cosine similarity (~0.2–0.9). **Anything older than a couple of months cannot
outrank a marginally-relevant recent memory, however relevant it is.**

> **The `now` column is measured on the corrected population; the `anchored` column is
> not.** The harness excludes all **446 LoCoMo adversarial items**
> (`BenchQuery.scorable_retrieval`): every one is `unanswerable` and carries gold
> evidence, and the correct behaviour for them is *refusal*, so scoring retrieval there
> would count 22.6% of the population with the sign flipped. That leaves **1 531**
> scorable queries. The `now` arm is measured on them (55.2 min,
> `null_embeddings = 0`); the `anchored` figures below come from the pre-correction
> population of 1 977 queries and are marked as such.

Measured with the **real Qwen3-Embedding-0.6B model** (`qwen3-embedding:0.6b@1024`,
`sqlite_cosine` backend, `null_embeddings = 0`), same corpus / ranker / embedder, the
only difference being whether the decay term is allowed to act:

| | `--timeline now` (decay neutral) | `--timeline anchored` (real gaps) |
|---|---|---|
| strict session recall@1 | **0.4742** (1 531 queries) | 0.0814 (1 977 queries)¹ |
| strict session recall@3 | **0.6303** (1 531 queries) | 0.1388 (1 484 queries)¹ |
| strict session recall@5 | **0.7139** (1 531 queries) | 0.3288 (**73** queries)¹ |
| strict turn recall@5 | **0.4304** (1 531 queries) | 0.0754 (1 977 queries)¹ |
| queries where @5 is measurable | 1 531 / 1 531 | **73 / 1 977**¹ |
| queries where @10 is measurable | 1 405 / 1 531 | 0¹ |

¹ The `anchored` column is from the pre-correction run over 1 977 queries, so it is
**not** directly comparable to the `now` column beside it, and its re-measurement on
the corrected population is outstanding. The decay *effect* is not in doubt — the
mechanism is arithmetic, and the measurability collapse below is far too large to be a
population artefact — but the anchored numbers themselves must be re-run before being
quoted.

Excluding the 446 adversarial queries does not move every metric the same way: it
lowers session recall (0.4942 → 0.4742 at @1) and **raises** turn recall
(0.4112 → 0.4304 at @5). Those items are therefore easier than average at session
level and harder at turn level — their gold session is usually retrieved while the
specific gold turn is not.

Read `recall@1` and `turn recall@5` first: within a single arm those are the cut-offs
measurable on every scorable query, so nothing about them is bounded by the retrieval
window.

The `@5` row is included to show the trap, not the result: 0.7139 against 0.3288
looks like a smaller gap, but the second figure is an average over 73 queries (3.7%
of its 1 977-query population) and the first over 1 531. `compare_reports` refuses that pairing for
exactly this reason.

**The denominators are the sharper signal.** A cut-off is only measurable when the
retrieval window actually exposed that many distinct sessions (see below). With the
decay active only 73 queries see 5 distinct sessions and none see 10 — the top
fragments collapse into the newest one or two sessions. The decay therefore does not
merely rank relevant old sessions lower; it makes the retrieved set **monotonous**,
so no practical `k` reaches the older material. `unattributed_hits` rises from 369 to
1 023 across the two arms in the pre-correction run, consistent with the same
collapse.

The harness quantifies the magnitude in the report itself: this corpus spans 293
days, so `exp(-0.03 * 293) ≈ 1.52e-04` separates its oldest sessions from its
newest. At that magnitude recency does not merely tilt the ranking — it dominates
semantic similarity outright.

**On the `round(score, 4)` collapse:** these two arms confirm the decay, and they do
*not* isolate the rounding. Scores near the `1.52e-04` floor do collapse into
identical 4-decimal values, and the measurability collapse is consistent with that,
but decay and rounding are confounded in the `anchored` arm — separating them needs a
third arm with the decay active and the rounding removed. Stated here rather than
implied, because the two defects have the same visible symptom.

*(The toy stand-in embedder, which scores term overlap rather than semantics, gives
0.4901 / 0.2279 with 746 measurable at @5. Those figures describe a different ranker,
so they are kept out of this table.)*

### A session cut-off is not free to choose

Retrieval asks the store for `limit` **fragments**; the distinct sessions among them
are however many they span. With a 20-fragment window and the decay neutralized that
is 8–14 distinct sessions per query (median 12), so a session-level `recall_all@20`
is observable for essentially nobody — and computing it anyway reports a value
bounded by the window rather than by the ranker.

Enlarging the window is not the fix: MMR reranking is applied to the returned set, so
requesting more fragments changes the composition of the result and measures a
configuration production never runs. Instead a cut-off is scored only on queries
whose observed ranked list has at least `k` entries, that surviving count is printed
as the row's denominator, and a cut-off that survives for nobody is omitted with the
reason stated rather than shown as a low score.

**2. `round(score, 4)` destroys ordering past ~306 days.** The score is rounded to
four decimals before sorting: in `vector_memory_runtime/episodic_search.py`,
`search_episodic` on the FAISS path and `episodic_candidate` and
`rank_from_scoring_set` on the stored-vector path (the store methods
`search_episodic`, `_episodic_candidate` and `_rank_from_scoring_set` delegate to
them). Solving `cosine * 0.85 * exp(-0.03d) < 0.00005` gives d ≈ 306
days, after which every candidate ties at `0.0` and the "ranking" is whatever order
sqlite returned. Measured directly:

| memory age | returned scores | distinct values |
|---|---|---|
| 0 d | 0.4907, 0.0, 0.0 | 2 |
| 300 d | 0.0001, 0.0, 0.0 | 2 (one significant digit left) |
| 400 d | 0.0, 0.0, 0.0 | **1 — all tied** |

This also explains an initially confusing result: `--timeline literal` scored
*higher* than `--timeline anchored` on the same corpus. With 2023 dates every score
rounds to `0.0`, all candidates tie, and the sort becomes a no-op — the number is an
artifact of ties, not ranking.

**Degraded embedders are refused, not scored.** When the vendored `llama_cpp_libs`
payload for the host platform is incomplete, `get_shared_embedder().wait_ready()`
returns False and `make_sync_embed_fn()` returns `None` for every input, so semantic
memory degrades to FTS5 keyword `LIKE` matching. The harness refuses to run in that
state rather than reporting a substring-overlap score as a retrieval number.

## Running it on a schedule

`.github/workflows/memory-benchmark.yml` runs the retrieval ruler nightly at
08:00 UTC (and on `workflow_dispatch`), and reports the **drift from the last
accepted baseline** rather than an absolute number.

The baseline lives in `bench_baselines/accepted/` and only changes when a human
merges a baseline PR. The workflow pushes the baseline branch and opens that PR when
GitHub Actions may create pull requests; where it may not (this repository's
setting), it posts a compare link a maintainer opens instead. The PR body carries
`Part of #16362` for the Issue Gate lane. That is deliberate: it makes re-baselining an
explicit act, so "the number moved" is always measured against a figure someone
agreed to. `MANIFEST.in` prunes the directory, so baselines never ride into an
sdist.

**What fails the job is the instrument breaking, not the score moving.** A corpus
checksum mismatch, an embedder that will not load, or a crashed run all exit
non-zero, because each one means the next measurement would be silently
meaningless. A recall regression does not fail it — the run pushes a branch carrying
the new numbers and the comparison, and a human decides whether to accept.

Three details that are load-bearing:

* The model cache is keyed on `_GGUF_SHA256`, not a version string, so swapping
  the embedding model invalidates the cache *and* invalidates comparisons against
  baselines from the previous vector space — which `compare` then refuses, as it
  should.
* The run never passes `--toy-embedder`. Since the harness refuses to run when the
  real model is not resident, a failed model download fails the job loudly instead
  of quietly filling the trend line with term-overlap scores.
* The job is single-flight. Two concurrent runs would contend for one in-process
  embedder, so the timings would describe the contention rather than the code.

Not built, and deliberately: a **path-filtered PR comment** for changes to
`vector_memory.py` / `vector_memory_runtime/` / `embeddings.py` / `context.py`. It
is the natural second step once the nightly has produced a stable baseline for a
few weeks, and it
should stay a comment rather than a check for the reasons in the workflow's own
header comment.

### Symlink protection is weaker on Windows, by an explicit decision

Writes go through `open_write_nofollow`, which guards the path, walks the resolved
parent chain one component at a time with `O_NOFOLLOW` on each `openat`, and then opens
the final name **as given** with `O_NOFOLLOW`. Three separate protections, each covering
something the others do not:

* **final component** — a symlink at the report name is refused rather than followed;
* **ancestors** — a directory that became a symlink after the parent was resolved fails
  `O_NOFOLLOW` during the walk. A single `os.open(parent, O_DIRECTORY)` is *not*
  sufficient here: it carries no `O_NOFOLLOW`, so it follows such a link and then pins
  the attacker's target. Pinning protects everything after the open and nothing before
  it;
* **hardlinks** — `st_nlink > 1` on the open descriptor is refused. A hardlink shares
  its target's inode, so no path check can see it: `realpath` yields the alias's own
  name and `is_symlink()` is False. `O_TRUNC` is deliberately absent from the open and
  applied afterwards, because truncating at open time destroys the file before its
  inode can be judged.

Two limits, stated because a security note that implies completeness is worse than one
that does not:

* A component swapped **before** the parent is resolved is followed by that resolution.
  Closing it would mean refusing every symlinked ancestor, which breaks
  `--out-dir /tmp/...` on macOS, where `/tmp` is itself a link — and this repo builds
  there. The window is narrowed from guard-to-open down to resolve-to-first-`openat`,
  which contains no I/O.
* `O_NOFOLLOW` and `dir_fd` do not exist on Windows, so that platform gets an explicit
  `is_symlink()` pre-check for the final component and **no ancestor protection at
  all**. The stronger option is a ctypes `CreateFileW` with
  `FILE_FLAG_OPEN_REPARSE_POINT`; it was not taken because it cannot be exercised on the
  machine this harness is developed on, and unverifiable security code in a benchmark
  tool is a worse trade than a weaker guard whose limit is written down and tested.

Every one of those properties is asserted, including the limits: `test_bench_windows_symlink.py`
deletes `os.O_NOFOLLOW` to run the Windows branch on Linux, and
`test_bench_pinned_parent.py` forces the unpinned path and asserts the write **is**
redirected. If a future change closes a gap, those tests fail — which is the signal to
update this section rather than to weaken them.

The **read** path differs on purpose: it resolves, re-checks the resolved target against
`is_sensitive_path`, opens the canonical path, and refuses `st_nlink > 1`. A link to an
ordinary file therefore stays readable, because refusing every link would break
legitimate layouts such as a corpus cache symlinked to another disk, while a link into a
protected path is refused through the link. A write through a link destroys its target;
a read through one discloses only what the resolved-target check already approved.

### Adversarial queries are not scored for retrieval

LoCoMo's category-5 items are marked `unanswerable`: the correct behaviour is to
*refuse*, not to surface evidence. All 446 of them nonetheless carry gold refs, so a
recall metric that includes them rewards exactly the wrong outcome — and since they are
22.6% of the otherwise-scorable population, that is not a rounding effect.

`BenchQuery.scorable_retrieval` therefore requires resolvable gold **and**
`not unanswerable`. The rule lives on the property rather than at the two filter sites
so the retrieval loop and the `skipped_unscorable` count cannot disagree, and so a third
caller cannot forget half of it. `test_bench_round12.py` pins the resulting population
(1 531 of 1 986) against the cached corpus, so a corpus revision that moves it fails a
test instead of silently changing what the published figures mean.

The 455 excluded queries are reported **by reason**, because the two reasons mean
opposite things: **446 are unanswerable by design** (the dataset working as intended) and
**9 have no resolvable gold** (the dataset's own bookkeeping failing — a `dia_id` present
in no conversation). Counting `unanswerable` over the already-filtered results would
always give zero and print the whole 455 as "with no resolvable gold" — inviting the
reader to distrust the corpus rather than read the denominator. `test_bench_round17.py` pins both counts.

Judging refusal behaviour on those 446 items is a different measurement — it needs an
abstention scorer, not a recall one — and this harness does not attempt it.

## Design constraints worth knowing before changing this

### Session granularity has no turn level

Under `--granularity session` the ingested unit is a whole transcript, so a search
hit can only be attributed back to a **session** id. The turn-level metric block
is therefore **omitted** from the report rather than computed: scoring session ids
against gold turn ids yields a number that is arithmetically well-formed and
semantically empty. Dropped-gold accounting in that mode names the gold *turns*
contained in the dropped session, because that is what was actually lost — testing
the session id against a set of turn ids never matches, and would give a clean
bill of health that could not fail.

Session granularity is also not runnable on LoCoMo as shipped: ingest refuses with
`IngestError` any fragment longer than the store's `_EPISODIC_TEXT_MAX` episodic
limit, and most whole LoCoMo sessions exceed it. Use turn granularity, or raise the
store's limit.

### A NULL embedding aborts the run

`prepare_embedder` refuses at startup when the model is not resident. That
guarantee now extends to the whole run: an embedding failure mid-ingest raises
rather than storing a NULL row and carrying on. Blank text is filtered before the
embed call, so a `None` there is an inference failure, never a benign empty
input. The earlier behaviour counted the NULLs and surfaced a warning, which was
visible but not sufficient — a warning does not stop the headline recall number
from being published as a semantic measurement, and a NULL row is reachable only
through the FTS5 keyword fallback.

### The embedder identity describes the live embedder

It is read from the running embedder, not from the module constants.
`KIROCREW_EMBED_MODEL_PATH` (and `memory.embed_model_path`) make Kiro Crew run a
different model, and the vector width can be adopted from the model file itself.
Since `compare_reports` refuses only when two identities **differ**, recording the
bundled constants for a custom run would let two different vector spaces be
diffed and the delta called exact. An identity that cannot be read is a refusal,
not a fallback to the constants.

**One instance, one store.** This and the dedup behaviour below are Global (V1)
store behaviour, which is the store the bench harness builds; a V2 store has no
episodic cap (`_enforce_episodic_cap` returns early) and dedups on exact text only.
`longmemeval_s` carries ~40 sessions across 500
instances. Merged into a single store that overruns `episodic_max` (10 000) and
`_enforce_episodic_cap` starts tombstoning by `importance ASC, created_at ASC` —
deleting the oldest evidence first. The measurement would be reporting the eviction
policy.

**The oracle variant cannot measure retrieval.** `longmemeval_oracle` is the
smallest and most tempting variant, and its haystack contains *only* evidence
sessions — verified `set(gold_sessions) == set(all_sessions)` for 500/500 instances.
Recall against it is trivially 1.0. `corpus_has_distractors()` refuses the run and
names `longmemeval_s` as the fix.

**Dedup can eat gold, and does not work the way its config suggests.**
`write_episodic` rejects any text whose lowercased first 80 characters already
exist (`LOWER(SUBSTR(text, 1, 80))` in `vector_memory.write_episodic_outcome`,
which `write_episodic` wraps)
**unconditionally** — `dedup_threshold` is never consulted for that path. The
cosine near-duplicate check that *does* use the threshold is gated on a live FAISS
index in the same function, so **on a host without faiss, near-duplicate dedup never
runs at all and `dedup_threshold` is dead config in both directions**. Semantic
near-duplicates accumulate unbounded there; byte-identical text is always dropped
everywhere. `--no-dedup` therefore relaxes near-duplicate rejection only, and only
where FAISS is present. Refused fragments are counted either way, and gold ones
reported separately, so an unreachable recall ceiling is visible rather than
silent.

**Attribution never touches `tags`.** A search hit is mapped back to a turn id via
its text, not via a tag or a row id. Tags are visible to the FTS5 fallback's
`tags LIKE` clause, so putting ids there would open a label channel in a field the
ranker reads. `conversation_id` carries the session id because no backend searches
it.

**No label leaks from session ids.** LongMemEval prefixes evidence sessions with
`answer_`. Gold comes from `answer_session_ids` only; nothing parses that prefix.

**Corpora are fetched, not vendored,** pinned to immutable upstream revisions (a git
commit for LoCoMo, a repo revision for the HuggingFace dataset). `longmemeval_s` is
277 MB and `_m` is 2.7 GB. Integrity has two honestly-labelled tiers:
`pinned-upstream` (hardcoded SHA-256, mismatch is a hard error) and
`pinned-on-first-fetch` (a sidecar written on first download, catching drift from
the second run onward).

## Usage

```bash
kirocrew bench list                    # corpora, sizes, what is cached
kirocrew bench fetch locomo10          # download + verify (2.8 MB)

# the deterministic ruler
kirocrew bench retrieval locomo10

# isolate semantic ranking from the recency decay
kirocrew bench retrieval locomo10 --timeline now

# relax near-duplicate rejection (byte-identical text is always dropped regardless)
kirocrew bench retrieval locomo10 --no-dedup

# smoke the harness on a host that cannot load the embedder
kirocrew bench retrieval locomo10 --instances 1 --queries 25 --toy-embedder

# A/B two commits
kirocrew bench retrieval locomo10 --stem before   # on main
kirocrew bench retrieval locomo10 --stem after    # on the branch
kirocrew bench compare bench_results/before.json bench_results/after.json
```

`compare` refuses to attribute a delta when the two runs disagree on corpus
fingerprint, ingest config, retrieval config or search backend — any of those makes
the difference unattributable to the code change. The store picks one of several
ranking backends based on which optional dependencies import (FAISS inner product,
stdlib cosine, or FTS5 keyword), so two hosts can rank the same corpus differently.

## The statistics layer

`src/kiro_crew/eval/bench/stats.py` carries the paired-interleaved-median protocol for the noisy
end-to-end half: warmups discarded, ≥2 reps, **median never mean**, arms measured
alternating so host drift cancels in the paired delta, and a noise band from 2σ of
the untouched baseline. The protocol is lifted from
`src/kiro_crew/apps/builtins/auto_improvement/spine/measurer.py`, which already gets this right; its code is not
reusable because it is shaped around a git worktree and a duration to be minimized.

It refuses to attach a confidence band to the deterministic ruler, and refuses to
omit one from the stochastic one. `verdict` distinguishes `unchanged` (a
deterministic zero delta) from `inconclusive` (a noisy delta inside the band) —
calling the latter "unchanged" would license the wrong conclusion.

`sensitivity_check` is the canary: it proves the ruler can resolve a difference
known to exist before any null result from it is believed. A ruler that reports "no
change" because it is blind looks exactly like one that reports "no change" because
there was none.
