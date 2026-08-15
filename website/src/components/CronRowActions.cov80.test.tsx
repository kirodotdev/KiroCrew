import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import CronRowActions from './CronRowActions'
import type { CronJob } from '../types'
import type { CronFolder } from '../utils/cronFolders'

const FOLDERS: CronFolder[] = [
  { id: 'f2', name: 'zzq-beta', order: 2 },
  { id: 'f1', name: 'zzq-alpha', order: 1 },
]

function job(over: Partial<CronJob> = {}): CronJob {
  return {
    id: 'j1',
    name: 'zzq-job',
    message: 'zzq-msg',
    enabled: true,
    schedule: 'zzq-sched',
    last_status: 'ok',
    ...over,
  }
}

function setup(over: Partial<CronJob> = {}, handlers: Record<string, unknown> = {}) {
  const spies = {
    onRun: vi.fn(),
    onCancelRun: vi.fn(),
    onOpenInChat: vi.fn(),
    onToggleEnabled: vi.fn(),
    onToggleStrict: vi.fn(),
    onMove: vi.fn(),
    onNewFolder: vi.fn(),
    ...handlers,
  }
  render(
    <CronRowActions
      job={job(over)}
      folders={FOLDERS}
      running={false}
      cancelling={false}
      {...(spies as never)}
    />,
  )
  fireEvent.keyDown(screen.getByLabelText('Actions'), { key: 'Enter' })
  return spies
}

function item(label: string | RegExp) {
  return screen.getByText(label).closest('[role="menuitem"]')!
}

async function openMoveSubmenu() {
  const trigger = (await screen.findByText('Move to folder')).closest('[role="menuitem"]')!
  fireEvent.keyDown(trigger, { key: 'Enter' })
  await screen.findByText('Ungrouped')
}

describe('CronRowActions', () => {
  it('an idle enabled job offers Run now and Pause', async () => {
    const spies = setup()
    fireEvent.click(await screen.findByText('Run Now'))
    await waitFor(() => expect(spies.onRun).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('Cancel running execution')).not.toBeInTheDocument()
  })

  it('a disabled job cannot be run from the menu', async () => {
    setup({ enabled: false })
    await screen.findByText('Run Now')
    expect(item('Run Now')).toHaveAttribute('data-disabled')
    // A paused job offers Resume instead of Pause.
    expect(screen.getByText('Resume')).toBeInTheDocument()
  })

  it('a running job offers Cancel running execution instead', async () => {
    const spies = setup({ is_running: true })
    fireEvent.click(await screen.findByText('Cancel running execution'))
    await waitFor(() => expect(spies.onCancelRun).toHaveBeenCalledTimes(1))
  })

  it('Pause toggles enabled', async () => {
    const spies = setup()
    fireEvent.click(await screen.findByText('Pause'))
    await waitFor(() => expect(spies.onToggleEnabled).toHaveBeenCalledTimes(1))
  })

  it('the result row is disabled with no result, and says Continue session with a slot', async () => {
    const { unmount } = render(
      <CronRowActions
        job={job()}
        folders={FOLDERS}
        running={false}
        cancelling={false}
        onRun={vi.fn()}
        onCancelRun={vi.fn()}
        onOpenInChat={vi.fn()}
        onToggleEnabled={vi.fn()}
        onToggleStrict={vi.fn()}
        onMove={vi.fn()}
        onNewFolder={vi.fn()}
      />,
    )
    fireEvent.keyDown(screen.getByLabelText('Actions'), { key: 'Enter' })
    await screen.findByText('View last result')
    expect(item('View last result')).toHaveAttribute('data-disabled')
    unmount()

    const spies = setup({ has_slot: true })
    fireEvent.click(await screen.findByText('Continue session'))
    await waitFor(() => expect(spies.onOpenInChat).toHaveBeenCalledTimes(1))
  })

  it('Strict shows a check only when the job is strict, and toggles', async () => {
    const spies = setup({ strict_schedule: true })
    await screen.findByText('Strict')
    expect(item('Strict').querySelector('.ml-auto')).toBeTruthy()
    fireEvent.click(screen.getByText('Strict'))
    await waitFor(() => expect(spies.onToggleStrict).toHaveBeenCalledTimes(1))
  })

  it('the folder submenu lists folders in order and checks the current one', async () => {
    setup({ folder_id: 'f1' })
    await openMoveSubmenu()
    const labels = Array.from(document.querySelectorAll('[role="menuitem"] span.truncate'))
      .map(el => el.textContent)
    expect(labels).toEqual(['zzq-alpha', 'zzq-beta'])
    expect(item('zzq-alpha').querySelector('.ml-auto')).toBeTruthy()
    expect(item('Ungrouped').querySelector('.ml-auto')).toBeNull()
  })

  it('the folder submenu moves to Ungrouped and to a named folder', async () => {
    const spies = setup()
    await openMoveSubmenu()
    expect(item('Ungrouped').querySelector('.ml-auto')).toBeTruthy()
    fireEvent.click(screen.getByText('zzq-beta'))
    await waitFor(() => expect(spies.onMove).toHaveBeenCalledWith('f2'))
  })

  it('the folder submenu can move a job out to Ungrouped', async () => {
    const spies = setup({ folder_id: 'f2' })
    await openMoveSubmenu()
    fireEvent.click(screen.getByText('Ungrouped'))
    await waitFor(() => expect(spies.onMove).toHaveBeenCalledWith(''))
  })

  it('New folder in the submenu moves the job into what it created', async () => {
    const onNewFolder = vi.fn().mockResolvedValue('f-new')
    const spies = setup({}, { onNewFolder })
    await openMoveSubmenu()
    fireEvent.click(screen.getByText('New folder'))
    await waitFor(() => expect(onNewFolder).toHaveBeenCalledWith(true))
    await waitFor(() => expect(spies.onMove).toHaveBeenCalledWith('f-new'))
  })

  it('an abandoned New folder leaves the job where it was', async () => {
    const onNewFolder = vi.fn().mockResolvedValue(undefined)
    const spies = setup({}, { onNewFolder })
    await openMoveSubmenu()
    fireEvent.click(screen.getByText('New folder'))
    await waitFor(() => expect(onNewFolder).toHaveBeenCalled())
    expect(spies.onMove).not.toHaveBeenCalled()
  })

  it('with no folders the submenu is just Ungrouped plus New folder', async () => {
    render(
      <CronRowActions
        job={job()}
        folders={[]}
        running={false}
        cancelling={false}
        onRun={vi.fn()}
        onCancelRun={vi.fn()}
        onOpenInChat={vi.fn()}
        onToggleEnabled={vi.fn()}
        onToggleStrict={vi.fn()}
        onMove={vi.fn()}
        onNewFolder={vi.fn()}
      />,
    )
    fireEvent.keyDown(screen.getByLabelText('Actions'), { key: 'Enter' })
    await openMoveSubmenu()
    expect(document.querySelectorAll('[role="menuitem"] span.truncate')).toHaveLength(0)
    expect(screen.getByText('New folder')).toBeInTheDocument()
  })
})
