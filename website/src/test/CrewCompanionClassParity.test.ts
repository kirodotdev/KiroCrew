/**
 * Every `cc-` class the companion styles itself with must actually be defined.
 *
 * This exists because of the bug that took the longest to find in this port. The
 * bubble host renders `className="cc-bubble-host"`, while the stylesheet defined
 * `.cc-pet-bubble` — and that one rule is what re-enables `pointer-events` on a
 * page whose body sets `pointer-events: none` so the desktop shows through. With the
 * names out of step the bubble inherited `none`, so every click and every hover
 * passed straight THROUGH it.
 *
 * It failed in the most misleading way available. The OS-level hitbox was correct
 * and the overlay window did accept the click. The ✕ was in the DOM. React's
 * dismiss handler worked under test. Reading the source pointed the wrong way four
 * separate times, because every layer really was correct except the element's
 * ability to receive the event — and nothing logged anything.
 *
 * The repo's `cssClassParity.test.ts` cannot catch it, for two reasons that are both
 * reasonable in their own scope: it checks only `mc-`-prefixed classes, and it reads
 * definitions only from `.css` files, while an app window's rules live in an inline
 * `<style>` block in its HTML entry. This file closes both gaps for this app rather
 * than widening the shared test, which would drag every other app's debt in with it.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join, extname, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const APP = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../apps/crew-companion',
)

const entries = readdirSync(APP).map((n) => join(APP, n))
const read = (f: string) => readFileSync(f, 'utf-8')

/** Every class name used in a `className="..."` / `class="..."` position. */
function usedClasses(text: string): Set<string> {
  const out = new Set<string>()
  // Static attributes only. A template-built name cannot be checked statically, and
  // pretending otherwise would make this test lie in the other direction.
  for (const m of text.matchAll(/class(?:Name)?=["']([^"'{}]+)["']/g)) {
    for (const cls of m[1].split(/\s+/)) if (cls.startsWith('cc-')) out.add(cls)
  }
  return out
}

/** Every class name any selector in this app defines. */
function definedClasses(text: string): Set<string> {
  const out = new Set<string>()
  for (const m of text.matchAll(/\.(cc-[A-Za-z0-9_-]+)/g)) out.add(m[1])
  return out
}

const sources = entries.filter((f) => ['.ts', '.tsx'].includes(extname(f)))
/*
 * Definitions come from three places, and all three are load-bearing:
 *   .css   — the overlay windows' stylesheets
 *   .html  — the inline <style> block in each window entry, which is where the
 *            pointer-events rules live and the gap this file was written to close
 *   .ts    — CSS-in-TS: the dashboard page ships its rules as a string in styles.ts
 *            and injects them with <style>{CC_CSS}</style>, so a selector there is
 *            every bit as real as one in a .css file
 * Missing the third would report all 32 of the page's classes as undefined and make
 * this test useless noise; missing the second is exactly how the original bug hid.
 */
const styles = entries.filter((f) => ['.css', '.html', '.ts'].includes(extname(f)))

const used = new Set<string>()
for (const f of sources) for (const c of usedClasses(read(f))) used.add(c)

const defined = new Set<string>()
for (const f of styles) for (const c of definedClasses(read(f))) defined.add(c)

describe('companion cc- class parity', () => {
  it('found a plausible corpus on both sides', () => {
    // A broken walker would report zero of everything and "pass" vacuously.
    expect(sources.length).toBeGreaterThan(5)
    expect(styles.length).toBeGreaterThan(1)
    expect(used.size).toBeGreaterThan(5)
    expect(defined.size).toBeGreaterThan(5)
  })

  it('every cc- class a component renders is defined somewhere', () => {
    const missing = [...used].filter((c) => !defined.has(c)).sort()
    expect(missing).toEqual([])
  })

  it('the bubble host in particular is defined — it carries pointer-events', () => {
    // Named explicitly because a generic set comparison would let a future rename
    // pass by renaming BOTH sides to something that is styled but not interactive.
    expect(used.has('cc-bubble-host')).toBe(true)
    expect(defined.has('cc-bubble-host')).toBe(true)
    const html = styles.filter((f) => extname(f) === '.html').map(read).join('\n')
    const rule = html.match(/\.cc-bubble-host\s*\{[^}]*\}/)
    expect(rule, '.cc-bubble-host must be defined in the window entry').not.toBeNull()
    expect(rule![0]).toContain('pointer-events: auto')
  })
})
