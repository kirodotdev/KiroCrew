import { useState } from 'react'
import { act, fireEvent, screen } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'

/* `directFilePicker = isMobile || isTouchDevice()` decides which host carries
   Send later: the "+" drop-up on a pointer device, the ··· overflow on touch.
   Both read ONE disabled-reason chain, and this file pins that the platform
   arms reach both -- a reason added to only one host is exactly the class of
   defect the mobile Send later entry point already had once. */
const flags = vi.hoisted(() => ({ touch: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => flags.touch }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => flags.touch }))

import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { renderWithProviders } from './helpers'
import { stubStripHeights } from './stripHeights'

/* The backend refuses scheduled-message provenance on BOTH hosts
   (`scheduled_message_provenance_supported` in autonudge_selfarm.py): neither
   gives Crew the filesystem isolation that keeps a delegated same-user process
   away from the signing key. Each host gets its own sentence, so the reader is
   told which OS is the reason, in their own language. */
const UNSUPPORTED = [
  ['Windows', "Send later isn't available when the gateway runs on Windows."],
  ['macOS', "Send later isn't available when the gateway runs on macOS."],
] as const
const ENABLED_SUBTITLE = 'Delivers this draft at a time you pick'
const GENERIC_FAILURE = "Couldn't schedule the message. Your draft was kept — try again."

function Host({ initial = 'follow up with the release owner' }: { initial?: string }) {
  const [value, setValue] = useState(initial)
  const [automationOpen, setAutomationOpen] = useState(false)
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        onUploadFiles={vi.fn()}
        /* The touch overflow is hosted only by the collapsible main composer
           (ChatPage's), so the host opts in the way that caller does. */
        collapsible
        automation={null}
        automationOpen={automationOpen}
        onAutomationClick={setAutomationOpen}
        automationCreationReady
        onAutomationChange={vi.fn()}
      />
    </SlotProvider>
  )
}

/* `useGatewayPlatform` is a pure reader over the prerequisite gate's cache
   entry (the gate owns the fetch, the hook has `enabled: false`), so seeding
   that key is how a test says "the gateway is Windows" without a request.
   The wire value is the gate's DISPLAY label (`Windows`, `macOS`), not
   `sys.platform`, matching what `_platform_label` actually sends. react-query
   delivers the cache notification through its batching scheduler (a timeout),
   so the helper flushes that tick before handing control back. */
async function setGatewayPlatform(client: QueryClient, platform: string) {
  await act(async () => {
    client.setQueryData(['kiro-prerequisite'], { platform })
    await new Promise(resolve => setTimeout(resolve, 0))
  })
}

const openPlusMenu = () => fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
// The overflow is the repo's Radix DropdownMenu, which opens on KEYBOARD
// activation in jsdom -- its mouse open is PointerEvent-driven and jsdom does
// not deliver that.
const openOverflow = () => fireEvent.keyDown(
  screen.getByTestId('composer-more-trigger'), { key: 'Enter' },
)

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  stubStripHeights()
  localStorage.clear()
  flags.touch = false
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ChatInput Send later on an unsupported gateway platform', () => {
  it.each(UNSUPPORTED)('disables the desktop row and names the host on a %s gateway', async (platform, reason) => {
    const { queryClient } = renderWithProviders(<Host />)
    await setGatewayPlatform(queryClient, platform)

    openPlusMenu()
    const row = await screen.findByTestId('plus-menu-send-later')

    expect(row).toBeDisabled()
    expect(row).toHaveAttribute('title', reason)
    // The reason is visible text on the row, not only hover text: a disabled
    // control is out of the tab order, so the explanation has to be readable
    // in place for a keyboard or touch reader who cannot hover it.
    expect(row).toHaveTextContent(reason)
    expect(row).not.toHaveTextContent(ENABLED_SUBTITLE)
  })

  it.each(UNSUPPORTED)('names %s even before a draft exists, because it is the permanent reason', async (platform, reason) => {
    const { queryClient } = renderWithProviders(<Host initial="" />)
    await setGatewayPlatform(queryClient, platform)

    openPlusMenu()
    const row = await screen.findByTestId('plus-menu-send-later')

    expect(row).toBeDisabled()
    expect(row).toHaveTextContent(reason)
    expect(row).not.toHaveTextContent('Type a message first.')
  })

  it.each(UNSUPPORTED)('disables the touch overflow item with the same %s reason', async (platform, reason) => {
    flags.touch = true
    const { queryClient } = renderWithProviders(<Host />)
    await setGatewayPlatform(queryClient, platform)

    expect(screen.queryByRole('button', { name: 'Add files & options' })).not.toBeInTheDocument()
    openOverflow()
    const item = screen.getByTestId('mobile-menu-send-later')

    expect(item).toHaveAttribute('data-disabled')
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(item).toHaveAttribute('title', reason)
    expect(item).toHaveTextContent(reason)
    fireEvent.click(item)
    expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument()
  })

  it('tells a Windows reader about Windows and a macOS reader about macOS, never the other', async () => {
    const { queryClient } = renderWithProviders(<Host />)
    await setGatewayPlatform(queryClient, 'Windows')
    openPlusMenu()
    const row = await screen.findByTestId('plus-menu-send-later')
    expect(row).toHaveTextContent('Windows')
    expect(row).not.toHaveTextContent('macOS')

    await setGatewayPlatform(queryClient, 'macOS')

    expect(row).toHaveTextContent('macOS')
    expect(row).not.toHaveTextContent('Windows')
  })

  it.each([
    ['Linux', 'a Linux host'],
    ['gateway', 'the masked value a non-owner receives'],
  ])('keeps Send later enabled when the platform is %s (%s)', async (platform) => {
    const { queryClient } = renderWithProviders(<Host />)
    await setGatewayPlatform(queryClient, platform)

    openPlusMenu()
    const row = await screen.findByTestId('plus-menu-send-later')

    expect(row).toBeEnabled()
    expect(row).toHaveTextContent(ENABLED_SUBTITLE)
    expect(row).not.toHaveTextContent('Windows')
    expect(row).not.toHaveTextContent('macOS')
  })

  it('keeps the touch overflow item enabled on a supported gateway', async () => {
    flags.touch = true
    const { queryClient } = renderWithProviders(<Host />)
    await setGatewayPlatform(queryClient, 'Linux')

    openOverflow()
    const item = screen.getByTestId('mobile-menu-send-later')

    expect(item).not.toHaveAttribute('data-disabled')
    expect(item).toHaveTextContent(ENABLED_SUBTITLE)
    fireEvent.click(item)
    expect(await screen.findByTestId('schedule-later-popover')).toBeInTheDocument()
  })

  it('re-enables the row when the gateway platform resolves to a supported host', async () => {
    const { queryClient } = renderWithProviders(<Host />)
    await setGatewayPlatform(queryClient, 'macOS')
    openPlusMenu()
    expect(await screen.findByTestId('plus-menu-send-later')).toBeDisabled()

    await setGatewayPlatform(queryClient, 'Linux')

    expect(screen.getByTestId('plus-menu-send-later')).toBeEnabled()
  })

  it('keeps the generic recovery copy for a 503 refusal on a supported gateway', async () => {
    /* The backend answers every failed arm with `autonudge_not_armed`, so a
       transient provenance-store failure on Linux is indistinguishable from
       the platform refusal by code alone. On a supported host that 503 IS
       transient, and the copy that keeps the draft and invites a retry stays
       the right one; the platform arms above are what keep a Windows or macOS
       reader from ever reaching this message. */
    vi.stubGlobal('fetch', vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input) === '/api/slash-commands') {
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
      }
      return Promise.resolve(new Response(JSON.stringify({
        error: 'scheduled message provenance unavailable — loop not armed',
        code: 'autonudge_not_armed',
      }), { status: 503, headers: { 'Content-Type': 'application/json' } }))
    }))
    const { queryClient } = renderWithProviders(<Host />)
    await setGatewayPlatform(queryClient, 'Linux')

    openPlusMenu()
    fireEvent.click(await screen.findByTestId('plus-menu-send-later'))
    await screen.findByTestId('schedule-later-popover')
    fireEvent.click(screen.getByTestId('schedule-later-confirm'))

    const notice = await screen.findByTestId('schedule-error')
    expect(notice).toHaveTextContent(GENERIC_FAILURE)
    expect(notice).not.toHaveTextContent('Windows')
    expect(notice).not.toHaveTextContent('macOS')
    expect(screen.getByLabelText('Message input')).toHaveValue('follow up with the release owner')
  })
})
