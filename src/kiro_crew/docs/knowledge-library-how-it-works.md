# Knowledge Library — How the Graph Works

## Overview

The Knowledge Library builds a graph from documents without embeddings. It uses LLM extraction to identify entities and their relationships, then connects them through shared entity names across chunks.

## 1. Chunking

**Supported file types**: Alongside plain-text, markdown, and Org-mode (`.org`), KB folders can ingest PDF (via `pdfplumber`) and DOCX (via `python-docx`); both deps are declared in `setup.cfg` so the binary formats work at runtime.

Files are split into ~400 token chunks using recursive separator splitting (LightRAG style):
- Separators tried in order: `\n\n` → `\n` → ` `
- Overlap: 100 tokens between chunks for context preservation
- Cap: 50 chunks max per file
- A typical design doc (5000-8000 tokens) produces 12-20 chunks

## 2. Extraction (Per Chunk)

Each chunk is sent independently to the `kirocrew-knowledge` agent (Claude Haiku 4.5, cheapest model at 0.4x rate). The LLM returns structured JSON:

```json
{
  "title": "Auth token storage design",
  "entities": [
    {"name": "AuthService", "type": "service", "description": "Handles JWT issuance"},
    {"name": "DynamoDB", "type": "technology", "description": "NoSQL database for tokens"}
  ],
  "relations": [
    {"source": "AuthService", "target": "DynamoDB", "type": "uses", "description": "Stores refresh tokens"}
  ],
  "category": "design_doc",
  "summary": "The auth service issues JWT access tokens with 1h expiry..."
}
```

**Worker pool**: 3 ACP sessions process chunks in parallel (round-robin distribution). Workers are reused across files in a batch, destroyed after the batch completes.

## 3. Graph Construction

### Entities → Nodes

Each extracted entity becomes a node in the graph:
- Deduplication: case-insensitive name matching + alias lookup
- If "DynamoDB" appears in chunk 1 and chunk 5, both map to the same node
- Stored in SQLite `entities` table + in-memory `SimpleDiGraph`

### Relations → Edges

Each extracted relation becomes a directed edge:
- Only created between entities extracted from the **same chunk**
- Edge types: `owns | uses | works_on | part_of | calls | depends_on`
- Stored in SQLite `entity_relations` table + in-memory graph

### Cross-Chunk Connections

There is NO cross-chunk relation extraction (too expensive). Connections across chunks happen through **shared entity names**:

```
Chunk 1: AuthService ──uses──► DynamoDB
Chunk 5: BackupService ──depends_on──► DynamoDB

Graph result:
  AuthService ──uses──► DynamoDB ◄──depends_on── BackupService
```

The shared "DynamoDB" node creates an implicit connection between AuthService and BackupService — they're 2 hops apart in the graph.

### Mentions

Every entity-in-chunk creates a `mention` record linking the item (chunk) to the entity. This enables: "show me all chunks that mention DynamoDB."

## 4. Search & Retrieval

### Without Embeddings (Current)

Two retrieval paths, fused with RRF (Reciprocal Rank Fusion):

**Path 1: FTS5 Keyword Search**
- SQLite FTS5 index on item content
- User query → tokenized → matched against content
- Returns ranked chunks by keyword relevance

**Path 2: Graph Traversal**
- User query → find matching entities by name
- Traverse graph (1-2 hops) to find connected entities
- Look up mentions → find chunks that discuss those entities
- Returns chunks ranked by graph proximity

**Fusion**: Results from both paths are merged using RRF scoring, giving weight to items that appear in both result sets.

### Citations

`local_knowledge_search` surfaces where each result came from. Every hit carries its `section_title` and chunk line range, plus a per-document locator resolved from the source type. The citation renders as:

```
**Source:** [local_folder] design-docs — Auth token storage (lines 42-71)
**File:** design-docs/auth/token-storage.md
```

- **Source line**: `**Source:** [type] name — section (lines X-Y)`. The `[type]`, section, and line range are each omitted when unavailable.
- **Locator line** (the most specific the source type affords):
  - Folder/vault sources → `**File:** <path>` — the specific file within the folder (the source URI is only the folder root).
  - The aggregate artifact source → `**Artifact:** <slug>`, which deep-links to `/artifacts/<slug>`.
  - Every other source type → `**Link:** <uri>` (uploads, etc.), where the source URI is already the document locator.

Results whose source is missing or unmapped degrade cleanly — the extra lines are simply absent.

### With Embeddings (Optional, Future)

If an embedding model is available:
- Chunks get vector embeddings stored in the `embedding` column
- Adds a third retrieval path: cosine similarity search
- Three-way RRF fusion: keyword + graph + vector

The system degrades gracefully — graph + FTS works without any embedding model.

## 5. Limitations & Future Improvements

| Limitation | Impact | Future Fix |
|-----------|--------|-----------|
| No cross-chunk relations | Entities only connect through shared names | Entity resolution pass (LLM merges aliases) |
| Name inconsistency | "auth layer" ≠ "AuthService" → no connection | Post-processing merge with `merge_entities()` |
| Per-chunk extraction cost | ~0.4 credits per chunk (Haiku) | Already mitigated by larger chunks (400 tokens) |
| No community detection | Can't identify clusters of related entities | Leiden algorithm on graph (like Graphify/MS GraphRAG) |

## 6. Data Model

```
┌──────────────┐         ┌──────────────┐
│   sources    │         │   entities   │ ← Graph Nodes
│ (files/URLs) │         │ (name, type) │
└──────┬───────┘         └──────┬───────┘
       │ source_id               │ entity_id
       ▼                         ▼
┌──────────────┐         ┌──────────────┐
│    items     │◄────────│   mentions   │
│  (chunks)    │ item_id │(item↔entity) │
└──────────────┘         └──────────────┘
                                 
                         ┌──────────────────┐
                         │ entity_relations  │ ← Graph Edges
                         │(src→tgt, type)   │
                         └──────────────────┘
```

## 7. Industry Comparison

| Product | Cross-chunk strategy | Cost model |
|---------|---------------------|-----------|
| **KiroCrew** | Shared entity names | 0.4x per chunk (Haiku) |
| **LightRAG** | Same (shared names) | Varies by model |
| **Microsoft GraphRAG** | Shared names + community detection | Higher (2-pass) |
| **Cognee** | Shared names + entity resolution pass | Higher (merge pass) |
| **Graphify** | AST edges for code + shared names for docs | Free for code, LLM for docs |

## 8. Embedding Integration (Optional)

### What Changes With Embeddings

| Component | Without Embeddings | With Embeddings |
|-----------|-------------------|-----------------|
| **Storage** | `items.embedding` column is NULL | Stores binary vector (e.g., 1536-dim float32) |
| **Ingestion** | Chunk → extract → store | Chunk → extract → **embed** → store |
| **Retrieval** | FTS5 keyword + graph traversal | FTS5 + graph + **vector cosine similarity** |
| **Fusion** | 2-way RRF (keyword + graph) | 3-way RRF (keyword + graph + vector) |
| **Semantic search** | Only finds exact/stemmed keyword matches | Finds semantically similar content ("auth" finds "authentication") |
| **Cost** | Zero (local FTS5 + graph) | Per-chunk embedding API call or local model inference |

### How It Improves Search

Without embeddings, searching "how do we handle authentication" only finds chunks containing those exact words. With embeddings, it also finds chunks about "JWT tokens", "login flow", "OAuth integration" — because they're semantically close in vector space.

### Configuration

Embeddings are configured in `~/.kirocrew/config.json`:

```json
{
  "knowledge": {
    "embeddings": {
      "enabled": false,
      "provider": "ollama",
      "model": "nomic-embed-text",
      "dimensions": 768
    }
  }
}
```

For the local Ollama provider, pull the model first:
`ollama pull nomic-embed-text`.

**Provider options:**

| Provider | Model | Dimensions | Cost | Requires |
|----------|-------|-----------|------|----------|
| `ollama` | `nomic-embed-text` | 768 | Free (local) | Ollama running locally |
| `ollama` | `mxbai-embed-large` | 1024 | Free (local) | Ollama running locally |
| `none` | — | — | Free | Nothing (current default) |

### Graceful Degradation

The system works at three levels:

```
Level 3: FTS5 + Graph + Vector  (best quality, requires embedding provider)
Level 2: FTS5 + Graph           (good quality, current default, zero cost)
Level 1: FTS5 only              (basic, works even if extraction pool is down)
```

If `embeddings.enabled = false` or the provider is unavailable:
- Ingestion still works (embedding column stays NULL)
- Search still works (skips vector path, uses FTS5 + graph only)
- No errors, no degraded UX — just slightly less semantic recall

### Implementation Notes

- Embeddings are generated **after** extraction, in the same ingestion pipeline
- Stored as base64-encoded binary in the `items.embedding` BLOB column
- Vector search uses brute-force cosine similarity (no index needed for <10K items)
- For larger collections (>10K items), would add FAISS or SQLite-vec index
- Re-embedding existing items is a one-time migration (iterate items, embed, update)
