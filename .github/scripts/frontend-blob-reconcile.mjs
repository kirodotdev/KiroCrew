// Reconcile the frontend shard verdicts against the merged blob reports.
//
// The sharded `vitest run --reporter=blob` jobs and the `--merge-reports` step
// can exit non-zero without naming a failing test anywhere in the log: a blob
// records `[version, files, unhandledErrors, coverage, executionTime,
// environmentModules]` (flatted-encoded), and a worker that dies mid-run (see
// the worker-OOM note in website/vite.config.ts) fails the job while every
// test inside the blob still reads "pass". This script makes the reason
// visible in one place: it decodes every blob, prints each failing test and
// each unhandled runner error by name, and — when the shard job failed but no
// test reported failing — states that contradiction explicitly instead of
// leaving it to manual artifact probing.
//
// Usage (cwd must be website/ so `flatted` resolves from its node_modules):
//   node ../.github/scripts/frontend-blob-reconcile.mjs [blob-dir]
// Env:
//   FRONTEND_TEST_RESULT  result of the frontend-test job (needs.*.result)
//   MERGE_RESULT          outcome of the merge step (steps.<id>.outcome)
//
// Diagnostic only: always exits 0. The merge step's own exit code remains the
// job verdict; this script never turns a red job green or a green job red.

import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { resolve, join } from 'node:path';

// Resolve `flatted` from the invoking directory (website/), not from this
// script's own location — .github/ has no node_modules.
const require = createRequire(pathToFileURL(join(process.cwd(), 'noop.js')));

function main() {
  const blobDir = resolve(process.cwd(), process.argv[2] ?? '.vitest-reports');
  const shardFailed = process.env.FRONTEND_TEST_RESULT === 'failure';
  const mergeFailed = process.env.MERGE_RESULT === 'failure';

  let parse;
  try {
    ({ parse } = require('flatted'));
  } catch {
    console.log(
      '::warning::frontend-blob-reconcile: `flatted` is not resolvable from '
      + `${process.cwd()} — run this from website/ after npm ci. Skipping.`,
    );
    return;
  }

  let entries;
  try {
    entries = readdirSync(blobDir).filter((f) => f.endsWith('.json'));
  } catch {
    console.log(`frontend-blob-reconcile: no blob directory at ${blobDir} — nothing to scan.`);
    return;
  }
  if (entries.length === 0) {
    console.log(`frontend-blob-reconcile: no blob files under ${blobDir} — nothing to scan.`);
    return;
  }

  const failingTests = [];   // { name, message }
  const fileErrors = [];     // files failing with no failing test inside (collection errors)
  const unhandled = [];      // run-level errors (worker deaths land here)
  const shardsSeen = [];     // [index, count] parsed from blob-<index>-<count>.json
  let blobsRead = 0;

  for (const entry of entries) {
    const fullPath = join(blobDir, entry);
    let files, errors;
    try {
      if (!statSync(fullPath).isFile()) continue;
      [, files, errors] = parse(readFileSync(fullPath, 'utf-8'));
    } catch (err) {
      console.log(`::warning::frontend-blob-reconcile: could not read ${entry}: ${err?.message ?? err}`);
      continue;
    }
    blobsRead += 1;
    const m = /^blob-(\d+)-(\d+)\.json$/.exec(entry);
    if (m) shardsSeen.push([Number(m[1]), Number(m[2])]);
    for (const err of errors ?? []) {
      unhandled.push(firstLine(err?.message ?? err?.stack ?? String(err)));
    }
    for (const file of files ?? []) {
      const before = failingTests.length;
      collectFailures(file, failingTests);
      if (failingTests.length === before && file?.result?.state === 'fail') {
        const msgs = (file.result.errors ?? []).map((e) => firstLine(e?.message ?? ''));
        fileErrors.push({ name: file.name ?? file.filepath ?? '(unknown file)', message: msgs.join('; ') });
      }
    }
  }

  console.log(`frontend-blob-reconcile: scanned ${blobsRead} blob(s) under ${blobDir}.`);

  // A shard killed before onTestRunEnd never writes its blob at all, so its
  // absence -- not anything inside the surviving blobs -- is the failure's
  // trace. The blob filename encodes blob-<index>-<count>, so the expected
  // set is self-describing; no shard-count plumbing from the workflow needed.
  // Disagreeing counts (blobs from different runs mixed into one dir) make
  // the expected set undefined, so detection is skipped rather than guessed.
  const counts = new Set(shardsSeen.map(([, c]) => c));
  const expectedCount = counts.size === 1 ? [...counts][0] : 0;
  const missingShards = [];
  if (expectedCount > 0) {
    const seen = new Set(shardsSeen.map(([i]) => i));
    for (let i = 1; i <= expectedCount; i += 1) {
      if (!seen.has(i)) missingShards.push(i);
    }
  }
  if (missingShards.length > 0) {
    console.log(
      `\n::warning::Only ${shardsSeen.length} of ${expectedCount} shard blobs are present -- `
      + `missing shard(s): ${missingShards.join(', ')}. A missing blob means that shard's vitest `
      + 'process died before it could write its results (see the worker-OOM failure mode '
      + 'documented in website/vite.config.ts); its verdict is not represented below.',
    );
  }

  if (failingTests.length > 0) {
    console.log(`\nFailing tests recorded in the merged shard results (${failingTests.length}):`);
    for (const t of failingTests) {
      console.log(`  FAIL ${t.name}${t.message ? ` — ${t.message}` : ''}`);
    }
  }
  if (fileErrors.length > 0) {
    console.log(`\nTest files that failed without a failing test inside (collection/setup errors, ${fileErrors.length}):`);
    for (const f of fileErrors) {
      console.log(`  FAIL ${f.name}${f.message ? ` — ${f.message}` : ''}`);
    }
  }
  if (unhandled.length > 0) {
    console.log(`\nUnhandled runner-level errors recorded in the blobs (${unhandled.length}):`);
    for (const msg of unhandled) console.log(`  ERROR ${msg}`);
  }

  const nothingNamed = failingTests.length === 0 && fileErrors.length === 0;
  if ((shardFailed || mergeFailed) && nothingNamed) {
    const source = shardFailed ? 'a frontend-test shard' : 'the merge step';
    if (unhandled.length > 0) {
      const alsoMissing = missingShards.length > 0
        ? ` Shard(s) ${missingShards.join(', ')} also never wrote a blob, so their verdicts may be an additional cause.`
        : '';
      console.log(
        `\n::warning::No test in the merged shard results reported failing, but ${source} exited `
        + `non-zero: the exit came from ${unhandled.length} unhandled runner error(s) listed above `
        + '(e.g. a test worker dying mid-run), not a test assertion. See the documented worker-OOM '
        + `failure mode in website/vite.config.ts.${alsoMissing}`,
      );
    } else if (missingShards.length > 0) {
      console.log(
        `\n::warning::No test in the present shard results reported failing, but ${source} exited `
        + `non-zero: the likely reason is the missing shard blob(s) listed above -- that shard's `
        + 'results were never written, so its failure cannot be named here.',
      );
    } else {
      console.log(
        `\n::warning::No test in the merged shard results reported failing and no unhandled error `
        + `was recorded, but ${source} exited non-zero: the exit came from the runner itself, not `
        + 'an assertion. See the documented worker-OOM failure mode in website/vite.config.ts.',
      );
    }
  } else if (nothingNamed && unhandled.length === 0 && missingShards.length === 0) {
    console.log('All tests in the merged shard results passed and no unhandled errors were recorded.');
  }
}

function firstLine(text) {
  return String(text ?? '').split('\n', 1)[0].slice(0, 300);
}

// Walk a task tree depth-first, collecting every leaf test whose recorded
// state is "fail" with its full name and first error line.
function collectFailures(task, out) {
  if (!task || typeof task !== 'object') return;
  if (task.type === 'test' && task.result?.state === 'fail') {
    const msgs = (task.result.errors ?? []).map((e) => firstLine(e?.message ?? ''));
    out.push({ name: task.fullName ?? task.name ?? '(unnamed test)', message: msgs.join('; ') });
  }
  for (const child of task.tasks ?? []) collectFailures(child, out);
}

// Diagnostic only, so a defect in this script must never become the failure it
// exists to explain: an uncaught throw here would red a green merge job and --
// because later steps default to `if: success()` -- block the coverage-frontend
// upload the Coverage Gate consumes.
try {
  main();
} catch (err) {
  console.log(`::warning::frontend-blob-reconcile: reconciliation failed: ${err?.message ?? err}`);
}
