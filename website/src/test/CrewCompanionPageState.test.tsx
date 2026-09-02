/**
 * The app page's two states, as the mainline page defined them.
 *
 * The page decides from its own READS, not from a separate presence probe: when the
 * companion's endpoints answer, it shows the live sections; when they do not, it shows
 * one card with a title, a line of copy and a single action to bring the companion back.
 *
 * A previous revision replaced this with a three-state model plus an extra "Companion"
 * card carrying its own Open / Change avatar buttons. That duplicated the card this page
 * already had, so it was reverted — these tests pin the original shape so it does not
 * drift again. The one thing the card must never lose is its button: an earlier version
 * kept the copy and dropped the action, leaving the user told that the companion was
 * away with no way to bring it back.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { i18nT } from '../i18n/t'

/** Per-test switch: does the companion's backend answer, or not? */
const state = vi.hoisted(() => ({ reachable: true, enabled: true }))
/** Every POST path the page made, in order — the open/enable sequence is the assertion. */
const posts = vi.hoisted(() => [] as string[])
/** Paths whose POST should reject, so "the app is switched off" can be simulated. */
const postFailFor = vi.hoisted(() => new Set<string>())
const FAILURE_REASON = 'kaboom-from-the-server'

vi.mock('../apps/crew-companion/api', () => ({
  apiGet: vi.fn(async (path: string) => {
    // Disable 403s every companion route (`_require_enabled`). Reachable is a
    // separate fact: the overlay process may be down while the app is still on.
    if (!state.reachable || !state.enabled) throw new Error('offline')
    if (path.includes('/reminders')) {
      return {
        reminders: [],
        breakNudgesEnabled: true,
        breakReminderMins: 45,
        sessionNotificationsEnabled: true,
      }
    }
    if (path.includes('/stats')) {
      return { stats: { companionSeconds: 0, breathingSessions: 0, remindersFired: 0 } }
    }
    return {}
  }),
  apiPost: vi.fn(async (path: string) => {
    posts.push(path)
    for (const frag of postFailFor) {
      if (path.includes(frag)) throw new Error(FAILURE_REASON)
    }
    // `/window` is wrapped in _require_enabled server-side, so it refuses while the
    // app is off and works once it has been enabled. Model that, rather than letting
    // the test flip the switch itself at a moment of its own choosing.
    if (path.includes('/window') && !state.enabled) throw new Error(FAILURE_REASON)
    if (path.includes('/enable')) state.enabled = true
    if (path.includes('/disable')) state.enabled = false
    return {}
  }),
}))

// Imported after the mock so it binds to the mocked api module.
const { default: CrewCompanionPage } = await import(
  '../apps/crew-companion/CrewCompanionPage'
)

beforeEach(() => {
  state.reachable = true
  state.enabled = true
  posts.length = 0
  postFailFor.clear()
})

describe('CrewCompanionPage: reachable vs not', () => {
  it('shows the live sections when the companion answers', async () => {
    state.reachable = true
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-quit-tip')).not.toBeNull())
    // The away card is not showing.
    expect(container.querySelector('.cc-offline')).toBeNull()
  })

  it('shows the away card when the companion does not answer', async () => {
    state.reachable = false
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    expect(container.querySelector('.cc-quit-tip')).toBeNull()
  })

  it('keeps an action on the away card', async () => {
    state.reachable = false
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    // The regression this pins: copy without a way to act on it.
    const cta = container.querySelector('.cc-offline .cc-cta')
    expect(cta).not.toBeNull()
    expect((cta as HTMLElement).textContent?.trim().length).toBeGreaterThan(0)
  })

  it('does not add a second card duplicating that action', async () => {
    // The reverted revision put Open panel / Change avatar in their own card while the
    // away card already existed. One entry point, not two.
    state.reachable = true
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-quit-tip')).not.toBeNull())
    const buttons = Array.from(container.querySelectorAll('button')).map((b) =>
      (b.textContent ?? '').toLowerCase(),
    )
    expect(buttons.some((t) => t.includes('change avatar'))).toBe(false)
  })
})

describe('the away card\'s button actually opens the companion', () => {
  it('re-sends the open request after switching the app on', async () => {
    // The bug: /window fails because the app is off, /enable succeeds, and that was
    // the end of it — the app came on and NOTHING opened. The user's actual request
    // was dropped, with no error, and only a second click worked.
    state.reachable = false
    state.enabled = false               // the app is switched off, so /window refuses
    posts.length = 0

    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    ;(container.querySelector('.cc-offline .cc-cta') as HTMLElement).click()

    await waitFor(() => {
      const windowCalls = posts.filter((p) => p.includes('/window')).length
      expect(windowCalls).toBeGreaterThanOrEqual(2)
    })
    expect(posts.some((p) => p.includes('/enable'))).toBe(true)
    // and the open must come AFTER the enable, not before it only
    const lastEnable = posts.lastIndexOf(posts.filter((p) => p.includes('/enable')).pop()!)
    const lastWindow = posts.lastIndexOf(posts.filter((p) => p.includes('/window')).pop()!)
    expect(lastWindow).toBeGreaterThan(lastEnable)
  })

  it('reports the real reason when it cannot open, not a line of guidance prose', async () => {
    // It used to show `offline.body` ("Open it to change break nudges…") on failure —
    // guidance copy with no {{error}} slot, so the actual reason was thrown away.
    state.reachable = false
    state.enabled = false
    posts.length = 0
    postFailFor.add('/enable')          // enabling fails too, so there is no way through

    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    ;(container.querySelector('.cc-offline .cc-cta') as HTMLElement).click()

    // The reason has to reach the screen. `offline.body` is this card's standing copy
    // and is on screen either way, so the assertion is about the REASON appearing.
    await waitFor(() => expect(container.textContent ?? '').toContain(FAILURE_REASON))
    postFailFor.clear()
    state.enabled = true
  })
})

describe('the live page can turn the companion off', () => {
  it('posts disable and then shows the away card', async () => {
    state.reachable = true
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-turn-off')).not.toBeNull())
    const turnOff = container.querySelector('.cc-turn-off') as HTMLButtonElement
    expect(turnOff.textContent).toContain(i18nT('apps.crewCompanion.menu.quit'))
    turnOff.click()

    await waitFor(() => expect(posts.some((p) => p.includes('/disable'))).toBe(true))
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    expect(container.querySelector('.cc-turn-off')).toBeNull()
  })

  it('keeps the live page and reports the reason when disable fails', async () => {
    state.reachable = true
    postFailFor.add('/disable')
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-turn-off')).not.toBeNull())
    ;(container.querySelector('.cc-turn-off') as HTMLButtonElement).click()

    await waitFor(() => expect(container.textContent ?? '').toContain(FAILURE_REASON))
    expect(container.querySelector('.cc-offline')).toBeNull()
    expect(container.querySelector('.cc-turn-off')).not.toBeNull()
  })

  it('does not put a turn-off control on the away card', async () => {
    state.reachable = false
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    expect(container.querySelector('.cc-turn-off')).toBeNull()
  })

  it('shows the away card when the companion is already disabled', async () => {
    state.reachable = true
    state.enabled = false
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    expect(container.querySelector('.cc-turn-off')).toBeNull()
  })

  it('returns to the live page after Open re-enables the companion', async () => {
    state.reachable = true
    state.enabled = false
    const { container } = render(<CrewCompanionPage />)
    await waitFor(() => expect(container.querySelector('.cc-offline')).not.toBeNull())
    ;(container.querySelector('.cc-offline .cc-cta') as HTMLElement).click()

    await waitFor(() => expect(container.querySelector('.cc-turn-off')).not.toBeNull())
    expect(container.querySelector('.cc-offline')).toBeNull()
  })
})
