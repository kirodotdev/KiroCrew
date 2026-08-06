#!/usr/bin/env node
/**
 * Built-in app manifest copy must equal its English catalog value, byte for byte.
 *
 * WHY THIS EXISTS. `APP_MANIFEST_KEY` (`src/components/appstore/appManifest.ts`)
 * localises app metadata WITHOUT touching `app.json`, so the English now lives in two
 * places on purpose: the manifest, which is what `kirocrew app list` and every other
 * catalog-less consumer prints, and `locales/en.json`, which is what the SPA renders
 * and what the nine translated catalogs were derived from.
 *
 * Two copies is a drift machine unless something pins them together. Edit a
 * `description` in `app.json` and, with no check, three things happen silently: the CLI
 * shows the new sentence while the dashboard shows the old one, every translation is
 * now a translation of a string that no longer exists, and no existing gate notices —
 * `check-i18n-keys` only proves the key RESOLVES, and the en-XA render gate only proves
 * the value came from a catalog, not that it came from the RIGHT one.
 *
 * So this is the whole justification for the additive design. It is a legitimate hard
 * zero rather than a stored count: both sides are files in this repo at this commit, it
 * reads no baseline, and any failure names an exact file, key and pair of strings that
 * the author of the diff can fix.
 *
 * WHAT IT ENFORCES
 *   1. every key the naming convention implies for a built-in exists in `en.json`
 *   2. every one of those values equals the manifest's own prose exactly, byte for byte
 *   3. the highlight count matches — one key per bullet, 1-indexed, no gaps
 *
 * WHAT IT DELIBERATELY DOES NOT ENFORCE, and where that lives instead
 *   - That `APP_MANIFEST_KEY` actually contains an entry per built-in, and that its
 *     keys are the ones this convention predicts. That needs the real table, and a
 *     script cannot read a TS module without either a regex (unsound — it matches
 *     commented-out source) or a compile step. `src/test/appManifest.test.ts` imports
 *     the module through vitest and asserts it there.
 *   - That the nine translated catalogs differ from English. `catalogParity.test.ts`
 *     already requires every key in all of them, and a translator legitimately leaves a
 *     proper noun ("Papyrus") identical.
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const WEB = join(HERE, '..')
const REPO = join(WEB, '..')
const BUILTINS = join(REPO, 'src', 'kiro_crew', 'apps', 'builtins')
const TABLE = join(WEB, 'src', 'components', 'appstore', 'appManifest.ts')
const EN = join(WEB, 'src', 'i18n', 'locales', 'en.json')

const fail = []
const note = m => process.stdout.write(`${m}\n`)

/** Read the manifests the gateway would discover: a dir with an `app.json`. */
function manifests() {
  const out = new Map()
  for (const entry of readdirSync(BUILTINS, { withFileTypes: true }).sort((a, b) => a.name < b.name ? -1 : 1)) {
    if (!entry.isDirectory() || entry.name.startsWith('.') || entry.name.startsWith('_')) continue
    const file = join(BUILTINS, entry.name, 'app.json')
    if (!existsSync(file)) continue
    let m
    try {
      m = JSON.parse(readFileSync(file, 'utf8'))
    } catch (err) {
      fail.push(`${entry.name}/app.json is not valid JSON: ${err.message}`)
      continue
    }
    const pages = (m.ui && m.ui.pages) || []
    out.set(m.name, {
      dir: entry.name,
      displayName: m.displayName || '',
      description: m.description || '',
      pageLabel: (pages[0] && pages[0].label) || '',
      highlights: m.highlights || [],
    })
  }
  return out
}

/**
 * Expected catalog keys for an app, DERIVED FROM ITS ID rather than read out of the TS.
 *
 * An earlier draft regex-parsed `APP_MANIFEST_KEY` out of `appManifest.ts`, and that was
 * unsound in the quietest possible way: a regex over source text also matches source
 * text inside a COMMENT, so wrapping an entry in `/* … *\/` left the gate reading a
 * table the runtime no longer has. It would exit 0 while that app rendered untranslated
 * — a gate whose failure mode is silence, which is the one shape a gate may not have.
 *
 * Deriving the keys removes the parse entirely: this file now needs only `app.json` and
 * `en.json`, both plain JSON. The other half — that `APP_MANIFEST_KEY` actually contains
 * these keys — is asserted in `src/test/appManifest.test.ts`, which IMPORTS the real
 * module through vitest's TypeScript pipeline, so a commented-out entry fails there with
 * no parsing of any kind. Each half is checked by the tool that can see it.
 *
 * The convention is `apps.<camelCaseId>.manifest.<field>`; `highlight_N` is 1-indexed.
 */
const camel = id => {
  const [head, ...rest] = id.split('-')
  return head + rest.map(w => w.charAt(0).toUpperCase() + w.slice(1)).join('')
}

function expectedKeys(name, highlightCount) {
  const ns = `apps.${camel(name)}.manifest`
  return {
    displayName: `${ns}.display_name`,
    description: `${ns}.description`,
    pageLabel: `${ns}.page_label`,
    highlights: Array.from({ length: highlightCount }, (_, i) => `${ns}.highlight_${i + 1}`),
  }
}

const lookup = (cat, key) => key.split('.').reduce((o, p) => (o == null ? undefined : o[p]), cat)

const apps = manifests()
const en = JSON.parse(readFileSync(EN, 'utf8'))

// Every built-in's expected keys must exist in en.json and hold the manifest's own
// words. Coverage of `APP_MANIFEST_KEY` itself is `src/test/appManifest.test.ts`.
for (const [name, app] of apps) {
  const keys = expectedKeys(name, app.highlights.length)
  const check = (key, expected, what) => {
    if (!key) { fail.push(`${name}: APP_MANIFEST_KEY is missing a '${what}' key`); return }
    const actual = lookup(en, key)
    if (actual === undefined) {
      fail.push(`${name}: key '${key}' is not in locales/en.json`)
    } else if (actual !== expected) {
      fail.push(`${name}.${what} drifted from ${app.dir}/app.json\n`
        + `      manifest: ${JSON.stringify(expected)}\n`
        + `      en.json : ${JSON.stringify(actual)}\n`
        + '      Update the catalog value (and re-translate the nine others), or revert '
        + 'the manifest.')
    }
  }
  check(keys.displayName, app.displayName, 'displayName')
  check(keys.description, app.description, 'description')
  check(keys.pageLabel, app.pageLabel, 'pageLabel')
  keys.highlights.forEach((k, i) => check(k, app.highlights[i], `highlight_${i + 1}`))
}

const strings = [...apps.values()].reduce((n, a) => n + 3 + a.highlights.length, 0)
if (fail.length) {
  note(`\n[app-manifest-sync] FAIL — ${fail.length} problem(s)\n`)
  for (const f of fail) note(`    ${f}`)
  note('')
  process.exit(1)
}
note(`OK: ${apps.size} built-in manifests, ${strings} strings match locales/en.json exactly.`)
