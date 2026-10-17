// Rewrite the absolute workspace root inside each frontend shard blob to THIS
// runner's workspace root, so `vitest --merge-reports --coverage` sees one
// path per source file.
//
// Why: a blob stores every path absolute — the coverage map is keyed by
// `/<workspace>/website/src/…`. GitHub-hosted runners all check out under the
// same `/home/runner/work/<repo>/<repo>`, so four shards and the merge job
// agree on the root by accident. The CodeBuild-hosted runner gives EVERY build
// its own root (`/codebuild/output/src<random>/src/actions-runner/_work/…`),
// so four shards produce four roots, none of them the merge job's. Vitest
// then unions nothing: the text report lists each file four times (once per
// shard, each with that shard's partial numbers), the cobertura/lcov output
// points at four directories that do not exist here, and the merge step exits
// non-zero without naming a failing test. Seen on the pilot's first two runs.
//
// What it does: for each `blob-*.json`, find the workspace root — the prefix
// of any `"/…/website/"` path inside the blob — and replace every occurrence
// with the root derived from the current working directory. Plain string
// replacement on the raw file: the root appears only inside JSON string values
// and contains no quote characters, and the blob is flatted-encoded, so
// re-serializing through a parser is neither needed nor safe. A blob whose
// root already matches is left untouched (hosted runners: no-op). A blob with
// no recognizable root is left alone and reported.
//
// Usage (cwd must be website/):
//   node ../.github/scripts/frontend-blob-normalize-paths.mjs [blob-dir]
//
// Exits 0 always: a blob this script cannot normalize still reaches the merge
// step, whose own exit code stays the verdict.

import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, join, basename, dirname } from 'node:path';

const WEBSITE_DIR = 'website';

function main() {
  const cwd = process.cwd();
  if (basename(cwd) !== WEBSITE_DIR) {
    console.log(`::warning::frontend-blob-normalize-paths: expected to run from ${WEBSITE_DIR}/, got ${cwd}. Skipping.`);
    return;
  }
  const localRoot = dirname(cwd);
  const blobDir = resolve(cwd, process.argv[2] ?? '.vitest-reports');

  let entries;
  try {
    entries = readdirSync(blobDir).filter((f) => f.endsWith('.json'));
  } catch {
    console.log(`frontend-blob-normalize-paths: no blob directory at ${blobDir} — nothing to do.`);
    return;
  }
  if (entries.length === 0) {
    console.log(`frontend-blob-normalize-paths: no blob files under ${blobDir} — nothing to do.`);
    return;
  }

  // A JSON string value that is an absolute path into the website tree:
  // capture everything before "/website/". Quote-delimited on the left so the
  // root cannot span into a preceding value.
  const rootPattern = new RegExp(`"(/[^"\\\\]*?)/${WEBSITE_DIR}/`, 'g');

  for (const name of entries) {
    const file = join(blobDir, name);
    const text = readFileSync(file, 'utf8');
    const roots = new Set();
    for (const match of text.matchAll(rootPattern)) roots.add(match[1]);

    if (roots.size === 0) {
      console.log(`frontend-blob-normalize-paths: ${name}: no /…/${WEBSITE_DIR}/ path found — left as is.`);
      continue;
    }
    if (roots.size > 1) {
      // One shard, one checkout: more than one root means the blob is not what
      // this script understands. Do not guess.
      console.log(`::warning::frontend-blob-normalize-paths: ${name}: ${roots.size} distinct roots (${[...roots].join(', ')}) — left as is.`);
      continue;
    }
    const [blobRoot] = roots;
    if (blobRoot === localRoot) {
      console.log(`frontend-blob-normalize-paths: ${name}: root already ${localRoot} — no change.`);
      continue;
    }
    // Replace every occurrence, quoted or not: stack traces and error
    // messages inside the blob carry the same root mid-string, and the root
    // is long and specific enough that a stray match is not a concern.
    const parts = text.split(`${blobRoot}/`);
    const count = parts.length - 1;
    const rewritten = parts.join(`${localRoot}/`);
    writeFileSync(file, rewritten);
    console.log(`frontend-blob-normalize-paths: ${name}: ${blobRoot} -> ${localRoot} (${count} path(s)).`);
  }
}

main();
