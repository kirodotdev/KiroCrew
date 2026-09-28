import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { SlotProvider } from '../providers/SlotContext'

/* The "+" menu is portaled to <body> and positioned from a snapshot of the
 * trigger's rect, so it must remeasure while open: the mobile keyboard
 * closing moves the composer and announces itself only on
 * window.visualViewport (#10580, same class as the busy-send picker). */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
  fileSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTrust.mockResolvedValue({ project: '/work/p', project_key: '/work/p' })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
  mockApi.fileSearch.mockResolvedValue({ results: [] })
})

function Host() {
  const [val, setVal] = useState('')
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput value={val} onChange={setVal} onSend={vi.fn()} onUploadFiles={vi.fn()} />
    </SlotProvider>
  )
}

describe('ChatInput "+" menu keyboard anchoring', () => {
  it('remeasures the open menu when the visual viewport changes', async () => {
    const originalViewport = Object.getOwnPropertyDescriptor(window, 'visualViewport')
    const viewport = new EventTarget()
    Object.defineProperty(window, 'visualViewport', {
      configurable: true,
      value: viewport as unknown as VisualViewport,
    })

    try {
      renderWithProviders(<Host />)
      const trigger = screen.getByLabelText('Add files & options')

      let anchor = DOMRect.fromRect({ x: 100, y: 600, width: 32, height: 32 })
      vi.spyOn(trigger, 'getBoundingClientRect').mockImplementation(() => anchor)

      fireEvent.click(trigger)
      const menu = (await screen.findByText('Upload file')).closest('div.fixed') as HTMLElement
      expect(menu).toBeInstanceOf(HTMLElement)
      expect(menu.style.bottom).toBe(`${window.innerHeight - anchor.top + 8}px`)

      // The mobile keyboard closing moves the anchor and announces itself only
      // on window.visualViewport -- window resize/scroll never fire.
      anchor = DOMRect.fromRect({ x: 100, y: 700, width: 32, height: 32 })
      fireEvent(viewport, new Event('resize'))

      await waitFor(() => {
        expect(menu.style.bottom).toBe(`${window.innerHeight - anchor.top + 8}px`)
      })
    } finally {
      if (originalViewport) Object.defineProperty(window, 'visualViewport', originalViewport)
      else delete (window as { visualViewport?: VisualViewport }).visualViewport
    }
  })
})
