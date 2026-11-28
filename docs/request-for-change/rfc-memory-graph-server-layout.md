---
title: Memory graph -- lay out every memory on the server, draw it with cosmos.gl
status: accepted
author: buluoray
created: 2026-11-26
last-audited: 2026-11-26
audited-at: fa33c7d2fd
doc-pr: 42947
implementation-prs: [42948]
tracking-issues: [42180]
supersedes: []
superseded-by: []
---

# RFC: Memory graph, laid out on the server

**Status:** accepted. Accepted at team review, 2026-11-26 (maintainer Ray Xu),
with the memory page and search redesign it belongs to
([#43002](https://github.com/kirodotdev/KiroCrew/pull/43002)). The document
merged in [#42947](https://github.com/kirodotdev/KiroCrew/pull/42947). It
accepts two things: a new file on disk next to each memory store, and a new
frontend dependency (cosmos.gl, §5.6). §5.7 says what the graph shows and what
it no longer shows, and §12 records the decisions made while building it. The
build is [#42948](https://github.com/kirodotdev/KiroCrew/pull/42948), stacked
on the memory page PRs. Every main-side reference below was read at
`3e1a8a99cd`; this revision was checked against `fa33c7d2fd`.

## 1. Summary

The Memory page's graph shows a sample. The server reads at most 200 facts and
200 lessons (`GRAPH_SEMANTIC_LIMIT`, `GRAPH_LESSON_LIMIT` in
`dashboard/handlers/memory_graph.py`), the browser lays them out with a force
simulation, and a note says only part of memory is shown. On a store of 23,010
memories the graph draws 759.

This RFC proposes:

- **Lay out every memory once, on the server.** The result goes in a derived
  file, `graph_layout.sqlite`, next to each store's database.
- **Keep the file current by placing each new or changed row.** Nothing else
  moves. A full rebuild runs in the background when drift is large.
- **Serve the graph by zoom level.** Zoomed out, the server sends clusters with
  counts. Zoomed in, the browser asks for the nodes inside the view box. Search
  and box select run on the server over every node.
- **Place rows with no vector by their tags.**
- **Draw with cosmos.gl 3.x** instead of sigma 3, in a lazy chunk. On an NVIDIA
  T4 it spends about 0.7 ms per frame against sigma's 6.2 ms at 23k, which is
  the headroom phones and larger stores need (§5.6).

## 2. What main has today

- `GET /api/memory/graph` returns up to 200 facts, 200 lessons, 14 days of
  history, preferences and project lines, with edges by project name.
- `website/src/pages/overview/MemoryGraphTab.tsx` lays them out in the browser
  (d3-force) and draws with sigma 3.0.2 + graphology 0.26.0.
- Nothing about the graph is stored on disk.

## 3. Goals

- The graph shows every memory in a store: the global store and each bot's.
- Search, filters and box select cover every node, loaded on screen or not.
- A dense view pans smoothly on machines without a GPU (cloud desktops, VMs,
  phones).
- New memories appear in the right neighbourhood at once, with no full
  relayout.

## 4. Non-goals

- A 3D view.
- Dense similarity edges. The graph draws a few thin links between similar
  memories, under the rules in §12; it never draws every pair above a floor.
- Any change to recall, ranking or what is stored in memory.
- New Python dependencies.

## 5. Design

### 5.1 The layout file

There is one `graph_layout.sqlite` beside each store's `memory.db`. It is a
derived cache. It holds:

- for each memory: x, y, cluster id, recall weight, how it was placed (vector,
  tags or band) and its tokens;
- for each cluster: x, y, radius, count, and its 64-dim centre and 2-axis map;
- the PCA basis (1024 × 64), token positions, and a `meta` row with the
  version, row count, embedding width and incremental count.

It never holds memory text. A missing or corrupt file, or one with an older
`version`, is rebuilt and never read. Deleting it loses nothing.

Size, measured on copies: 5.3 MB for the global store (23k rows), 3.5 MB for a
bot store of 14.7k rows, and 10.5 MB for a synthetic store of 100k rows.

### 5.2 Full build

The build runs in a child process with every BLAS thread variable set to 1. On
a busy 32-core host, numpy's default thread pool made a 23k build 300 times
slower. The child reads the store read-only (it runs in WAL mode):

1. A streaming PCA projects each embedding to 64 dims, in two passes of 1024
   rows each.
2. k-means groups the rows into `2 × √(n/2)` clusters, classical MDS places
   the cluster centres, and a push step removes overlaps.
3. Each cluster spreads its rows over a disc with its own 2-axis PCA. That map
   is kept, so new rows go through it.
4. Rows with no vector are placed by tags (§5.4).

The build took 2.2 s with 109 MB peak RAM on the global copy (23k), and 9.8 s
with 142 MB on the synthetic 100k store. The gateway swaps the finished file
in with one `os.replace`, and the old file serves until then.

**Why our own build, and not UMAP, PaCMAP or openTSNE.** All were measured on
the same store copies and input. Quality is on a 2,000-row sample against the
1024-dim vectors.

| Method | Build at 23k | Peak RAM at 100k | Neighbour cos@10, global / slack | kNN@10, global / slack | New dependencies |
|---|---|---|---|---|---|
| **ours** (PCA-64, k ×2) | **0.6 s** | **178 MB** | **0.649 / 0.671** | **0.235 / 0.265** | **none** |
| PaCMAP 0.9.1 | 7.7 s | 497 MB | 0.592 / 0.629 | 0.179 / 0.210 | faiss-cpu, numba, llvmlite, scikit-learn: about 440 MB |
| UMAP (umap-learn 0.6.0), PCA-50 | 44 s | 688 MB | — | 0.215 / 0.264 | numba, llvmlite, scipy, scikit-learn, pynndescent: about 370 MB |
| openTSNE 1.0.4 | 61 s | 900 MB | — | 0.166 / 0.226 | scipy, scikit-learn: about 170 MB; no Linux arm64 or Windows arm64 wheel |

Build time here excludes the store read. numba 0.63 and later ship no macOS
Intel wheel. The full comparison, with the method, the sources and topic purity
on labelled stores, is section 11 of the design doc that the build PR links.

The mature products split the same way:

- Apple's Embedding Atlas uses UMAP, ported to WebAssembly
  ([algorithms](https://apple.github.io/embedding-atlas/algorithms.html)).
- Nomic Atlas uses UMAP below 50,000 points and its own projection above that
  ([docs](https://docs.nomic.ai/atlas/models/dimensionality-reduction)).

The layout file does not depend on the method. A later build could switch to
UMAP or PaCMAP and change nothing else.

### 5.3 Keeping it current

`place()` puts a new or changed row where a full build would:

- a row with a vector goes through its nearest cluster's map;
- a row without one goes by its tags (§5.4);
- otherwise it goes on the band.

No other row moves. Placing a row takes about 10 ms at 23k and 12 ms at 100k.
This is an out-of-sample extension, as in
[Hägele & Weiskopf 2024](https://arxiv.org/html/2408.04129v1). On the global
copy, rows held out of the build and then placed scored kNN@10 0.114, against
0.125 for UMAP's `transform()`, in 1/30 of the time.

No memory write path calls into the graph, so a graph failure can never fail
a write. Instead, each graph read first brings the file up to date in a worker
thread. When the store's files (database and WAL) changed since the last look,
it reads every live row's id and a 16-byte mark of its vector. Then it places
the rows the file lacks and the rows whose vector appeared or changed (the
embedding backfill), and removes the rows that were forgotten. When nothing
changed, the look is one `stat` per file. A full rebuild starts in the
background when more than 20% of rows were placed incrementally, or when the
embedding width changes.

### 5.4 Rows with no vector

On copies made 2026-11-25:

| Store | Rows without a vector |
|---|---|
| global | 0 of 23,010 |
| slack bot | 9,746 of 14,707 (66%) |
| kiro-crew-rust bot | 81 of 7,766 |

Such a row is placed in these steps, best signal first:

1. **Tags.** It goes to the mean position of the rows that share its tags, or
   share its fact key's namespace (`project.acme_bug.*`). Rare tokens weigh
   more, and tokens on more than 5% of rows are ignored. Up to 3 more rounds
   let rows placed this way place others.
2. **The band.** What is left goes on an outer band, one arc per fact
   namespace.

On the slack copy, 85% of rows get a position that means something. When the
backfill adds a vector, the row moves to its real place.

### 5.5 API

All of it is JSON on the dashboard's authenticated API, with the same store
scoping as today's graph route, and none of it is tied to a renderer:

| Route | Returns |
|---|---|
| `GET /api/memory/graph/overview?store=` | clusters with position, radius and count, plus totals |
| `GET /api/memory/graph/nodes?store=&bbox=&kinds=&cursor=&limit=` | nodes in the box, heaviest first, at most 2,000 per page, with a cursor |
| `GET /api/memory/graph/search?store=&q=&mode=&kind=` | id and position of every record the Memories list lists for the same words |
| `GET /api/memory/graph/select?store=&bbox=&kind=` | how many nodes the box holds, loaded or not |

"View in Memories" for a box selection sends the box itself to the list
(`GET /api/memory/records?box=…&box_kinds=…`). The list resolves it against the
same layout, so a selection of any size reaches the list without sending its ids.

Search reuses the Memories list's own search, so the list and the graph never
disagree. The node weight is recall's weight, computed by recall's own scoring
helper; the browser maps it to opacity and holds no formula.

### 5.6 Renderer: sigma 3 to cosmos.gl 3.x

The graph draws in the user's own browser, on their own device. The canvas
moves from sigma 3 to cosmos.gl 3.x (`@cosmos.gl/graph`, MIT). The reason is
headroom, for two reasons:

- **Phone GPUs** are several times weaker than the T4 measured below.
- **Stores grow.** They hold 15k-23k memories today and will pass 100k.

**Measured 2026-11-26 on an NVIDIA T4.** The host was an EC2 g4dn.xlarge with
Chrome 158 headless and `--use-angle=gl-egl`.
`UNMASKED_RENDERER_WEBGL` read `ANGLE (NVIDIA Corporation, Tesla T4/PCIe/SSE2,
OpenGL ES 3.2)`. The bench panned and zoomed the camera every frame for 5 s at
1280×800 over clustered points:

| Library | Bundle min / gzip | 23k at 60 Hz | 100k at 60 Hz | 23k uncapped | 100k uncapped |
|---|---|---|---|---|---|
| sigma 3.0.2 (today) | 157 / 38 KB | 59.6 fps | 59.6 fps | 157 fps (6.2 ms per frame) | 123 fps (8.1 ms) |
| **cosmos.gl 3.6.0** | **808 / 217 KB** | **59.6 fps** | **59.6 fps** | **1,476 fps (0.7 ms)** | **1,440 fps (0.7 ms)** |
| regl-scatterplot 1.16.0 | 221 / 76 KB | 59.6 fps | 59.6 fps | 1,400 fps | 1,331 fps |

- At 60 Hz both renderers keep up on a T4.
- Uncapped, sigma 3 spends 6.2 ms of the 16.7 ms frame at 23k, and 8.1 ms at
  100k. cosmos.gl spends about 0.7 ms at either size, about 9x the headroom.
- On a device with no GPU (SwiftShader), sigma 3 ran 1.6 fps at 23k and
  cosmos.gl 59 fps.

**What else comes with it:**

- cosmos.gl takes the server's positions as they are, with its simulation off.
  It has rect and polygon select, and it fades points in and out.
- It draws no text and takes no keyboard focus. A thin DOM layer draws labels
  for the top visible nodes, and an offscreen list gives keyboard access. Both
  are measured to stay inside the frame budget.
- It needs luma.gl ~9.3 as a peer. The canvas loads as a lazy chunk when the
  Graph tab opens, so the main bundle and the Bundle Size Gate do not change.
  sigma, graphology and d3-force leave the bundle once nothing else imports
  them.
- `@cosmograph/cosmos` and deepscatter are not used: their licences restrict
  commercial use.

The API returns plain JSON positions and does not depend on the renderer.

### 5.7 What the graph shows

The graph draws the three kinds of memory record: facts, lessons and
experiences. They are the rows the Memories list lists, each one has a vector
or tags to place it by, and a click on one opens it in that list.

It no longer draws the preference, project and history nodes that today's
graph adds (`GET /api/memory/graph`, §2), or the edges by project name:

- **Preferences and projects** are two Markdown documents
  (`preferences.md`, `projects.md`), drawn as one node per bullet line and
  per project heading. A line is not a memory
  record: it has no vector, no tags and no row in the Memories list, so it has
  no place by meaning and nothing for a click to open.
- **History** is the last 14 days of chat, not memory. Recall reads the
  experiences that consolidation writes from it, and those are on the graph.
- **The project edges** joined nodes that shared a project name. Position by
  meaning shows that relation for every record, so the graph draws no project
  edges. It draws a few similarity links instead (§12).

All three stay on the page: preferences and projects on the Profile tab,
experiences in the Memories list and on the graph.

## 6. Migration plan

There is no data migration. The first open of a store with no layout file
starts a build: 0.6-2.2 s at today's sizes. Until it finishes, the graph shows
its normal loading state.

## 7. Backward compatibility

- `GET /api/memory/graph` stays until the new tab ships. Then it is removed
  with its only reader, the old tab.
- A redacted off-host backup drops the layout file and any half-written
  build as a derived index (`snapshot_redact._is_derived_index`), like
  `memory_index.db`; the next graph read rebuilds it.
- Deleting a store deletes the layout file with it.

## 8. Risks

| Risk | Answer |
|---|---|
| A large store makes builds heavy | 100k rows: 9.8 s and 142 MB, in a child process with one thread |
| The layout goes stale as memories change | each graph read places new and changed rows first; a background rebuild runs past 20% drift |
| cosmos.gl is younger than sigma (1.3k stars, v3.6.0 on 2026-11-04) | it is MIT; the API is renderer-independent, so a swap changes only the canvas chunk |
| cosmos.gl draws no text and takes no focus | a DOM label layer for the top visible nodes and an offscreen keyboard list, both measured against the frame budget |

## 9. Security and data considerations

- The layout file holds positions, ids, tokens (tags and key namespaces) and a
  PCA basis, never memory text. Tags and key namespaces already appear in the
  Memories list.
- The file sits beside the store's database, with the same owner and mode.
- No new route is reachable without dashboard authentication, and none takes a
  path from the client.

## 10. Alternatives considered

- **Raise the sample cap.** The browser layout falls apart: a force simulation
  over 23k nodes runs on the main thread.
- **Lay out in the browser with WebAssembly UMAP**, as embedding-atlas does. It
  took 65 s at 23k in the survey, in the browser, on every open.
- **UMAP, PaCMAP or openTSNE on the server.** See §5.2: lower or equal quality
  on our stores, 13-100x slower, and 170-440 MB of new dependencies.
- **Keep sigma 3.** It holds 60 Hz on a T4 at 23k and 100k, but with about
  1/9 of cosmos.gl's headroom, which phones and growing stores would use up
  (§5.6).

## 11. Open questions

None left open; the one this draft had is answered in §12.

## 12. Decision

Accepted at team review, 2026-11-26 (maintainer Ray Xu), with the memory page
redesign ([#43002](https://github.com/kirodotdev/KiroCrew/pull/43002)). These
decisions were made while building it, in
[#42948](https://github.com/kirodotdev/KiroCrew/pull/42948):

- **Renderer: cosmos.gl 3.x.** Ray first kept sigma 3 unless it fell under
  60 fps on a real GPU. It held 60 fps on the T4 (§5.6), and he then chose
  cosmos.gl for its headroom on phones and larger stores, with every feature of
  the sigma tab kept: gestures, click to open, box select, keyboard access and
  reduced motion.
- **Layout: our own build** (PCA-64, k-means, a 2-axis map per cluster), not
  UMAP, PaCMAP or openTSNE, by the measurements in §5.2.
- **Cluster labels: yes.** The build names each cluster by its most
  distinctive tokens, with no model call, and the overview shows them.
- **What it shows:** facts, lessons and experiences only (§5.7).
- **V2 stores do not fade.** V2 recall is age-neutral by default
  (`memory_v2.rank_score`: no decay; an opt-in recency boost only lifts recent
  rows), so the memory page shows no strength for a V2 store
  (`memory_decay.applies`, [#43075](https://github.com/kirodotdev/KiroCrew/pull/43075)).
  The graph follows it: every experience of a V2 store has weight 1, and none
  fades.
- **No work at gateway boot.** The layout code imports numpy, so the dashboard
  registers the graph routes without it and imports the graph service on the
  first graph request.
- **The routes say what they do.** Each one first brings the layout file up to
  date, and may start a background build, so `route_access` names them
  `memory.graph.<route>.refresh`, not `.read`.
- **Where it lives:** a Graph tab on the memory page
  ([rfc-memory-page-audit-and-recall.md](rfc-memory-page-audit-and-recall.md),
  [#43064](https://github.com/kirodotdev/KiroCrew/pull/43064)).
- **Similarity links: a few, thin, tidy.** Ray asked for animated zoom with
  thin nearest-neighbour links on 2026-11-26, and on 2026-11-27 said the first
  links looked messy: related memories may be joined, but strong joining reads
  as clutter. The build keeps each memory's 3 nearest rows at recall's own
  cosine floor (0.55) as `links` in the layout file, and the canvas draws:
  - at most 2 links per memory, closest first;
  - curved links, fainter for a less similar pair and faint at the overview,
    stronger as the view zooms in;
  - at the farthest level, no memory's own links, only one link between two
    neighbouring topics;
  - in the middle levels, links inside a topic only; close up, also a short
    link to a neighbouring topic, never a long one across the map.
- **Later, in their own changes:** group-by, colour-by and density contours.
