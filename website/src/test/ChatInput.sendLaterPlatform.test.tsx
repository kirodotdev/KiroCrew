import { useState } from 'react'
import { act, fireEvent, screen } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'

const flags = vi.hoisted(() => ({ touch: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => flags.touch }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => flags.touch }))

import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { renderWithProviders } from './helpers'
import { stubStripHeights } from './stripHeights'

const ENABLED_SUBTITLE = 'Delivers this draft at a time you pick'

function Host() {
  const [value, setValue] = useState('follow up with the release owner')
  const [automationOpen, setAutomationOpen] = useState(false)
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        onUploadFiles={vi.fn()}
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

async function setGatewayPlatform(client: QueryClient, platform: string) {
  await act(async () => {
    client.setQueryData(['kiro-prerequisite'], { platform })
    await new Promise(resolve => setTimeout(resolve, 0))
  })
}

const openPlusMenu = () => fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
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

describe('ChatInput Send later cross-platform support', () => {
  it.each(['Windows', 'macOS', 'Linux', 'gateway'])(
    'keeps the desktop action enabled when the gateway platform is %s',
    async platform => {
      const { queryClient } = renderWithProviders(<Host />)
      await setGatewayPlatform(queryClient, platform)

      openPlusMenu()
      const row = await screen.findByTestId('plus-menu-send-later')

      expect(row).toBeEnabled()
      expect(row).toHaveTextContent(ENABLED_SUBTITLE)
      fireEvent.click(row)
      expect(await screen.findByTestId('schedule-later-popover')).toBeInTheDocument()
    },
  )

  it.each(['Windows', 'macOS'])(
    'keeps the touch overflow action enabled when the gateway platform is %s',
    async platform => {
      flags.touch = true
      const { queryClient } = renderWithProviders(<Host />)
      await setGatewayPlatform(queryClient, platform)

      openOverflow()
      const item = screen.getByTestId('mobile-menu-send-later')

      expect(item).not.toHaveAttribute('data-disabled')
      expect(item).not.toHaveAttribute('aria-disabled')
      expect(item).toHaveTextContent(ENABLED_SUBTITLE)
      fireEvent.click(item)
      expect(await screen.findByTestId('schedule-later-popover')).toBeInTheDocument()
    },
  )
})
