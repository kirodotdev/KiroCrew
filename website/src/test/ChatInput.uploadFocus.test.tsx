import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { act, screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { SlotProvider } from '../providers/SlotContext'

/* Focus after the "+" -> Upload file picker closes, and after an upload ends.
 * Opening the picker closes the menu that held focus, so without a return
 * path focus sits on <body> and the first keystroke after attaching a file is
 * lost. A chosen file returns focus to the composer, a cancelled picker to the
 * "+" trigger, and neither ever pulls focus off a control the user moved to. */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
  fileSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

// Touch swaps the "+" menu for a bare file label (directFilePicker).
const touchEnv = vi.hoisted(() => ({ touch: false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

import ChatInput from '../components/ChatInput'

beforeEach(() => {
  touchEnv.touch = false
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTrust.mockResolvedValue({ project: '/work/p', project_key: '/work/p' })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
  mockApi.fileSearch.mockResolvedValue({ results: [] })
  // jsdom has no OS picker; the click is the browser's hand-off to it.
  vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {})
})

let finishUpload: () => void = () => {}

function Host({ initial = '' }: { initial?: string }) {
  const [val, setVal] = useState(initial)
  const [uploading, setUploading] = useState(false)
  finishUpload = () => setUploading(false)
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput
        value={val}
        onChange={setVal}
        onSend={vi.fn()}
        onUploadFiles={() => setUploading(true)}
        onCancelUpload={() => setUploading(false)}
        uploading={uploading}
      />
      <button type="button">Elsewhere</button>
    </SlotProvider>
  )
}

const composer = () => document.querySelector('textarea[data-composer-input]') as HTMLTextAreaElement
const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement
const trigger = () => screen.getByRole('button', { name: 'Add files & options' })

/** "+" -> Upload file, with focus where a browser leaves it: on the menu item
 *  until the menu unmounts, then <body>. */
async function openPicker() {
  fireEvent.click(trigger())
  const item = (await screen.findByText('Upload file')).closest('button') as HTMLButtonElement
  item.focus()
  fireEvent.click(item)
}

function chooseFile(name = 'notes.txt') {
  const input = fileInput()
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['x'], name)] })
  fireEvent.change(input)
}

describe('ChatInput focus after the upload file picker', () => {
  it('leaves focus nowhere when the menu closes for the picker (the bug this guards)', async () => {
    renderWithProviders(<Host />)
    await openPicker()
    expect(document.activeElement).toBe(document.body)
  })

  it('returns focus to the composer after a file is chosen, keeping draft and caret', async () => {
    renderWithProviders(<Host initial="hello world" />)
    composer().focus()
    composer().setSelectionRange(5, 5)
    await openPicker()
    chooseFile()
    expect(document.activeElement).toBe(composer())
    expect(composer().value).toBe('hello world')
    expect(composer().selectionStart).toBe(5)
    expect(composer().selectionEnd).toBe(5)
  })

  it('returns focus to the composer on every repeated upload', async () => {
    renderWithProviders(<Host />)
    for (const name of ['a.txt', 'b.txt']) {
      await openPicker()
      chooseFile(name)
      expect(document.activeElement).toBe(composer())
      act(() => finishUpload())
      expect(document.activeElement).toBe(composer())
    }
  })

  it('returns focus to the "+" trigger when the picker is cancelled', async () => {
    renderWithProviders(<Host />)
    await openPicker()
    fireEvent(fileInput(), new Event('cancel'))
    expect(document.activeElement).toBe(trigger())
  })

  it('does not take focus from a control the user moved to', async () => {
    renderWithProviders(<Host />)
    await openPicker()
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    elsewhere.focus()
    chooseFile()
    expect(document.activeElement).toBe(elsewhere)
    fireEvent(fileInput(), new Event('cancel'))
    expect(document.activeElement).toBe(elsewhere)
  })

  it('keeps focus on the attach slot when the user cancels the upload from its cancel control', async () => {
    renderWithProviders(<Host />)
    await openPicker()
    chooseFile()
    const cancel = screen.getByRole('button', { name: 'Cancel upload' })
    cancel.focus()
    fireEvent.click(cancel)
    expect(screen.queryByRole('button', { name: 'Cancel upload' })).not.toBeInTheDocument()
    // The cancel control and the "+" trigger share one slot and one element,
    // so focus stays on the restored trigger instead of dropping to <body>.
    expect(document.activeElement).toBe(trigger())
  })

  it('leaves focus alone when an upload ends while the user is elsewhere', async () => {
    renderWithProviders(<Host />)
    await openPicker()
    chooseFile()
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    elsewhere.focus()
    act(() => finishUpload())
    expect(document.activeElement).toBe(elsewhere)
  })

  it('does not focus the composer on touch, where it would raise the soft keyboard', () => {
    touchEnv.touch = true
    renderWithProviders(<Host />)
    expect(screen.queryByRole('button', { name: 'Add files & options' })).not.toBeInTheDocument()
    chooseFile()
    expect(document.activeElement).not.toBe(composer())
    fireEvent(fileInput(), new Event('cancel'))
    expect(document.activeElement).not.toBe(composer())
  })
})
