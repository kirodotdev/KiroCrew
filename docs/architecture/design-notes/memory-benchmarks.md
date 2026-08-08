# Memory benchmarks (LongMemEval, LoCoMo)

`kirocrew bench` measures the Kiro Crew memory layer against two published benchmarks.
It exists to answer one question the existing `kirocrew eval` cannot: **did this
change make the agent's memory better or worse?**

## Why not extend `kirocrew eval`

`kirocrew eval` runs 4 hand-written scenarios carrying 19 substring assertions, in
a single pass. Three properties make it unable to answer that question, and none of
them are fixed by adding scenarios:

* **No repetitions and no seed.** `temperature`, `top_p` and `seed` are not
  threaded through the provider stack at all — the only sampling-adjacent knob is
  `reasoning_effort`. Every score is one draw from a distribution.
* **19 binary assertions.** The smallest observable effect is one flipped
  assertion, ~5pp, which is well inside the noise of a single draw.
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
`cosine * (0.7 + 0.3 * importance) * exp(-0.03 * days_old)`. A decay rate of
0.03/day means `ln2 / 0.03 ≈ 23` days: a memory three weeks old is scored half as
relevant as an identical one from today. Across three months the factor is
`exp(-0.03 * 90) ≈ 0.067`, a 15× penalty — far outside the realistic dynamic range
of cosine similarity (~0.2–0.9). **Anything older than a couple of months cannot
outrank a marginally-relevant recent memory, however relevant it is.**

Measured over all 1 977 scorable LoCoMo queries, same corpus / ranker / embedder,
the only difference being whether the decay term is allowed to act:

| | `--timeline now` (decay neutral) | `--timeline anchored` (real gaps) |
|---|---|---|
| strict session recall@5 | 0.4901 | 0.2279 |
| queries where @5 is measurable | 1 977 / 1 977 | **746 / 1 977** |
| queries where @8 is measurable | 1 977 / 1 977 | **23 / 1 977** |
| queries where @10 is measurable | 1 966 / 1 977 | **0** |

The recall drop is the obvious half. **The denominators are the sharper signal.** A
cut-off is only measurable when the retrieval window actually exposed that many
distinct sessions (see below). With the decay active only 746 queries see 5 distinct
sessions and only 23 see 8 — the top fragments collapse into the newest one or two
sessions. The decay therefore does not merely rank relevant old sessions lower; it
makes the retrieved set **monotonous**, so no practical `k` reaches the older
material.

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
four decimals before sorting (`vector_memory.py:1448` on the FAISS path, `:1547` on
the sqlite path). Solving `cosine * 0.85 * exp(-0.03d) < 0.00005` gives d ≈ 306
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

**Also found while building this:** on this Linux host `libllama.so` is absent from
the vendored `llama_cpp_libs/linux_x86_64/` payload (only `libggml*.so` are
present), so `get_shared_embedder().wait_ready()` returns False and
`make_sync_embed_fn()` returns `None` for every input. Semantic memory silently
degrades to FTS5 keyword `LIKE` matching. The harness refuses to run in that state
rather than reporting a substring-overlap score as a retrieval number.

## Design constraints worth knowing before changing this

**One instance, one store.** `longmemeval_s` carries ~40 sessions across 500
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
exist (`LOWER(SUBSTR(text, 1, 80))`, `vector_memory.py:1126` and `:1184`)
**unconditionally** — `dedup_threshold` is never consulted for that path. The
cosine near-duplicate check that *does* use the threshold is gated on a live FAISS
index (`:1200-1210`), so **on a host without faiss, near-duplicate dedup never runs
at all and `dedup_threshold` is dead config in both directions**. Semantic
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

`bench/stats.py` carries the paired-interleaved-median protocol for the noisy
end-to-end half: warmups discarded, ≥2 reps, **median never mean**, arms measured
alternating so host drift cancels in the paired delta, and a noise band from 2σ of
the untouched baseline. The protocol is lifted from
`auto_improvement/spine/measurer.py`, which already gets this right; its code is not
reusable because it is shaped around a git worktree and a duration to be minimized.

It refuses to attach a confidence band to the deterministic ruler, and refuses to
omit one from the stochastic one. `verdict` distinguishes `unchanged` (a
deterministic zero delta) from `inconclusive` (a noisy delta inside the band) —
calling the latter "unchanged" would license the wrong conclusion.

`sensitivity_check` is the canary: it proves the ruler can resolve a difference
known to exist before any null result from it is believed. A ruler that reports "no
change" because it is blind looks exactly like one that reports "no change" because
there was none.
