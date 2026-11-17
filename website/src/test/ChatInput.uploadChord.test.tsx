import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { act, screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { SlotProvider } from '../providers/SlotContext'

/* Cmd/Ctrl+Shift+U in the composer opens the "+" menu's Upload file picker
 * (#34607), from either editor, wherever that tile could be pressed. jsdom is
 * not macOS, so the chord here is Ctrl+Shift+U. */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
  fileSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'
import ShortcutsModal from '../components/ShortcutsModal'

// The rich-text composer sits behind a React.lazy boundary; a cold chunk on a
// loaded CI shard can outlast findBy*'s 1 s default.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }
const CHORD = { key: 'U', code: 'KeyU', ctrlKey: true, shiftKey: true }

let pickerOpens: HTMLInputElement[] = []
beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTrust.mockResolvedValue({ project: '/work/p', project_key: '/work/p' })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
  mockApi.fileSearch.mockResolvedValue({ results: [] })
  pickerOpens = []
  // jsdom has no OS picker; the click is the browser's hand-off to it.
  vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function (this: HTMLInputElement) {
    pickerOpens.push(this)
  })
})

function Host(props: Partial<React.ComponentProps<typeof ChatInput>>) {
  const [val, setVal] = useState('draft')
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput value={val} onChange={setVal} onSend={vi.fn()} onUploadFiles={vi.fn()} {...props} />
      <output data-testid="value">{val}</output>
    </SlotProvider>
  )
}

const textarea = () => document.querySelector('textarea[data-composer-input]') as HTMLTextAreaElement

describe('ChatInput Cmd/Ctrl+Shift+U opens the Upload file picker', () => {
  it('opens the file picker once from the textarea and leaves the draft as it was', () => {
    renderWithProviders(<Host />)
    expect(fireEvent.keyDown(textarea(), CHORD)).toBe(false)
    expect(pickerOpens).toHaveLength(1)
    expect(pickerOpens[0].type).toBe('file')
    expect(screen.getByTestId('value').textContent).toBe('draft')
  })

  it('opens it from the rich-text composer too', async () => {
    renderWithProviders(<Host lexicalComposer />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    await act(async () => {})
    fireEvent.keyDown(input, CHORD)
    expect(pickerOpens).toHaveLength(1)
    expect(pickerOpens[0].type).toBe('file')
  })

  it.each([
    ['the host takes no uploads', { onUploadFiles: undefined }],
    ['an upload is in flight', { uploading: true, onCancelUpload: vi.fn() }],
    ['another teammate holds the typing lock', { typingLock: { holderName: 'Alice' } }],
  ])('does nothing while %s, as the Upload file tile does', (_label, props) => {
    renderWithProviders(<Host {...props} />)
    expect(fireEvent.keyDown(textarea(), CHORD)).toBe(true)
    expect(pickerOpens).toHaveLength(0)
  })

  it('leaves Ctrl+U to the browser, and Ctrl+Shift+U to an input method that took it', () => {
    renderWithProviders(<Host />)
    expect(fireEvent.keyDown(textarea(), { key: 'u', code: 'KeyU', ctrlKey: true })).toBe(true)
    // IBus starts Unicode entry on Ctrl+Shift+U; the page sees keyCode 229.
    expect(fireEvent.keyDown(textarea(), { ...CHORD, key: 'Process', keyCode: 229 })).toBe(true)
    expect(pickerOpens).toHaveLength(0)
  })

  it('names the chord on the Upload file tile and in the shortcuts list', async () => {
    renderWithProviders(<Host />)
    fireEvent.click(screen.getByRole('button', { name: 'Add files & options' }))
    const tile = (await screen.findByText('Upload file')).closest('button') as HTMLButtonElement
    expect(tile).toHaveAttribute('title', 'Upload file · Ctrl+Shift+U')

    renderWithProviders(<ShortcutsModal onClose={vi.fn()} />)
    fireEvent.change(screen.getByRole('textbox', { name: 'Search' }), { target: { value: 'upload' } })
    const row = screen.getAllByText('Upload file').find(el => el.closest('[role="dialog"]'))
    expect(row?.parentElement?.querySelector('[data-testid="shortcut-combo"]')?.textContent).toBe('Ctrl+Shift+U')
  })
})
