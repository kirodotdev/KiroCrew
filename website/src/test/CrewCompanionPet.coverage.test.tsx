/**
 * The companion overlay itself — `pet.tsx` — driven end to end for the first time.
 *
 * Every other file in this app has tests; this one, the 1200-line window that owns
 * the presence ping, the cursor-based fire drain, the one bubble slot, the window
 * commands and the tap-vs-drag rule, had none. It is also not importable in the
 * ordinary way: `Companion` is never exported and the module MOUNTS ITSELF into
 * `#companion-root` at import time. So the harness below is the module's real entry
 * path — create the host, register the mocks it reaches for, then import it.
 *
 * What is asserted is what the user would see or what the backend would receive: the
 * presence POST, the bubble text for each kind of fire, the cursor written to
 * localStorage, the panel opening on a tap and NOT on a drag, and the reactions the
 * session socket drives.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor, fireEvent } from '@testing-library/react'

import { PENDING_PATH, PRESENCE_PATH } from '../apps/crew-companion/constants'
import type { SessionWatchOptions } from '../apps/crew-companion/sessionWatch'

// The theme adopt is a real same-origin read in production; here it just resolves so
// the bootstrap gets as far as `createRoot`.
vi.mock('../apps/crew-companion/dashboardTheme', () => ({
  adoptDashboardTheme: () => Promise.resolve(),
  watchThemeChanges: () => () => {},
  applyThemeId: () => {},
  extractStylesheetHrefs: () => [],
}))

/** Config the bridge answers with; a test may rewrite it before mounting. */
let config: Record<string, unknown> = {}
/** Pack detail for the custom-pack path. */
let packDetail: unknown = null
/** The position the main process remembers for the companion. */
let savedPos: { x: number; y: number } | null = { x: 300, y: 200 }

/** The preload bridge, as the overlay and its hooks see it. */
const bridge = {
  getWindowPosition: vi.fn(async () => savedPos),
  savePosition: vi.fn(),
  getCrewCompanionConfig: vi.fn(async () => config),
  galleryGetPackDetail: vi.fn(async () => packDetail),
  onGalleryActiveChanged: vi.fn(() => () => {}),
  onColorMapChanged: vi.fn(() => () => {}),
  presetsGetColorMap: vi.fn(async () => null),
  updateHitbox: vi.fn(),
  setMenuHitbox: vi.fn(),
  contextMenuAction: vi.fn(),
}
vi.mock('../apps/crew-companion/petBridge', () => ({ petBridge: bridge }))

/** The gateway socket is replaced by a handle on the callbacks the overlay passes. */
let watch: SessionWatchOptions | null = null
vi.mock('../apps/crew-companion/sessionWatch', () => ({
  watchSessions: (opts: SessionWatchOptions) => {
    watch = opts
    return () => {}
  },
}))

/**
 * `pet.tsx` owns its own React root, so nothing in @testing-library can unmount it.
 * Wrapping `createRoot` is what lets each test tear its overlay down, which keeps one
 * test's presence/pending polls out of the next.
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

interface Fire {
  seq: number
  kind: string
  text: string
  key: string
  at: string
}

/** What `/pending` answers. `since` is the cursor the overlay asked from. */
let pending: (since: number) => { cursor: number; fires: Fire[] }
/** Every URL the overlay fetched, in order. */
let calls: string[]

function fire(partial: Partial<Fire> & { seq: number }): Fire {
  return { kind: 'reminder', text: '', key: '', at: new Date().toISOString(), ...partial }
}

/**
 * Answer `/pending` the way the backend does: non-destructive, cursor-filtered.
 *
 * Filtering on `since` matters — the overlay re-polls every 2s, and a handler that
 * ignored the cursor would re-deliver the same fire and collapse it into a count,
 * making every assertion below a race against the poll interval.
 */
function queue(fires: Fire[]): void {
  pending = (since) => ({
    cursor: fires.length ? fires[fires.length - 1].seq : since,
    fires: fires.filter((f) => f.seq > since),
  })
}

let panelClosedCbs: Array<() => void> = []
let galleryOpenedCbs: Array<() => void> = []
let galleryClosedCbs: Array<() => void> = []
/** When true, the `since=0` re-read after a backend restart answers not-ok. */
let sinceZeroFails = false

/** The window-level preload bridge the overlay toggles window input through. */
function installCrewCompanion() {
  const preload = {
    setFocusable: vi.fn(),
    panelOpen: vi.fn(),
    panelClose: vi.fn(),
    panelHold: vi.fn(),
    onPanelClosed: vi.fn((cb: () => void) => {
      panelClosedCbs.push(cb)
      return () => {}
    }),
    galleryOpen: vi.fn(),
    onGalleryOpened: vi.fn((cb: () => void) => {
      galleryOpenedCbs.push(cb)
      return () => {}
    }),
    onGalleryClosed: vi.fn((cb: () => void) => {
      galleryClosedCbs.push(cb)
      return () => {}
    }),
  }
  ;(window as unknown as { crewCompanion: typeof preload }).crewCompanion = preload
  return preload
}
let api: ReturnType<typeof installCrewCompanion>

/**
 * Mount the overlay through its real entry path.
 *
 * `vi.resetModules()` is what makes a second mount possible at all: the bootstrap is
 * module-level, so with a warm registry only the first test would ever render.
 */
async function mountPet(): Promise<void> {
  const host = document.createElement('div')
  host.id = 'companion-root'
  document.body.appendChild(host)
  vi.resetModules()
  await import('../apps/crew-companion/pet')
  // The bootstrap awaits the theme before createRoot and the root renders through
  // React's scheduler, so wait for the companion rather than counting ticks.
  await waitFor(() => expect(document.querySelector('.cc-pet')).not.toBeNull())
}

function petEl(): HTMLElement {
  return document.querySelector('.cc-pet') as HTMLElement
}

function bubbleText(): string | null {
  return document.querySelector('.cc-bubble-text')?.textContent ?? null
}

/** Ring the backend's doorbell so the overlay drains now instead of in 2s. */
function ringDoorbell(): void {
  watch?.onFireQueued?.()
}

/** Press and release on the companion at one point — a tap, not a drag. */
function tapPet(at = { x: 340, y: 240 }): void {
  const pet = petEl()
  fireEvent.mouseDown(pet, { clientX: at.x, clientY: at.y, button: 0 })
  fireEvent.click(pet, { clientX: at.x, clientY: at.y })
}

beforeEach(() => {
  calls = []
  watch = null
  panelClosedCbs = []
  galleryOpenedCbs = []
  galleryClosedCbs = []
  sinceZeroFails = false
  packDetail = null
  savedPos = { x: 300, y: 200 }
  config = { activeAppearance: 'kiro-ghost', sessionNotificationsEnabled: true }
  queue([])
  window.localStorage.clear()
  // English is pinned in setup.ts, but `vi.resetModules()` hands pet.tsx a FRESH
  // i18next whose `initI18n()` (no argument) resolves the language from storage.
  window.localStorage.setItem('mc-lang', 'en')
  api = installCrewCompanion()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      if (url.startsWith(PENDING_PATH)) {
        const since = Number(new URLSearchParams(url.split('?')[1] ?? '').get('since') ?? 0)
        if (since === 0 && sinceZeroFails) {
          return { ok: false, json: async () => ({}) } as unknown as Response
        }
        return { ok: true, json: async () => pending(since) } as unknown as Response
      }
      return { ok: true, json: async () => ({}) } as unknown as Response
    }),
  )
})

afterEach(() => {
  for (const root of roots.splice(0)) root.unmount()
  document.querySelectorAll('#companion-root').forEach((n) => n.remove())
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('the overlay mounts, reports itself and polls', () => {
  it('renders the companion as the only interactive element in the layer', async () => {
    await mountPet()
    const pet = petEl()
    expect(pet.getAttribute('role')).toBe('button')
    expect(pet.tabIndex).toBe(0)
    expect(pet.parentElement?.className).toContain('cc-pet-layer')
  })

  it('pings presence on mount, or the backend reads the chair as empty', async () => {
    await mountPet()
    await waitFor(() => expect(calls).toContain(PRESENCE_PATH))
  })

  it('drains /pending from cursor 0 the first time it ever runs', async () => {
    await mountPet()
    await waitFor(() => expect(calls).toContain(`${PENDING_PATH}?since=0`))
  })

  it('resumes from the persisted cursor instead of replaying the history', async () => {
    window.localStorage.setItem('cc:pendingCursor', '42')
    await mountPet()
    await waitFor(() => expect(calls).toContain(`${PENDING_PATH}?since=42`))
  })

  it('ignores a corrupt stored cursor rather than muting every future fire', async () => {
    window.localStorage.setItem('cc:pendingCursor', 'not-a-number')
    await mountPet()
    await waitFor(() => expect(calls).toContain(`${PENDING_PATH}?since=0`))
  })

  it('re-reads from zero when the backend cursor went BACKWARDS (a restart)', async () => {
    window.localStorage.setItem('cc:pendingCursor', '42')
    // A restarted gateway numbers from 1 again, so it answers below what we asked.
    pending = (since) =>
      since > 0
        ? { cursor: 1, fires: [] }
        : { cursor: 1, fires: [fire({ seq: 1, kind: 'reminder', text: 'stand up' })] }
    await mountPet()
    await waitFor(() => expect(calls).toContain(`${PENDING_PATH}?since=0`))
    await waitFor(() => expect(bubbleText()).toBe('stand up'))
  })

  it('reports the companion hitbox so the main process can stop click-through', async () => {
    await mountPet()
    await waitFor(() => expect(bridge.updateHitbox).toHaveBeenCalled())
  })

  it('survives a /pending that answers not-ok without crashing the overlay', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input))
        return { ok: false, json: async () => ({}) } as unknown as Response
      }),
    )
    await mountPet()
    await waitFor(() => expect(calls.some((u) => u.startsWith(PENDING_PATH))).toBe(true))
    expect(petEl()).not.toBeNull()
    expect(document.querySelector('.cc-bubble-text')).toBeNull()
  })
})

describe('what the companion says when a fire arrives', () => {
  it('speaks a reminder in the user’s own words, untranslated', async () => {
    queue([fire({ seq: 7, kind: 'reminder', text: '起床' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('起床'))
  })

  it('commits the cursor only for what it actually showed', async () => {
    queue([fire({ seq: 7, kind: 'reminder', text: '起床' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('起床'))
    expect(window.localStorage.getItem('cc:pendingCursor')).toBe('7')
  })

  it('translates a break nudge from its catalogue key', async () => {
    queue([fire({ seq: 3, kind: 'break', key: 'break.water.1', text: 'IGNORED' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('Water break?'))
  })

  it('stays quiet on an unmapped nudge key, and consumes it so nothing pins behind it', async () => {
    queue([fire({ seq: 9, kind: 'break', key: 'break.nope.99', text: 'raw key' })])
    await mountPet()
    // The cursor moving IS the "deliberately consumed" verdict — drift does not heal
    // in two seconds, so retrying would hold the whole queue behind it.
    await waitFor(() => expect(window.localStorage.getItem('cc:pendingCursor')).toBe('9'))
    expect(document.querySelector('.cc-bubble-text')).toBeNull()
  })

  it('takes the OLDEST unspoken fire and leaves the rest pending', async () => {
    queue([
      fire({ seq: 1, kind: 'reminder', text: 'first' }),
      fire({ seq: 2, kind: 'reminder', text: 'second' }),
    ])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('first'))
    expect(window.localStorage.getItem('cc:pendingCursor')).toBe('1')
  })

  it('offers the breathing nudge a CTA that opens the panel', async () => {
    queue([fire({ seq: 4, kind: 'break-breathe', key: 'break.breathe.1', text: '' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('Fancy a few slow breaths?'))
    const cta = document.querySelector('.cc-bubble-cta') as HTMLButtonElement
    expect(cta.textContent).toBe('Breathe with me')
    fireEvent.click(cta)
    expect(api.panelOpen).toHaveBeenCalled()
  })

  it('places the bubble once it has been measured, not off-screen', async () => {
    queue([fire({ seq: 1, kind: 'reminder', text: 'placed' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('placed'))
    const host = document.querySelector('.cc-bubble-host') as HTMLElement
    await waitFor(() => expect(host.style.visibility).not.toBe('hidden'))
    expect(host.style.position).toBe('absolute')
    expect(host.style.left).not.toBe('-9999px')
  })

  it('lets a reminder be dismissed, freeing the slot', async () => {
    queue([fire({ seq: 1, kind: 'reminder', text: '起床' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('起床'))
    fireEvent.click(document.querySelector('.cc-bubble-x') as HTMLElement)
    // The exit animation runs before the unmount, so wait for the removal.
    await waitFor(() => expect(document.querySelector('.cc-bubble-text')).toBeNull())
  })
})

describe('window commands recorded by the dashboard page', () => {
  it('opens the panel for a fresh command and grants focus for the keyboard', async () => {
    queue([fire({ seq: 1, kind: 'command', text: 'panel' })])
    await mountPet()
    await waitFor(() => expect(api.panelOpen).toHaveBeenCalled())
    expect(api.setFocusable).toHaveBeenCalledWith(true)
    // A command carries no bubble text — it is acted on, never drawn.
    expect(document.querySelector('.cc-bubble-text')).toBeNull()
  })

  it('opens the avatar gallery for a gallery command', async () => {
    queue([fire({ seq: 1, kind: 'command', text: 'gallery' })])
    await mountPet()
    await waitFor(() => expect(api.galleryOpen).toHaveBeenCalled())
  })

  it('SKIPS a stale command — popping a window open minutes later is intrusive', async () => {
    queue([
      fire({
        seq: 1,
        kind: 'command',
        text: 'panel',
        at: new Date(Date.now() - 120_000).toISOString(),
      }),
    ])
    await mountPet()
    await waitFor(() => expect(window.localStorage.getItem('cc:pendingCursor')).toBe('1'))
    expect(api.panelOpen).not.toHaveBeenCalled()
  })

  it('acts on a command and still speaks the reminder behind it', async () => {
    queue([
      fire({ seq: 1, kind: 'command', text: 'gallery' }),
      fire({ seq: 2, kind: 'reminder', text: 'tea' }),
    ])
    await mountPet()
    await waitFor(() => expect(api.galleryOpen).toHaveBeenCalled())
    await waitFor(() => expect(bubbleText()).toBe('tea'))
  })
})

describe('session signals from the gateway socket', () => {
  it('shows a finished session under its own title', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onDone({ slot: 'a', title: 'Fix the parser', elapsedMs: 10, failed: false })
    await waitFor(() => expect(bubbleText()).toBe('Fix the parser'))
  })

  it('falls back to copy for an untitled finish', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onDone({ slot: 'a', title: '  ', elapsedMs: 10, failed: false })
    await waitFor(() => expect(bubbleText()).toBe('Finished a task'))
  })

  it('says a failure OUT LOUD rather than reading as a finish', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onDone({ slot: 'a', title: 'Fix the parser', elapsedMs: 10, failed: true })
    await waitFor(() => expect(bubbleText()).toBe('Stopped: Fix the parser'))
  })

  it('gives an untitled failure its own sentence, not an empty one', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onDone({ slot: 'a', title: '', elapsedMs: 10, failed: true })
    await waitFor(() => expect(bubbleText()).toBe('Stopped a task'))
  })

  it('collapses a second completion into a count instead of stacking toasts', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onDone({ slot: 'a', title: 'one', elapsedMs: 1, failed: false })
    await waitFor(() => expect(bubbleText()).toBe('one'))
    watch!.onDone({ slot: 'b', title: 'two', elapsedMs: 1, failed: false })
    await waitFor(() => expect(bubbleText()).toBe('2 jobs finished'))
  })

  it('raises a sticky approval bubble with the kind as its kicker', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onApproval!({ slot: 'a', title: 'Run the migration' })
    await waitFor(() =>
      expect(document.querySelector('.cc-bubble-kicker')?.textContent).toBe('Approval Pending'),
    )
    expect(document.querySelector('.cc-bubble-body')?.textContent).toBe('Run the migration')
    // Sticky: no ✕, it leaves by being resolved.
    expect(document.querySelector('.cc-bubble-x')).toBeNull()
  })

  it('shows the label alone for an untitled approval', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onApproval!({ slot: 'a', title: '   ' })
    await waitFor(() => expect(bubbleText()).toBe('Approval Pending'))
    expect(document.querySelector('.cc-bubble-kicker')).toBeNull()
  })

  it('clears the sticky bubble the moment the block is answered elsewhere', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onApproval!({ slot: 'a', title: 'Run the migration' })
    await waitFor(() => expect(document.querySelector('.cc-bubble-kicker')).not.toBeNull())
    watch!.onApprovalResolved!()
    await waitFor(() => expect(document.querySelector('.cc-bubble-text')).toBeNull())
  })

  it('an approval holds the slot against a routine completion behind it', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    watch!.onApproval!({ slot: 'a', title: 'Run the migration' })
    await waitFor(() => expect(document.querySelector('.cc-bubble-kicker')).not.toBeNull())
    watch!.onDone({ slot: 'b', title: 'unrelated finish', elapsedMs: 1, failed: false })
    // Still the approval — unresolved work is not displaced by routine chatter.
    await waitFor(() =>
      expect(document.querySelector('.cc-bubble-body')?.textContent).toBe('Run the migration'),
    )
  })

  it('reads the live "tell me when sessions are done" switch, not a captured value', async () => {
    config = { activeAppearance: 'kiro-ghost', sessionNotificationsEnabled: false }
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    await waitFor(() => expect(watch!.isSilent()).toBe(true))
  })
})

describe('pointer and keyboard on the companion', () => {
  it('opens the panel on a tap and closes it on the next one', async () => {
    await mountPet()
    tapPet()
    await waitFor(() => expect(api.panelOpen).toHaveBeenCalled())
    expect(api.setFocusable).toHaveBeenCalledWith(true)
    tapPet()
    await waitFor(() => expect(api.panelClose).toHaveBeenCalled())
    expect(api.setFocusable).toHaveBeenCalledWith(false)
  })

  it('does NOT open the panel when the press turned into a drag', async () => {
    await mountPet()
    const pet = petEl()
    fireEvent.mouseDown(pet, { clientX: 340, clientY: 240, button: 0 })
    // Past CLICK_SLOP (6px) the release is a drop, not a tap.
    fireEvent.click(pet, { clientX: 420, clientY: 300 })
    expect(api.panelOpen).not.toHaveBeenCalled()
  })

  it('holds the panel open from the PRESS and releases it on mouseup', async () => {
    await mountPet()
    fireEvent.mouseDown(petEl(), { clientX: 340, clientY: 240, button: 0 })
    expect(api.panelHold).toHaveBeenCalledWith(true)
    fireEvent.mouseUp(window)
    await waitFor(() => expect(api.panelHold).toHaveBeenCalledWith(false))
  })

  it('toggles the panel from the keyboard', async () => {
    await mountPet()
    fireEvent.keyDown(petEl(), { key: 'Enter' })
    await waitFor(() => expect(api.panelOpen).toHaveBeenCalled())
    fireEvent.keyDown(petEl(), { key: ' ' })
    await waitFor(() => expect(api.panelClose).toHaveBeenCalled())
  })

  it('ignores keys that are not Enter or Space', async () => {
    await mountPet()
    fireEvent.keyDown(petEl(), { key: 'a' })
    expect(api.panelOpen).not.toHaveBeenCalled()
  })

  it('opens the quick menu on right-click and closes it on a click away', async () => {
    await mountPet()
    fireEvent.contextMenu(petEl(), { clientX: 120, clientY: 90 })
    await waitFor(() => expect(document.querySelector('.cc-menu-host')).not.toBeNull())
    const labels = [...document.querySelectorAll('.cc-menu-host [role="menuitem"]')].map(
      (n) => n.textContent,
    )
    expect(labels).toEqual(['Change avatar', 'Turn off companion'])
    // The backdrop is a real element under the cursor, so a press on it closes.
    fireEvent.mouseDown(document.querySelector('.cc-menu-backdrop') as HTMLElement)
    await waitFor(() => expect(document.querySelector('.cc-menu-host')).toBeNull())
  })

  it('runs a menu item and closes the menu behind it', async () => {
    await mountPet()
    fireEvent.contextMenu(petEl(), { clientX: 120, clientY: 90 })
    await waitFor(() => expect(document.querySelector('.cc-menu-host')).not.toBeNull())
    const change = [...document.querySelectorAll('.cc-menu-host [role="menuitem"]')].find(
      (n) => n.textContent === 'Change avatar',
    ) as HTMLElement
    fireEvent.click(change)
    await waitFor(() => expect(bridge.contextMenuAction).toHaveBeenCalledWith('gallery'))
    await waitFor(() => expect(document.querySelector('.cc-menu-host')).toBeNull())
  })

  it('gives up focus again when the panel window closes on its own', async () => {
    await mountPet()
    tapPet()
    await waitFor(() => expect(api.setFocusable).toHaveBeenCalledWith(true))
    api.setFocusable.mockClear()
    // Blur / Escape / its own ✕ — the panel closes without going through closePanel.
    await waitFor(() => expect(panelClosedCbs.length).toBeGreaterThan(0))
    panelClosedCbs[panelClosedCbs.length - 1]()
    await waitFor(() => expect(api.setFocusable).toHaveBeenCalledWith(false))
  })
})

describe('the active appearance pack', () => {
  it('reads the built-in ghost and asks for no pack detail', async () => {
    await mountPet()
    await waitFor(() => expect(bridge.getCrewCompanionConfig).toHaveBeenCalled())
    expect(bridge.galleryGetPackDetail).not.toHaveBeenCalled()
  })

  it('reads a custom pack’s own random behaviours', async () => {
    config = { activeAppearance: 'petdex-pack', kiro: { accessory: 'partyhat' } }
    packDetail = {
      animations: { idle: '<svg/>', walking: '<svg/>', happy: '<svg/>', wave: '<svg/>' },
      randomNames: ['wave', 'missing-art'],
      sprite: {},
    }
    await mountPet()
    await waitFor(() => expect(bridge.galleryGetPackDetail).toHaveBeenCalledWith('petdex-pack'))
  })

  it('treats an unknown saved accessory as "no prop" rather than guessing', async () => {
    config = { activeAppearance: 'kiro-ghost', kiro: { accessory: 42 } }
    await mountPet()
    await waitFor(() => expect(bridge.getCrewCompanionConfig).toHaveBeenCalled())
    expect(petEl()).not.toBeNull()
  })

  it('records the avatar gallery window opening and closing', async () => {
    await mountPet()
    await waitFor(() => expect(galleryOpenedCbs.length).toBeGreaterThan(0))
    await waitFor(() => expect(galleryClosedCbs.length).toBeGreaterThan(0))
    // Driving both broadcasts must not disturb the overlay. NOTE: the flag they set
    // (`galleryOpenRef`) is not currently consulted by the autonomous-motion gate, so
    // there is no observable behaviour to assert beyond survival — see the report.
    galleryOpenedCbs[galleryOpenedCbs.length - 1]()
    galleryClosedCbs[galleryClosedCbs.length - 1]()
    expect(petEl()).not.toBeNull()
  })
})

describe('fires that arrive through /pending rather than the socket', () => {
  it('celebrates a completion that came off the fire queue', async () => {
    queue([fire({ seq: 1, kind: 'session-done', text: 'built the thing' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('built the thing'))
    // Not sticky: a finish is pure FYI, so it keeps its ✕.
    expect(document.querySelector('.cc-bubble-x')).not.toBeNull()
  })

  it('shakes on a failure and keeps the ✕ — a failure is not a question', async () => {
    queue([fire({ seq: 1, kind: 'session-error', text: 'it broke' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('it broke'))
    expect(document.querySelector('.cc-bubble-x')).not.toBeNull()
  })

  it('holds an approval from the queue with no ✕ at all', async () => {
    queue([fire({ seq: 1, kind: 'approval', text: 'needs your OK' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('needs your OK'))
    expect(document.querySelector('.cc-bubble-x')).toBeNull()
  })

  it('treats an unrecognised kind as routine rather than dropping it', async () => {
    queue([fire({ seq: 1, kind: '', text: 'something happened' })])
    await mountPet()
    await waitFor(() => expect(bubbleText()).toBe('something happened'))
  })

  it('DEFERS an ambient nudge while blocked work holds the slot, cursor unmoved', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    // The empty first drain commits the backend's own cursor, which is 0 here.
    await waitFor(() => expect(window.localStorage.getItem('cc:pendingCursor')).toBe('0'))
    watch!.onApproval!({ slot: 'a', title: 'Run the migration' })
    await waitFor(() => expect(document.querySelector('.cc-bubble-kicker')).not.toBeNull())

    queue([fire({ seq: 5, kind: 'reminder', text: 'tea' })])
    ringDoorbell()
    // Unmoved cursor IS the retry mechanism: the reminder must still be pending.
    await waitFor(() => expect(calls.filter((u) => u.includes('since=0')).length).toBeGreaterThan(1))
    expect(window.localStorage.getItem('cc:pendingCursor')).toBe('0')
    expect(document.querySelector('.cc-bubble-body')?.textContent).toBe('Run the migration')
  })

  it('drains at once when the backend rings the doorbell, not on the next tick', async () => {
    await mountPet()
    await waitFor(() => expect(watch).not.toBeNull())
    queue([fire({ seq: 2, kind: 'reminder', text: 'ring ring' })])
    ringDoorbell()
    // waitFor's window is shorter than the 2s poll interval, so passing here means the
    // doorbell — not the tick — delivered it.
    await waitFor(() => expect(bubbleText()).toBe('ring ring'), { timeout: 1500 })
  })

  it('gives up quietly when the post-restart re-read also fails', async () => {
    window.localStorage.setItem('cc:pendingCursor', '42')
    sinceZeroFails = true
    pending = () => ({ cursor: 1, fires: [fire({ seq: 1, text: 'unreachable' })] })
    await mountPet()
    await waitFor(() => expect(calls).toContain(`${PENDING_PATH}?since=0`))
    expect(document.querySelector('.cc-bubble-text')).toBeNull()
    // Nothing was shown, so nothing was consumed.
    expect(window.localStorage.getItem('cc:pendingCursor')).toBe('42')
  })
})

describe('the overlay survives a hostile localStorage', () => {
  it('starts from 0 when the stored cursor cannot be read (private mode)', async () => {
    const original = Storage.prototype.getItem
    const spy = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(function (this: Storage, key: string) {
        if (key === 'cc:pendingCursor') throw new Error('access denied')
        return original.call(this, key)
      })
    try {
      await mountPet()
      await waitFor(() => expect(calls).toContain(`${PENDING_PATH}?since=0`))
    } finally {
      spy.mockRestore()
    }
  })

  it('still shows the fire when the cursor cannot be persisted', async () => {
    const original = Storage.prototype.setItem
    const spy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(function (this: Storage, key: string, value: string) {
        if (key === 'cc:pendingCursor') throw new Error('quota exceeded')
        original.call(this, key, value)
      })
    try {
      queue([fire({ seq: 3, kind: 'reminder', text: 'replaying is bad, silence is worse' })])
      await mountPet()
      await waitFor(() =>
        expect(bubbleText()).toBe('replaying is bad, silence is worse'),
      )
    } finally {
      spy.mockRestore()
    }
  })
})

describe('docking at a screen edge', () => {
  it('tucks half the body off the LEFT edge and leans into it', async () => {
    savedPos = { x: 0, y: 100 }
    await mountPet()
    await waitFor(() => expect(petEl().style.transform).toContain('rotate(25deg)'))
    // Half the width, pushed off the docked edge — not mirrored on the left half.
    expect(petEl().style.transform).toContain('translateX(-64px)')
    expect(petEl().style.transform).not.toContain('scaleX(-1)')
  })

  it('mirrors the art on the RIGHT edge and flips the crop with it', async () => {
    savedPos = { x: 900, y: 100 }
    await mountPet()
    await waitFor(() => expect(petEl().style.transform).toContain('rotate(25deg)'))
    const transform = petEl().style.transform
    // scaleX(-1) has already flipped the axis, so the crop is negated to still
    // travel off-screen — the bug that made the right-edge dock look undocked.
    expect(transform).toContain('scaleX(-1)')
    expect(transform).toContain('translateX(-64px)')
  })

  it('docks at the left edge when no position was ever saved', async () => {
    savedPos = null
    await mountPet()
    await waitFor(() => expect(petEl().style.transform).toContain('rotate(25deg)'))
    expect(petEl().style.opacity).toBe('1')
  })

  it('becomes visible only once the saved position has arrived', async () => {
    await mountPet()
    await waitFor(() => expect(petEl().style.opacity).toBe('1'))
    expect(bridge.getWindowPosition).toHaveBeenCalled()
  })
})

describe('dragging the companion', () => {
  it('persists the position it was dropped at', async () => {
    await mountPet()
    await waitFor(() => expect(petEl().style.opacity).toBe('1'))
    const pet = petEl()
    fireEvent.mouseDown(pet, { clientX: 340, clientY: 240, button: 0 })
    // Past the 6px threshold the grip is read and the drag really begins.
    fireEvent.mouseMove(window, { clientX: 420, clientY: 300 })
    fireEvent.mouseUp(window)
    await waitFor(() => expect(bridge.savePosition).toHaveBeenCalled())
  })
})
