import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import CronFolderHeader from './CronFolderHeader'
import { Table, TableBody } from './ui/table'

const folder = { id: 'f1', name: 'zzq-alpha', order: 1 }

function setup(over: Partial<Parameters<typeof CronFolderHeader>[0]> = {}) {
  const spies = {
    onToggleCollapse: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
  }
  const view = render(
    <Table>
      <TableBody>
        <CronFolderHeader
          folder={folder}
          jobCount={3}
          collapsed={false}
          colSpan={4}
          {...spies}
          {...over}
        />
      </TableBody>
    </Table>,
  )
  return { ...spies, ...view }
}

/** The rename item defers setEditing by a macrotask so Radix can unmount first. */
async function startRename() {
  fireEvent.keyDown(screen.getByLabelText('Folder actions'), { key: 'Enter' })
  fireEvent.click(await screen.findByText('Rename'))
  return await screen.findByLabelText('Rename')
}

describe('CronFolderHeader', () => {
  it('shows the folder name, job count and an expanded chevron', () => {
    setup()
    const toggle = screen.getByRole('button', { name: /collapse folder zzq-alpha/i })
    expect(toggle).toBeInTheDocument()
    expect(toggle.querySelector('svg')!.getAttribute('class')).toContain('rotate-90')
    expect(screen.getByText(/3/)).toBeInTheDocument()
  })

  it('labels the toggle for expanding when collapsed', () => {
    setup({ collapsed: true })
    const toggle = screen.getByRole('button', { name: /expand folder zzq-alpha/i })
    expect(toggle.querySelector('svg')!.getAttribute('class')).not.toContain('rotate-90')
  })

  it('clicking the name row toggles collapse', () => {
    const { onToggleCollapse } = setup()
    fireEvent.click(screen.getByRole('button', { name: /collapse folder zzq-alpha/i }))
    expect(onToggleCollapse).toHaveBeenCalledTimes(1)
  })

  it('Enter commits a changed rename', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: '  zzq-renamed  ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(onRename).toHaveBeenCalledWith('zzq-renamed'))
    // Editing closes: the collapse toggle is back.
    expect(screen.getByRole('button', { name: /collapse folder/i })).toBeInTheDocument()
  })

  it('an unchanged or blank name commits nothing', async () => {
    const { onRename } = setup()
    let input = await startRename()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onRename).not.toHaveBeenCalled()

    input = await startRename()
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onRename).not.toHaveBeenCalled()
  })

  it('Escape abandons the rename without committing', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-discarded' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /collapse folder/i })).toBeInTheDocument())
    expect(onRename).not.toHaveBeenCalled()
  })

  it('blurring the input commits the rename', async () => {
    const { onRename } = setup()
    const input = await startRename()
    fireEvent.change(input, { target: { value: 'zzq-blurred' } })
    fireEvent.blur(input)
    await waitFor(() => expect(onRename).toHaveBeenCalledWith('zzq-blurred'))
  })

  it('delete arms a confirm row, and Cancel disarms it', async () => {
    const { onDelete } = setup()
    fireEvent.keyDown(screen.getByLabelText('Folder actions'), { key: 'Enter' })
    fireEvent.click(await screen.findByText('Delete folder'))

    const cancel = await screen.findByRole('button', { name: 'Cancel' })
    expect(onDelete).not.toHaveBeenCalled()
    await act(async () => { fireEvent.click(cancel) })
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument())
  })

  it('confirming the delete calls onDelete and disarms', async () => {
    const { onDelete } = setup()
    fireEvent.keyDown(screen.getByLabelText('Folder actions'), { key: 'Enter' })
    fireEvent.click(await screen.findByText('Delete folder'))

    const confirm = await screen.findByRole('button', { name: /^Delete "zzq-alpha"$/ })
    await act(async () => { fireEvent.click(confirm) })
    expect(onDelete).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument())
  })
})
