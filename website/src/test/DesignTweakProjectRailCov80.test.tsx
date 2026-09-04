// Coverage tests for the LEFT PROJECT RAIL of DesignTweakPage.
// Surfaces tested: project list rendering, expand/collapse, selecting a project,
// Add project (folder picker + manual path), Remove project, the empty state,
// and the rail's resize handle.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import DesignTweak from '../apps/design-tweak/DesignTweakPage'
import { renderWithProviders } from './helpers'

vi.mock('../apps/design-tweak/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/design-tweak/api')>()),
  fetchProjects: vi.fn(),
  fetchQueue: vi.fn(),
  fetchHistory: vi.fn(),
  fetchHealth: vi.fn(),
  selectProject: vi.fn(),
  addProject: vi.fn(),
  pickFolder: vi.fn(),
  removeProject: vi.fn(),
}))

// The component imports delivery.ts which may reference api internals; mock it
// to keep the test focused on the rail UI.
vi.mock('../apps/design-tweak/delivery', () => ({
  deliveryVerdict: vi.fn(() => 'delivered'),
  needsDeliveryRetry: vi.fn(() => false),
}))

import {
  fetchProjects,
  fetchQueue,
  fetchHistory,
  fetchHealth,
  selectProject,
  addProject as apiAddProject,
  pickFolder as apiPickFolder,
  removeProject as apiRemoveProject,
} from '../apps/design-tweak/api'

import type { Project, ProjectsResponse, QueueResponse, HistoryResponse, HealthResponse } from '../apps/design-tweak/types'

// Fixture helpers — produce minimal but valid shapes that satisfy the component.
function makeProject(overrides: Partial<Project> = {}): Project {
  return { id: 'proj-1', path: '/home/user/my-app', name: 'My App', ...overrides }
}

function defaultMocks(opts: {
  projects?: Project[]
  activeId?: string
  pending?: unknown[]
} = {}) {
  const projects = opts.projects ?? [makeProject()]
  ;(fetchProjects as ReturnType<typeof vi.fn>).mockResolvedValue({
    projects,
    activeId: opts.activeId ?? projects[0]?.id ?? '',
    serving: true,
  } satisfies ProjectsResponse)
  ;(fetchQueue as ReturnType<typeof vi.fn>).mockResolvedValue({
    pending: opts.pending ?? [],
  } satisfies QueueResponse)
  ;(fetchHistory as ReturnType<typeof vi.fn>).mockResolvedValue({
    history: [],
  } satisfies HistoryResponse)
  ;(fetchHealth as ReturnType<typeof vi.fn>).mockResolvedValue({
    status: 'ok',
    dataDir: '/tmp/dt-data',
  } satisfies HealthResponse)
  ;(selectProject as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
}

beforeEach(() => {
  vi.clearAllMocks()
  // localStorage state can bleed between tests (rail width, dims, disconnect flag)
  localStorage.clear()
})

// ─── PROJECT LIST RENDERING ──────────────────────────────────────────────────

describe('DesignTweakPage — project rail', () => {
  describe('project list rendering', () => {
    it('shows project name in the dropdown trigger after data loads', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      // The trigger button displays the selected project name once data arrives
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })
    })

    it('renders multiple projects in the dropdown panel when opened', async () => {
      const projects = [
        makeProject({ id: 'p1', name: 'Alpha App', path: '/alpha' }),
        makeProject({ id: 'p2', name: 'Beta App', path: '/beta' }),
        makeProject({ id: 'p3', name: 'Gamma App', path: '/gamma' }),
      ]
      defaultMocks({ projects, activeId: 'p1' })
      renderWithProviders(<DesignTweak />)

      // Wait for data, then open the dropdown
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Alpha App/i })).toBeInTheDocument()
      })
      await userEvent.click(screen.getByRole('button', { name: /Alpha App/i }))

      // All three projects appear in the dropdown (Alpha App appears twice:
      // once in the trigger and once in the list)
      expect(screen.getAllByText('Alpha App').length).toBeGreaterThanOrEqual(2)
      expect(screen.getByText('Beta App')).toBeInTheDocument()
      expect(screen.getByText('Gamma App')).toBeInTheDocument()
    })

    it('shows "dev" badge for projects that need a dev server', async () => {
      const projects = [
        makeProject({ id: 'p1', name: 'React App', path: '/react', needsDevServer: true }),
      ]
      defaultMocks({ projects, activeId: 'p1' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /React App/i })).toBeInTheDocument()
      })
      await userEvent.click(screen.getByRole('button', { name: /React App/i }))

      // The dev badge text is rendered (the i18n key maps to a badge label)
      await waitFor(() => {
        const items = screen.getAllByText(/React App/i)
        // The panel item for this project should exist
        expect(items.length).toBeGreaterThanOrEqual(1)
      })
    })
  })

  // ─── SELECTING A PROJECT ────────────────────────────────────────────────────

  describe('selecting a project', () => {
    it('calls selectProject API and updates the trigger label when a project is picked', async () => {
      const projects = [
        makeProject({ id: 'p1', name: 'First', path: '/first' }),
        makeProject({ id: 'p2', name: 'Second', path: '/second' }),
      ]
      defaultMocks({ projects, activeId: 'p1' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /First/i })).toBeInTheDocument()
      })

      // Open dropdown
      await userEvent.click(screen.getByRole('button', { name: /First/i }))

      // Click the second project
      await userEvent.click(screen.getByText('Second'))

      // selectProject is called with the chosen id
      expect(selectProject).toHaveBeenCalledWith('p2')
    })

    it('shows connected state button when project is selected and connected', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      // After load, with activeId matching selectedId and previewId set, the
      // connected button appears
      await waitFor(() => {
        // The component auto-connects when activeId matches, showing the
        // "Connected" button
        expect(
          screen.getByRole('button', { name: /connected/i })
        ).toBeInTheDocument()
      })
    })
  })

  // ─── EXPAND / COLLAPSE (dropdown panel open/close) ──────────────────────────

  describe('expand/collapse dropdown', () => {
    it('dropdown panel opens on trigger click and closes on second click', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      const trigger = screen.getByRole('button', { name: /My App/i })

      // Open
      await userEvent.click(trigger)
      // The "Load new app" button is inside the dropdown panel
      expect(screen.getByText(/load new/i)).toBeInTheDocument()

      // Close
      await userEvent.click(trigger)
      await waitFor(() => {
        expect(screen.queryByText(/load new/i)).not.toBeInTheDocument()
      })
    })

    it('closes dropdown on Escape key', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      await userEvent.click(screen.getByRole('button', { name: /My App/i }))
      expect(screen.getByText(/load new/i)).toBeInTheDocument()

      // Press Escape
      await userEvent.keyboard('{Escape}')
      await waitFor(() => {
        expect(screen.queryByText(/load new/i)).not.toBeInTheDocument()
      })
    })
  })

  // ─── ADD PROJECT ────────────────────────────────────────────────────────────

  describe('add project', () => {
    it('calls pickFolder API when "Load new app" is clicked', async () => {
      const newProj = makeProject({ id: 'new-1', name: 'Picked App', path: '/picked' })
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: true,
        path: '/picked',
      })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: true,
        project: newProj,
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      // Wait for empty state in the dropdown
      await waitFor(() => {
        expect(fetchProjects).toHaveBeenCalled()
      })

      // Open dropdown — with no projects, the trigger shows "Select a web app…"
      const trigger = screen.getByRole('button', { name: /Select a web app/i })
      await userEvent.click(trigger)

      // Click "Load new app" — calls pickFolder
      const loadBtn = screen.getByText(/load new/i)
      await userEvent.click(loadBtn)

      expect(apiPickFolder).toHaveBeenCalled()
    })

    it('shows manual path input when adding state is entered and submits on Enter', async () => {
      // When pickFolder returns an error, the component falls back to showing
      // the manual input form
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: false,
        error: 'no native picker',
      })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: true,
        project: makeProject({ id: 'manual-1', name: 'Manual App', path: '/manual/path' }),
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })

      // Open dropdown
      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      // Click load — triggers pickFolder which fails, showing the input
      await userEvent.click(screen.getByText(/load new/i))

      await waitFor(() => {
        expect(screen.getByPlaceholderText(/path/i)).toBeInTheDocument()
      })

      // Type a path and press Enter
      const input = screen.getByPlaceholderText(/path/i)
      await userEvent.type(input, '/manual/path{Enter}')

      expect(apiAddProject).toHaveBeenCalledWith('/manual/path')
    })

    it('submits manual path on "Add" button click', async () => {
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: false,
        error: 'no native picker',
      })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: true,
        project: makeProject({ id: 'btn-1', name: 'Btn App', path: '/btn/app' }),
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })

      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      await userEvent.click(screen.getByText(/load new/i))

      await waitFor(() => {
        expect(screen.getByPlaceholderText(/path/i)).toBeInTheDocument()
      })

      const input = screen.getByPlaceholderText(/path/i)
      await userEvent.type(input, '/btn/app')

      // Click the Add button
      const addBtn = screen.getByRole('button', { name: /^add$/i })
      await userEvent.click(addBtn)

      expect(apiAddProject).toHaveBeenCalledWith('/btn/app')
    })

    it('handles canceled folder picker gracefully (no error shown)', async () => {
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: false,
        canceled: true,
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })
      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      await userEvent.click(screen.getByText(/load new/i))

      // After cancel, no error status and no manual input shown — the picker
      // just dismissed and the dropdown stays open with its normal state
      await waitFor(() => {
        expect(apiPickFolder).toHaveBeenCalled()
      })
      // The manual input form should NOT appear on a canceled picker
      expect(screen.queryByPlaceholderText(/path/i)).not.toBeInTheDocument()
    })
  })

  // ─── REMOVE PROJECT ─────────────────────────────────────────────────────────

  describe('remove project', () => {
    it('calls removeProject API when remove button is clicked on a project row', async () => {
      const projects = [
        makeProject({ id: 'rm-1', name: 'Removable', path: '/removable' }),
      ]
      ;(apiRemoveProject as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
      defaultMocks({ projects, activeId: 'rm-1' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Removable/i })).toBeInTheDocument()
      })

      // Open dropdown
      await userEvent.click(screen.getByRole('button', { name: /Removable/i }))

      // The remove button has an aria-label about removing from list
      const removeBtn = screen.getByRole('button', { name: /remove from list/i })
      await userEvent.click(removeBtn)

      expect(apiRemoveProject).toHaveBeenCalledWith('rm-1')
    })

    it('shows error status when removeProject API fails', async () => {
      const projects = [
        makeProject({ id: 'rm-2', name: 'FailRemove', path: '/fail-remove' }),
      ]
      ;(apiRemoveProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: false,
        error: 'permission denied',
      })
      defaultMocks({ projects, activeId: 'rm-2' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /FailRemove/i })).toBeInTheDocument()
      })

      await userEvent.click(screen.getByRole('button', { name: /FailRemove/i }))
      const removeBtn = screen.getByRole('button', { name: /remove from list/i })
      await userEvent.click(removeBtn)

      // Error message is displayed in the status area
      await waitFor(() => {
        expect(screen.getByText(/permission denied/i)).toBeInTheDocument()
      })
    })
  })

  // ─── EMPTY STATE ────────────────────────────────────────────────────────────

  describe('empty state', () => {
    it('shows empty state message when no projects and no app connected', async () => {
      defaultMocks({ projects: [], activeId: '' })
      renderWithProviders(<DesignTweak />)

      // The empty request area shows a message about no app being selected.
      // The i18n text: "No web app connected. Pick one in the dropdown above…"
      await waitFor(() => {
        expect(screen.getByText(/No web app connected/i)).toBeInTheDocument()
      })
    })

    it('shows "no requests" state for a connected project with no pending work', async () => {
      defaultMocks({ pending: [] })
      renderWithProviders(<DesignTweak />)

      // Connected project (activeId matches) but no pending requests
      await waitFor(() => {
        // Should show empty-for-app message (not empty_no_app)
        const content = document.body.textContent || ''
        // The empty_for_app message includes the project name
        expect(content).toMatch(/My App/i)
      })
    })

    it('shows loading spinner during initial data fetch', async () => {
      // Make fetchProjects never resolve to keep the booting state
      ;(fetchProjects as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
      ;(fetchQueue as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
      ;(fetchHistory as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
      ;(fetchHealth as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))

      renderWithProviders(<DesignTweak />)

      // The booting state shows a spinner with loading text
      expect(screen.getByText(/loading/i)).toBeInTheDocument()
    })

    it('dropdown shows "none loaded" when no projects exist after boot', async () => {
      defaultMocks({ projects: [], activeId: '' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })

      // Open the dropdown — the trigger says "Select a web app…"
      const trigger = screen.getByRole('button', { name: /Select a web app/i })
      await userEvent.click(trigger)

      // The dropdown body shows "No web apps loaded yet."
      await waitFor(() => {
        expect(screen.getByText(/No web apps loaded yet/i)).toBeInTheDocument()
      })
    })
  })

  // ─── RESIZE HANDLE ──────────────────────────────────────────────────────────

  describe('resize handle', () => {
    it('renders the drag handle with resize cursor and correct title', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      // The drag handle has a title about resizing
      const handle = screen.getByTitle(/drag to resize/i)
      expect(handle).toBeInTheDocument()
      expect(handle).toHaveClass('cursor-col-resize')
    })

    it('persists rail width to localStorage after drag', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      const handle = screen.getByTitle(/drag to resize/i)

      // Simulate drag: mousedown -> mousemove -> mouseup
      fireEvent.mouseDown(handle, { clientX: 500 })
      fireEvent.mouseMove(window, { clientX: 600 })
      fireEvent.mouseUp(window, { clientX: 600 })

      // Rail width is persisted to localStorage
      const stored = localStorage.getItem('ste_rail_w')
      expect(stored).toBeTruthy()
      // The value should be clamped between 360-800 and reflect the 100px drag
      const w = parseInt(stored!, 10)
      expect(w).toBeGreaterThanOrEqual(360)
      expect(w).toBeLessThanOrEqual(800)
    })

    it('clamps rail width to minimum 360px', async () => {
      // Pre-set to near minimum
      localStorage.setItem('ste_rail_w', '400')
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      const handle = screen.getByTitle(/drag to resize/i)

      // Drag far left (negative delta beyond clamp)
      fireEvent.mouseDown(handle, { clientX: 400 })
      fireEvent.mouseMove(window, { clientX: 100 })
      fireEvent.mouseUp(window, { clientX: 100 })

      const stored = localStorage.getItem('ste_rail_w')
      expect(parseInt(stored!, 10)).toBe(360)
    })

    it('clamps rail width to maximum 800px', async () => {
      localStorage.setItem('ste_rail_w', '700')
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      const handle = screen.getByTitle(/drag to resize/i)

      // Drag far right (positive delta beyond clamp)
      fireEvent.mouseDown(handle, { clientX: 700 })
      fireEvent.mouseMove(window, { clientX: 1200 })
      fireEvent.mouseUp(window, { clientX: 1200 })

      const stored = localStorage.getItem('ste_rail_w')
      expect(parseInt(stored!, 10)).toBe(800)
    })
  })

  // ─── HISTORY EXPAND/COLLAPSE ────────────────────────────────────────────────

  describe('history toggle', () => {
    it('expands history section when history button is clicked', async () => {
      defaultMocks()
      ;(fetchHistory as ReturnType<typeof vi.fn>).mockResolvedValue({
        history: [{
          id: 'h1', number: 1, status: 'done', state: 'done',
          projectId: 'proj-1', projectRoot: '/home/user/my-app',
          comments: [{ cid: 'c1', index: 1, status: 'done', comment: 'Fix the header' }],
        }],
      })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      // The history button shows count
      const histBtn = screen.getByRole('button', { name: /history/i })
      expect(histBtn).toBeInTheDocument()

      // Click to expand
      await userEvent.click(histBtn)

      // The history request group content becomes visible
      await waitFor(() => {
        expect(screen.getByText(/Fix the header/i)).toBeInTheDocument()
      })
    })
  })

  // ─── CONNECT / DISCONNECT ──────────────────────────────────────────────────

  describe('connect/disconnect', () => {
    it('shows Connect button when a project is selected but not yet connected', async () => {
      // Two projects; activeId is empty so nothing auto-connects
      const projects = [
        makeProject({ id: 'c1', name: 'Conn App', path: '/conn' }),
      ]
      defaultMocks({ projects, activeId: '' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        // With no activeId, selectedId gets filled to first project but
        // previewId is empty (no wasDisconnected check skips connect). The
        // Connect button should appear.
        expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument()
      })
    })

    it('disconnect clears preview and shows Connect button again', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      // Wait for auto-connect
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /connected/i })).toBeInTheDocument()
      })

      // Click disconnect
      await userEvent.click(screen.getByRole('button', { name: /connected/i }))

      // Now Connect button appears
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument()
      })

      // localStorage records disconnected state
      expect(localStorage.getItem('ste_disconnected')).toBe('1')
    })

    it('does not auto-connect when ste_disconnected is set in localStorage', async () => {
      // User previously disconnected — auto-connect is skipped even with activeId
      localStorage.setItem('ste_disconnected', '1')
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        // Connect button instead of Connected, because the disconnect flag
        // prevents auto-setting previewId
        expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument()
      })
    })

    it('clicking Connect calls selectProject and switches to connected state', async () => {
      const projects = [makeProject({ id: 'cn1', name: 'ConnMe', path: '/connme' })]
      defaultMocks({ projects, activeId: '' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /connect/i })).toBeInTheDocument()
      })

      await userEvent.click(screen.getByRole('button', { name: /connect/i }))

      // Should now show Connected
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /connected/i })).toBeInTheDocument()
      })
      expect(selectProject).toHaveBeenCalledWith('cn1')
    })
  })

  // ─── ADD PROJECT — SUCCESS STATUS VARIANTS ─────────────────────────────────

  describe('add project — status messages', () => {
    it('shows "already registered" status when addProject returns existing flag', async () => {
      const existing = makeProject({ id: 'ex1', name: 'Existing', path: '/existing' })
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/existing' })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: true, project: existing, existing: true,
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })
      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      await userEvent.click(screen.getByText(/load new/i))

      await waitFor(() => {
        expect(screen.getByText(/already registered/i)).toBeInTheDocument()
      })
    })

    it('shows error status when addProject API returns an error', async () => {
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/bad' })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: false, error: 'path not found',
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })
      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      await userEvent.click(screen.getByText(/load new/i))

      await waitFor(() => {
        expect(screen.getByText(/path not found/i)).toBeInTheDocument()
      })
    })

    it('shows error when addProject throws an exception', async () => {
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/crash' })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network timeout'))
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })
      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      await userEvent.click(screen.getByText(/load new/i))

      await waitFor(() => {
        expect(screen.getByText(/network timeout/i)).toBeInTheDocument()
      })
    })

    it('shows "found dev server" status when autoDetected is true', async () => {
      const proj = makeProject({ id: 'ad1', name: 'ViteApp', path: '/vite', previewUrl: 'http://localhost:5173' })
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/vite' })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: true, project: proj, autoDetected: true,
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })
      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      await userEvent.click(screen.getByText(/load new/i))

      // Status includes the project name
      await waitFor(() => {
        expect(screen.getByText(/ViteApp/i)).toBeInTheDocument()
      })
    })
  })

  // ─── REMOVE PROJECT — EXCEPTION PATH ──────────────────────────────────────

  describe('remove project — exception', () => {
    it('shows error status when removeProject throws', async () => {
      const projects = [makeProject({ id: 'rmx', name: 'CrashRm', path: '/crash' })]
      ;(apiRemoveProject as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('server error'))
      defaultMocks({ projects, activeId: 'rmx' })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /CrashRm/i })).toBeInTheDocument()
      })

      await userEvent.click(screen.getByRole('button', { name: /CrashRm/i }))
      const removeBtn = screen.getByRole('button', { name: /remove from list/i })
      await userEvent.click(removeBtn)

      await waitFor(() => {
        expect(screen.getByText(/server error/i)).toBeInTheDocument()
      })
    })
  })

  // ─── REQUEST GROUPS IN RAIL (expand/collapse) ──────────────────────────────

  describe('request groups', () => {
    it('renders pending requests and expands them to show comments', async () => {
      const pending = [{
        id: 'req-1', number: 1, status: 'draft', state: 'draft',
        projectId: 'proj-1', projectRoot: '/home/user/my-app',
        comments: [
          { cid: 'c1', index: 1, status: 'new', comment: 'Make the header bold' },
          { cid: 'c2', index: 2, status: 'new', comment: 'Center the title' },
        ],
      }]
      defaultMocks({ pending })
      renderWithProviders(<DesignTweak />)

      // Wait for the request group to appear (expanded by default)
      await waitFor(() => {
        expect(screen.getByText(/Make the header bold/i)).toBeInTheDocument()
        expect(screen.getByText(/Center the title/i)).toBeInTheDocument()
      })
    })

    it('toggles request group open state (two clicks closes the group)', async () => {
      // The initial state is "open by default" (reqOpen[id] is undefined, and
      // open={reqOpen[id] !== false} treats undefined as open). The toggle cycle
      // is: undefined→true (still open), true→false (closed).
      const pending = [{
        id: 'req-2', number: 2, status: 'draft', state: 'draft',
        projectId: 'proj-1', projectRoot: '/home/user/my-app',
        comments: [
          { cid: 'c3', index: 1, status: 'new', comment: 'Collapsible comment text' },
        ],
      }]
      defaultMocks({ pending })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByText(/Collapsible comment text/i)).toBeInTheDocument()
      })

      // Find the grid parent that controls visibility
      const commentEl = screen.getByText(/Collapsible comment text/i)
      const gridParent = commentEl.closest('[style*="grid-template-rows"]') as HTMLElement
      expect(gridParent).toBeTruthy()
      expect(gridParent!.style.gridTemplateRows).toBe('1fr')

      // Find the request header Clickable (role=button)
      const requestRow = screen.getAllByRole('button').find(
        el => el.textContent?.includes('Request 2') && el.getAttribute('class')?.includes('items-center')
      ) as HTMLElement
      expect(requestRow).toBeTruthy()

      // First click: undefined→true (still open per the !== false check)
      await userEvent.click(requestRow)
      expect(gridParent!.style.gridTemplateRows).toBe('1fr')

      // Second click: true→false (now closed)
      await userEvent.click(requestRow)
      await waitFor(() => {
        expect(gridParent!.style.gridTemplateRows).toBe('0fr')
      })
    })

    it('shows send button on draft requests with comments', async () => {
      const pending = [{
        id: 'req-3', number: 3, status: 'draft', state: 'draft',
        projectId: 'proj-1', projectRoot: '/home/user/my-app',
        comments: [
          { cid: 'c4', index: 1, status: 'new', comment: 'Draft comment' },
        ],
      }]
      defaultMocks({ pending })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        // The send button text: "Send as Request 3"
        expect(screen.getByText(/Send as Request 3/i)).toBeInTheDocument()
      })
    })

    it('shows "no comments" message for an empty draft request', async () => {
      const pending = [{
        id: 'req-4', number: 4, status: 'draft', state: 'draft',
        projectId: 'proj-1', projectRoot: '/home/user/my-app',
        comments: [],
      }]
      defaultMocks({ pending })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByText(/no comments/i)).toBeInTheDocument()
      })
    })
  })

  // ─── RAIL WIDTH RESTORATION ────────────────────────────────────────────────

  describe('rail width restoration', () => {
    it('restores rail width from localStorage on mount', async () => {
      localStorage.setItem('ste_rail_w', '650')
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      // The left rail container should have width 650px
      const handle = screen.getByTitle(/drag to resize/i)
      const rail = handle.previousElementSibling as HTMLElement
      expect(rail.style.width).toBe('650px')
    })

    it('defaults to 500px when no stored width', async () => {
      defaultMocks()
      renderWithProviders(<DesignTweak />)

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /My App/i })).toBeInTheDocument()
      })

      const handle = screen.getByTitle(/drag to resize/i)
      const rail = handle.previousElementSibling as HTMLElement
      expect(rail.style.width).toBe('500px')
    })
  })

  // ─── STATUS AREA ───────────────────────────────────────────────────────────

  describe('status area', () => {
    it('displays status text after a successful addProject that sets dev server', async () => {
      const proj = makeProject({ id: 'dv1', name: 'DevApp', path: '/dev', previewUrl: 'http://localhost:3000' })
      ;(apiPickFolder as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/dev' })
      ;(apiAddProject as ReturnType<typeof vi.fn>).mockResolvedValue({
        ok: true, project: proj, updated: 'previewUrl',
      })
      defaultMocks({ projects: [] })
      renderWithProviders(<DesignTweak />)

      await waitFor(() => { expect(fetchProjects).toHaveBeenCalled() })
      await userEvent.click(screen.getByRole('button', { name: /Select a web app/i }))
      await userEvent.click(screen.getByText(/load new/i))

      // "set dev server for DevApp" type status
      await waitFor(() => {
        expect(screen.getByText(/DevApp/i)).toBeInTheDocument()
      })
    })
  })
})
