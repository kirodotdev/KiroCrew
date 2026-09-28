import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import CronJobMoveMenu from './CronJobMoveMenu'
import type { CronFolder } from '../utils/cronFolders'

const FOLDERS: CronFolder[] = [
  { id: 'f2', name: 'zzq-beta', order: 2 },
  { id: 'f1', name: 'zzq-alpha', order: 1 },
]

function openMenu() {
  fireEvent.keyDown(screen.getByLabelText('Move to folder'), { key: 'Enter' })
}

function item(label: string | RegExp) {
  return screen.getByText(label).closest('[role="menuitem"]')!
}

describe('CronJobMoveMenu', () => {
  it('lists folders in order and checks the current one', async () => {
    render(
      <CronJobMoveMenu
        folders={FOLDERS}
        currentFolderId="f1"
        onMove={vi.fn()}
        onNewFolder={vi.fn()}
      />,
    )
    openMenu()
    await screen.findByText('zzq-alpha')

    const labels = Array.from(document.querySelectorAll('[role="menuitem"] span'))
      .map(el => el.textContent)
    expect(labels.slice(0, 3)).toEqual(['Ungrouped', 'zzq-alpha', 'zzq-beta'])
    // The check mark sits on the current folder's row, not on Ungrouped.
    expect(item('zzq-alpha').querySelector('.ml-auto')).toBeTruthy()
    expect(item('Ungrouped').querySelector('.ml-auto')).toBeNull()
  })

  it('checks Ungrouped when the job has no folder', async () => {
    render(<CronJobMoveMenu folders={FOLDERS} onMove={vi.fn()} onNewFolder={vi.fn()} />)
    openMenu()
    await screen.findByText('Ungrouped')
    expect(item('Ungrouped').querySelector('.ml-auto')).toBeTruthy()
  })

  it('picking Ungrouped moves to the empty folder id', async () => {
    const onMove = vi.fn()
    render(
      <CronJobMoveMenu folders={FOLDERS} currentFolderId="f1" onMove={onMove} onNewFolder={vi.fn()} />,
    )
    openMenu()
    fireEvent.click(await screen.findByText('Ungrouped'))
    await waitFor(() => expect(onMove).toHaveBeenCalledWith(''))
  })

  it('picking a folder moves to its id', async () => {
    const onMove = vi.fn()
    render(<CronJobMoveMenu folders={FOLDERS} onMove={onMove} onNewFolder={vi.fn()} />)
    openMenu()
    fireEvent.click(await screen.findByText('zzq-beta'))
    await waitFor(() => expect(onMove).toHaveBeenCalledWith('f2'))
  })

  it('renders no folder rows at all when the list is empty', async () => {
    render(<CronJobMoveMenu folders={[]} onMove={vi.fn()} onNewFolder={vi.fn()} />)
    openMenu()
    await screen.findByText('Ungrouped')
    const labels = Array.from(document.querySelectorAll('[role="menuitem"] span'))
      .map(el => el.textContent)
    expect(labels).toEqual(['Ungrouped', 'New folder'])
  })

  it('New folder moves the job into the folder it created', async () => {
    const onMove = vi.fn()
    const onNewFolder = vi.fn().mockResolvedValue('f-new')
    render(<CronJobMoveMenu folders={FOLDERS} onMove={onMove} onNewFolder={onNewFolder} />)
    openMenu()
    fireEvent.click(await screen.findByText('New folder'))
    await waitFor(() => expect(onNewFolder).toHaveBeenCalledWith(true))
    await waitFor(() => expect(onMove).toHaveBeenCalledWith('f-new'))
  })

  it('an abandoned New folder does not move the job', async () => {
    const onMove = vi.fn()
    const onNewFolder = vi.fn().mockResolvedValue(undefined)
    render(<CronJobMoveMenu folders={FOLDERS} onMove={onMove} onNewFolder={onNewFolder} />)
    openMenu()
    fireEvent.click(await screen.findByText('New folder'))
    await waitFor(() => expect(onNewFolder).toHaveBeenCalled())
    expect(onMove).not.toHaveBeenCalled()
  })
})
