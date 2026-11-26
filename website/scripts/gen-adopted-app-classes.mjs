/**
 * Generate `src/tailwind-adopted-app-classes.css`, the adopted-app class safelist.
 * Usage: node scripts/gen-adopted-app-classes.mjs   (or `npm run gen:adopted-app-classes`)
 *
 * Networked: it downloads the codeload archive of every row in
 * `src/kiro_crew/apps/adoption-pins.json`. Run it by hand whenever that table
 * changes; `src/test/adoptedAppClassSafelist.test.ts` fails until you do.
 *
 * No shebang: that test imports this module (src/test/scriptsShebang.test.ts).
 *
 * WHY THE LIST EXISTS. A former built-in that installs from its own repository
 * (app-kit-platform.md section 33) ships a UI bundle with no stylesheet: its
 * Tailwind utilities resolve against the dashboard's compiled sheet. That sheet
 * holds only the classes `src/index.css` finds in its `@source` scan of `src/`,
 * so while core still carries the app's wheel copy under `src/apps/<name>/`, the
 * copy is what keeps the adopted bundle's classes compiled. Deleting the copy
 * drops every class nothing else in core uses, and the adopted app renders
 * unstyled on every host, including hosts already running a pin that a newer pin
 * cannot reach. The safelist lists those classes explicitly, as `@source inline`
 * candidates, so the sheet keeps them whether or not a copy exists.
 *
 * WHAT IT SCANS, for every pin row:
 *  - the pinned tree's `ui/src/` (the App Kit layout), read straight out of the
 *    archive after its SHA-256 matches the row's `archiveSha256`. This is the
 *    source of the bundle the host runs, so it is the truth after a copy is gone
 *    and for an app whose copy never lived under `src/apps/<name>/`;
 *  - the wheel copy under `src/apps/<name>/`, while it exists.
 * Test files are skipped in both: their class strings are assertion data.
 * A candidate is kept only when Tailwind emits a rule for it, asked through the
 * phantom-classes gate's oracle (`emittedClasses`), so the theme is never
 * re-implemented here and English words in comments do not reach the file.
 *
 * APPEND-ONLY. A class already in the file is kept even when no scan finds it:
 * the copy it came from may be gone, and a host may still run the pin that uses
 * it. The test fails a change that removes one. The file also records the pin
 * commits it was generated against, so a re-pin without a regeneration fails the
 * same test.
 */
import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, extname, join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { gunzipSync } from 'node:zlib'
import { emittedClasses } from './check-phantom-classes.mjs'

const WEBSITE = join(dirname(fileURLToPath(import.meta.url)), '..')
const { Scanner } = createRequire(import.meta.url)('@tailwindcss/oxide')

export const SAFELIST_PATH = join(WEBSITE, 'src', 'tailwind-adopted-app-classes.css')
const PINS_PATH = join(WEBSITE, '..', 'src', 'kiro_crew', 'apps', 'adoption-pins.json')
/** Where a pinned app's wheel copy lives in core, the same root PR Hygiene's
 *  `adopted_wheel_copy_check.py` guards. */
const WHEEL_COPY_ROOT = join(WEBSITE, 'src', 'apps')

/** Source files the scans read. Tailwind reads any text; these are the ones a
 *  UI bundle is built from. */
const SOURCE_EXT = new Set(['.ts', '.tsx', '.js', '.jsx', '.mjs'])

/** A test path, relative to the scanned root, with `/` separators. */
function isTestPath(rel) {
  return /(?:^|\/)(?:test|tests|__tests__)\//.test(rel) || /\.(?:test|spec)\.[cm]?[jt]sx?$/.test(rel)
}

const isSource = (rel) => SOURCE_EXT.has(extname(rel)) && !rel.endsWith('.d.ts') && !isTestPath(rel)

/** The pin rows: `{ name, repository, commit, archiveSha256 }`. */
export function readPins(text = readFileSync(PINS_PATH, 'utf8')) {
  return JSON.parse(text).pins.map(({ name, repository, commit, archiveSha256 }) => ({
    name, repository, commit, archiveSha256,
  }))
}

/** Tailwind candidates in a set of `{ content, extension }` sources. */
function candidatesOf(sources) {
  if (sources.length === 0) return []
  return new Scanner({}).scanFiles(sources)
}

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else out.push(full)
  }
  return out
}

/** The non-test source files of `name`'s wheel copy, or `[]` when it has none. */
export function wheelCopyFiles(name) {
  const dir = join(WHEEL_COPY_ROOT, name)
  if (!existsSync(dir)) return []
  return walk(dir).filter((file) => isSource(relative(dir, file).split(sep).join('/')))
}

/** Tailwind candidates in `name`'s wheel copy. */
export function wheelCopyCandidates(name) {
  return candidatesOf(
    wheelCopyFiles(name).map((file) => ({ content: readFileSync(file, 'utf8'), extension: extname(file).slice(1) })),
  )
}

/** The regular files of a `.tar.gz`, as `{ path, data }` with the archive's own
 *  top directory (`<repo>-<commit>/`) stripped. Reads ustar and the pax `path`
 *  records `git archive` writes for a long name. */
function tarGzFiles(gz) {
  const tar = gunzipSync(gz)
  const files = []
  let paxPath = null
  for (let off = 0; off + 512 <= tar.length;) {
    const header = tar.subarray(off, off + 512)
    if (header.every((b) => b === 0)) break
    const field = (start, len) => {
      const raw = header.subarray(start, start + len)
      const end = raw.indexOf(0)
      return raw.subarray(0, end < 0 ? len : end).toString('utf8')
    }
    const size = parseInt(field(124, 12).trim() || '0', 8)
    const type = String.fromCharCode(header[156] || 48)
    const body = tar.subarray(off + 512, off + 512 + size)
    off += 512 + Math.ceil(size / 512) * 512
    if (type === 'x') {
      paxPath = null
      for (const record of body.toString('utf8').split('\n')) {
        const m = /^\d+ path=(.*)$/.exec(record)
        if (m) paxPath = m[1]
      }
      continue
    }
    if (type === 'g') continue
    const prefix = field(345, 155)
    const path = paxPath ?? (prefix ? `${prefix}/${field(0, 100)}` : field(0, 100))
    paxPath = null
    if (type === '0') files.push({ path: path.split('/').slice(1).join('/'), data: body })
  }
  return files
}

/** Tailwind candidates in `ui/src/` of the archive `gz`, and how many files it read. */
export function treeCandidates(gz) {
  const sources = []
  for (const { path, data } of tarGzFiles(gz)) {
    if (!path.startsWith('ui/src/') || !isSource(path.slice('ui/src/'.length))) continue
    sources.push({ content: data.toString('utf8'), extension: extname(path).slice(1) })
  }
  return { candidates: candidatesOf(sources), files: sources.length }
}

/** The pinned archive of `pin`, refused unless it is the one the row names. */
async function fetchPinnedArchive(pin) {
  const slug = pin.repository.replace(/^https:\/\/github\.com\//, '')
  const url = `https://codeload.github.com/${slug}/tar.gz/${pin.commit}`
  const res = await fetch(url, { redirect: 'error' })
  if (!res.ok) throw new Error(`${pin.name}: GET ${url} answered ${res.status}`)
  const gz = Buffer.from(await res.arrayBuffer())
  const digest = createHash('sha256').update(gz).digest('hex')
  if (digest !== pin.archiveSha256) {
    throw new Error(`${pin.name}: archive sha256 ${digest} is not the pinned ${pin.archiveSha256}`)
  }
  return gz
}

/** The candidates Tailwind emits a rule for, sorted and unique. */
export async function utilitiesOf(candidates) {
  const unique = [...new Set(candidates)]
  const emitted = await emittedClasses(unique)
  return unique.filter((c) => emitted.has(c)).sort()
}

const HEADER = `/* Adopted-app class safelist. GENERATED by scripts/gen-adopted-app-classes.mjs;
   do not edit by hand, run \`npm run gen:adopted-app-classes\`.

   A former built-in installed from its own repository ships a UI bundle with no
   stylesheet, so its Tailwind utilities come from this sheet. Each line below is
   a utility that a pinned tree's ui/src/ or core's wheel copy under
   src/apps/<name>/ uses, kept compiled whether or not the copy still exists.
   Append-only: a pinned bundle on some host may still need a class no scan
   finds any more. The contract is website/docs/theming-contract.md, "Adopted
   apps' utilities".

   Generated against these adoption-pins.json commits:
`

/** The file's text for `classes` (sorted, unique) and the pin `commits`. */
export function renderSafelist({ classes, commits }) {
  const lines = [HEADER.trimEnd()]
  for (const commit of [...commits].sort()) lines.push(`   pin ${commit}`)
  lines.push('*/')
  for (const cls of classes) {
    if (/[\s{}\\]/.test(cls) || (cls.includes('"') && cls.includes("'"))) {
      throw new Error(`cannot express ${JSON.stringify(cls)} as an @source inline() candidate`)
    }
    const q = cls.includes('"') ? "'" : '"'
    lines.push(`@source inline(${q}${cls}${q});`)
  }
  return lines.join('\n') + '\n'
}

/** `{ classes, commits }` read back out of the file's text, in file order. */
export function parseSafelist(text) {
  const classes = [...text.matchAll(/^@source inline\((["'])(.*)\1\);$/gm)].map((m) => m[2])
  const commits = [...text.matchAll(/^ {3}pin ([0-9a-f]{40})$/gm)].map((m) => m[1])
  return { classes, commits }
}

async function main() {
  const pins = readPins()
  const kept = existsSync(SAFELIST_PATH) ? parseSafelist(readFileSync(SAFELIST_PATH, 'utf8')).classes : []
  const candidates = []
  for (const pin of pins) {
    const tree = treeCandidates(await fetchPinnedArchive(pin))
    if (tree.files === 0) throw new Error(`${pin.name}@${pin.commit}: the pinned tree has no ui/src/ sources`)
    const copy = wheelCopyCandidates(pin.name)
    candidates.push(...tree.candidates, ...copy)
    console.log(`${pin.name}: ${tree.files} pinned file(s), ${wheelCopyFiles(pin.name).length} wheel-copy file(s)`)
  }
  const found = await utilitiesOf(candidates)
  const classes = [...new Set([...kept, ...found])].sort()
  writeFileSync(SAFELIST_PATH, renderSafelist({ classes, commits: pins.map((p) => p.commit) }))
  console.log(`${relative(WEBSITE, SAFELIST_PATH)}: ${classes.length} classes (${classes.length - kept.length} new)`)
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await main()
}
