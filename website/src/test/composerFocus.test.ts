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
 *  (5) It finds the composer by the same aria-label the keyboard-shortcut layer
 *      uses, so a rename breaks here rather than silently breaking focus.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { focusComposer, focusComposerAfter } from '../pages/chat/composerFocus'

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
  composer.setAttribute('aria-label', 'Message input')
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
  it('locates it by the aria-label, not by tag order or a test id', async () => {
    // Both this module and useKeyboardShortcuts look the composer up by this
    // label, so a rename must break here rather than silently breaking focus.
    // A decoy textarea earlier in the document would win any tag-order lookup.
    const decoy = document.createElement('textarea')
    decoy.setAttribute('aria-label', 'Something else')
    document.body.insertBefore(decoy, composer)
    focusComposer()
    await flushFrame()
    expect(document.activeElement).toBe(composer)
    expect(document.activeElement).not.toBe(decoy)
    decoy.remove()
  })
})
