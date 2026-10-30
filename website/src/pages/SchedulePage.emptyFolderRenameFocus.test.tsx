/**
 * Schedule empty-folder chip rename: closing the inline editor never strands
 * focus on document.body.
 *
 * The chip name is a plain span, so the folder-actions menu trigger is the
 * only control that opens the rename and stays mounted through it. Enter and
 * Escape both return focus there. A pointer blur commits but moves focus
 * nowhere: redirecting focus off the element the user chose is the context
 * change WCAG 3.2.2 forbids.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { EmptyFolderChip } from './SchedulePage'

vi.mock('../api/client', () => ({ api: {} }))

const folder = { id: 'f1', name: 'zzq-alpha', order: 1 }

function setup() {
  const onRename = vi.fn()
  render(
    <>
      <EmptyFolderChip folder={folder} onRename={onRename} onDelete={vi.fn()} error={null} />
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </>,
  )
  return { onRename }
}

const trigger = () => screen.getByLabelText('Folder actions')
const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

/** The rename item defers setEditing by a macrotask so Radix can unmount first. */
async function startRename() {
  fireEvent.keyDown(trigger(), { key: 'Enter' })
  fireEvent.click(await screen.findByText('Rename'))
  const input = await screen.findByLabelText('Rename')
  await waitFor(() => expect(document.activeElement).toBe(input))
  return input
}

describe('EmptyFolderChip rename focus', () => {
  it('Enter commits once and returns focus to the folder-actions trigger', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-renamed' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(document.activeElement).toBe(trigger()))
    await nextFrame()
    expect(onRename).toHaveBeenCalledTimes(1)
    expect(onRename).toHaveBeenCalledWith('zzq-renamed')
  })

  it('Escape cancels and returns focus to the folder-actions trigger', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-discarded' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(document.activeElement).toBe(trigger()))
    await nextFrame()
    expect(onRename).not.toHaveBeenCalled()
  })

  it('a pointer blur commits but leaves focus where the user put it', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-blurred' } })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    await waitFor(() => expect(onRename).toHaveBeenCalledWith('zzq-blurred'))
    // The editor is closed and a frame has passed, so a stray restore would have run.
    expect(screen.queryByLabelText('Rename')).toBeNull()
    await nextFrame()
    expect(document.activeElement).toBe(elsewhere)
  })
})
