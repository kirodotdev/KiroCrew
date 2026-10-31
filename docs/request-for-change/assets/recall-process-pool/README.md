# Recall process pool benchmark

The harness behind the numbers in `../../rfc-recall-process-pool.md`.

- `gen_db.py <repo>/src <out_dir>`: builds a synthetic V1 `memory.db` (54k facts, 12k lessons,
  27k episodes, 1024-d embeddings).
- `bench.py <repo>/src <db_dir> --mode threads|proc2|proc4|pool2|pool4 --sessions N`: N sessions
  build the recall part of a turn context at once, twice, with an optional consolidation-style
  writer (`--write-period S`, or `--no-writer`). `threads` is today's 8-thread pool. `procN`
  is a prototype `ProcessPoolExecutor`. `poolN` is the shipped `kiro_crew.recall_pool`, so
  run it with `PYTHONPATH=<repo>/src`, which the children inherit. It works on a private copy
  of the database under `/tmp`.
- `results.jsonl`: one line per run, the rows quoted in the RFC (`proc*` rows are the
  prototype, `pool*` rows the shipped code).
