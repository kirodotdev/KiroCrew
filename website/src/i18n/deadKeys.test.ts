/**
 * Dead catalog keys — a ratchet, not a deletion tool.
 *
 * A key nothing references costs a translator ten languages of work for a string
 * nobody sees. But automatically deleting one is dangerous: the reference may be
 * indirect (`surfaceLabel()` resolves a `labelKey` held in data), and i18next
 * returns the key string itself for a missing key rather than throwing, so a wrong
 * deletion shows up as `pages.settings.aboutPanel.update_error_offline` in the UI
 * instead of a crash. `AboutPanel.tsx` carries a comment about exactly that class of
 * failure taking the whole Settings panel down.
 *
 * So this asserts the count does not GROW. It never proposes a deletion, and it
 * never fails for a key that was dead already.
 *
 * ## What counts as a reference
 *
 * A quoted occurrence of the dotted key anywhere in `src`, or of its plural base
 * (`…_one` / `…_other` strip to the base, which is what `t()` is called with). Keys
 * listed in `pluralKeys.json` are always considered live: that registry IS the
 * reference.
 *
 * The scan is a floor, deliberately. It cannot see a key assembled at runtime — which
 * is why `dynamicKeys.test.ts` forbids assembling one in the first place, and why
 * that guard is a precondition for this one meaning anything.
 */

import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { DEFAULT_LANGUAGE } from './languages'
import pluralKeys from './pluralKeys.json'

/**
 * Dead keys at the time this gate went in. Ratchet DOWN when keys are removed; never
 * raise it. A rise means a new key was added and nothing uses it — usually a typo at
 * the call site, or copy that was deleted without its key.
 */
const BASELINE = 22

const SRC = join(__dirname, '..')
const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

function readAllSource(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'locales') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) readAllSource(full, out)
    else if (/\.tsx?$/.test(entry)) out.push(readFileSync(full, 'utf-8'))
  }
  return out
}

const en = flatten((RUNTIME_CATALOGS[DEFAULT_LANGUAGE] as { translation: unknown }).translation)
const registry = new Set(pluralKeys as string[])
const blob = readAllSource(SRC).join('\n')

const dead = Object.keys(en)
  .filter((key) => {
    const base = key.replace(PLURAL_SUFFIX, '')
    if (registry.has(key) || registry.has(base)) return false
    return !(
      blob.includes(`'${key}'`) || blob.includes(`"${key}"`)
      || blob.includes(`'${base}'`) || blob.includes(`"${base}"`)
    )
  })
  .sort()

describe('dead catalog keys', () => {
  it('the reference scan found a plausible corpus', () => {
    // A broken walker would report every key as dead and "pass" a ratchet that only
    // checks for growth in the wrong direction. Assert the scan actually saw code.
    expect(blob.length).toBeGreaterThan(1_000_000)
    expect(Object.keys(en).length).toBeGreaterThan(3000)
  })

  it('does not grow', () => {
    expect(
      dead.length,
      `${dead.length} catalog keys are referenced nowhere (baseline ${BASELINE}).\n`
        + `${dead.slice(0, 12).map((k) => `  ${k}`).join('\n')}\n`
        + 'A new dead key usually means a typo at the call site, or copy deleted without '
        + 'its key. If you removed keys, lower BASELINE in this file so the gain is kept.',
    ).toBeLessThanOrEqual(BASELINE)
  })

  it('reports the exact count so the baseline can be ratcheted', () => {
    // Fails when the count DROPS, for the same reason the untranslated-string gate
    // does: a one-way ratchet never comes down on its own.
    expect(
      dead.length,
      `only ${dead.length} dead keys now, below the baseline of ${BASELINE} — lower `
        + 'BASELINE in this file to lock the improvement in.',
    ).toBe(BASELINE)
  })
})
