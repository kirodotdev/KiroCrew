// Recover the test files a frontend shard lost to a Vitest fork that exited
// before the file started.
//
// The failure this exists for: a forks worker dies after the pool handed it a
// test file but before the worker queued that file (`onCollectStart`, which
// runs before `setupFiles` and before the file is imported). Vitest 4.1 then
// reports one unhandled error, `[vitest-pool]: Worker forks emitted error.`
// caused by `Worker exited unexpectedly`, every reported test passes, and the
// lost file appears in no count at all ("1048 passed (1048)" with 1049 files
// in the shard). The blob keeps that error only as `{ type: 'Unhandled Error' }`
// (an Error's message and cause are not enumerable), so the blob alone can
// neither name the error nor the file.
//
// This module is both halves of the recovery:
//
// 1. The default export is a Vitest reporter. Added to the shard run with
//    `--reporter=<this file>` and `FRONTEND_RUN_MANIFEST=<path>`, it writes a
//    run manifest at `onTestRunEnd`: the files the pool was handed (the same
//    `sequence.sequencer.shard()` call the pool makes on the same
//    specifications), the files the run reported and their states, and each
//    unhandled error's message and cause, index-aligned with the blob's
//    `errors` array (every reporter receives the same array).
//
// 2. The CLI:
//      node frontend-lost-files.mjs plan <manifest>
//        Prints the lost files, one per line, and exits 0 only when losing
//        them is the WHOLE failure (see `planRecovery`). Otherwise exits 1 and
//        says why on stderr.
//      node frontend-lost-files.mjs finalize <manifest> <shard-blob> <recovery-blob>
//        Checks that the re-run's blob holds exactly the lost files, all
//        passed, with no unhandled error and coverage collected exactly when
//        the shard collected it. Only then rewrites the shard blob without the
//        worker-exit errors and exits 0. Otherwise exits 1 and leaves the shard
//        blob untouched, so `vitest --merge-reports` still fails on it.
//    Run the CLI from website/ so `flatted` resolves from its node_modules.
//
// Why re-running is not a flake retry: the lost file never executed a line of
// project code in the dead fork, so the re-run is its first run. A file that
// STARTED and then lost its worker is reported with a non-passing state and is
// never recovered.

import { readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const MANIFEST_VERSION = 1;

// Every recorded hit lost one file. Two independent fork deaths in one shard
// are already improbable, so more than this means the forks are dying for a
// shared reason (memory, a broken environment) and the shard must stay red.
export const MAX_RECOVERABLE_FILES = 2;

const POOL_ERROR = '[vitest-pool]: Worker forks emitted error.';
// Vitest 4.1 says exactly this; later majors append the exit code, signal and
// file, so the cause is matched as a prefix.
const WORKER_EXIT = 'Worker exited unexpectedly';

const toPosix = (path) => path.split(sep).join('/');

export function describeError(error) {
  if (error === null || typeof error !== 'object') {
    return { message: String(error), cause: null };
  }
  const { cause } = error;
  return {
    message: typeof error.message === 'string' ? error.message : null,
    cause: cause !== null && typeof cause === 'object' && typeof cause.message === 'string'
      ? cause.message
      : null,
  };
}

export async function buildManifest(vitest, specifications, testModules, unhandledErrors, reason) {
  const { config } = vitest;
  const rel = (moduleId) => toPosix(relative(config.root, moduleId));
  const handed = config.shard
    ? await new config.sequence.sequencer(vitest).shard([...specifications])
    : specifications;
  return {
    version: MANIFEST_VERSION,
    vitestVersion: vitest.version,
    root: config.root,
    shard: config.shard ? { index: config.shard.index, count: config.shard.count } : null,
    projects: vitest.projects.map((project) => ({
      name: project.name,
      pool: project.config.pool,
      isolate: project.config.isolate,
    })),
    coverageThresholds: config.coverage?.thresholds != null,
    reason,
    expected: handed.map((spec) => rel(spec.moduleId)).sort(),
    reported: testModules.map((module) => ({ file: rel(module.moduleId), state: module.state() })),
    errors: unhandledErrors.map(describeError),
  };
}

export default class LostFilesManifestReporter {
  vitest = null;
  specifications = [];

  onInit(vitest) {
    this.vitest = vitest;
  }

  onTestRunStart(specifications) {
    this.specifications = [...specifications];
  }

  async onTestRunEnd(testModules, unhandledErrors, reason) {
    const target = process.env.FRONTEND_RUN_MANIFEST;
    if (!target || !this.vitest) return;
    // A reporter that throws becomes the run's failure. This one only feeds the
    // recovery, and a missing manifest already means "not recoverable".
    try {
      const manifest = await buildManifest(
        this.vitest, this.specifications, testModules, unhandledErrors, reason,
      );
      writeFileSync(target, `${JSON.stringify(manifest)}\n`, 'utf8');
    } catch (err) {
      console.error(`frontend-lost-files: could not write the run manifest: ${err?.message ?? err}`);
    }
  }
}

export function isWorkerExit(error) {
  return error?.message === POOL_ERROR
    && typeof error.cause === 'string'
    && error.cause.startsWith(WORKER_EXIT);
}

const refuse = (reason) => ({ recoverable: false, reason, lost: [] });

// Decide whether the shard failed ONLY because forks died before their files
// started, and if so which files. Every check refuses toward "stay red".
export function planRecovery(manifest) {
  if (manifest?.version !== MANIFEST_VERSION) return refuse('there is no run manifest of a known version');
  const { projects = [], errors = [], reported = [], expected = [] } = manifest;
  if (projects.length !== 1) return refuse(`the run has ${projects.length} projects; recovery handles one`);
  const [project] = projects;
  // Only an isolated forks pool gives every file its own fresh process, which is
  // what makes "one worker exit, one lost file" hold. A reused runner can lose
  // further files after its worker died.
  if (project.pool !== 'forks' || project.isolate !== true) {
    return refuse(`the pool is ${project.pool} with isolate=${project.isolate}; recovery needs isolated forks`);
  }
  // A shard-level threshold is judged on the whole shard's coverage, which a
  // re-run of a few files cannot re-derive.
  if (manifest.coverageThresholds) return refuse('a coverage threshold is configured on the run');
  if (manifest.reason !== 'passed') {
    return refuse(`Vitest judged the run "${manifest.reason}" apart from its unhandled errors`);
  }
  if (errors.length === 0) return refuse('the run recorded no unhandled error');
  const foreign = errors.find((error) => !isWorkerExit(error));
  if (foreign) {
    return refuse(`an unhandled error is not a worker exit: ${foreign.message}${foreign.cause ? ` (cause: ${foreign.cause})` : ''}`);
  }
  const unfinished = reported.find((module) => module.state !== 'passed' && module.state !== 'skipped');
  if (unfinished) {
    return refuse(`${unfinished.file} is "${unfinished.state}"; a failed file, or one whose worker died after it started, is not recovered`);
  }
  const handed = new Set(expected);
  const stray = reported.find((module) => !handed.has(module.file));
  if (stray) return refuse(`${stray.file} was reported but is not in the shard the pool was handed`);
  const seen = new Set(reported.map((module) => module.file));
  const lost = expected.filter((file) => !seen.has(file));
  if (lost.length !== errors.length) {
    return refuse(`${lost.length} file(s) never reported for ${errors.length} worker exit(s); each exit loses exactly one file`);
  }
  if (lost.length > MAX_RECOVERABLE_FILES) {
    return refuse(`${lost.length} files were lost; more than ${MAX_RECOVERABLE_FILES} is not a stray fork death`);
  }
  return {
    recoverable: true,
    reason: `${lost.length} fork(s) exited before their file started; every reported test passed`,
    lost,
  };
}

function failingTests(task, out = []) {
  if (!task || typeof task !== 'object') return out;
  if (task.type === 'test' && task.result?.state === 'fail') out.push(task.fullName ?? task.name);
  for (const child of task.tasks ?? []) failingTests(child, out);
  return out;
}

const sameSet = (a, b) => a.length === b.length && [...a].sort().join('\n') === [...b].sort().join('\n');

// The blob stores the pool's Error with neither message nor cause (they are
// not enumerable), so an entry matches its manifest twin when it has no
// message of its own, or the same message.
const blobErrorMatches = (blobError, manifestError) => blobError !== null
  && typeof blobError === 'object'
  && (blobError.message === undefined || blobError.message === manifestError.message);

// Given the parsed blobs (`[version, files, errors, coverage, ...]`), return the
// shard report to write back, or a refusal. Pure: the caller does the I/O.
export function finalizeRecovery(manifest, shardReport, recoveryReport) {
  const plan = planRecovery(manifest);
  if (!plan.recoverable) return { ok: false, reason: plan.reason };
  const no = (reason) => ({ ok: false, reason });
  const [shardVersion, shardFiles = [], shardErrors = [], shardCoverage] = shardReport ?? [];
  const [rerunVersion, rerunFiles = [], rerunErrors = [], rerunCoverage] = recoveryReport ?? [];
  if (shardVersion !== manifest.vitestVersion || rerunVersion !== shardVersion) {
    return no(`the blobs were written by Vitest ${shardVersion} and ${rerunVersion}, the run by ${manifest.vitestVersion}`);
  }
  const rel = (file) => toPosix(relative(manifest.root, file.filepath));

  if (!sameSet(shardFiles.map(rel), manifest.reported.map((module) => module.file))) {
    return no('the shard blob does not hold the files the run manifest reported');
  }
  if (shardErrors.length !== manifest.errors.length
    || !shardErrors.every((error, i) => blobErrorMatches(error, manifest.errors[i]))) {
    return no('the shard blob\'s unhandled errors do not line up with the run manifest');
  }

  if (rerunErrors.length > 0) return no(`the re-run recorded ${rerunErrors.length} unhandled error(s)`);
  const rerunNames = rerunFiles.map(rel);
  if (!sameSet(rerunNames, plan.lost)) {
    return no(`the re-run ran [${rerunNames.join(', ')}], not exactly the lost [${plan.lost.join(', ')}] (a CLI file filter is a substring match)`);
  }
  for (const file of rerunFiles) {
    const failed = failingTests(file);
    if (failed.length > 0) return no(`the re-run of ${rel(file)} failed: ${failed.join('; ')}`);
    const state = file.result?.state;
    if (state !== 'pass' && state !== 'skip') return no(`the re-run of ${rel(file)} ended "${state}"`);
  }
  // The merge unions coverage per blob. A re-run without it would drop the lost
  // file's lines from the Coverage Gate; one with it, beside a shard without
  // it, would add numbers no other shard carries.
  if (Boolean(shardCoverage) !== Boolean(rerunCoverage)) {
    return no('the re-run must collect coverage exactly when the shard did');
  }

  const report = [...shardReport];
  report[2] = [];
  return { ok: true, report, lost: plan.lost };
}

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'));
}

function loadFlatted() {
  // From the invoking directory (website/): .github/ has no node_modules.
  const require = createRequire(pathToFileURL(join(process.cwd(), 'noop.js')));
  return require('flatted');
}

export function cli(argv) {
  const [command, ...args] = argv;
  if (command === 'plan' && args.length === 1) {
    let manifest = null;
    try {
      manifest = readJson(args[0]);
    } catch (err) {
      console.error(`frontend-lost-files: could not read the run manifest ${args[0]}: ${err?.message ?? err}`);
    }
    const plan = planRecovery(manifest);
    if (!plan.recoverable) {
      console.error(`frontend-lost-files: not recovering: ${plan.reason}.`);
      return 1;
    }
    console.error(`frontend-lost-files: ${plan.reason}. Running the lost file(s) on their own: ${plan.lost.join(', ')}`);
    process.stdout.write(`${plan.lost.join('\n')}\n`);
    return 0;
  }
  if (command === 'finalize' && args.length === 3) {
    const [manifestPath, shardBlob, recoveryBlob] = args;
    let result;
    try {
      const { parse } = loadFlatted();
      result = finalizeRecovery(
        readJson(manifestPath),
        parse(readFileSync(shardBlob, 'utf8')),
        parse(readFileSync(recoveryBlob, 'utf8')),
      );
    } catch (err) {
      result = { ok: false, reason: `could not read the inputs: ${err?.message ?? err}` };
    }
    if (!result.ok) {
      console.error(`frontend-lost-files: recovery refused, the shard stays red: ${result.reason}.`);
      return 1;
    }
    writeFileSync(shardBlob, loadFlatted().stringify(result.report), 'utf8');
    console.log(
      '::warning::A Vitest fork exited before its test file started. The lost file(s) '
      + `${result.lost.join(', ')} ran again on their own and passed; their results and coverage `
      + 'are in this shard\'s blobs. A file lost again and again gets its own Flaky issue.',
    );
    return 0;
  }
  console.error('usage: frontend-lost-files.mjs plan <manifest> | finalize <manifest> <shard-blob> <recovery-blob>');
  return 2;
}

function invokedDirectly() {
  // Vitest's module runner may hand this module a non-file `import.meta.url`
  // when it loads it as a reporter or a test import; that is never a CLI run.
  try {
    // Both sides as real paths: Node resolves the ESM entry's symlinks but
    // leaves argv[1] as typed, so a symlinked workspace would otherwise make
    // the CLI a silent no-op that exits 0.
    return (
      Boolean(process.argv[1]) &&
      realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url))
    );
  } catch {
    return false;
  }
}

if (invokedDirectly()) process.exitCode = cli(process.argv.slice(2));
