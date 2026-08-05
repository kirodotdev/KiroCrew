#!/usr/bin/env node
/**
 * Split / rejoin the English catalog for translation.
 *
 * Translating ~3.8k keys in one pass is unreliable (truncation, silent key
 * drops), so the work is sharded by top-level namespace group into flat
 * key→value JSON files that a translator fills in place. `join` reassembles the
 * shards into a nested catalog and REFUSES to write a partial result — a
 * missing or untranslated key must fail here rather than ship as English text
 * masquerading as a translation.
 *
 *   node scripts/i18n-shard.mjs split <outDir> [shardSize]
 *   node scripts/i18n-shard.mjs join  <inDir> <locale>
 */

import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
/**
 * English source = the SAME two files the runtime merges (`src/i18n/index.ts`).
 *
 * Reading only `en.json` was a real bug: the 43 hand-authored keys in
 * `en.manual.json` (nav labels, Settings tab labels/descriptions, the language
 * picker's own strings) never reached the shards, so `join` wrote a catalog
 * missing them — silently reverting those labels to English in every language it
 * rebuilt. `join`'s completeness check passed too, because it compared against
 * the same truncated corpus it split from.
 */
const EN_FILES = [
  path.join(ROOT, 'src/i18n/locales/en.json'),
  path.join(ROOT, 'src/i18n/locales/en.manual.json'),
]

/**
 * Translator context, keyed the same way as the catalog. Carried into the shard
 * directory as `shard-NN.context.json` so a translator filling shards sees why
 * `KB` is kilobytes and not "knowledge base", and that `K` is a keyboard key.
 * See `src/i18n/en.context.json` for what belongs in it.
 *
 * Absent-tolerant on purpose. The sidecar is a separate change, and a translation
 * run must not hard-fail because context has not landed yet — but it warns rather
 * than falling silent, because silently shipping context-free shards is the exact
 * failure the sidecar exists to prevent.
 */
const CONTEXT_FILE = path.join(ROOT, 'src/i18n/en.context.json')
const CONTEXT = fs.existsSync(CONTEXT_FILE)
  ? JSON.parse(fs.readFileSync(CONTEXT_FILE, 'utf-8')).entries
  : {}

function flatten(obj, prefix = '') {
  const out = {}
  for (const [k, v] of Object.entries(obj)) {
    const dotted = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object') Object.assign(out, flatten(v, dotted))
    else out[dotted] = String(v)
  }
  return out
}

/**
 * Segments that must never be written through a computed path — `__proto__`
 * would REPLACE the prototype rather than create an own property, silently
 * dropping that branch of the catalog. See the same guard in `i18n-codemod.mjs`.
 */
const UNSAFE_KEY_SEGMENTS = new Set(['__proto__', 'constructor', 'prototype'])

function nest(flat) {
  const out = Object.create(null)
  for (const [key, value] of Object.entries(flat)) {
    const parts = key.split('.')
    if (parts.some(p => UNSAFE_KEY_SEGMENTS.has(p))) {
      throw new Error(`Refusing to nest key '${key}': unsafe object-key segment.`)
    }
    let cur = out
    for (let i = 0; i < parts.length - 1; i++) {
      if (!Object.hasOwn(cur, parts[i])
          || typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) {
        cur[parts[i]] = Object.create(null)
      }
      cur = cur[parts[i]]
    }
    cur[parts[parts.length - 1]] = value
  }
  return out
}

function sortDeep(o) {
  if (o === null || typeof o !== 'object') return o
  const out = {}
  for (const k of Object.keys(o).sort()) out[k] = sortDeep(o[k])
  return out
}

const [cmd, dir, arg] = process.argv.slice(2)

// Flatten and merge every English source, so shards cover the full corpus a
// translation must provide.
const flat = {}
for (const file of EN_FILES) {
  Object.assign(flat, flatten(JSON.parse(fs.readFileSync(file, 'utf-8'))))
}

if (cmd === 'split') {
  const size = Number(arg) || 400
  if (!fs.existsSync(CONTEXT_FILE)) {
    console.warn(
      'warning: src/i18n/en.context.json is missing, so shards carry no translator '
      + 'context. Short and ambiguous strings (`KB`, `K`, `Run`) will be guessed at.',
    )
  }
  fs.mkdirSync(dir, { recursive: true })

  // Clear previously generated context sidecars before rewriting. `split` into an
  // existing directory otherwise leaves a stale one behind whenever a shard's keys
  // lose all their context -- the write below is conditional, so nothing overwrites
  // it -- and stale guidance is worse than none: a translator would follow it.
  // Only the sidecars, never `shard-NN.json`: those are filled in place by a
  // translator, so deleting one would destroy work.
  for (const f of fs.readdirSync(dir)) {
    if (/^shard-\d+\.context\.json$/.test(f)) fs.unlinkSync(path.join(dir, f))
  }

  const keys = Object.keys(flat).sort()
  let shard = 0
  let described = 0
  for (let i = 0; i < keys.length; i += size) {
    const chunk = {}
    for (const k of keys.slice(i, i + size)) chunk[k] = flat[k]
    const stem = `shard-${String(++shard).padStart(2, '0')}`
    const file = path.join(dir, `${stem}.json`)
    fs.writeFileSync(file, JSON.stringify(chunk, null, 2) + '\n')

    // Translator context, as a SIBLING file rather than inside the shard: the
    // shard is filled in place, so a description living in it would either be
    // overwritten or arrive back as a translation. `join` skips `*.context.json`.
    // Without this the sidecar is inert — a translator working the documented
    // workflow would never learn that `KB` is kilobytes and not "knowledge base".
    const ctx = {}
    for (const k of Object.keys(chunk)) if (CONTEXT[k]) ctx[k] = CONTEXT[k]
    if (Object.keys(ctx).length) {
      fs.writeFileSync(path.join(dir, `${stem}.context.json`), JSON.stringify(ctx, null, 2) + '\n')
      described += Object.keys(ctx).length
    }
    console.log(`${file}  (${Object.keys(chunk).length} keys${Object.keys(ctx).length ? `, ${Object.keys(ctx).length} with context` : ''})`)
  }
  console.log(`\n${shard} shards, ${keys.length} keys total, ${described} carrying translator context`)
} else if (cmd === 'join') {
  const locale = arg
  if (!locale) throw new Error('join requires a locale, e.g. zh-CN')
  const merged = {}
  for (const f of fs.readdirSync(dir).filter(f => f.endsWith('.json') && !f.endsWith('.context.json')).sort()) {
    Object.assign(merged, JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8')))
  }

  // Fail closed on an incomplete translation.
  const missing = Object.keys(flat).filter(k => !(k in merged) || !String(merged[k]).trim())
  const extra = Object.keys(merged).filter(k => !(k in flat))
  if (missing.length || extra.length) {
    console.error(
      `Refusing to write ${locale}.json — shards do not cover en.json exactly:\n`
      + `  missing/empty: ${missing.length}${missing.length ? ` (e.g. ${missing.slice(0, 5).join(', ')})` : ''}\n`
      + `  unknown keys:  ${extra.length}${extra.length ? ` (e.g. ${extra.slice(0, 5).join(', ')})` : ''}`,
    )
    process.exit(1)
  }

  const out = path.join(ROOT, 'src/i18n/locales', `${locale}.json`)
  fs.writeFileSync(out, JSON.stringify(sortDeep(nest(merged)), null, 2) + '\n')

  const untranslated = Object.keys(flat).filter(k => merged[k] === flat[k])
  console.log(
    `wrote ${out} (${Object.keys(merged).length} keys)\n`
    + `  identical to English: ${untranslated.length} `
    + `(expected for proper nouns/product names)`,
  )
} else {
  console.error('usage: i18n-shard.mjs split <outDir> [shardSize] | join <inDir> <locale>')
  process.exit(1)
}
