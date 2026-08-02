#!/usr/bin/env node
// Render the bundle report written by the `analyze` build mode.
//
// Usage:
//   npm run analyze                 build in analyze mode, then print the report
//   node scripts/bundle-report.mjs  print the report from the last analyze build
//   node scripts/bundle-report.mjs --json
//   node scripts/bundle-report.mjs --diff <baseline.json>
//
// Debug-only tooling: nothing here runs in a normal `npm run build`, and no part
// of it ships in the bundle.
import { readFileSync, existsSync } from 'fs'
import path from 'path'
import { renderReport, diffSummaries, formatBytes } from './lib/bundleReport.mjs'

const REPORT_PATH = path.resolve('dist', 'bundle-report.json')

function fail(message, code = 1) {
  process.stderr.write(`${message}\n`)
  process.exit(code)
}

function loadSummary(file) {
  if (!existsSync(file)) {
    fail(
      `No bundle report at ${file}.\n` +
        'Run `npm run analyze` first -- a plain `npm run build` deliberately does ' +
        'not write one, so the normal build stays unaffected.',
      2
    )
  }
  let parsed
  try {
    parsed = JSON.parse(readFileSync(file, 'utf-8'))
  } catch (e) {
    fail(`${file} is not valid JSON: ${e && e.message}`, 3)
  }
  if (!parsed || typeof parsed !== 'object') fail(`${file} does not contain a report object.`, 3)
  if (parsed.version !== 1) {
    // Refuse rather than misread a future shape as v1.
    fail(`${file} has version ${JSON.stringify(parsed.version)}; this build understands 1.`, 3)
  }
  return parsed
}

const args = process.argv.slice(2)
const summary = loadSummary(REPORT_PATH)

const diffAt = args.indexOf('--diff')
if (diffAt !== -1) {
  const baselineFile = args[diffAt + 1]
  if (!baselineFile) fail('--diff needs a path to a baseline bundle-report.json', 2)
  const baseline = loadSummary(path.resolve(baselineFile))
  const d = diffSummaries(baseline, summary)
  const sign = (n) => (n > 0 ? `+${formatBytes(n)}` : n < 0 ? `-${formatBytes(-n)}` : '0 B')
  process.stdout.write(`Bundle diff vs ${baselineFile}\n`)
  process.stdout.write(`  JS chunks: ${sign(d.chunkBytesDelta)}\n`)
  process.stdout.write(`  other assets: ${sign(d.assetBytesDelta)}\n`)
  if (d.owners.length) {
    process.stdout.write('\n  Changed contributors:\n')
    for (const o of d.owners.slice(0, 20)) {
      process.stdout.write(`    ${sign(o.delta).padStart(11)}  ${o.owner}\n`)
    }
  } else {
    process.stdout.write('\n  No per-contributor change.\n')
  }
  process.exit(0)
}

if (args.includes('--json')) {
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`)
  process.exit(0)
}

const topAt = args.indexOf('--top')
const top = topAt !== -1 ? Number.parseInt(args[topAt + 1], 10) : undefined
process.stdout.write(`${renderReport(summary, { top })}\n`)
process.stdout.write(`\nFull data: ${REPORT_PATH}\n`)
