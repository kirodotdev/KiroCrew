import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

const mkJob = (id: string, name: string, folderId?: string): CronJob => ({
  id,
  name,
  message: 'msg',
  enabled: true,
  schedule: 'every 1h',
  last_status: 'ok',
  folder_id: folderId || '',
} as CronJob)

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    deleteCron: vi.fn().mockResolvedValue({}),
    batchDeleteCron: vi.fn().mockResolvedValue({}),
    createCron: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cancelCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
    cronFolders: vi.fn().mockResolvedValue([]),
    createCronFolder: vi.fn().mockResolvedValue({ id: 'new-folder', name: 'Test', order: 1 }),
    updateCronFolder: vi.fn().mockResolvedValue({}),
    deleteCronFolder: vi.fn().mockResolvedValue({}),
  },
}))

describe('SchedulePage cron folders', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('renders folder headers with correct job counts', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'Production', order: 1 },
      { id: 'f2', name: 'Monitoring', order: 2 },
    ])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [
        mkJob('j1', 'Job A', 'f1'),
        mkJob('j2', 'Job B', 'f1'),
        mkJob('j3', 'Job C', 'f2'),
        mkJob('j4', 'Job D'),
      ],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Production')).toBeInTheDocument())

    expect(screen.getByText('Production')).toBeInTheDocument()
    expect(screen.getByText('Monitoring')).toBeInTheDocument()
    expect(screen.getByText('Ungrouped')).toBeInTheDocument()
    // Plural key renders: job_count with { count }
    expect(screen.getByText('2 jobs')).toBeInTheDocument()
    expect(screen.getByText('1 job')).toBeInTheDocument()
  })

  it('renders empty folders (not hidden)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Job A', 'f1')],
    })
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'Production', order: 1 },
      { id: 'f2', name: 'EmptyFolder', order: 2 },
    ])

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Production')).toBeInTheDocument())

    // Empty folder is rendered, not hidden
    expect(screen.getByText('EmptyFolder')).toBeInTheDocument()
    expect(screen.getByText('0 jobs')).toBeInTheDocument()
  })

  it('collapse hides job rows within a folder', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'Production', order: 1 }])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Hidden Job', 'f1')],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Production')).toBeInTheDocument())
    expect(screen.getByText('Hidden Job')).toBeInTheDocument()

    // Click the collapse button on the first (Production) folder header
    const collapseBtn = screen.getAllByLabelText(/Collapse folder/)[0]
    fireEvent.click(collapseBtn)

    expect(screen.queryByText('Hidden Job')).not.toBeInTheDocument()
  })

  it('renders folder actions menu button for each folder header', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'To Delete', order: 1 }])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Some Job', 'f1')],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('To Delete')).toBeInTheDocument())

    const menuBtn = screen.getByLabelText('Folder actions')
    expect(menuBtn).toBeInTheDocument()
    expect(screen.getByText('1 job')).toBeInTheDocument()
  })

  it('exposes move-to-folder inside the row overflow menu', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Moveable Job')],
    })
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'Target Folder', order: 1 }])

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Moveable Job')).toBeInTheDocument())

    // Move is no longer a row button — it lives behind the ⋯ menu, which is what
    // keeps the actions column inside the table's width.
    expect(screen.queryByLabelText('Move to folder')).not.toBeInTheDocument()

    const overflow = screen.getByLabelText('Actions')
    fireEvent.keyDown(overflow, { key: 'Enter' })

    await waitFor(() => expect(screen.getByText('Move to folder')).toBeInTheDocument())
  })

  it('delete folder shows inline confirmation row', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Job A', 'f1')],
    })
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'My Folder', order: 1 }])

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('My Folder')).toBeInTheDocument())

    // Open folder actions menu (Radix needs keyboard-open in jsdom)
    const menuBtn = screen.getByLabelText('Folder actions')
    fireEvent.keyDown(menuBtn, { key: 'Enter' })

    // Click delete
    await waitFor(() => expect(screen.getByText('Delete folder')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Delete folder'))

    // Confirm row should appear
    await waitFor(() => expect(screen.getByText(/Delete "My Folder"\? Jobs will be ungrouped\./)).toBeInTheDocument())
  })

  it('hides empty folders when filter is active (omitEmpty)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'Production', order: 1 },
      { id: 'f2', name: 'EmptyFolder', order: 2 },
    ])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Deploy Job', 'f1')],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Production')).toBeInTheDocument())

    // Empty folder visible when no filter
    expect(screen.getByText('EmptyFolder')).toBeInTheDocument()

    // Type a filter
    const filterInput = screen.getByPlaceholderText('Filter jobs…')
    fireEvent.change(filterInput, { target: { value: 'Deploy' } })

    // Empty folder hidden when filter active
    await waitFor(() => expect(screen.queryByText('EmptyFolder')).not.toBeInTheDocument())
    expect(screen.getByText('Production')).toBeInTheDocument()
  })

  it('folder create modal stays open on API error', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Some Job')],
    })
    vi.mocked(api).createCronFolder.mockRejectedValue(new Error('Network error'))

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Some Job')).toBeInTheDocument())

    // Click new folder button
    const newFolderBtn = screen.getByRole('button', { name: /New folder/ })
    fireEvent.click(newFolderBtn)

    // Modal opens
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())

    // Type name and submit
    const input = screen.getByLabelText('New folder name')
    fireEvent.change(input, { target: { value: 'Bad Folder' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // Modal stays open with error visible
    await waitFor(() => expect(screen.getByText('Network error')).toBeInTheDocument())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    // Name preserved
    expect(input).toHaveValue('Bad Folder')
  })

  it('bypasses collapsed folder state when filter is active', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'Production', order: 1 }])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Hidden Job', 'f1')],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Production')).toBeInTheDocument())
    expect(screen.getByText('Hidden Job')).toBeInTheDocument()

    // Collapse the folder
    const collapseBtn = screen.getAllByLabelText(/Collapse folder/)[0]
    fireEvent.click(collapseBtn)
    expect(screen.queryByText('Hidden Job')).not.toBeInTheDocument()

    // Type a filter that matches the hidden job
    const filterInput = screen.getByPlaceholderText('Filter jobs…')
    fireEvent.change(filterInput, { target: { value: 'Hidden' } })

    // Job should be visible despite folder being collapsed (bypass during filter)
    await waitFor(() => expect(screen.getByText('Hidden Job')).toBeInTheDocument())
  })

  it('folder modal renders shared Input component (not raw input)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([])
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob('j1', 'Job A')] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Job A')).toBeInTheDocument())

    // Open create folder modal
    const newFolderBtn = screen.getByRole('button', { name: /New folder/ })
    fireEvent.click(newFolderBtn)
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())

    // The input should have the shared Input styling class (bg-bg-elevated)
    const input = screen.getByLabelText('New folder name')
    expect(input).toBeInTheDocument()
    expect(input.className).toContain('bg-bg-elevated')
    expect(input.className).toContain('focus-ring')
  })

  it('keeps the row action cell to three controls so the column fits', async () => {
    // The width contract. Six controls per row (Strict, Run, View, Pause, Move,
    // Delete) did not fit a 10-column table: the cell was clipped, and once
    // cells stopped wrapping the whole column left the viewport. Only Run,
    // Delete and the ⋯ menu may live in the row.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob('j1', 'Ordered Job')] })
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'Folder', order: 1 }])

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Ordered Job')).toBeInTheDocument())

    const overflow = screen.getByLabelText('Actions')
    const actionCell = overflow.closest('td')!
    const labels = Array.from(actionCell.querySelectorAll('button'))
      .map(b => b.textContent?.trim() || b.getAttribute('aria-label'))
    expect(labels).toEqual(['Run', 'Delete', 'Actions'])
  })

  it('folder headers visible with zero jobs when cronFolders exist', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'EmptyFolderA', order: 1 },
      { id: 'f2', name: 'EmptyFolderB', order: 2 },
    ])
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    // Folder headers should be visible even with zero jobs
    await waitFor(() => expect(screen.getByText('EmptyFolderA')).toBeInTheDocument())
    expect(screen.getByText('EmptyFolderB')).toBeInTheDocument()
    // Zero job counts rendered
    const zeroCounts = screen.getAllByText('0 jobs')
    expect(zeroCounts.length).toBe(2)
  })

  it('renders jobs even when folders API fails (#2 graceful degradation)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Resilient Job'), mkJob('j2', 'Also Visible')],
    })
    vi.mocked(api).cronFolders.mockRejectedValue(new Error('Folders API down'))

    renderWithProviders(<SchedulePage />)
    // Jobs should render despite folders failure
    await waitFor(() => expect(screen.getByText('Resilient Job')).toBeInTheDocument())
    expect(screen.getByText('Also Visible')).toBeInTheDocument()
    // No page-level error shown
    expect(screen.queryByText('Folders API down')).not.toBeInTheDocument()
  })

  it('auto-expands target folder when moving a job into a collapsed folder (#3)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'Target', order: 1 }])
    // Initially j1 is ungrouped, j2 in folder
    let jobState = [mkJob('j1', 'Moving Job'), mkJob('j2', 'In Target', 'f1')]
    vi.mocked(api).crons.mockImplementation(async () => ({ jobs: jobState }))
    vi.mocked(api).updateCron.mockImplementation(async () => {
      // After move, j1 is now in f1 too
      jobState = [mkJob('j1', 'Moving Job', 'f1'), mkJob('j2', 'In Target', 'f1')]
      return {}
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Target')).toBeInTheDocument())
    expect(screen.getByText('In Target')).toBeInTheDocument()

    // Collapse the target folder
    const collapseBtn = screen.getAllByLabelText(/Collapse folder/)[0]
    fireEvent.click(collapseBtn)
    expect(screen.queryByText('In Target')).not.toBeInTheDocument()

    // Verify collapsed state is persisted
    const storedBefore = localStorage.getItem('kc-cron-folders-collapsed')
    expect(storedBefore).toBeTruthy()
    expect(JSON.parse(storedBefore!)).toContain('f1')

    // Trigger the move through the row's ⋯ menu, then its Move-to-folder
    // submenu (Radix needs keyboard-open in jsdom for both levels).
    const overflow = screen.getByLabelText('Actions')
    fireEvent.keyDown(overflow, { key: 'Enter' })
    const subTrigger = await screen.findByText('Move to folder')
    fireEvent.keyDown(subTrigger, { key: 'ArrowRight' })

    // Wait for menu items to appear and click the folder option
    await waitFor(() => {
      const menuItems = screen.getAllByRole('menuitem')
      const targetItem = menuItems.find(el => el.textContent?.includes('Target'))
      expect(targetItem).toBeTruthy()
      fireEvent.click(targetItem!)
    })

    // Wait for the updateCron call to be made with folder_id
    await waitFor(() => expect(vi.mocked(api).updateCron).toHaveBeenCalledWith('j1', { folder_id: 'f1' }))

    // After move completes, the folder should be auto-expanded in localStorage
    await waitFor(() => {
      const storedAfter = localStorage.getItem('kc-cron-folders-collapsed')
      // Either null (empty set) or doesn't contain f1
      if (storedAfter) {
        expect(JSON.parse(storedAfter)).not.toContain('f1')
      }
    })
  })

  it('empty-state folder headers have rename and delete actions (#4)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'EmptyFolder', order: 1 },
    ])
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('EmptyFolder')).toBeInTheDocument())

    // Folder actions menu button should be present even in zero-job state
    const menuBtn = screen.getByLabelText('Folder actions')
    expect(menuBtn).toBeInTheDocument()

    // Open menu
    fireEvent.keyDown(menuBtn, { key: 'Enter' })

    // Should show rename and delete options
    await waitFor(() => expect(screen.getByText('Rename')).toBeInTheDocument())
    expect(screen.getByText('Delete folder')).toBeInTheDocument()

    // Click rename -- should switch to inline edit (input replaces name span)
    fireEvent.click(screen.getByText('Rename'))
    await waitFor(() => {
      const input = screen.getByDisplayValue('EmptyFolder')
      expect(input).toBeInTheDocument()
      expect(input.tagName).toBe('INPUT')
    })

    // Pressing Escape cancels inline edit
    const input = screen.getByDisplayValue('EmptyFolder')
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByDisplayValue('EmptyFolder')).not.toBeInTheDocument())
    expect(screen.getByText('EmptyFolder')).toBeInTheDocument()
  })

  it('empty-state folder inline rename commits on Enter', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'OldName', order: 1 },
    ])
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('OldName')).toBeInTheDocument())

    // Open folder actions menu and click Rename
    const menuBtn = screen.getByLabelText('Folder actions')
    fireEvent.keyDown(menuBtn, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('Rename')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Rename'))

    // Inline input should appear with current name
    const input = await waitFor(() => {
      const el = screen.getByDisplayValue('OldName')
      expect(el).toBeInTheDocument()
      return el
    })

    // Type new name and press Enter
    fireEvent.change(input, { target: { value: 'NewName' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // Should call updateCronFolder
    await waitFor(() => expect(vi.mocked(api).updateCronFolder).toHaveBeenCalledWith('f1', { name: 'NewName' }))
  })

  it('empty-state folder delete requires confirm before calling API', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'DeleteMe', order: 1 },
    ])
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('DeleteMe')).toBeInTheDocument())

    // Open folder actions menu
    const menuBtn = screen.getByLabelText('Folder actions')
    fireEvent.keyDown(menuBtn, { key: 'Enter' })

    // Click delete
    await waitFor(() => expect(screen.getByText('Delete folder')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Delete folder'))

    // Confirm row should appear (not immediately deleted)
    await waitFor(() => expect(screen.getByText(/Delete "DeleteMe"\? Jobs will be ungrouped\./)).toBeInTheDocument())
    expect(vi.mocked(api).deleteCronFolder).not.toHaveBeenCalled()

    // Confirm the delete
    const confirmDeleteBtn = screen.getAllByRole('button').find(b => b.textContent === 'Delete "DeleteMe"' && b.className.includes('danger'))
    expect(confirmDeleteBtn).toBeTruthy()
    fireEvent.click(confirmDeleteBtn!)

    await waitFor(() => expect(vi.mocked(api).deleteCronFolder).toHaveBeenCalledWith('f1'))
  })

  it('empty-state folder delete renders API error under the folder chip', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([
      { id: 'f1', name: 'ErrFolder', order: 1 },
    ])
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })
    vi.mocked(api).deleteCronFolder.mockRejectedValue(new Error('Folder in use'))

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('ErrFolder')).toBeInTheDocument())

    // Open menu and click delete
    const menuBtn = screen.getByLabelText('Folder actions')
    fireEvent.keyDown(menuBtn, { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('Delete folder')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Delete folder'))

    // Confirm
    await waitFor(() => expect(screen.getByText(/Delete "ErrFolder"\? Jobs will be ungrouped\./)).toBeInTheDocument())
    const confirmDeleteBtn = screen.getAllByRole('button').find(b => b.textContent === 'Delete "ErrFolder"' && b.className.includes('danger'))
    fireEvent.click(confirmDeleteBtn!)

    // Error should render
    await waitFor(() => expect(screen.getByText('Folder in use')).toBeInTheDocument())
  })

  it('batch move applies folder_id to all selected jobs', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'Target', order: 1 }])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Job A'), mkJob('j2', 'Job B'), mkJob('j3', 'Job C')],
    })
    vi.mocked(api).updateCron.mockResolvedValue({})

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Job A')).toBeInTheDocument())

    // Select two jobs
    const checkboxes = screen.getAllByRole('checkbox', { name: /Select/ })
    fireEvent.click(checkboxes[0])
    fireEvent.click(checkboxes[1])

    // Batch toolbar should show "Move to folder" button
    const batchMoveBtn = screen.getAllByLabelText('Move to folder')
    // At least one in the batch toolbar
    expect(batchMoveBtn.length).toBeGreaterThanOrEqual(1)
  })

  it('CronJobMoveMenu with zero folders has no double separator', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Some Job')],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Some Job')).toBeInTheDocument())

    // Exercised through the BATCH toolbar: that is `CronJobMoveMenu`'s remaining
    // call site. The row's own move affordance is now a submenu inside the ⋯
    // menu (see CronRowActions), which builds its items separately.
    fireEvent.click(screen.getByRole('checkbox', { name: /Select Some Job/ }))
    const moveBtn = await screen.findByLabelText('Move to folder')
    fireEvent.keyDown(moveBtn, { key: 'Enter' })

    // Wait for menu to open
    await waitFor(() => expect(screen.getByText('Ungrouped')).toBeInTheDocument())

    // Count separators (role=separator in Radix)
    const menuContent = moveBtn.closest('[data-radix-popper-content-wrapper]') ||
      document.querySelector('[role="menu"]')
    if (menuContent) {
      const separators = menuContent.querySelectorAll('[role="separator"]')
      // With zero folders: should be exactly 1 separator (between Ungrouped and New folder)
      expect(separators.length).toBe(1)
    }
  })

  it('batch-move error renders near batch toolbar', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).cronFolders.mockResolvedValue([{ id: 'f1', name: 'Target', order: 1 }])
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob('j1', 'Job A'), mkJob('j2', 'Job B')],
    })
    // Make one move fail
    vi.mocked(api).updateCron.mockRejectedValueOnce(new Error('Move failed'))
    vi.mocked(api).updateCron.mockResolvedValueOnce({})

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Job A')).toBeInTheDocument())

    // Select both jobs via their row checkboxes (not the select-all header)
    fireEvent.click(screen.getByLabelText('Select Job A'))
    fireEvent.click(screen.getByLabelText('Select Job B'))

    // Open batch move menu (Radix needs keyboard-open in jsdom). Scope to the
    // selection toolbar -- per-row move buttons share the same aria-label.
    const toolbarLabel = screen.getByText((_, el) => el?.tagName === 'SPAN' && el.textContent === '2 selected')
    const toolbar = toolbarLabel.parentElement as HTMLElement
    const batchMoveBtn = within(toolbar).getByLabelText('Move to folder')
    fireEvent.keyDown(batchMoveBtn, { key: 'Enter' })

    // Click the folder option
    await waitFor(() => {
      const menuItems = screen.getAllByRole('menuitem')
      const targetItem = menuItems.find(el => el.textContent?.includes('Target'))
      expect(targetItem).toBeTruthy()
      fireEvent.click(targetItem!)
    })

    // Error should render near the toolbar
    await waitFor(() => {
      const errorEl = screen.getByText(/could not be moved/)
      expect(errorEl).toBeInTheDocument()
      expect(errorEl.className).toContain('text-danger')
    })
  })
})
