/**
 * Dev Fleet live-state outage notice: the hand-off sits UNDER the copy (#10831).
 *
 * `ErrorNotice`'s block variant defaults the `askAgent` link to the banner's
 * right-hand column, on the copy's first line. This notice's copy names the
 * hand-off ("Ask the agent before making a checkout live"), so with the link
 * floating into that same line the sentence read around it at some widths —
 * "use the ✨Ask the agent Ask-the-agent link…" (PR #10330, UX span 9dae00dc8e4b).
 *
 * The notice opts into `actionPlacement="below"`: the link is a block of its
 * own inside the text column, after the copy, so no width can interleave the
 * two. jsdom lays nothing out, so the pin is structural — containment in the
 * text column and document order — which is exactly what the default layout
 * violates (there the link is a flex SIBLING of the text column).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from '../pages/DevFleetPage'

const FLEET_UNKNOWN = {
  live_state_known: false,
  worktrees: [
    { name: 'main', is_main: true, is_live: false, is_staged: false, running: false, has_dist: true, behind: 0, last_updated_at: Date.now() / 1000 },
    { name: 'feature-x', is_main: false, is_live: false, is_staged: false, running: true, has_dist: true, port: 7780, health: 200, behind: 3, last_updated_at: Date.now() / 1000 - 3600 },
  ],
}

beforeEach(() => {
  __resetDevFleetNoticesForTests()
  vi.restoreAllMocks()
  vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_UNKNOWN), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
})

describe('DevFleetPage live-state-unknown notice layout', () => {
  it('renders the Ask-the-agent hand-off on its own row under the copy, inside the text column', async () => {
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const notice = await waitFor(() => screen.getByTestId('fleet-live-state-unknown'))
    const handoff = screen.getByRole('button', { name: /ask the agent/i })
    expect(notice.contains(handoff)).toBe(true)

    // The text column is the notice's `flex-1` child; the copy lives in it. With
    // the default `beside` placement the hand-off is that column's flex SIBLING
    // (the right-hand column) and shares the copy's first line. Opted below, it
    // is INSIDE the column, as a block after the copy.
    const textColumn = notice.querySelector('.flex-1')
    expect(textColumn).not.toBeNull()
    expect(textColumn!.contains(handoff)).toBe(true)

    // Its own row: the hand-off's wrapper is a block element (a div, not an
    // inline run), and it follows every text node of the copy in document order.
    const wrapper = handoff.parentElement
    expect(wrapper).not.toBeNull()
    expect(wrapper!.tagName).toBe('DIV')
    expect(wrapper!.parentElement).toBe(textColumn)
    const copyNodes = [...textColumn!.childNodes].filter(n => n !== wrapper && (n.textContent ?? '').trim() !== '')
    expect(copyNodes.length).toBeGreaterThan(0)
    for (const node of copyNodes) {
      expect(node.compareDocumentPosition(wrapper!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })

  it('the copy ends by naming the hand-off, which then follows as the link', async () => {
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    const notice = await waitFor(() => screen.getByTestId('fleet-live-state-unknown'))
    const handoff = screen.getByRole('button', { name: /ask the agent/i })
    expect(notice.contains(handoff)).toBe(true)
    const wrapper = handoff.parentElement!
    // The copy is every node of the text column EXCEPT the hand-off's wrapper.
    // (Not a string replace of the button's label: the copy itself says "Ask
    // the agent", and a first-occurrence replace would eat that sentence.)
    const copy = [...wrapper.parentElement!.childNodes]
      .filter(n => n !== wrapper)
      .map(n => n.textContent ?? '')
      .join('')
      .trim()
    // Catalog copy, not a literal in the page: the English catalog is what the
    // test renders, so the shortened string (#10831) is pinned here once.
    expect(copy).toMatch(/this is unknown, not empty\. Ask the agent before making a checkout live\.$/)
    // "live" is glossed once, so a first-time reader knows which checkout that is.
    expect(copy).toMatch(/the checkout the gateway itself runs from/i)
    // The copy no longer points at "the link in this notice" — the link's
    // position does that.
    expect(copy).not.toMatch(/link in this notice/i)
    // And the link is what follows the copy.
    expect(wrapper.parentElement!.lastElementChild).toBe(wrapper)
  })
})
