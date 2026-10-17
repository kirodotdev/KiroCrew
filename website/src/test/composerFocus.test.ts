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
import { COMPOSER_EXPAND_EVENT, focusComposer, focusComposerAfter, focusComposerForOpenedSession, focusComposerForResumedSession, focusComposerNow, queryComposer, revealComposer, releaseComposerForKeyboardSwitch, consumeComposerRelease } from '../pages/chat/composerFocus'

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

describe('focusComposerForOpenedSession — a quick-search surface opened a session (#15732)', () => {
  const OPENED = 'zzq-opened'
  type Claim = { requestId: string | null; target: string | null }
  /** A store whose active slot and switch claim the test moves by hand, standing
   *  in for the `switchSlot` reducers: `pending` enters the target and takes the
   *  claim, `fulfilled` clears the claim it owns, a 404's `rejected` clears it
   *  and unwinds to the origin, a later gesture moves the slot elsewhere. */
  const storeOn = (initial: string, claim: Claim = { requestId: null, target: null }) => {
    let activeSlot = initial
    let current = claim
    const listeners = new Set<() => void>()
    return {
      getState: () => ({ chat: { activeSlot, slotSwitchRequestId: current.requestId, slotSwitchTarget: current.target } }),
      subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } },
      set: (patch: { activeSlot?: string; claim?: Claim }) => {
        if (patch.activeSlot !== undefined) activeSlot = patch.activeSlot
        if (patch.claim !== undefined) current = patch.claim
        listeners.forEach(listener => listener())
      },
    }
  }

  it('does NOT focus while the switch is in flight, then focuses once it has fulfilled', async () => {
    // The window under test: `pending` has already entered the target, so a caret
    // placed here would route keystrokes to a slot the gateway may still refuse.
    let resolve!: (v: unknown) => void
    const store = storeOn(OPENED)
    focusComposerForOpenedSession(new Promise(r => { resolve = r }), OPENED, store)
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    resolve({ key: OPENED })
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('focuses on the frame after an already-fulfilled switch, not synchronously', async () => {
    focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
    await Promise.resolve()
    await Promise.resolve()
    expect(document.activeElement).not.toBe(composer)
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('focuses nothing when the switch rejects — the unwind restored the origin — and does not leak the rejection', async () => {
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)
    // The store reads as the reducers leave it after a 404: back on the origin.
    focusComposerForOpenedSession(Promise.reject(Object.assign(new Error('not found'), { status: 404 })), OPENED, storeOn('zzq-origin'))
    await flushFrame()
    await flushFrame()
    process.off('unhandledRejection', unhandled)
    expect(document.activeElement).not.toBe(composer)
    expect(unhandled).not.toHaveBeenCalled()
  })

  it('focuses nothing when the user has moved on before the switch fulfilled', async () => {
    // A second gesture during the round trip: `pending` for the newer target has
    // taken the active slot, and the fulfilled reducer ignores this payload. So
    // does the focus -- the newer switch owns the composer now.
    let resolve!: (v: unknown) => void
    const store = storeOn(OPENED)
    focusComposerForOpenedSession(new Promise(r => { resolve = r }), OPENED, store)
    store.set({ activeSlot: 'zzq-newer' })
    resolve({ key: OPENED })
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('reads the active slot ON the frame, so a slot change between settlement and paint still counts', async () => {
    const store = storeOn(OPENED)
    focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, store)
    await Promise.resolve()
    await Promise.resolve()
    store.set({ activeSlot: 'zzq-newer' })
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('defers to a newer SAME-KEY switch that still owns the claim, and focuses once it has landed', async () => {
    // The older of two overlapping reads of the same slot fulfilled first: the
    // slot is active, but the newer request owns the claim and may yet unwind the
    // selection. Focus waits for that claim to clear with the slot still active.
    const store = storeOn(OPENED, { requestId: 'zzq-req-2', target: OPENED })
    focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, store)
    await flushFrame()
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    // An unrelated store change while the claim is still pending changes nothing.
    store.set({})
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    store.set({ claim: { requestId: null, target: null } })
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('drops the deferred focus when the newer same-key switch unwinds the selection (404)', async () => {
    const store = storeOn(OPENED, { requestId: 'zzq-req-2', target: OPENED })
    focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, store)
    await flushFrame()
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    // The rejected reducer clears the claim and restores the origin in one write.
    store.set({ activeSlot: 'zzq-origin', claim: { requestId: null, target: null } })
    await flushFrame()
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    // And a later switch back to the slot is not this gesture's to focus.
    store.set({ activeSlot: OPENED })
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('does not defer to a pending switch for ANOTHER slot — that one moved the active slot already', async () => {
    const store = storeOn('zzq-newer', { requestId: 'zzq-req-2', target: 'zzq-newer' })
    focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, store)
    await flushFrame()
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    store.set({ claim: { requestId: null, target: null } })
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('skips touch devices, as the sidebar autofocus does', async () => {
    touch = true
    focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('leaves a collapsed composer collapsed: opening a session is navigation, not a typing intent', async () => {
    // A collapsed composer is unmounted; the only way to reach it is the expand
    // request, which `focusComposer` sends and this helper deliberately does not —
    // the sidebar's autofocus leaves the reading preference alone too.
    composer.remove()
    const expandRequested = vi.fn((e: Event) => e.preventDefault())
    window.addEventListener(COMPOSER_EXPAND_EVENT, expandRequested)
    try {
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      expect(expandRequested).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(COMPOSER_EXPAND_EVENT, expandRequested)
    }
  })

  it('declines while an editable element holds focus — a field the user chose mid-flight keeps the caret', async () => {
    // The sidebar's own rule, read on the frame. The surfaces leave focus on
    // `<body>` when they close (the Command Bar's trap captures its own autoFocus
    // input, so its restore reaches nothing), so an editable element focused by
    // the time the switch lands is one the user chose during the round trip --
    // the sidebar's search box, a title editor, the bar opened again -- and the
    // late caret must not take it from them.
    const searchBox = document.createElement('input')
    document.body.appendChild(searchBox)
    try {
      searchBox.focus()
      expect(document.activeElement).toBe(searchBox)
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).toBe(searchBox)
    } finally {
      searchBox.remove()
    }
  })

  describe('split view (#15937): the caret goes to the pane bound to the OPENED slot', () => {
    // Split view: every pane mounts its own composer bound to its own slot, and
    // the grid's focus model never follows `activeSlot`. Each pane names its slot
    // in the DOM (`data-pane-slot`, ChatPane's root) so the lookup can find the
    // pane that renders the opened session instead of the grid-focused one.
    const GRID_FOCUSED = 'zzq-grid-focused'
    type Pane = { pane: HTMLDivElement; ta: HTMLTextAreaElement }
    const mountPane = (slot: string, gridFocused: boolean): Pane => {
      const pane = document.createElement('div')
      pane.setAttribute('data-chat-pane', gridFocused ? 'focused' : '')
      pane.setAttribute('data-pane-slot', slot)
      const ta = document.createElement('textarea')
      ta.setAttribute('data-composer-input', '')
      pane.appendChild(ta)
      document.body.appendChild(pane)
      return { pane, ta }
    }
    let focusedPane: Pane
    let openedPane: Pane
    beforeEach(() => {
      // The suite-level fixture composer sits OUTSIDE any pane: with panes
      // mounted it is nobody's composer, so it must never be the answer.
      composer.remove()
      focusedPane = mountPane(GRID_FOCUSED, true)
      openedPane = mountPane(OPENED, false)
    })
    afterEach(() => {
      focusedPane.pane.remove()
      openedPane.pane.remove()
      document.body.appendChild(composer)
    })

    it('focuses the composer of the pane bound to the opened key, never the grid-focused pane\'s', async () => {
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).toBe(openedPane.ta)
      expect(document.activeElement).not.toBe(focusedPane.ta)
    })

    it('never answers with the pane the grid marks focused when the key names another pane, whichever came first in the document', async () => {
      // Document order reversed: the opened pane precedes the grid-focused one.
      // A first-match or focused-marker lookup would each pick differently; the
      // key decides regardless.
      document.body.insertBefore(openedPane.pane, focusedPane.pane)
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).toBe(openedPane.ta)
    })

    it('focuses nothing when panes are mounted and none is bound to the opened key', async () => {
      // The opened session is not on screen: the store switched, the grid did
      // not change (what the split should DO here is the open product question
      // on the issue), and the grid-focused pane is a session the gesture did
      // not open -- the next Enter would send there. So: no caret.
      openedPane.pane.setAttribute('data-pane-slot', 'zzq-some-other-slot')
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).not.toBe(focusedPane.ta)
      expect(document.activeElement).not.toBe(openedPane.ta)
      expect(document.activeElement).not.toBe(composer)
    })

    it('compares the slot key as bytes: a pane whose slot merely starts with the key is not the opened pane', async () => {
      openedPane.pane.setAttribute('data-pane-slot', `${OPENED}-2`)
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).not.toBe(openedPane.ta)
      expect(document.activeElement).not.toBe(focusedPane.ta)
    })

    it('a pane that names no slot at all (the pre-#15937 DOM) is never the answer', async () => {
      // The shape #15785 pinned: a `data-chat-pane` with no slot name. Such a pane
      // cannot be shown to be the opened session's, so nothing is focused -- the
      // same outcome #15785 chose, now for the stated reason rather than for
      // every pane unconditionally.
      openedPane.pane.removeAttribute('data-pane-slot')
      focusedPane.pane.removeAttribute('data-pane-slot')
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).not.toBe(openedPane.ta)
      expect(document.activeElement).not.toBe(focusedPane.ta)
    })

    it('keeps the sidebar\'s rules in split view too: an editable element the user holds keeps the caret', async () => {
      const searchBox = document.createElement('input')
      document.body.appendChild(searchBox)
      try {
        searchBox.focus()
        focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
        await flushFrame()
        await flushFrame()
        expect(document.activeElement).toBe(searchBox)
      } finally {
        searchBox.remove()
      }
    })

    it('skips touch devices in split view too', async () => {
      touch = true
      focusComposerForOpenedSession(Promise.resolve({ key: OPENED }), OPENED, storeOn(OPENED))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).not.toBe(openedPane.ta)
    })
  })
})

describe('focusComposerForResumedSession — only once the resume entered the session', () => {
  const RESUMED = 'zzq-resumed'
  const entered = { ok: true, surface: '', key: RESUMED }

  it('does NOT focus while the resume is in flight, then focuses once it has entered', async () => {
    let resolve!: (r: { ok: boolean; surface?: string; key: string }) => void
    focusComposerForResumedSession(new Promise(r => { resolve = r }))
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
    resolve(entered)
    await flushFrame()
    expect(document.activeElement).toBe(composer)
  })

  it('focuses nothing for an `ok: false` answer — the reducer left the active slot where it was', async () => {
    focusComposerForResumedSession(Promise.resolve({ ok: false, surface: '', key: RESUMED }))
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('focuses nothing for a surface the chat page cannot display (#3624)', async () => {
    focusComposerForResumedSession(Promise.resolve({ ok: true, surface: 'slack', key: RESUMED }))
    await flushFrame()
    expect(document.activeElement).not.toBe(composer)
  })

  it('focuses nothing when the resume rejects, and does not leak the rejection', async () => {
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)
    focusComposerForResumedSession(Promise.reject(new Error('404')))
    await flushFrame()
    await flushFrame()
    process.off('unhandledRejection', unhandled)
    expect(document.activeElement).not.toBe(composer)
    expect(unhandled).not.toHaveBeenCalled()
  })

  describe('split view (#15937): the caret goes to the pane bound to the RESUMED slot', () => {
    // The history path: the key the reducer entered is the thunk's own payload
    // field, so the same pane lookup applies as for a live-row switch.
    const GRID_FOCUSED = 'zzq-grid-focused'
    const mountPane = (slot: string, gridFocused: boolean) => {
      const pane = document.createElement('div')
      pane.setAttribute('data-chat-pane', gridFocused ? 'focused' : '')
      pane.setAttribute('data-pane-slot', slot)
      const ta = document.createElement('textarea')
      ta.setAttribute('data-composer-input', '')
      pane.appendChild(ta)
      document.body.appendChild(pane)
      return { pane, ta }
    }
    let focusedPane: ReturnType<typeof mountPane>
    let resumedPane: ReturnType<typeof mountPane>
    beforeEach(() => {
      composer.remove()
      focusedPane = mountPane(GRID_FOCUSED, true)
      resumedPane = mountPane(RESUMED, false)
    })
    afterEach(() => {
      focusedPane.pane.remove()
      resumedPane.pane.remove()
      document.body.appendChild(composer)
    })

    it('focuses the composer of the pane bound to the resumed key, never the grid-focused pane\'s', async () => {
      focusComposerForResumedSession(Promise.resolve(entered))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).toBe(resumedPane.ta)
      expect(document.activeElement).not.toBe(focusedPane.ta)
    })

    it('focuses nothing when panes are mounted and none is bound to the resumed key', async () => {
      resumedPane.pane.setAttribute('data-pane-slot', 'zzq-some-other-slot')
      focusComposerForResumedSession(Promise.resolve(entered))
      await flushFrame()
      await flushFrame()
      expect(document.activeElement).not.toBe(focusedPane.ta)
      expect(document.activeElement).not.toBe(resumedPane.ta)
    })
  })
})

describe('focusComposerNow with no opened key (the palette\'s dismiss fallback)', () => {
  // The legacy palette falls back to `focusComposerNow()` when a dismiss has no
  // element to restore focus to. That gesture opened NO session, so in split
  // view there is no pane for it to claim, and it focuses nothing -- the
  // pre-#15937 answer, pinned here so a change to it is a decision and not an
  // accident. (A single composer with no pane mounted is focused, as before.)
  it('focuses the single composer when no pane is mounted', async () => {
    focusComposerNow()
    expect(document.activeElement).toBe(composer)
  })

  it('focuses nothing while panes are mounted, not even the grid-focused pane\'s composer', async () => {
    composer.remove()
    const pane = document.createElement('div')
    pane.setAttribute('data-chat-pane', 'focused')
    pane.setAttribute('data-pane-slot', 'zzq-grid-focused')
    const ta = document.createElement('textarea')
    ta.setAttribute('data-composer-input', '')
    pane.appendChild(ta)
    document.body.appendChild(pane)
    try {
      focusComposerNow()
      expect(document.activeElement).not.toBe(ta)
    } finally {
      pane.remove()
      document.body.appendChild(composer)
    }
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

describe('the Lexical composer (a contenteditable div) is found the same way', () => {
  // The Style Markdown While Typing setting renders the Lexical composer, a
  // contenteditable <div data-composer-input>, instead of the textarea. Every
  // focus intent must still reach it.
  let rich: HTMLDivElement
  beforeEach(() => {
    composer.remove()
    rich = document.createElement('div')
    rich.setAttribute('data-composer-input', '')
    rich.contentEditable = 'true'
    rich.tabIndex = 0
    document.body.appendChild(rich)
  })
  afterEach(() => { rich.remove() })

  it('queryComposer resolves the rich composer', () => {
    expect(queryComposer()).toBe(rich)
  })

  it('focusComposer focuses the rich composer', async () => {
    focusComposer()
    await flushFrame()
    expect(document.activeElement).toBe(rich)
  })

  it('a keyboard switch blurs the rich composer too', () => {
    rich.focus()
    expect(document.activeElement).toBe(rich)
    releaseComposerForKeyboardSwitch()
    expect(document.activeElement).not.toBe(rich)
    consumeComposerRelease()
  })
})

describe('split view: the lookup is scoped to the pane holding focus', () => {
  // The session grid mounts one composer PER pane, so a document-global
  // first-match lookup would always land on the first pane regardless of
  // where the user is working. These lock the active-pane scoping and the
  // document-wide fallback that keeps single-pane behaviour unchanged.
  const buildPane = () => {
    const pane = document.createElement('div')
    pane.setAttribute('data-chat-pane', '')
    const ta = document.createElement('textarea')
    ta.setAttribute('data-composer-input', '')
    pane.appendChild(ta)
    document.body.appendChild(pane)
    return { pane, ta }
  }

  let first: ReturnType<typeof buildPane>
  let second: ReturnType<typeof buildPane>

  beforeEach(() => {
    // The suite-level fixture composer sits OUTSIDE any pane; remove it so
    // these tests exercise the grid shape alone.
    composer.remove()
    first = buildPane()
    second = buildPane()
  })
  afterEach(() => {
    first.pane.remove()
    second.pane.remove()
  })

  it('resolves the SECOND pane composer when focus is inside the second pane', () => {
    second.ta.focus()
    expect(queryComposer()).toBe(second.ta)
    expect(queryComposer()).not.toBe(first.ta)
  })

  it('resolves via any focused element inside the pane, not only the composer itself', () => {
    // A shortcut can fire while a header button or picker inside the pane
    // holds focus — the pane boundary, not the focused element type, decides.
    const btn = document.createElement('button')
    second.pane.appendChild(btn)
    btn.focus()
    expect(queryComposer()).toBe(second.ta)
  })

  it('falls back to first-in-document-order when focus is outside every pane', () => {
    // Focus on <body>: no pane context, so the document-wide fallback applies
    // — identical to the pre-split behaviour.
    ;(document.activeElement as HTMLElement | null)?.blur?.()
    expect(queryComposer()).toBe(first.ta)
  })

  it('falls back document-wide when the active pane has no composer', () => {
    second.ta.remove()
    const btn = document.createElement('button')
    second.pane.appendChild(btn)
    btn.focus()
    expect(queryComposer()).toBe(first.ta)
  })

  it('resolves the grid-focused pane when focus sits in a portal outside every pane', () => {
    // The pane's pickers render through createPortal under document.body, so
    // their focused input has NO pane ancestor. The grid marks its focused
    // pane with data-chat-pane="focused"; that marker must win over the
    // document-order fallback, or Alt+Enter from pane 2's picker would send
    // the caret to pane 1.
    second.pane.setAttribute('data-chat-pane', 'focused')
    const portalInput = document.createElement('input')
    document.body.appendChild(portalInput)
    portalInput.focus()
    expect(queryComposer()).toBe(second.ta)
    portalInput.remove()
  })

  it('activeElement pane ancestry outranks the grid-focused marker', () => {
    // Clicking INTO pane 1 while the grid still marks pane 2 as focused: the
    // element the user is actually in wins.
    first.pane.setAttribute('data-chat-pane', '')
    second.pane.setAttribute('data-chat-pane', 'focused')
    first.ta.focus()
    expect(queryComposer()).toBe(first.ta)
  })

  it('focusComposer moves the caret to the active pane composer, not the first pane', async () => {
    // The end-to-end behavioural claim from the issue: Alt+Enter (and every
    // focus-the-composer path) acts on the pane the user is working in.
    const btn = document.createElement('button')
    second.pane.appendChild(btn)
    btn.focus()
    focusComposer()
    await flushFrame()
    expect(document.activeElement).toBe(second.ta)
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

describe('releaseComposerForKeyboardSwitch / consumeComposerRelease', () => {
  afterEach(() => { consumeComposerRelease() }) // never leak an armed one-shot into other suites

  it('blurs the focused composer and arms a one-shot release', () => {
    composer.focus()
    expect(document.activeElement).toBe(composer)
    releaseComposerForKeyboardSwitch()
    expect(document.activeElement).not.toBe(composer)
    expect(consumeComposerRelease()).toBe(true)
    // One-shot: a second consume finds nothing.
    expect(consumeComposerRelease()).toBe(false)
  })

  it('arms without blurring when focus is outside the composer (only the composer we own is released)', () => {
    const other = document.createElement('input')
    document.body.appendChild(other)
    try {
      other.focus()
      releaseComposerForKeyboardSwitch()
      expect(document.activeElement).toBe(other)
      expect(consumeComposerRelease()).toBe(true)
    } finally {
      other.remove()
    }
  })

  it('a leaked release EXPIRES: a flag no consumer collected cannot suppress a later autofocus', () => {
    // Opus finding on aa05ae203: in split view no mounted ChatInput transitions
    // autoFocusKey, so an armed flag survives until split exit and eats the
    // first legitimate autofocus there. The consumer runs within the same
    // commit (ms); anything older than the TTL is a leak and must read false.
    vi.useFakeTimers()
    try {
      releaseComposerForKeyboardSwitch()
      vi.advanceTimersByTime(2000) // well past the 1.5s TTL
      expect(consumeComposerRelease()).toBe(false)
      // A fresh arming right after still consumes normally.
      releaseComposerForKeyboardSwitch()
      expect(consumeComposerRelease()).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})
