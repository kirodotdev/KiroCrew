// Inline rename of a meeting: Enter or blur saves, Escape cancels, and a failed
// save keeps the typed text with an error.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'

import MeetingTitle from '../apps/meetings/components/MeetingTitle'

afterEach(cleanup)

function openEditor(onRename = vi.fn().mockResolvedValue(undefined)) {
  render(<MeetingTitle title="Standup" onRename={onRename} />)
  fireEvent.click(screen.getByRole('button', { name: 'Standup' }))
  return { onRename, input: screen.getByRole('textbox', { name: 'Meeting title' }) as HTMLInputElement }
}

describe('MeetingTitle', () => {
  it('saves the trimmed title on Enter and closes the editor', async () => {
    const { onRename, input } = openEditor()
    fireEvent.change(input, { target: { value: '  Retro  ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onRename).toHaveBeenCalledWith('Retro')
    await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull())
  })

  it('saves on blur', () => {
    const { onRename, input } = openEditor()
    fireEvent.change(input, { target: { value: 'Retro' } })
    fireEvent.blur(input)
    expect(onRename).toHaveBeenCalledWith('Retro')
  })

  it('cancels on Escape without saving', () => {
    const { onRename, input } = openEditor()
    fireEvent.change(input, { target: { value: 'Retro' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onRename).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Standup' })).toBeInTheDocument()
  })

  it('keeps a draft typed while the save was in flight and sends no second save meanwhile', async () => {
    let resolve = () => {}
    const { onRename, input } = openEditor(vi.fn(() => new Promise<void>(r => { resolve = r })))
    fireEvent.change(input, { target: { value: 'Retro' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.change(input, { target: { value: 'Retro 2' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onRename).toHaveBeenCalledTimes(1)
    await act(async () => { resolve() })
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Retro 2')
  })

  it('treats an emptied title as a cancel', () => {
    const { onRename, input } = openEditor()
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.blur(input)
    expect(onRename).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Standup' })).toBeInTheDocument()
  })

  it('keeps the draft and shows an error when the save fails', async () => {
    const { input } = openEditor(vi.fn().mockRejectedValue(new Error('400')))
    fireEvent.change(input, { target: { value: 'Retro' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByText('Could not rename the meeting.')).toBeInTheDocument()
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('Retro')
  })

  it('shows the fallback when the meeting has no title', () => {
    render(<MeetingTitle title="" onRename={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Untitled meeting' })).toBeInTheDocument()
  })

  // Closing the editor unmounts the focused input, and a browser then drops
  // focus to document.body: Enter and Escape must hand it back to the title
  // trigger so a keyboard user keeps their place (ARIA edit-in-place). A
  // pointer blur saves but moves nothing. The restore runs in a rAF, so the
  // assertions flush one frame first.
  const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

  it('returns focus to the trigger after a successful save on Enter', async () => {
    const { input } = openEditor()
    fireEvent.change(input, { target: { value: 'Retro' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // onRename resolves without changing the title prop, so the closed trigger
    // keeps its original accessible name; wait for the editor to unmount.
    await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull())
    const trigger = screen.getByRole('button', { name: 'Standup' })
    await nextFrame()
    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('returns focus to the trigger when Enter closes an unchanged draft', async () => {
    const { onRename, input } = openEditor()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onRename).not.toHaveBeenCalled()
    const trigger = await screen.findByRole('button', { name: 'Standup' })
    await nextFrame()
    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('returns focus to the trigger after Escape cancels', async () => {
    const { input } = openEditor()
    fireEvent.change(input, { target: { value: 'Retro' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    const trigger = await screen.findByRole('button', { name: 'Standup' })
    await nextFrame()
    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('saves on a pointer blur but does not move focus to the trigger', async () => {
    const { onRename, input } = openEditor()
    fireEvent.change(input, { target: { value: 'Retro' } })
    fireEvent.blur(input)
    expect(onRename).toHaveBeenCalledWith('Retro')
    await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull())
    const trigger = screen.getByRole('button', { name: 'Standup' })
    await nextFrame()
    expect(document.activeElement).not.toBe(trigger)
  })
})
