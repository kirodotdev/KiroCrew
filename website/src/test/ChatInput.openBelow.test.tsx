import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { SlotProvider } from '../providers/SlotContext'
import type { ComposerActionItem } from '../composerActions/composerActionRegistry'

/* `openBelow` (the top-down chat layout): the composer sits at the top of the
 * window, so its own popups must open BELOW their control and stop at the
 * viewport's bottom edge. Opening upward there puts their top rows above the
 * window, where nothing can be clicked. With the prop off each popup keeps the
 * bottom layout's upward placement exactly. */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
  fileSearch: vi.fn(),
  chatSlotAutocompact: vi.fn(),
  sessionCrewLogProjections: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

// The app frame host is tested on its own; here it only has to exist.
vi.mock('../composerActions/composerActionHost', () => ({
  ComposerActionHost: class {
    start() {}
    cancel() {}
  },
}))

import ChatInput from '../components/ChatInput'

const action: ComposerActionItem = { key: 'snip:insert', appName: 'snip', appLabel: 'Snippets', id: 'insert', title: 'Insert snippet', icon: '', width: 360, maxHeight: 320 }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTrust.mockResolvedValue({ project: '/work/p', project_key: '/work/p' })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
  mockApi.fileSearch.mockResolvedValue({ results: [] })
  // Never settles: the popover's placement is what these tests read.
  mockApi.chatSlotAutocompact.mockReturnValue(new Promise(() => {}))
  mockApi.sessionCrewLogProjections.mockReturnValue(new Promise(() => {}))
})

function Host({ openBelow }: { openBelow?: boolean }) {
  const [val, setVal] = useState('')
  return (
    <SlotProvider slotId="chat-1">
      {/* A model chip draws the context shelf the readout stands on. */}
      <ChatInput value={val} onChange={setVal} onSend={vi.fn()} onUploadFiles={vi.fn()} contextPct={40} modelName="model-a" onModelClick={vi.fn()} composerActions={[action]} {...(openBelow === undefined ? {} : { openBelow })} />
    </SlotProvider>
  )
}

/** The "+" button near the top of the window, as the top-down dock places it. */
const PLUS = DOMRect.fromRect({ x: 100, y: 100, width: 32, height: 32 })

async function openPlusMenu() {
  const trigger = screen.getByLabelText('Add files & options')
  vi.spyOn(trigger, 'getBoundingClientRect').mockImplementation(() => PLUS)
  fireEvent.click(trigger)
  return (await screen.findByText('Upload file')).closest('div.fixed') as HTMLElement
}

describe('ChatInput openBelow: the "+" menu', () => {
  it('opens below the button, capped to the room below it', async () => {
    renderWithProviders(<Host openBelow />)
    const menu = await openPlusMenu()
    expect(menu.style.bottom).toBe('')
    expect(menu.style.top).toBe(`${PLUS.bottom + 8}px`)
    expect(menu.style.maxHeight).toBe(`${window.innerHeight - PLUS.bottom - 16}px`)
    expect(menu.className).toContain('overflow-y-auto')
  })

  it.each([['off', false], ['left out', undefined]])('opens above the button as on main with the prop %s', async (_name, openBelow) => {
    renderWithProviders(<Host openBelow={openBelow} />)
    const menu = await openPlusMenu()
    expect(menu.style.top).toBe('')
    expect(menu.style.maxHeight).toBe('')
    expect(menu.style.bottom).toBe(`${window.innerHeight - PLUS.top + 8}px`)
    expect(menu.className).not.toContain('overflow-y-auto')
  })
})

describe('ChatInput openBelow: an app action popover', () => {
  /** The draft box, above the "+" button's row. */
  const BOX = DOMRect.fromRect({ x: 90, y: 60, width: 600, height: 40 })

  async function openAction() {
    const box = screen.getByLabelText('Message input')
    expect(box).toBeInstanceOf(HTMLTextAreaElement)
    vi.spyOn(box, 'getBoundingClientRect').mockImplementation(() => BOX)
    const menu = await openPlusMenu()
    const row = menu.querySelector(`[data-composer-action-row="${action.key}"]`) as HTMLElement
    act(() => { fireEvent.click(row) })
    return screen.getByRole('dialog', { name: 'Insert snippet, from Snippets' })
  }

  it('opens below the composer, capped to the room below it', async () => {
    renderWithProviders(<Host openBelow />)
    const dialog = await openAction()
    // Under the lower of the draft box and the "+" button: the whole composer stays in view.
    const top = Math.max(PLUS.bottom, BOX.bottom) + 8
    expect(dialog.style.bottom).toBe('')
    expect(dialog.style.top).toBe(`${top}px`)
    expect(dialog.style.maxHeight).toBe(`${window.innerHeight - top - 8}px`)
  })

  it('opens above the draft box as on main with the prop off', async () => {
    renderWithProviders(<Host />)
    const dialog = await openAction()
    const bottom = window.innerHeight - BOX.top + 8
    expect(dialog.style.top).toBe('')
    expect(dialog.style.bottom).toBe(`${bottom}px`)
    expect(dialog.style.maxHeight).toBe(`${Math.max(48, window.innerHeight - bottom - 8)}px`)
  })
})

describe('ChatInput openBelow: the context-usage popover', () => {
  /** The card is the shared Radix popover: `data-side` is the side it opened on,
   *  and Radix sets `--radix-popover-content-available-height` to the room there. */
  function openContext() {
    fireEvent.click(screen.getByLabelText('Context usage'))
    return screen.getByTestId('context-usage-popover')
  }

  it('opens below the readout, capped to the room below it', () => {
    renderWithProviders(<Host openBelow />)
    const pop = openContext()
    expect(pop.getAttribute('data-side')).toBe('bottom')
    expect(pop.className).toContain('max-h-[var(--radix-popover-content-available-height)]')
    expect(pop.className).toContain('overflow-y-auto')
  })

  it('opens above the readout as on main with the prop off', () => {
    renderWithProviders(<Host />)
    const pop = openContext()
    expect(pop.getAttribute('data-side')).toBe('top')
    expect(pop.className).not.toContain('radix-popover-content-available-height')
    expect(pop.className).not.toContain('overflow-y-auto')
  })
})

describe('ChatInput openBelow: the busy send-mode menu', () => {
  /** The steer/queue split button near the top of the window, as the top-down dock places it. */
  const SPLIT = DOMRect.fromRect({ x: 600, y: 130, width: 56, height: 32 })

  /** A running turn with a draft: the send slot is the split button. */
  function BusyHost({ openBelow }: { openBelow?: boolean }) {
    const [val, setVal] = useState('more')
    return (
      <SlotProvider slotId="chat-1">
        <ChatInput value={val} onChange={setVal} onSend={vi.fn()} isRunning canSteer onSteer={vi.fn()} onStop={vi.fn()} {...(openBelow === undefined ? {} : { openBelow })} />
      </SlotProvider>
    )
  }

  function openMenu() {
    const caret = screen.getByTestId('busy-send-caret')
    // The menu is placed from the whole split button, the caret's grandparent.
    vi.spyOn(caret.parentElement!.parentElement as HTMLElement, 'getBoundingClientRect').mockImplementation(() => SPLIT)
    fireEvent.click(caret)
    return screen.getByRole('menu')
  }

  it('opens below the split button, capped to the room below it', () => {
    renderWithProviders(<BusyHost openBelow />)
    const menu = openMenu()
    expect(menu.style.bottom).toBe('')
    expect(menu.style.top).toBe(`${SPLIT.bottom + 8}px`)
    expect(menu.style.maxHeight).toBe(`${window.innerHeight - SPLIT.bottom - 16}px`)
    expect(menu.className).toContain('overflow-y-auto')
  })

  it.each([['off', false], ['left out', undefined]])('opens above the split button as on main with the prop %s', (_name, openBelow) => {
    renderWithProviders(<BusyHost openBelow={openBelow} />)
    const menu = openMenu()
    expect(menu.style.top).toBe('')
    expect(menu.style.maxHeight).toBe('')
    expect(menu.style.bottom).toBe(`${window.innerHeight - SPLIT.top + 8}px`)
    expect(menu.className).not.toContain('overflow-y-auto')
  })
})
