/**
 * Composer focus after creating a session.
 *
 * The bug this locks: there is exactly ONE composer element and it is bound to
 * whichever slot is ACTIVE. Focusing it while `createSlot` is still in flight
 * puts the caret on the OLD session, so anything typed in that window becomes
 * the old slot's draft and is lost when the new slot activates. The collapsed
 * sidebar's flyout originally dispatched and focused on the next frame without
 * waiting, which is that window.
 *
 * Locks the contract:
 *  (1) `focusComposerAfter` does NOT focus before the promise fulfils.
 *  (2) It focuses after fulfilment.
 *  (3) A rejected create focuses nothing and produces no unhandled rejection.
 *  (4) Touch devices are skipped — focusing raises the on-screen keyboard over
 *      the thing the user just made.
 *  (5) The composer is found by the stable `data-composer-input` attribute,
 *      NEVER by its aria-label: the label is translated in all twelve catalogs,
 *      so a label-based lookup matches in English only and silently no-ops for
 *      every other locale.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, extname, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { focusComposer, focusComposerAfter, queryComposer, revealComposer } from '../pages/chat/composerFocus'

let touch = false
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touch }))

/** Drive the rAF the helper schedules. */
const flushFrame = async () => {
  await Promise.resolve()
  await new Promise<void>(r => requestAnimationFrame(() => r()))
  await Promise.resolve()
}

let composer: HTMLTextAreaElement

beforeEach(() => {
  touch = false
  composer = document.createElement('textarea')
  composer.setAttribute('data-composer-input', '')
  // A translated label, exactly as a non-English catalog renders it. Every
  // assertion below passing against THIS label is what proves the lookup is
  // language-agnostic.
  composer.setAttribute('aria-label', '消息输入')
  document.body.appendChild(composer)
})
afterEach(() => { composer.remove() })

describe('focusComposer', () => {
  it('focuses the composer on the next frame', async () => {
    expect(document.activeElement).not.toBe(composer)
    focusComposer()
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('does nothing synchronously — the new slot has not committed yet', () => {
    focusComposer()
    expect(document.activeElement).not.toBe(composer)
  })

  it('skips touch devices, where focus raises the keyboard over the new session', async () => {
    touch = true
    focusComposer()
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('does not throw when the composer is absent', async () => {
    composer.remove()
    focusComposer()
    await expect(flushFrame()).resolves.toBeUndefined()
  })
})

describe('revealComposer', () => {
  it('focuses on desktop, which scrolls the composer into view', async () => {
    revealComposer()
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('scrolls into view WITHOUT focusing on touch — focus would pop the soft keyboard', async () => {
    touch = true
    const scrolled = vi.fn()
    composer.scrollIntoView = scrolled
    revealComposer()
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    expect(scrolled).toHaveBeenCalledWith({ block: 'nearest' })
  })

  it('does not throw when the composer is absent', async () => {
    composer.remove()
    revealComposer()
    await expect(flushFrame()).resolves.toBeUndefined()
  })
})

describe('focusComposerAfter', () => {
  it('does NOT focus while creation is still in flight', async () => {
    // The whole point: this window is where a keystroke would land in the OLD
    // session's draft and be lost on activation.
    let resolve!: () => void
    focusComposerAfter(new Promise<void>(r => { resolve = r }))
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    // ...and it still focuses once creation lands.
    resolve()
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('focuses after an already-fulfilled create', async () => {
    focusComposerAfter(Promise.resolve({ key: 'new-slot' }))
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('focuses nothing when creation rejects, and does not leak the rejection', async () => {
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)
    focusComposerAfter(Promise.reject(new Error('gateway offline')))
    await flushFrame()
    await flushFrame()
    process.off('unhandledRejection', unhandled)
    expect(document.activeElement).not.toBe(composer)
    expect(unhandled).not.toHaveBeenCalled()
  })
})

describe('how the composer is found', () => {
  it('resolves by the stable data attribute even under a translated aria-label', () => {
    // The fixture's label is Chinese; a lookup that consulted the label would
    // return null here exactly as it did in production for eleven locales.
    expect(queryComposer()).toBe(composer)
  })

  it('ignores a decoy textarea that lacks the attribute', async () => {
    const decoy = document.createElement('textarea')
    decoy.setAttribute('aria-label', 'Message input')
    document.body.insertBefore(decoy, composer)
    // The decoy carries the ENGLISH label — the old selector's exact target —
    // so this fails loudly if the lookup ever reverts to the label.
    focusComposer()
    await flushFrame()
    expect(document.activeElement).toBe(composer)
    expect(document.activeElement).not.toBe(decoy)
    decoy.remove()
  })
})

describe('no site queries the translated label (class ratchet)', () => {
  // The nine hand-rolled `textarea[aria-label="Message input"]` queries this
  // module replaced all no-opped outside English, and the compiler cannot flag
  // a selector string that names a translated label. This scan holds the whole
  // class shut: production code (including CSS, which had the same bug in
  // cli-mode.css) must never target the composer through its label again —
  // `queryComposer()` / `data-composer-input` is the one sanctioned lookup.
  const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')
  // Quote- and whitespace-tolerant: `[aria-label='Message input']` or
  // `[aria-label = "Message input"]` is the same defect as the canonical form.
  const SELECTOR_FORM = /\[\s*aria-label\s*=\s*(['"])Message input\1\s*\]/

  const walk = (dir: string): string[] => {
    const out: string[] = []
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) {
        // Tests may name the English label as a testing-library query or a
        // decoy fixture; only production lookups are the defect. Exclusion is
        // the exact src/test tree (plus node_modules), not any dir named
        // "test" — a future production dir named test stays inside the guard.
        // *.test.* files elsewhere (e.g. src/apps/mochi/test) are already
        // excluded by the filename filter below.
        if (p === join(SRC, 'test') || name === 'node_modules') continue
        out.push(...walk(p))
      } else if (['.ts', '.tsx', '.css'].includes(extname(name)) && !/\.test\.[jt]sx?$/.test(name)) {
        out.push(p)
      }
    }
    return out
  }

  it('no production source targets the composer via its aria-label', () => {
    const offenders = walk(SRC).filter(f => SELECTOR_FORM.test(readFileSync(f, 'utf-8')))
    expect(offenders).toEqual([])
  })
})
