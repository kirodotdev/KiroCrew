/**
 * Cron folder rename: closing the inline editor never strands focus on
 * document.body.
 *
 * Enter and Escape both return focus to the folder's collapse toggle, the
 * element the editor stood in for. A pointer blur commits but moves focus
 * nowhere: redirecting focus off the element the user chose is the context
 * change WCAG 3.2.2 forbids.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import CronFolderHeader from './CronFolderHeader'
import { Table, TableBody } from './ui/table'

const folder = { id: 'f1', name: 'zzq-alpha', order: 1 }

function setup() {
  const onRename = vi.fn()
  render(
    <>
      <Table>
        <TableBody>
          <CronFolderHeader
            folder={folder}
            jobCount={3}
            collapsed={false}
            colSpan={4}
            onToggleCollapse={vi.fn()}
            onRename={onRename}
            onDelete={vi.fn()}
          />
        </TableBody>
      </Table>
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </>,
  )
  return { onRename }
}

/** The rename item defers setEditing by a macrotask so Radix can unmount first. */
async function startRename() {
  fireEvent.keyDown(screen.getByLabelText('Folder actions'), { key: 'Enter' })
  fireEvent.click(await screen.findByText('Rename'))
  const input = await screen.findByLabelText('Rename')
  await waitFor(() => expect(document.activeElement).toBe(input))
  return input
}

const toggle = () => screen.getByRole('button', { name: /collapse folder zzq-alpha/i })
const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

describe('CronFolderHeader rename focus', () => {
  it('Enter commits and returns focus to the collapse toggle', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-renamed' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(document.activeElement).toBe(toggle()))
    expect(onRename).toHaveBeenCalledTimes(1)
    expect(onRename).toHaveBeenCalledWith('zzq-renamed')
  })

  it('Escape cancels and returns focus to the collapse toggle', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-discarded' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(document.activeElement).toBe(toggle()))
    expect(onRename).not.toHaveBeenCalled()
  })

  it('a pointer blur commits but leaves focus where the user put it', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-blurred' } })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    await waitFor(() => expect(onRename).toHaveBeenCalledWith('zzq-blurred'))
    // The toggle is back, and a frame has passed, so a stray restore would have run.
    expect(toggle()).toBeInTheDocument()
    await nextFrame()
    expect(document.activeElement).toBe(elsewhere)
  })
})
