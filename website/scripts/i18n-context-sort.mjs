#!/usr/bin/env node
/**
 * Put the `entries` of `src/i18n/en.context.json` in key order.
 *
 * Key order gives every description its own place in the file. Two branches that
 * each add one then change different lines and merge without a conflict, unless
 * their keys sort next to each other; at the end of the file every addition
 * would touch the same closing lines. `contextSidecar.test.ts` fails on an entry
 * out of order. Only the order of `entries` changes: every key and value keeps
 * its bytes, and the output is serialized the way the file is written by hand.
 *
 *   npm run i18n:context:sort
 *   npm run i18n:context:sort -- --replay <commit>
 *   npm run i18n:context:sort -- --replay <base>..<tip>
 *
 * `--replay` first applies the entries a commit added, changed or deleted against
 * its parent (or what `<tip>` did since `<base>`), and its `_comment` edit if it
 * made one, to the file as it stands. It
 * resolves the one conflict a branch that appended its entries at the end gets
 * from the key order: take main's side of the file, then replay the branch's own
 * change (website/docs/i18n-catalog.md has the steps).
 */
import { execFileSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { inKeyOrder, replayEntries } from './lib/i18n-context-order.mjs'

const WEBSITE = path.join(path.dirname(fileURLToPath(import.meta.url)), '..')
const FILE = path.join(WEBSITE, 'src', 'i18n', 'en.context.json')
const REPO_PATH = 'website/src/i18n/en.context.json'

/** The sidecar as committed at `revision`. */
function sidecarAt(revision) {
  const text = execFileSync('git', ['show', `${revision}:${REPO_PATH}`], { cwd: WEBSITE, encoding: 'utf-8' })
  return JSON.parse(text)
}

const args = process.argv.slice(2)
const replay = args[0] === '--replay' && args.length === 2 ? args[1] : null
if (args.length > 0 && !replay) {
  console.error('usage: i18n-context-sort.mjs [--replay <commit> | --replay <base>..<tip>]')
  process.exit(2)
}

const sidecar = JSON.parse(fs.readFileSync(FILE, 'utf-8'))
if (replay) {
  const [base, tip] = replay.includes('..') ? replay.split('..') : [`${replay}^`, replay]
  if (!base || !tip || replay.includes('...')) {
    console.error(`--replay needs <commit> or <base>..<tip> (two dots), got ${replay}`)
    process.exit(2)
  }
  const [before, after] = [sidecarAt(base), sidecarAt(tip)]
  sidecar.entries = replayEntries(sidecar.entries, before.entries, after.entries)
  // The branch's own `_comment` edit rides along, like its entry changes.
  if (after._comment !== before._comment) sidecar._comment = after._comment
}
const keys = Object.keys(sidecar.entries)
sidecar.entries = inKeyOrder(sidecar.entries)
const ordered = Object.keys(sidecar.entries)
const moved = keys.filter((key, index) => key !== ordered[index]).length
fs.writeFileSync(FILE, `${JSON.stringify(sidecar, null, 2)}\n`)
console.log(`src/i18n/en.context.json: ${keys.length} entries in key order (${moved} moved)`)
