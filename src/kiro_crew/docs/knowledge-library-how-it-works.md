# Knowledge Library

Semantic search over your own documents, folders, and generated artifacts. Add a
file and its text is chunked, indexed, and searchable by meaning rather than by
exact wording, with every result citing the source it came from.

## What gets indexed

Uploaded documents, synced folders, and artifacts you have saved. Text is split
into overlapping chunks so a match can be quoted in context, and entities and
relations found in each chunk are linked into a graph that connects passages
across different files.

## What search returns

A ranked list of passages, each with a citation naming where it came from:
`**Source:**` for a document, `**File:**` for a file inside a synced folder,
`**Artifact:**` for something you saved, and `**Link:**` for a URL. Results
combine keyword and meaning-based matching, so an exact phrase and a paraphrase
both find the passage.

## Duplicates

Near-identical passages from different sources are collapsed to one result, so
uploading the same document twice — directly and through a folder sync — does not
double every hit.

## Three levels of quality

Search runs three legs and fuses whatever they return, so it degrades in a defined
order rather than failing:

1. **Full** — the embedding model is loaded, so meaning-based, keyword and graph
   matching all contribute to the ranking.
2. **Keyword and graph** — no embedding model, so results come from full-text and
   graph matching. Exact wording still works; paraphrases are weaker.
3. **Keyword only** — the graph leg finds no entity matching your terms, so
   full-text matching answers alone. Keyword search is a local index with no model
   behind it, so it is always available.

## Settings

`knowledge.extraction_pool_size` bounds how many chunks are processed at once
(1–10; idle workers are released after five minutes). The embedding model can be
pointed elsewhere with `KIROCREW_EMBED_MODEL_URL` or `KIROCREW_EMBED_MODEL_PATH`.

For how the graph itself is built and stored, see the contributor spec
`docs/system-specs/modules/knowledge.md`.
