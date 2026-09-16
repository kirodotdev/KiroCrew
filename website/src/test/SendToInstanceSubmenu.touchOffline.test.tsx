/**
 * The submenu trigger's offline gate on a TOUCH device.
 *
 * On touch, `DropdownMenuSubTrigger` swaps Radix's own SubTrigger — which
 * implements `disabled` — for a plain div, so the `disabled` a caller passes was
 * inert: a gated submenu still opened under a tap and still reached its write.
 * These cases drive that branch specifically; the desktop branch was covered.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))

const sendSessionToInstance = vi.fn().mockResolvedValue({})
vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
    listInstances: vi.fn().mockResolvedValue({ instances: [{ id: 'i1', name: 'Crew A', url: 'https://a.invalid' }] }),
    sendSessionToInstance: (...a: unknown[]) => sendSessionToInstance(...a),
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

async function renderMenu(connected: boolean) {
  const store = createTestStore({ dashboard: { ...baseDashboard, connected, slots: [slot] } })
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
  // The submenu self-hides until its instance list resolves.
  await screen.findByText(/Send a copy to/)
  return utils
}

const trigger = () => screen.getByText(/Send a copy to/).closest('[role="button"]') as HTMLElement

describe('SendToInstanceSubmenu – the offline gate holds on a touch device', () => {
  beforeEach(() => sendSessionToInstance.mockClear())

  it('a tap on the offline trigger does not open the submenu', async () => {
    await renderMenu(false)
    fireEvent.click(trigger())
    expect(screen.queryByText(/Crew A/)).toBeNull()
  })

  it('connected, the same tap opens it — the control for the case above', async () => {
    await renderMenu(true)
    fireEvent.click(trigger())
    expect(await screen.findByText(/Crew A/)).toBeInTheDocument()
  })

  it('Enter on the offline trigger reaches no write', async () => {
    await renderMenu(false)
    fireEvent.keyDown(trigger(), { key: 'Enter' })
    expect(screen.queryByText(/Crew A/)).toBeNull()
    expect(sendSessionToInstance).not.toHaveBeenCalled()
  })

  it('the offline trigger stays reachable and explains itself', async () => {
    await renderMenu(false)
    // Reachable on purpose: a gated trigger dropped from focus is skipped in
    // silence, while its dimmed siblings announce why they will not act.
    expect(trigger().getAttribute('tabindex')).toBe('0')
    expect(trigger().getAttribute('aria-disabled')).toBe('true')
    expect(trigger().getAttribute('title')).toMatch(/Gateway offline/)
    expect(trigger().className).not.toContain('pointer-events-none')
  })
})
