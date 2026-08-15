/**
 * `panel.tsx` — the companion's panel window, driven through its real entry path.
 *
 * The file had no tests at all, and it is not importable in the ordinary way: `Panel`
 * is never exported and the module MOUNTS ITSELF into `#root` at import time, after
 * awaiting the dashboard theme. So the harness below is the module's own bootstrap —
 * create the host, register the mocks it reaches for, then import it.
 *
 * What is asserted is what the window would actually do: which snapshot fields reach
 * the card, that at most three chronological items are offered, the two suppress-close
 * reports the main process depends on (`panelBreathing`, `panelHold`), the Escape rule
 * and its one exception, and the four writes (add, remove, skip, breathing-done)
 * including what happens when each of them fails.
 *
 * The card and the breathing exercise are replaced by stubs on purpose: both are
 * covered by their own tests, `PetAvatar` inside the exercise resolves art through
 * image decode that never settles under this DOM, and stubbing them keeps every
 * assertion here about the panel's own logic.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor, fireEvent } from '@testing-library/react'

import {
  ADD_PATH,
  BREATHING_DONE_PATH,
  REMINDERS_PATH,
  REMOVE_PATH,
  SKIP_PATH,
} from '../apps/crew-companion/constants'
import type { PanelCardProps } from '../apps/crew-companion/PanelCard'

/** The fire time the stub's add button asks for. Fixed, so the body is assertable. */
const ADD_AT = '2031-04-05T06:07:00.000Z'

// The theme adopt is a real same-origin stylesheet read in production; here it just
// resolves so the bootstrap gets as far as `createRoot`.
vi.mock('../apps/crew-companion/dashboardTheme', () => ({
  adoptDashboardTheme: () => Promise.resolve(),
  watchThemeChanges: () => () => {},
  applyThemeId: () => {},
  extractStylesheetHrefs: () => [],
}))

/** Callbacks the panel registered for `config:updated`. */
let configCbs: Array<() => void> = []
const petBridgeStub = {
  onConfigUpdated: vi.fn((cb: () => void) => {
    configCbs.push(cb)
    return () => {
      configCbs = configCbs.filter((c) => c !== cb)
    }
  }),
}
vi.mock('../apps/crew-companion/petBridge', () => ({ petBridge: petBridgeStub }))

/** What the last add resolved to — the panel's own true/false contract. */
let addResult: boolean | null = null

/**
 * The card, reduced to the props the panel computes plus one control per callback.
 *
 * Everything the panel decides is readable from the DOM this renders, so the
 * assertions below never depend on the real card's layout or wording.
 */
vi.mock('../apps/crew-companion/PanelCard', () => ({
  PanelCard: (props: PanelCardProps) => (
    <div
      data-testid="card"
      data-view={props.view}
      data-side={props.openSide}
      data-break={String(props.breakMins)}
      data-session={String(props.sessionOn)}
      data-rows={String(props.upNext.length)}
    >
      {props.upNext.map((item) => (
        <div
          key={item.id}
          data-testid={`row-${item.id}`}
          data-rel={item.relLabel}
          data-abs={item.absLabel ?? ''}
          data-recurring={String(Boolean(item.recurring))}
        >
          {item.text}
        </div>
      ))}
      <button type="button" data-testid="breathe" onClick={() => props.onBreathe?.()}>
        breathe
      </button>
      <button type="button" data-testid="see-all" onClick={() => props.onSeeAll?.()}>
        see all
      </button>
      <button type="button" data-testid="settings" onClick={() => props.onSettings?.()}>
        settings
      </button>
      <button type="button" data-testid="back" onClick={() => props.onBack?.()}>
        back
      </button>
      <button type="button" data-testid="close" onClick={() => props.onClose?.()}>
        close
      </button>
      <button type="button" data-testid="remove" onClick={() => props.onRemove?.('r1')}>
        remove
      </button>
      <button type="button" data-testid="skip" onClick={() => props.onSkip?.('r2')}>
        skip
      </button>
      <button
        type="button"
        data-testid="add"
        onClick={() => {
          void Promise.resolve(props.onAdd?.('drink water', ADD_AT, 60)).then((ok) => {
            addResult = ok ?? null
          })
        }}
      >
        add
      </button>
    </div>
  ),
}))

/** The exercise, reduced to its two exits. */
vi.mock('../apps/crew-companion/BreathingOverlay', () => ({
  default: (props: { onDone: () => void; onEnd: () => void }) => (
    <div data-testid="breathing">
      <button type="button" data-testid="breathe-done" onClick={() => props.onDone()}>
        done
      </button>
      <button type="button" data-testid="breathe-end" onClick={() => props.onEnd()}>
        end
      </button>
    </div>
  ),
}))

/**
 * `panel.tsx` owns its own React root, so nothing in @testing-library can unmount it.
 * Wrapping `createRoot` is what lets each test tear its window down, which keeps one
 * test's 30s refresh interval out of the next.
 */
const roots: Array<{ unmount: () => void }> = []
vi.mock('react-dom/client', async () => {
  const actual = await vi.importActual<typeof import('react-dom/client')>('react-dom/client')
  return {
    ...actual,
    createRoot: (container: Element | DocumentFragment, options?: never) => {
      const root = actual.createRoot(container, options)
      roots.push(root)
      return root
    },
  }
})

interface Call {
  url: string
  method: string
  body: Record<string, unknown> | null
}

/** Every request the panel made, in order. */
let calls: Call[] = []
/** What GET /reminders answers with. A test may rewrite it before mounting. */
let snapshot: Record<string, unknown> = {}
/** Paths whose fetch REJECTS (the offline path). */
let rejecting = new Set<string>()
/** Paths that answer not-ok (the backend refused). */
let refusing = new Set<string>()

/** The window-level preload bridge, as the panel sees it. */
function installBridge() {
  const preload = {
    panelClose: vi.fn(),
    panelBreathing: vi.fn(),
    panelHold: vi.fn(),
    onPanelOpened: vi.fn((cb: (side: 'left' | 'right') => void) => {
      openedCbs.push(cb)
      return () => {}
    }),
  }
  ;(window as unknown as { crewCompanion?: typeof preload }).crewCompanion = preload
  return preload
}
let openedCbs: Array<(side: 'left' | 'right') => void> = []
let api: ReturnType<typeof installBridge>

/**
 * Mount the panel through its real entry path.
 *
 * `vi.resetModules()` is what makes a second mount possible at all: the bootstrap is
 * module-level, so with a warm registry only the first test would ever render.
 */
async function mountPanel(): Promise<void> {
  const host = document.createElement('div')
  host.id = 'root'
  document.body.appendChild(host)
  vi.resetModules()
  await import('../apps/crew-companion/panel')
  await waitFor(() =>
    expect(document.querySelector('[data-testid="card"]')).not.toBeNull(),
  )
}

function card(): HTMLElement {
  return document.querySelector('[data-testid="card"]') as HTMLElement
}

function control(id: string): HTMLElement {
  return document.querySelector(`[data-testid="${id}"]`) as HTMLElement
}

function row(id: string): HTMLElement | null {
  return document.querySelector(`[data-testid="row-${id}"]`)
}

function countOf(path: string): number {
  return calls.filter((c) => c.url === path).length
}

function iso(minutesFromNow: number): string {
  return new Date(Date.now() + minutesFromNow * 60_000).toISOString()
}

/** A snapshot with four pending reminders and one already-fired one-off. */
function fourPending(): Record<string, unknown> {
  return {
    reminders: [
      { id: 'r4', text: 'fourth', fireAt: iso(240), recurrence: null },
      { id: 'rDone', text: 'fired', fireAt: iso(-60), recurrence: null, done: true },
      { id: 'r2', text: 'second', fireAt: iso(120), recurrence: { everyMinutes: 60 } },
      { id: 'r1', text: 'first', fireAt: iso(45), recurrence: null },
      { id: 'r3', text: 'third', fireAt: iso(180), recurrence: null },
    ],
    breakReminderMins: 30,
    sessionNotificationsEnabled: false,
  }
}

beforeEach(() => {
  // The panel schedules a 30s refresh, and its callback would fire after this
  // environment is gone. Fake timers keep it inside the test's own lifetime;
  // `shouldAdvanceTime` keeps the clock moving so `waitFor` behaves as with real ones.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  calls = []
  configCbs = []
  openedCbs = []
  rejecting = new Set<string>()
  refusing = new Set<string>()
  snapshot = { reminders: [] }
  addResult = null
  window.localStorage.clear()
  // `vi.resetModules()` hands panel.tsx a FRESH i18next whose `initI18n()` (no
  // argument) resolves the language from storage, so English is pinned here too.
  window.localStorage.setItem('mc-lang', 'en')
  api = installBridge()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      let body: Record<string, unknown> | null = null
      if (typeof init?.body === 'string') {
        body = JSON.parse(init.body) as Record<string, unknown>
      }
      calls.push({ url, method: init?.method ?? 'GET', body })
      if (rejecting.has(url)) throw new Error('offline')
      if (refusing.has(url)) return { ok: false, json: async () => ({}) } as unknown as Response
      if (url === REMINDERS_PATH) {
        return { ok: true, json: async () => snapshot } as unknown as Response
      }
      return { ok: true, json: async () => ({}) } as unknown as Response
    }),
  )
})

afterEach(() => {
  for (const root of roots.splice(0)) root.unmount()
  document.querySelectorAll('#root').forEach((n) => n.remove())
  delete (window as unknown as { crewCompanion?: unknown }).crewCompanion
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('the panel window mounts and reads its snapshot', () => {
  it('mounts the card into #root once the theme has been adopted', async () => {
    await mountPanel()
    expect(card()).not.toBeNull()
    expect(document.getElementById('root')?.contains(card())).toBe(true)
  })

  it('reads the reminder snapshot on mount', async () => {
    await mountPanel()
    await waitFor(() => expect(countOf(REMINDERS_PATH)).toBeGreaterThan(0))
    expect(calls[0].method).toBe('GET')
  })

  it('offers at most three items, chronologically, with fired one-offs sunk', async () => {
    snapshot = fourPending()
    await mountPanel()
    await waitFor(() => expect(card().dataset.rows).toBe('3'))
    expect(row('r1')?.textContent).toBe('first')
    expect(row('r2')).not.toBeNull()
    expect(row('r3')).not.toBeNull()
    // Beyond the truthful window, and the already-fired one never displaces a pending.
    expect(row('r4')).toBeNull()
    expect(row('rDone')).toBeNull()
  })

  it('labels an imminent item relatively and a later one absolutely too', async () => {
    snapshot = fourPending()
    await mountPanel()
    await waitFor(() => expect(card().dataset.rows).toBe('3'))
    // Under an hour: a countdown and no clock time.
    expect(row('r1')?.dataset.rel).toContain('45')
    expect(row('r1')?.dataset.abs).toBe('')
    // Further out: a clock time appears beside it.
    expect(row('r2')?.dataset.abs).not.toBe('')
  })

  it('marks a repeating reminder as recurring, so Skip is offered only there', async () => {
    snapshot = fourPending()
    await mountPanel()
    await waitFor(() => expect(card().dataset.rows).toBe('3'))
    expect(row('r2')?.dataset.recurring).toBe('true')
    expect(row('r1')?.dataset.recurring).toBe('false')
  })

  it('threads the break cadence AND the session-alert state out of the same payload', async () => {
    snapshot = fourPending()
    await mountPanel()
    // The regression this guards: `sessionNotificationsEnabled` sat in the payload
    // and never left the fetch, so the footer claimed alerts were on regardless.
    await waitFor(() => expect(card().dataset.session).toBe('false'))
    expect(card().dataset.break).toBe('30')
  })

  it('keeps its defaults when the payload omits the two settings', async () => {
    snapshot = { reminders: [] }
    await mountPanel()
    expect(card().dataset.break).toBe('45')
    expect(card().dataset.session).toBe('true')
  })

  it('ignores settings of the wrong type rather than rendering NaN or blank', async () => {
    snapshot = { reminders: [], breakReminderMins: 'soon', sessionNotificationsEnabled: 'yes' }
    await mountPanel()
    expect(card().dataset.break).toBe('45')
    expect(card().dataset.session).toBe('true')
  })

  it('treats a non-array reminders field as an empty list', async () => {
    snapshot = { reminders: { r1: 'first' } }
    await mountPanel()
    expect(card().dataset.rows).toBe('0')
  })

  it('renders the empty card when the backend refuses the read', async () => {
    refusing.add(REMINDERS_PATH)
    await mountPanel()
    await waitFor(() => expect(countOf(REMINDERS_PATH)).toBeGreaterThan(0))
    expect(card().dataset.rows).toBe('0')
  })

  it('survives a rejected read instead of tearing the window down', async () => {
    rejecting.add(REMINDERS_PATH)
    await mountPanel()
    await waitFor(() => expect(countOf(REMINDERS_PATH)).toBeGreaterThan(0))
    expect(card()).not.toBeNull()
  })
})

describe('when the panel re-reads', () => {
  it('refreshes on its own interval while the window stays open', async () => {
    await mountPanel()
    await waitFor(() => expect(countOf(REMINDERS_PATH)).toBeGreaterThan(0))
    const before = countOf(REMINDERS_PATH)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(countOf(REMINDERS_PATH)).toBeGreaterThan(before)
  })

  it('re-reads the moment a setting changes, instead of waiting out the poll', async () => {
    await mountPanel()
    await waitFor(() => expect(configCbs.length).toBeGreaterThan(0))
    const before = countOf(REMINDERS_PATH)
    snapshot = { reminders: [], breakReminderMins: 90, sessionNotificationsEnabled: true }
    for (const cb of configCbs) cb()
    // The footer beside the settings view used to disagree with it for up to 30s.
    await waitFor(() => expect(card().dataset.break).toBe('90'))
    expect(countOf(REMINDERS_PATH)).toBeGreaterThan(before)
  })

  it('stops polling once the window is torn down', async () => {
    await mountPanel()
    await waitFor(() => expect(countOf(REMINDERS_PATH)).toBeGreaterThan(0))
    for (const root of roots.splice(0)) root.unmount()
    const after = countOf(REMINDERS_PATH)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(countOf(REMINDERS_PATH)).toBe(after)
  })
})

describe('the side the panel opened on, and re-opening it', () => {
  it('defaults to growing out of the companion on the right', async () => {
    await mountPanel()
    expect(card().dataset.side).toBe('right')
  })

  it('aims the spring left when the main process says the panel opened there', async () => {
    await mountPanel()
    await waitFor(() => expect(openedCbs.length).toBeGreaterThan(0))
    for (const cb of openedCbs) cb('left')
    await waitFor(() => expect(card().dataset.side).toBe('left'))
  })

  it('treats any other side as the right, never as an unset origin', async () => {
    await mountPanel()
    await waitFor(() => expect(openedCbs.length).toBeGreaterThan(0))
    for (const cb of openedCbs) cb('up' as 'left' | 'right')
    expect(card().dataset.side).toBe('right')
  })

  it('returns to the glance and re-reads the list on a fresh open', async () => {
    await mountPanel()
    fireEvent.click(control('settings'))
    await waitFor(() => expect(card().dataset.view).toBe('settings'))
    const before = countOf(REMINDERS_PATH)
    for (const cb of openedCbs) cb('right')
    // Relative labels go stale while the window is gone, so the list is re-read.
    await waitFor(() => expect(card().dataset.view).toBe('main'))
    expect(countOf(REMINDERS_PATH)).toBeGreaterThan(before)
  })
})

describe('what the panel reports back so it is not closed on blur', () => {
  it('reports the exercise as not running while the card is a glance', async () => {
    await mountPanel()
    await waitFor(() => expect(api.panelBreathing).toHaveBeenCalledWith(false))
  })

  it('reports the exercise as running the moment it starts', async () => {
    await mountPanel()
    fireEvent.click(control('breathe'))
    await waitFor(() => expect(api.panelBreathing).toHaveBeenCalledWith(true))
    expect(control('breathing')).not.toBeNull()
  })

  it('holds the window open while a secondary view is the destination', async () => {
    await mountPanel()
    await waitFor(() => expect(api.panelHold).toHaveBeenCalledWith(false))
    fireEvent.click(control('see-all'))
    await waitFor(() => expect(card().dataset.view).toBe('all'))
    expect(api.panelHold).toHaveBeenCalledWith(true)
  })

  it('releases the hold on the way back to the list', async () => {
    await mountPanel()
    fireEvent.click(control('see-all'))
    await waitFor(() => expect(card().dataset.view).toBe('all'))
    api.panelHold.mockClear()
    fireEvent.click(control('back'))
    await waitFor(() => expect(card().dataset.view).toBe('main'))
    expect(api.panelHold).toHaveBeenCalledWith(false)
  })
})

describe('closing the panel', () => {
  it('closes on Escape', async () => {
    await mountPanel()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(api.panelClose).toHaveBeenCalled()
  })

  it('does NOT close on Escape while the exercise is running', async () => {
    await mountPanel()
    fireEvent.click(control('breathe'))
    await waitFor(() => expect(control('breathing')).not.toBeNull())
    api.panelClose.mockClear()
    // Escape belongs to the exercise here — losing a 48-second commitment to a
    // stray key would be hostile.
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(api.panelClose).not.toHaveBeenCalled()
  })

  it('ignores every other key', async () => {
    await mountPanel()
    fireEvent.keyDown(window, { key: 'a' })
    expect(api.panelClose).not.toHaveBeenCalled()
  })

  it('closes from the card’s own exit', async () => {
    await mountPanel()
    fireEvent.click(control('close'))
    expect(api.panelClose).toHaveBeenCalled()
  })

  it('runs without a preload bridge at all rather than throwing', async () => {
    delete (window as unknown as { crewCompanion?: unknown }).crewCompanion
    await mountPanel()
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(control('close'))
    expect(card()).not.toBeNull()
  })
})

describe('the four writes the panel owns', () => {
  it('posts a new reminder and confirms it, then re-reads the list', async () => {
    await mountPanel()
    const before = countOf(REMINDERS_PATH)
    fireEvent.click(control('add'))
    await waitFor(() => expect(addResult).toBe(true))
    const add = calls.find((c) => c.url === ADD_PATH)
    expect(add?.method).toBe('POST')
    expect(add?.body).toEqual({ text: 'drink water', fireAt: ADD_AT, everyMinutes: 60 })
    expect(countOf(REMINDERS_PATH)).toBeGreaterThan(before)
  })

  it('reports the add as failed when the backend refuses it', async () => {
    refusing.add(ADD_PATH)
    await mountPanel()
    fireEvent.click(control('add'))
    // False, not a thrown error: the input keeps the text so it can be retried.
    await waitFor(() => expect(addResult).toBe(false))
  })

  it('reports the add as failed when the request never lands', async () => {
    rejecting.add(ADD_PATH)
    await mountPanel()
    fireEvent.click(control('add'))
    await waitFor(() => expect(addResult).toBe(false))
  })

  it('removes a reminder by id and re-reads', async () => {
    await mountPanel()
    const before = countOf(REMINDERS_PATH)
    fireEvent.click(control('remove'))
    await waitFor(() => expect(countOf(REMOVE_PATH)).toBe(1))
    expect(calls.find((c) => c.url === REMOVE_PATH)?.body).toEqual({ id: 'r1' })
    await waitFor(() => expect(countOf(REMINDERS_PATH)).toBeGreaterThan(before))
  })

  it('skips a recurring reminder’s next occurrence by id', async () => {
    await mountPanel()
    fireEvent.click(control('skip'))
    await waitFor(() => expect(countOf(SKIP_PATH)).toBe(1))
    expect(calls.find((c) => c.url === SKIP_PATH)?.body).toEqual({ id: 'r2' })
  })

  it('leaves the list unchanged when a mutation never lands', async () => {
    rejecting.add(REMOVE_PATH)
    snapshot = fourPending()
    await mountPanel()
    await waitFor(() => expect(card().dataset.rows).toBe('3'))
    fireEvent.click(control('remove'))
    await waitFor(() => expect(countOf(REMOVE_PATH)).toBe(1))
    expect(card().dataset.rows).toBe('3')
  })

  it('counts a completed exercise and returns to the card', async () => {
    await mountPanel()
    fireEvent.click(control('breathe'))
    await waitFor(() => expect(control('breathing')).not.toBeNull())
    fireEvent.click(control('breathe-done'))
    await waitFor(() => expect(countOf(BREATHING_DONE_PATH)).toBe(1))
    await waitFor(() =>
      expect(document.querySelector('[data-testid="breathing"]')).toBeNull(),
    )
  })

  it('still returns to the card when the tally POST fails', async () => {
    rejecting.add(BREATHING_DONE_PATH)
    await mountPanel()
    fireEvent.click(control('breathe'))
    await waitFor(() => expect(control('breathing')).not.toBeNull())
    fireEvent.click(control('breathe-done'))
    // The exercise still happened; only the tally missed it.
    await waitFor(() =>
      expect(document.querySelector('[data-testid="breathing"]')).toBeNull(),
    )
  })

  it('does NOT count an exercise abandoned halfway', async () => {
    await mountPanel()
    fireEvent.click(control('breathe'))
    await waitFor(() => expect(control('breathing')).not.toBeNull())
    fireEvent.click(control('breathe-end'))
    await waitFor(() =>
      expect(document.querySelector('[data-testid="breathing"]')).toBeNull(),
    )
    expect(countOf(BREATHING_DONE_PATH)).toBe(0)
    await waitFor(() => expect(api.panelBreathing).toHaveBeenLastCalledWith(false))
  })
})
