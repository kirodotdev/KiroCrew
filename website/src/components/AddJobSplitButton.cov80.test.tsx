import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import AddJobSplitButton from './AddJobSplitButton'

/** Radix opens on keydown/pointerdown, not a synthetic click — same helper
 *  shape as ChatSidebar.createMenu.test.tsx. */
function openCaretMenu() {
  fireEvent.keyDown(screen.getByLabelText('Browse schedule templates'), { key: 'Enter' })
}

describe('AddJobSplitButton', () => {
  it('the primary half starts a blank job', () => {
    const onBlank = vi.fn()
    render(<AddJobSplitButton onBlank={onBlank} onBrowseTemplates={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /add job/i }))
    expect(onBlank).toHaveBeenCalledTimes(1)
  })

  it('the caret half opens the gallery menu without starting a blank job', async () => {
    const onBrowseTemplates = vi.fn()
    const onBlank = vi.fn()
    render(<AddJobSplitButton onBlank={onBlank} onBrowseTemplates={onBrowseTemplates} />)

    openCaretMenu()
    const browse = await screen.findByText('Browse all templates')
    expect(browse.closest('[role="menuitem"]')).toBeTruthy()
    expect(onBlank).not.toHaveBeenCalled()

    fireEvent.click(browse.closest('[role="menuitem"]')!)
    await waitFor(() => expect(onBrowseTemplates).toHaveBeenCalledTimes(1))
  })

  it('the chat item navigates to the chat route', async () => {
    render(<AddJobSplitButton onBlank={vi.fn()} onBrowseTemplates={vi.fn()} />)
    openCaretMenu()
    const chat = (await screen.findByText(/^Open Chat$/i)).closest('[role="menuitem"]')!

    // happy-dom's real location object rejects href assignment, so swap in a
    // plain stand-in for the duration of the click.
    const original = window.location
    const stub = { href: '' } as Location
    Object.defineProperty(window, 'location', { value: stub, writable: true, configurable: true })
    try {
      fireEvent.click(chat)
      await waitFor(() => expect(stub.href).toBe('/chat'))
    } finally {
      Object.defineProperty(window, 'location', {
        value: original,
        writable: true,
        configurable: true,
      })
    }
  })
})
