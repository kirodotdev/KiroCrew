/**
 * The session menu dims Rename when the gateway is offline. Dimming ONE item
 * makes a claim about the others: a reader takes full weight to mean "this one
 * works offline". Pin, mark-read, mode-switch and close are all gateway writes
 * that fail after the click, so they must carry the same affordance — while the
 * purely local items (copy link, tags, reveal) must NOT, or the menu reads as
 * wholly dead and the signal stops meaning anything.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

import { ChatHeaderMenu } from '../pages/chat/ChatPageMessageContent'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const slot = { key: 'chat-1', title: 'My Session' } as unknown as ChatSlot

const baseDashboard = {
  status: {}, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as unknown as RootState['dashboard']

function renderMenu(connected: boolean, pinned = false) {
  const store = createTestStore({ dashboard: { ...baseDashboard, connected, slots: [{ ...slot, pinned }] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu activeSlot={slot.key} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return utils
}

const itemFor = (label: RegExp) =>
  screen.getByText(label).closest('[aria-disabled]') as HTMLElement | null

const GATEWAY_ITEMS = [/^Pin$|^Unpin$/, /^Close session$/, /^Reload session$/]

describe('SessionActionsMenu – offline affordance is not rename-only', () => {
  it('every gateway-backed item is marked disabled when offline, not just Rename', () => {
    renderMenu(false)
    for (const label of GATEWAY_ITEMS) {
      const item = itemFor(label)
      expect(item, String(label)).not.toBeNull()
      expect(item?.getAttribute('aria-disabled'), String(label)).toBe('true')
    }
  })

  it('mark-read is NOT dimmed: unread state is Redux plus localStorage, so it works offline', () => {
    renderMenu(false)
    expect(itemFor(/^Close session$/)?.getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByText(/^Mark as (read|unread)$/)).toBeInTheDocument()
    expect(itemFor(/^Mark as (read|unread)$/)).toBeNull()
  })

  it('an offline item announces the label it DISPLAYS, not a fixed one', () => {
    // A pinned slot displays "Unpin"; a fixed label would announce "Pin",
    // which is a name/visible-text mismatch (WCAG 2.5.3).
    renderMenu(false, true)
    const pin = itemFor(/^Unpin$/) as HTMLElement
    expect(pin).not.toBeNull()
    expect(pin.getAttribute('aria-label')).toContain('Unpin')
    expect(pin.getAttribute('aria-label')).not.toContain('Pin disabled')
  })

  it('the offline items say WHY, so dimming is not an unexplained state', () => {
    renderMenu(false)
    expect(itemFor(/^Pin$|^Unpin$/)?.getAttribute('title')).toContain('pin sessions')
    expect(itemFor(/^Close session$/)?.getAttribute('title')).toContain('close sessions')
  })

  it('offline marks the gateway items ONLY — a local item carries no disabled state', () => {
    renderMenu(false)
    // Positive control first: the same query DOES find a marked item, so the
    // null below is mark-read being untouched rather than the query missing.
    expect(itemFor(/^Close session$/)?.getAttribute('aria-disabled')).toBe('true')
    // Mark read/unread is a pure reducer over Redux + localStorage, so it keeps
    // working offline and must NOT be dimmed.
    expect(itemFor(/^Mark (as )?(read|unread)$/)).toBeNull()
  })

  it('Tags keeps full weight offline: the popover is a local read, its writes gate inside', () => {
    renderMenu(false)
    // Positive control: a genuinely gated sibling IS found marked by this query,
    // so a null for Tags is the opener being ungated, not a missed lookup.
    expect(itemFor(/^Close session$/)?.getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByText(/^Tags…$/)).toBeInTheDocument()
    expect(itemFor(/^Tags…$/)).toBeNull()
  })

  it('the live rows are visually distinct from the dimmed ones, not merely un-disabled', () => {
    renderMenu(false)
    const rowFor = (label: RegExp) =>
      screen.getByText(label).closest('[role="menuitem"]') as HTMLElement | null
    // Positive control: a row built by `offlineItem` IS found carrying the dim,
    // so a live row lacking it is a real difference, not an inert selector.
    expect(rowFor(/^Reload session$/)?.className).toContain('opacity-40')
    expect(rowFor(/^Reload session$/)?.className).toContain('text-muted')
    for (const live of [/^Mark as unread$/, /^Tags…$/]) {
      expect(rowFor(live)?.className, String(live)).not.toContain('opacity-40')
      expect(rowFor(live)?.className, String(live)).not.toContain('text-muted')
    }
  })

  it('when connected the same items are NOT marked disabled', () => {
    renderMenu(true)
    for (const label of GATEWAY_ITEMS) {
      expect(itemFor(label)?.getAttribute('aria-disabled'), String(label)).toBe('false')
    }
  })

  it('the open menu carries a standing reason, so a refused click is not a dead click', () => {
    renderMenu(false)
    const reason = screen.getByTestId('menu-offline-reason')
    expect(reason).toHaveAttribute('role', 'status')
    expect(reason.textContent).toMatch(/offline/i)
  })

  it('a natively disabled row still gets a reason, since it can fire no event at all', () => {
    // These rows carry pointer-events-none, so no click-triggered explanation
    // could ever reach them; the standing reason is why they clear.
    renderMenu(false)
    expect(screen.getByTestId('menu-offline-reason')).toBeInTheDocument()
    expect(itemFor(/^Export to a file$/)?.getAttribute('aria-disabled')).toBe('true')
  })

  it('the reason row sits LAST, so a mid-aim disconnect cannot shift items under the pointer', () => {
    renderMenu(false)
    const reason = screen.getByTestId('menu-offline-reason')
    const closeItem = itemFor(/^Close session$/) as HTMLElement
    expect(closeItem).not.toBeNull()
    // Node order decides which way a late-mounting row pushes: appended after the
    // last item, nothing above it moves when the gateway drops mid-aim.
    expect(closeItem.compareDocumentPosition(reason) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('connected leaves no reason row behind', () => {
    renderMenu(true)
    expect(screen.queryByTestId('menu-offline-reason')).not.toBeInTheDocument()
  })
})
