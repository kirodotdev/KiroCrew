/**
 * Schedule empty-folder chip delete confirmation: Cancel never strands focus
 * on document.body.
 *
 * Picking "Delete folder" keeps Radix's focus restore, so focus is on the
 * folder-actions trigger while the confirmation shows. Cancel unmounts the
 * row and the focused Cancel button with it, so it hands focus back to that
 * trigger.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { EmptyFolderChip } from './SchedulePage'

vi.mock('../api/client', () => ({ api: {} }))

const folder = { id: 'f1', name: 'zzq-alpha', order: 1 }

const trigger = () => screen.getByLabelText('Folder actions')

async function openConfirm() {
  fireEvent.keyDown(trigger(), { key: 'Enter' })
  fireEvent.click(await screen.findByText('Delete folder'))
  return screen.findByRole('button', { name: 'Cancel' })
}

describe('EmptyFolderChip delete confirmation focus', () => {
  it('Cancel closes the confirmation and returns focus to the folder-actions trigger', async () => {
    const onDelete = vi.fn()
    render(<EmptyFolderChip folder={folder} onRename={vi.fn()} onDelete={onDelete} error={null} />)
    const cancel = await openConfirm()
    cancel.focus()
    fireEvent.click(cancel)
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(trigger()))
    expect(onDelete).not.toHaveBeenCalled()
  })
})
