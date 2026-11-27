import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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
import { registerComposerUpload, unregisterComposerUpload } from '../utils/composerSendHolds'

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

/** The uploads a Host started, as the hosts keep them: in the shared
 *  registry, one per file, each ending when it settles or is aborted. */
const SLOT = 'chat-1'
const live = new Set<AbortController>()
const endUpload = (c: AbortController) => { if (live.delete(c)) unregisterComposerUpload(SLOT, c) }
const finishUpload = () => { [...live].forEach(endUpload) }
afterEach(() => { act(() => finishUpload()) })

function Host({ initial = '', collapsible = false }: { initial?: string; collapsible?: boolean }) {
  const [val, setVal] = useState(initial)
  const uploadFiles = (files: File[]) => {
    for (const file of files) {
      const c = new AbortController()
      live.add(c)
      c.signal.addEventListener('abort', () => endUpload(c))
      registerComposerUpload(SLOT, c, file.name)
    }
  }
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput
        collapsible={collapsible}
        value={val}
        onChange={setVal}
        onSend={vi.fn()}
        onUploadFiles={uploadFiles}
        uploadSlot={SLOT}
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

  it('moves focus to the next chip, then to "+", as the user cancels uploads from their chips', async () => {
    renderWithProviders(<Host />)
    await openPicker()
    const input = fileInput()
    Object.defineProperty(input, 'files', { configurable: true, value: [new File(['x'], 'a.txt'), new File(['y'], 'b.txt')] })
    fireEvent.change(input)
    const cancelA = screen.getByRole('button', { name: 'Cancel uploading a.txt' })
    cancelA.focus()
    fireEvent.click(cancelA)
    expect(screen.queryByRole('button', { name: 'Cancel uploading a.txt' })).not.toBeInTheDocument()
    // The cancelled chip is gone, so focus is on its neighbour, not <body>.
    const cancelB = screen.getByRole('button', { name: 'Cancel uploading b.txt' })
    expect(document.activeElement).toBe(cancelB)
    fireEvent.click(cancelB)
    expect(screen.queryByTestId('composer-upload-chip')).not.toBeInTheDocument()
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
    // The collapsible host: the one where touch opens the picker from "+" directly.
    renderWithProviders(<Host collapsible />)
    expect(screen.queryByRole('button', { name: 'Add files & options' })).not.toBeInTheDocument()
    chooseFile()
    expect(document.activeElement).not.toBe(composer())
    fireEvent(fileInput(), new Event('cancel'))
    expect(document.activeElement).not.toBe(composer())
  })

  it('does not focus the composer or the "+" trigger on touch when "+" still opens its menu', async () => {
    touchEnv.touch = true
    // A non-collapsible host (Crewmates DM, split pane, side chat): touch keeps the menu.
    renderWithProviders(<Host />)
    // Cancelled pick first, so the cancel half is checked with focus still parked.
    await openPicker()
    expect(document.activeElement).toBe(document.body)
    fireEvent(fileInput(), new Event('cancel'))
    expect(document.activeElement).toBe(document.body)
    await openPicker()
    chooseFile()
    expect(document.activeElement).toBe(document.body)
  })
})
