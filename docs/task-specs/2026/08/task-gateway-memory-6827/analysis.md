# Investigation: Gateway memory climb (issue #6827)

## Summary

Issue #6827 reports the Kiro Crew Python Gateway (`python.exe`) climbing from a
~600 MB baseline to ~3 GB within roughly 10 minutes on a Windows 11 desktop,
with no user interaction. The environment has 50+ skills, local git-clone
knowledge sources, Telegram, and RSS enabled.

Source analysis identified one large, provable waste in the in-process
embedding runtime: the vendored `Llama` constructor eagerly allocates a
per-token `self.scores` logits buffer sized `(n_batch, n_vocab)` that the
embedding read path never touches. For the shipped Qwen3-Embedding-0.6B model
this was ~1.24 GB of `float32` allocated once at model load. FEAT-002 skips that
allocation outright when the model is opened in embedding mode, reclaiming the
whole ~1.24 GB with no change to `n_ctx`, `n_batch`, `n_ubatch`, or the produced
vectors.

This is a defensive fix for a confirmed baseline over-allocation. It is **not**
proven to fully account for the reported 3 GB climb, because the live symptom
could not be reproduced in this sandbox (see next section). The remaining
sections separate what was proven from source, what was fixed, what was ruled
out, the diagnostic asked of the reporter, and what still needs a real-
environment repro.

## What was reproduced

Nothing live. The reported symptom was **not** reproduced, and here is why:

- This is a **Linux sandbox** in **INTEGRATIONS_ONLY** network mode. There is no
  external network access: PyPI is blocked (verified `pip install` returns a 403
  tunnel error), so `numpy`, `hypothesis`, `aiohttp`, and the rest of the heavy
  test dependencies cannot be installed.
- The in-process embedder needs a ~610 MB GGUF model download to run real
  inference. That download is blocked by the same network policy, so the actual
  embedding path (and therefore the actual `self.scores` allocation) cannot be
  exercised here.
- The live symptom is inherently a **Windows 11 desktop** workload (50+ skills,
  local git-clone knowledge sources, Telegram, RSS, 600 MB -> 3 GB in ~10 min,
  no user interaction). The sandbox cannot run that stack.

All findings below are therefore driven from **source analysis** plus targeted
unit tests that fake the vendored `Llama` class (no model, no network). This
matters for the PR description: we can prove the buffer exists and prove the fix
shrinks it and keeps vectors identical, but we cannot prove in-sandbox that it
eliminates the full 3 GB climb.

## Root-cause candidate

`LlamaCppEmbedder._load_model` (`src/kiro_crew/embeddings.py`, the
`_load_model` method) constructs the vendored `Llama` with `embedding=True`,
`n_ctx=_N_CTX(2048)`, `n_batch=_N_CTX(2048)`, `n_ubatch=_N_UBATCH(512)`.

Upstream's constructor eagerly allocates a per-token scores buffer
(`src/kiro_crew/_vendor/llama_cpp/llama.py`, lines ~476-478):

```python
self.scores: npt.NDArray[np.single] = np.ndarray(
    (n_ctx if logits_all == True else n_batch, self._n_vocab), dtype=np.single
)
```

`logits_all` defaults to `False` at construction, so the shape is
`(n_batch, n_vocab)`. For Qwen3-Embedding-0.6B, `n_vocab` is 151,936, so at
`n_batch == n_ctx == 2048`:

```
2048 * 151_936 * 4 bytes = 1,244,987,392 B  ~=  1.24 GB (1.16 GiB)
```

That buffer is **pure waste on the embedding path**. `Llama.embed` /
`create_embedding` (`src/kiro_crew/_vendor/llama_cpp/llama.py`, ~lines 1060-1185)
return vectors via `llama_get_embeddings_seq` / `llama_get_embeddings` and never
read `self.scores`. (The `logits_all = True` line inside `embed()` at ~line 1088
is a local output-marking flag; it does not resize the constructor's scores
array.) So for an embedding-only model this ~1.24 GB is allocated once at model
load and never used.

This aligns with related issue **#6216**: the growth appears once the in-process
embedding path becomes active, on top of a stable ~606 MB baseline. A single
~1.24 GB allocation at model load is consistent with a large, early step in
resident memory rather than a slow per-request creep.

Cited files/line areas:

- `src/kiro_crew/embeddings.py` -> `LlamaCppEmbedder._load_model` (the `Llama(...)`
  construction call site).
- `src/kiro_crew/_vendor/llama_cpp/llama.py` -> scores allocation (~lines 476-478),
  `embed()` / `create_embedding()` read path (~lines 1060-1185).

## Fix applied (FEAT-002)

Committed as `fix(embeddings): skip the embedder's unused scores buffer (#6827)`.

- Patched the vendored constructor
  (`src/kiro_crew/_vendor/llama_cpp/llama.py`, `Llama.__init__`) to allocate
  `self.scores` with **zero rows** when the model is opened with
  `embedding=True` and `logits_all` off:

  ```python
  n_score_rows = (
      0
      if (embedding and not self._logits_all)
      else (n_ctx if logits_all == True else n_batch)
  )
  self.scores: npt.NDArray[np.single] = np.ndarray(
      (n_score_rows, self._n_vocab), dtype=np.single
  )
  ```

- `src/kiro_crew/embeddings.py` is **unchanged** apart from an explanatory
  comment: `n_ctx`, `n_batch` (still `== n_ctx`), and `n_ubatch` all keep their
  existing values, so accepted context, logical batch, and micro-batching are
  untouched and the produced embedding vectors are **byte-identical for every
  input**, including unspaced text.
- Marked with a `kiro_crew DIVERGENCE FROM UPSTREAM` comment and recorded under
  "Local divergences from upstream" in `src/kiro_crew/_vendor/README.md`, since
  the documented upgrade procedure replaces `llama_cpp/` wholesale and would
  otherwise drop the patch.
- `scripts/vendor_manifest.sha256` regenerated with
  `python scripts/verify_vendor_manifest.py --write`, as
  `src/kiro_crew/_vendor/README.md` prescribes, so the `vendor-manifest` CI job
  stays green and the vendored change shows up in the reviewable diff.

Buffer-size math (n_vocab = 151,936 for Qwen3-Embedding-0.6B):

```
Before:  2048 * 151_936 * 4 bytes = 1,244,987,392 B  ~=  1.24 GB (1.16 GiB)
After:      0 * 151_936 * 4 bytes =             0 B
Reclaimed:                          ~ 1.24 GB, in full
```

The saving is the whole buffer, and it scales with any custom model's vocab
rather than being a fixed fraction of it.

### Why zero rows is safe (every reader of `self.scores` audited)

`self.scores` is read in exactly these places in the vendored file. All the ones
reachable in embedding mode **slice** rather than index, and numpy clamps an
out-of-range slice, so each yields an empty result on a zero-row array instead of
raising:

| Reader | Behaviour at zero rows |
| --- | --- |
| `_scores` property — `self.scores[: self.n_tokens, :]` | empty `(0, n_vocab)` slice, no raise |
| `eval_logits` property — same slice + `.tolist()` | empty list |
| `eval()` — `self.scores[n_past : ...] = logits` | guarded by `if self._logits_all:`, which is exactly the condition the zero-row branch excludes, so unreachable |
| `save_state()` — `scores=self._scores.copy()` | copies the empty slice |
| `load_state()` — assigns into `self.scores[: state.n_tokens, :]` | both sides are `(0, n_vocab)` for a state saved from an embedding model, so shapes match |
| `generate()` — `self._scores[sample_idx - self.n_tokens, :]` | row index; would raise `IndexError` |
| `_create_completion()` — `self._scores[token_offset - 1, :]`, `self._scores[-1, :]`, `self._scores[token_offset, :]`, `logits_to_logprobs(self._scores)` | row index / logprob path; would raise |

The last two rows are the generation and logprob paths. They are unreachable
here for two independent reasons: `src/kiro_crew/embeddings.py` is the only
in-repo constructor of the vendored `Llama` and it calls `create_embedding()`
only; and llama.cpp does not produce usable logits on a context created with
`embeddings=True` in the first place, so upstream generation on an embedding
model is already unsupported. If someone reached them anyway, a zero-row array
raises loudly rather than silently returning wrong logits — which is why zero
rows was chosen over `None` (a `None` would `AttributeError` in the slicing
readers too, including `save_state`).

`__getstate__` already round-trips `embedding` and `logits_all`, so an
unpickled instance re-derives the same row count.

### What this replaces

An earlier revision of this PR instead reduced `n_batch` from 2048 to 1536 at
the call site, shrinking the buffer by 25% (~311 MB). Review found that
`Llama.embed(truncate=True)` clips each input to `n_batch` tokens, so a
`n_batch < n_ctx` creates a band of token counts (1537-2048) that the context
window accepts but the logical batch silently truncates. The only upstream guard
is a **character** clip plus a whitespace-based word count, neither of which
bounds token count for unspaced input — CJK, hex blobs, minified JS — so such an
input could land in that band and be embedded from its prefix only, shifting the
last-token-pooled vector without any error. Skipping the allocation removes both
the truncation band and the remaining 75% of the buffer, so that approach and the
`_MIN_CHARS_PER_TOKEN` floor plus four import-time guards it needed were dropped.

Guarding tests: `test/test_embed_scores_buffer.py`

- fakes the vendored `Llama` class and captures the constructor kwargs to assert
  `embedding=True`, `logits_all` unset, and `n_batch == n_ctx == _N_CTX` — it
  fails if a reduced `n_batch` (and its truncation band) is reintroduced;
- AST-parses the shipped `_vendor/llama_cpp/llama.py`, extracts the real
  `n_score_rows` expression and **evaluates it** under controlled inputs:
  0 rows for `embedding=True, logits_all=False`, and upstream's
  `n_ctx if logits_all else n_batch` otherwise, so generation models are
  provably unaffected;
- asserts the allocation's shape actually consumes `n_score_rows`, so the guard
  cannot be computed and then ignored;
- fails with an explicit "the divergence was lost, probably by a re-vendor from
  upstream" message if `n_score_rows` disappears.

A mutation probe confirmed non-vacuity: reverting the guard to the upstream
expression fails 2 of the 7 tests.

## Leads ruled out (with evidence)

Each of the following was investigated in source and found to be bounded, so it
is **not** the cause of an unbounded climb:

- **(a) Per-embed KV-cache growth.** `Llama.embed()` calls `kv_cache_clear()` +
  `reset()` on every call (`src/kiro_crew/_vendor/llama_cpp/llama.py`, ~lines
  1103-1180). The KV cache is cleared per embed, so it cannot accumulate across
  calls.

- **(b) Compute-thread-pool-per-caller-thread leak.** The embedder serializes all
  inference onto a single owned worker thread (`kc-embed-infer`); callers hand
  work to it rather than each spinning up llama.cpp compute state. See the
  `LlamaCppEmbedder` class docstring and `_infer_loop` in
  `src/kiro_crew/embeddings.py`. One owned thread means no per-caller pool
  growth.

- **(c) Unbounded embed result cache.** The result cache is bounded by
  `functools.lru_cache(maxsize=_EMBED_CACHE_MAX)` with `_EMBED_CACHE_MAX = 128`
  (`src/kiro_crew/embeddings.py`, `_EMBED_CACHE_MAX = 128` at ~line 2154,
  applied to `_cached_embed` at ~line 2181). At ~4 MB for 128 entries this is
  fixed-size and cannot grow unbounded.

- **(d) Skills held per-session as parsed objects.** `SkillsLoader` caches
  (`_fm_cache` / `_iter_cache`) are **shared** and keyed by file path with
  TTL/invalidation, not per-session (`src/kiro_crew/skills.py`, ~lines
  1493-1774). 50+ skills are parsed into a shared, path-keyed cache, so session
  count does not multiply the footprint.

- **(e) folder_watcher retaining per-file buffers for local git clones.** The
  watcher persists per-file state in SQLite (`folder_file_state`) with batched
  `last_seen` flushes; the scan loop does not accumulate unbounded in-memory
  buffers (`src/kiro_crew/knowledge/folder_watcher.py`, scan loop ~lines
  380-540). File state lives in the database, not RAM.

- **(f) MCP/LLM subprocess fan-out.** `src/kiro_crew/knowledge/llm_pool.py` spawns
  `kiro-cli` subprocess workers. That is tracked as issue **#3259** and is
  **explicitly out of scope** for #6827 (subprocess memory is separate from the
  `python.exe` gateway heap under investigation here).

## Diagnostics for the reporter

To determine whether the climb is the in-process embedding path (this fix / and
related #6216) or a distinct leak, run the diagnostic requested in the issue
comment:

1. In `config.json`, set `memory.embedding_provider` to `""` (empty string).
   This is a documented diagnostic: an empty provider is coerced by
   `_coerce_embedding_provider` in `src/kiro_crew/config/loader.py` and makes the
   gateway fall back to keyword/FTS search instead of loading the in-process
   embedder (`knowledge/embedder.create_embedder_from_config`,
   `vector_memory._try_embed`).
2. Restart the gateway and watch `python.exe` for **10 minutes** with no user
   interaction.
   - If it stays around **~600 MB**: the climb is attributable to the in-process
     embedding path -> consolidate this with issue **#6216** (this fix reduces
     that path's baseline).
   - If it still climbs toward **~3 GB**: there is a **distinct leak** independent
     of the embedder -> keep #6827 open.
3. A few minutes **into the climb**, capture a `tracemalloc` snapshot or a `py-spy`
   dump. This repo already ships py-spy support in
   `src/kiro_crew/perf_sampler.py` (py-spy and in-process sampling), so no extra
   tooling install is required.
4. Note whether the growth is **smooth** (steady creep, suggests a real leak) or
   **stepped** (discrete jumps, suggests bounded allocations firing at intervals).

## What still needs real-environment confirmation

The FEAT-002 fix **provably** eliminates a large baseline allocation (~1.24 GB,
with byte-identical vectors), and that is proven from source and by unit tests.
What it does **not** prove is that this fully resolves the reported 3 GB climb.

Confirming that requires a repro the sandbox cannot run:

- A real **Windows 11 desktop** gateway with the real ~610 MB embedding model
  loaded (INTEGRATIONS_ONLY here blocks the model download and PyPI deps).
- The reporter's live configuration: **50+ skills**, **local git-clone knowledge
  sources**, **Telegram**, and **RSS** enabled, left idle with no user
  interaction.

Follow-up measurements the maintainer/reporter should take post-fix:

1. Record the resident set of `python.exe` at model-load time before and after
   the fix; confirm the ~1.24 GB baseline reduction shows up.
2. Re-run the 10-minute idle watch with the embedder enabled and record the
   peak. Compare against the pre-fix ~3 GB peak.
3. Run the `memory.embedding_provider=""` diagnostic above to split embedder
   baseline from any residual climb.
4. If a climb persists with the embedder disabled, attach the `tracemalloc` /
   `py-spy` dump (via `src/kiro_crew/perf_sampler.py`) and note smooth vs stepped
   growth so the remaining leak can be localized outside the embedding path.
