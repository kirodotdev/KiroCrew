/**
 * Cron folder delete confirmation: picking "Delete folder" from the menu, and
 * backing out of it, never strands focus on document.body.
 *
 * The menu skips Radix's focus restore (see `onCloseAutoFocus`), so the
 * confirmation must take focus itself, and Cancel hands it back to the
 * folder's collapse toggle. The Enter that picked the menu item may still be
 * held when focus lands on the destructive button, so a repeated Enter there
 * must not delete.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import CronFolderHeader from './CronFolderHeader'
import { Table, TableBody } from './ui/table'

const folder = { id: 'f1', name: 'zzq-alpha', order: 1 }

function setup() {
  const onDelete = vi.fn()
  render(
    <Table>
      <TableBody>
        <CronFolderHeader
          folder={folder}
          jobCount={3}
          collapsed={false}
          colSpan={4}
          onToggleCollapse={vi.fn()}
          onRename={vi.fn()}
          onDelete={onDelete}
        />
      </TableBody>
    </Table>,
  )
  return { onDelete }
}

async function pickDelete() {
  fireEvent.keyDown(screen.getByLabelText('Folder actions'), { key: 'Enter' })
  fireEvent.click(await screen.findByText('Delete folder'))
}

const confirmButton = () => screen.getByRole('button', { name: /delete .*zzq-alpha/i })
const toggle = () => screen.getByRole('button', { name: /collapse folder zzq-alpha/i })

describe('CronFolderHeader delete confirmation focus', () => {
  it('moves focus to the confirm button when the delete is picked', async () => {
    setup()
    await pickDelete()
    await waitFor(() => expect(document.activeElement).toBe(confirmButton()))
  })

  it('Cancel returns focus to the collapse toggle', async () => {
    setup()
    await pickDelete()
    await waitFor(() => expect(document.activeElement).toBe(confirmButton()))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(document.activeElement).toBe(toggle()))
  })

  it('ignores a repeated Enter or Space on the confirm button, but a fresh press deletes', async () => {
    const { onDelete } = setup()
    await pickDelete()
    await waitFor(() => expect(document.activeElement).toBe(confirmButton()))
    expect(fireEvent.keyDown(confirmButton(), { key: 'Enter', repeat: true })).toBe(false)
    expect(fireEvent.keyDown(confirmButton(), { key: ' ', repeat: true })).toBe(false)
    // A first (non-repeat) press is left to the browser.
    expect(fireEvent.keyDown(confirmButton(), { key: ' ' })).toBe(true)
    expect(onDelete).not.toHaveBeenCalled()
    fireEvent.click(confirmButton())
    expect(onDelete).toHaveBeenCalledTimes(1)
  })
})
