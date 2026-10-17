import { createRef } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import Composer from '../apps/design-critique/Composer'
import type { Blocked, StagedItem } from '../apps/design-critique/types'

/**
 * The composer decides what gets critiqued: staged screenshots (whose ORDER is
 * the flow order) or one pasted reference. Two things it must never get wrong —
 * the start affordance has to be disabled until there is actually something to
 * critique, and a blocked run has to offer the specific way forward for its
 * cause rather than a generic retry.
 */
function staged(n: number): StagedItem[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `zzz-${i}`,
    file: new File(['zzz'], `zzz-${i}.png`, { type: 'image/png' }),
    url: `blob:zzz-${i}`,
  }))
}

function mount(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  const props = {
    staged: [],
    refText: '',
    dragging: false,
    blocked: null as Blocked | null,
    showAuth: false,
    busy: false,
    err: '',
    inputRef: createRef<HTMLInputElement>(),
    onPick: vi.fn(),
    onDrop: vi.fn(),
    onDragOver: vi.fn(),
    onDragLeave: vi.fn(),
    pickFile: vi.fn(),
    dropStaged: vi.fn(),
    moveStaged: vi.fn(),
    clearStaged: vi.fn(),
    start: vi.fn(),
    setRefText: vi.fn(),
    setBlocked: vi.fn(),
    setShowAuth: vi.fn(),
    onTryAgain: vi.fn(),
    ...overrides,
  }
  return { props, ...render(<Composer {...props} />) }
}

function startButton(): HTMLElement {
  return screen.getByRole('button', { name: /Critique/ })
}

describe('Composer empty state', () => {
  it('offers the drop tile and cannot start with nothing staged or typed', () => {
    const { props } = mount()
    expect(screen.getByText(/Drop screenshots/i)).toBeInTheDocument()
    expect(startButton()).toBeDisabled()
    expect(props.start).not.toHaveBeenCalled()
  })

  it('opens the file picker from the tile, by click and by keyboard', async () => {
    const { props } = mount()
    const tile = screen.getByText(/Drop screenshots/i).closest('[role="button"]')!
    await userEvent.click(tile)
    expect(props.pickFile).toHaveBeenCalledTimes(1)

    fireEvent.keyDown(tile, { key: 'Enter' })
    fireEvent.keyDown(tile, { key: ' ' })
    expect(props.pickFile).toHaveBeenCalledTimes(3)

    // An unrelated key must not fire it.
    fireEvent.keyDown(tile, { key: 'a' })
    expect(props.pickFile).toHaveBeenCalledTimes(3)
  })

  it('forwards drag-and-drop events to the owner', () => {
    const { props } = mount({ dragging: true })
    const region = screen.getByText(/Drop screenshots/i).closest('[role="button"]')!.parentElement!
    fireEvent.dragOver(region)
    fireEvent.dragLeave(region)
    fireEvent.drop(region)
    expect(props.onDragOver).toHaveBeenCalled()
    expect(props.onDragLeave).toHaveBeenCalled()
    expect(props.onDrop).toHaveBeenCalled()
  })
})

describe('Composer with a pasted reference', () => {
  it('says back what it recognised and enables the start action', async () => {
    const { props } = mount({ refText: 'https://github.com/zzzowner/zzzrepo' })
    expect(startButton()).toBeEnabled()
    await userEvent.click(startButton())
    expect(props.start).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/clone it/i)).toBeInTheDocument()
  })

  it('marks unrecognised input rather than pretending it will work', () => {
    mount({ refText: 'zzz not a link' })
    expect(screen.getByText(/Unrecognised/i)).toBeInTheDocument()
  })

  it('starts on Enter in the reference field, and reports typing', async () => {
    const { props } = mount({ refText: '' })
    const field = screen.getByRole('textbox')
    await userEvent.type(field, 'z')
    expect(props.setRefText).toHaveBeenCalled()
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(props.start).toHaveBeenCalledTimes(1)
  })

  it('shows an error line when the owner reports one', () => {
    mount({ err: 'zzz could not read that' })
    expect(screen.getByText('zzz could not read that')).toBeInTheDocument()
  })

  it('cannot start while busy', () => {
    mount({ refText: 'https://zzz.example.com', busy: true })
    expect(startButton()).toBeDisabled()
  })
})

describe('Composer with staged screens', () => {
  it('numbers a single screen without step chips or reordering', () => {
    mount({ staged: staged(1) })
    expect(screen.getByText(/add another to critique it as a flow/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Move earlier/i })).not.toBeInTheDocument()
    expect(startButton()).toBeEnabled()
  })

  it('shows the flow order and lets a screen be moved or removed', async () => {
    const { props } = mount({ staged: staged(3) })
    expect(screen.getByText(/this order is the flow order/i)).toBeInTheDocument()
    expect(screen.getByText('Step 1')).toBeInTheDocument()
    expect(screen.getByText('Step 3')).toBeInTheDocument()

    // The first screen cannot move earlier and the last cannot move later.
    const earlier = screen.getAllByRole('button', { name: /Move earlier/i })
    const later = screen.getAllByRole('button', { name: /Move later/i })
    expect(earlier[0]).toBeDisabled()
    expect(later[later.length - 1]).toBeDisabled()

    await userEvent.click(later[0])
    expect(props.moveStaged).toHaveBeenCalledWith(0, 1)
    await userEvent.click(earlier[1])
    expect(props.moveStaged).toHaveBeenCalledWith(1, -1)

    await userEvent.click(screen.getAllByRole('button', { name: /^Remove/ })[0])
    expect(props.dropStaged).toHaveBeenCalledWith(0)
  })

  it('adds more screens and clears them all', async () => {
    const { props } = mount({ staged: staged(2) })
    await userEvent.click(screen.getByRole('button', { name: /Add screens/i }))
    expect(props.pickFile).toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: /Clear all/i }))
    expect(props.clearStaged).toHaveBeenCalled()
  })

  it('disables the reference field while screenshots are staged', () => {
    mount({ staged: staged(1) })
    expect(screen.getByRole('textbox')).toBeDisabled()
  })
})

describe('Composer blocked screen', () => {
  const base: Blocked = { say: 'zzz blocked say', fix: 'shots', hint: 'zzz blocked hint' }

  it('names the cause and offers the screenshots route', async () => {
    const { props } = mount({ blocked: { ...base, detail: 'zzz detail' } })
    expect(screen.getByText('zzz blocked say')).toBeInTheDocument()
    expect(screen.getByText('zzz blocked hint')).toBeInTheDocument()
    expect(screen.getByText('zzz detail')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /Send screenshots/i }))
    expect(props.setBlocked).toHaveBeenCalledWith(null)
  })

  it('offers a local folder for a no-access repo, seeding the field', async () => {
    const { props } = mount({ blocked: { ...base, fix: 'local' } })
    await userEvent.click(screen.getByRole('button', { name: /local folder instead/i }))
    expect(props.setBlocked).toHaveBeenCalledWith(null)
    expect(props.setRefText).toHaveBeenCalledWith('/')
  })

  it('offers a retype for a bad link, and no retry', async () => {
    const { props } = mount({ blocked: { ...base, fix: 'retype' } })
    expect(screen.queryByRole('button', { name: /Try again/i })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Fix the link/i }))
    expect(props.setBlocked).toHaveBeenCalledWith(null)
  })

  it('offers a retry for a transient failure and clears the auth panel with it', async () => {
    const { props } = mount({ blocked: { ...base, fix: 'retry' } })
    await userEvent.click(screen.getByRole('button', { name: /Try again/i }))
    expect(props.setBlocked).toHaveBeenCalledWith(null)
    expect(props.setShowAuth).toHaveBeenCalled()
    expect(props.onTryAgain).toHaveBeenCalledTimes(1)
  })

  it('toggles the access steps, showing the commands verbatim when open', async () => {
    const auth = { lead: 'zzz lead', cmds: ['zzz-cmd-one', 'zzz-cmd-two'], tail: 'zzz tail' }
    const { props, unmount } = mount({ blocked: { ...base, auth } })
    expect(screen.queryByText(/zzz-cmd-one/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Fix my access/i }))
    expect(props.setShowAuth).toHaveBeenCalled()
    unmount()

    const { container } = mount({ blocked: { ...base, auth }, showAuth: true })
    // One copy-paste block, newline-joined and byte-exact — a reflowed command
    // does not run.
    expect(container.querySelector('pre')?.textContent).toBe('zzz-cmd-one\nzzz-cmd-two')
    expect(screen.getByText('zzz lead')).toBeInTheDocument()
    expect(screen.getByText('zzz tail')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Hide access steps/i })).toBeInTheDocument()
  })
})
