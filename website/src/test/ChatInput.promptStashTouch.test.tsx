/**
 * The prompt stash on touch and under 768px.
 *
 * On a pointer device the stash rows live in the composer's "+" drop-up, and
 * the chord is Cmd/Ctrl+S. `directFilePicker = isMobile || isTouchDevice()`
 * replaces that drop-up with a bare file-input label, and a touch keyboard has
 * no Cmd/Ctrl -- so without a second host the feature does not exist on a
 * phone. `narrow-viewport-required` names this: "if a control is the only host
 * of an action, removing it on a phone removes the action." The host is the
 * existing overflow behind `composer-more-trigger`, whose row stays at two
 * controls (`max-two-buttons-per-row`).
 */
import { fireEvent, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Viewport width and input method are separate signals: a narrow window on a
// laptop (`mobile` without `touch`) still has a Cmd/Ctrl key.
const flags = vi.hoisted(() => ({ mobile: true, touch: true }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => flags.mobile }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => flags.touch }))

import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { PROMPT_STASH_MAX, addStashEntry, loadPromptStash, makeStashEntry } from '../utils/promptStash'
import { renderWithProviders } from './helpers'

function Host({
  slotId = 'chat-1', initial = '', withUpload = true, collapsible = false, promptStash = true,
}: { slotId?: string; initial?: string; withUpload?: boolean; collapsible?: boolean; promptStash?: boolean }) {
  const [value, setValue] = useState(initial)
  return (
    <SlotProvider slotId={slotId || null}>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        onUploadFiles={withUpload ? vi.fn() : undefined}
        collapsible={collapsible}
        promptStash={promptStash}
      />
    </SlotProvider>
  )
}

// Radix DropdownMenu opens on KEYBOARD activation in jsdom; its mouse open is
// PointerEvent-driven and jsdom does not deliver that (same path
// ChatInput.collapse.test.tsx uses for the same trigger).
const openOverflow = async () => {
  fireEvent.keyDown(screen.getByTestId('composer-more-trigger'), { key: 'Enter' })
  await screen.findByTestId('prompt-stash-menu')
}
// Radix items select on keyboard Enter as well; click is pointer-gated in jsdom.
const pick = (el: HTMLElement) => fireEvent.keyDown(el, { key: 'Enter' })

beforeEach(() => {
  localStorage.clear()
  flags.mobile = true
  flags.touch = true
})

describe('ChatInput prompt stash on touch', () => {
  it('stashes from the overflow menu and clears the composer, row still at two controls', async () => {
    const { container } = renderWithProviders(<Host initial="park me" />)
    // No "+" drop-up on touch: the attach control is a bare label.
    expect(screen.queryByTitle('Add files & options')).toBeNull()
    const row = container.querySelector('.input-area .flex.items-center.justify-between')!
    // Attach label + overflow trigger; the pencil folded into the overflow.
    expect(within(row as HTMLElement).getByTitle('Attach files')).toBeInTheDocument()
    expect(within(row as HTMLElement).getByTestId('composer-more-trigger')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sketch' })).toBeNull()
    await openOverflow()
    expect(screen.getByTitle('Sketch')).toBeInTheDocument()
    const stash = screen.getByTestId('prompt-stash-menu-stash')
    expect(stash).not.toHaveAttribute('data-disabled')
    expect(stash).toHaveTextContent('Set the text aside and clear the message input.')
    expect(stash.textContent).not.toMatch(/Ctrl\+S|⌘S/)
    pick(stash)
    expect(screen.getByRole('textbox')).toHaveValue('')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['park me'])
    const count = await screen.findByTestId('prompt-stash-count')
    expect(count).toHaveTextContent('1 draft stashed')
    expect(count.parentElement).toHaveAttribute('data-testid', 'prompt-stash-status-line')
  })

  it('explains a disabled stash row at the cap in the touch overflow', async () => {
    for (let i = 0; i < PROMPT_STASH_MAX; i++) {
      expect(addStashEntry('chat-1', makeStashEntry(`draft ${i}`, []))).toBe(true)
    }
    renderWithProviders(<Host initial="one too many" />)
    await openOverflow()
    const stash = screen.getByTestId('prompt-stash-menu-stash')
    expect(stash).toHaveAttribute('data-disabled')
    expect(stash).toHaveTextContent(`Stash full (${PROMPT_STASH_MAX}). Send or clear this message, then restore and send a stashed draft to make room.`)
  })

  it('restores the latest entry from the overflow menu', async () => {
    expect(addStashEntry('chat-1', makeStashEntry('older', []))).toBe(true)
    expect(addStashEntry('chat-1', makeStashEntry('newer', []))).toBe(true)
    renderWithProviders(<Host />)
    await openOverflow()
    expect(screen.getByTestId('prompt-stash-menu-stash')).toHaveAttribute('data-disabled')
    const restore = screen.getByTestId('prompt-stash-menu-restore')
    expect(restore).not.toHaveAttribute('data-disabled')
    expect(restore).toHaveTextContent('2 drafts stashed. Restores the latest from this menu.')
    expect(restore.textContent).not.toMatch(/Ctrl\+S|⌘S/)
    pick(restore)
    expect(screen.getByRole('textbox')).toHaveValue('newer')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older', 'newer'])
  })

  it('sits beside the Sketch and collapse rows in the collapsible composer', async () => {
    renderWithProviders(<Host initial="draft" collapsible />)
    await openOverflow()
    expect(screen.getByTitle('Sketch')).toBeInTheDocument()
    expect(screen.getByTestId('composer-collapse-row')).toBeInTheDocument()
    expect(screen.getByTestId('prompt-stash-menu-stash')).not.toHaveAttribute('data-disabled')
  })

  it('has a host without onUploadFiles (the side chat), holding only the stash rows', async () => {
    renderWithProviders(<Host initial="draft" withUpload={false} />)
    expect(screen.queryByTitle('Attach files')).toBeNull()
    await openOverflow()
    expect(screen.queryByTitle('Sketch')).toBeNull()
    expect(screen.queryByTestId('composer-collapse-row')).toBeNull()
    const menu = screen.getByTestId('prompt-stash-menu')
    // Nothing above the rows, so no rule either.
    expect(menu.className).not.toContain('border-t')
    pick(screen.getByTestId('prompt-stash-menu-stash'))
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['draft'])
  })

  it('does not create a touch overflow host when the stash is opted out', () => {
    renderWithProviders(<Host initial="off-record draft" withUpload={false} promptStash={false} />)
    expect(screen.queryByTestId('composer-more-trigger')).toBeNull()
    expect(screen.queryByTestId('prompt-stash-menu')).toBeNull()
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()
  })

  it('mounts no overflow without a session, keeping the dedicated pencil', () => {
    renderWithProviders(<Host slotId="" initial="draft" />)
    expect(screen.queryByTestId('composer-more-trigger')).toBeNull()
    expect(screen.getByTitle('Sketch')).toBeInTheDocument()
    expect(screen.queryByTestId('prompt-stash-menu')).toBeNull()
  })

  it('points the stashed confirmation at the menu, not at a chord a touch keyboard lacks', async () => {
    renderWithProviders(<Host initial="park me" />)
    await openOverflow()
    pick(screen.getByTestId('prompt-stash-menu-stash'))
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed. Restore it from the ⋯ menu.')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Draft stashed. 1 draft is stashed; restore it from the ⋯ menu.')
    expect(screen.getByTestId('prompt-stash-notice').textContent).not.toMatch(/Ctrl\+S|⌘S/)
    expect(screen.getByTestId('prompt-stash-status').textContent).not.toMatch(/Ctrl\+S|⌘S/)
  })

  it('uses menu copy for the other chord-instructing messages on a touch host', () => {
    // A hardware keyboard on a touch host can still press the chord; the copy
    // it gets back still names the menu, which is what the host renders.
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    fireEvent.keyDown(input, { key: 's', ctrlKey: true })
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Nothing is stashed. Type something, then stash it from the ⋯ menu.')
    fireEvent.change(input, { target: { value: 'one' } })
    fireEvent.keyDown(input, { key: 's', ctrlKey: true })
    fireEvent.change(input, { target: { value: 'two' } })
    fireEvent.keyDown(input, { key: 's', ctrlKey: true })
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('2 drafts are stashed; restore the latest from the ⋯ menu.')
  })

  it('keeps the chord copy on a pointer device', () => {
    flags.mobile = false
    flags.touch = false
    renderWithProviders(<Host initial="park me" />)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 's', ctrlKey: true })
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed. Ctrl+S in an empty message input brings it back.')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('press Ctrl+S in an empty message input to restore it.')
  })

  it('keeps the chord copy in a narrow window on a non-touch device, even though the rows mount in the overflow', async () => {
    // `isMobile` mounts the ⋯ overflow, but the copy follows the input method:
    // a keyboard user in a narrow window pressed the chord and is told the chord.
    flags.mobile = true
    flags.touch = false
    renderWithProviders(<Host initial="park me" />)
    expect(screen.getByTestId('composer-more-trigger')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 's', ctrlKey: true })
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed. Ctrl+S in an empty message input brings it back.')
    expect(screen.getByTestId('prompt-stash-notice').textContent).not.toContain('⋯')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('press Ctrl+S in an empty message input to restore it.')
    // The count's hint follows the same signal, not the viewport.
    expect(screen.getByTestId('prompt-stash-count').getAttribute('title')).toContain('Ctrl+S')
  })

  it('points the count hint at the menu, not the chord, on a touch host', async () => {
    addStashEntry('chat-1', makeStashEntry('kept', []))
    renderWithProviders(<Host />)
    const count = await screen.findByTestId('prompt-stash-count')
    expect(count).toHaveAttribute('title', 'Stashed drafts for this chat. Restore the latest from the ⋯ menu.')
    expect(count).toHaveTextContent('Stashed drafts for this chat. Restore the latest from the ⋯ menu.')
    expect(count.getAttribute('title')).not.toMatch(/Ctrl\+S|⌘S/)
    expect(count.textContent).not.toMatch(/Ctrl\+S|⌘S/)
  })

  it('points the full-stash count hint at the menu on a touch host', async () => {
    for (let i = 0; i < PROMPT_STASH_MAX; i++) addStashEntry('chat-1', makeStashEntry(`draft ${i}`, []))
    renderWithProviders(<Host />)
    const count = await screen.findByTestId('prompt-stash-count')
    expect(count).toHaveAttribute('title', 'The stash is full. Restore a draft before stashing another; the ⋯ menu restores the latest.')
    expect(count.getAttribute('title')).not.toMatch(/Ctrl\+S|⌘S/)
    expect(count.textContent).not.toMatch(/Ctrl\+S|⌘S/)
  })

  it('keeps the chord in the count hint on a pointer device', async () => {
    flags.mobile = false
    flags.touch = false
    addStashEntry('chat-1', makeStashEntry('kept', []))
    renderWithProviders(<Host />)
    const count = await screen.findByTestId('prompt-stash-count')
    expect(count).toHaveAttribute('title', 'Stashed drafts for this chat. Ctrl+S in an empty message input restores the latest.')
    expect(count.getAttribute('title')).not.toContain('⋯')
  })
})
