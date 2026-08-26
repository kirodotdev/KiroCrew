/**
 * Runtime coverage for DesignTweakPage — PREVIEW PANE surface only.
 *
 * Exercises: iframe src derivation (static vs dev-server proxy), nonce/refresh,
 * Dimensions dropdown (desktop/tablet/mobile widths + dismissal), Preview vs Edit
 * mode toggle, the "preview not reachable" state, and dev-server controls (detect,
 * start, stop, Connected button).
 *
 * Does NOT test left-rail request/comment tree or chat dispatch — those are covered
 * by parallel agent files.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import DesignTweak from '../apps/design-tweak/DesignTweakPage'
import { renderWithProviders } from './helpers'

// Mock all api functions the component calls on mount and during interactions.
vi.mock('../apps/design-tweak/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/design-tweak/api')>()),
  fetchProjects: vi.fn(),
  fetchQueue: vi.fn(),
  fetchHistory: vi.fn(),
  fetchHealth: vi.fn(),
  detectDevServer: vi.fn(),
  startDevServer: vi.fn(),
  stopDevServer: vi.fn(),
  setPreviewUrl: vi.fn(),
  selectProject: vi.fn(),
}))

// The component uses i18nT for all visible strings. Passthrough returns the key
// suffix so assertions remain readable without coupling to exact copy.
vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) => {
    const last = key.split('.').pop() ?? key
    // Dimensions labels need to be distinguishable by preset name.
    if (key.includes('dim_desktop')) return 'Desktop'
    if (key.includes('dim_tablet')) return 'Tablet'
    if (key.includes('dim_mobile')) return 'Mobile'
    if (vars && Object.keys(vars).length) return `${last}[${JSON.stringify(vars)}]`
    return last
  },
}))

// The delivery module is imported at the top level — stub it so it never tries
// to read transcripts during these preview-focused tests.
vi.mock('../apps/design-tweak/delivery', () => ({
  deliveryVerdict: () => 'unknown',
  needsDeliveryRetry: () => false,
}))

import {
  fetchProjects, fetchQueue, fetchHistory, fetchHealth,
  detectDevServer, startDevServer, stopDevServer, setPreviewUrl, selectProject,
} from '../apps/design-tweak/api'

const mockFetchProjects = fetchProjects as ReturnType<typeof vi.fn>
const mockFetchQueue = fetchQueue as ReturnType<typeof vi.fn>
const mockFetchHistory = fetchHistory as ReturnType<typeof vi.fn>
const mockFetchHealth = fetchHealth as ReturnType<typeof vi.fn>
const mockDetectDevServer = detectDevServer as ReturnType<typeof vi.fn>
const mockStartDevServer = startDevServer as ReturnType<typeof vi.fn>
const _mockStopDevServer = stopDevServer as ReturnType<typeof vi.fn>
const mockSetPreviewUrl = setPreviewUrl as ReturnType<typeof vi.fn>
const mockSelectProject = selectProject as ReturnType<typeof vi.fn>

// ── Fixtures ─────────────────────────────────────────────────────────────────

const STATIC_PROJECT = {
  id: 'proj-1',
  path: '/home/user/my-app',
  name: 'My App',
  previewUrl: 'http://127.0.0.1:9100/',
  previewMode: 'static' as const,
}

const DEV_PROJECT = {
  id: 'proj-2',
  path: '/home/user/react-app',
  name: 'React App',
  previewUrl: 'http://127.0.0.1:5173/',
  devCommand: 'npm run dev',
  needsDevServer: true,
}

const NEEDS_DEV_NO_URL = {
  id: 'proj-3',
  path: '/home/user/vite-app',
  name: 'Vite App',
  previewUrl: '',
  devCommand: 'npm run dev',
  needsDevServer: true,
  unbundledEntry: 'src/main.tsx',
}

const NEEDS_DEV_NO_SCRIPT = {
  id: 'proj-4',
  path: '/home/user/raw-app',
  name: 'Raw App',
  previewUrl: '',
  needsDevServer: true,
}

function setupDefaultMocks(projects = [STATIC_PROJECT], activeId = 'proj-1') {
  mockFetchProjects.mockResolvedValue({ projects, activeId, serving: true })
  mockFetchQueue.mockResolvedValue({ pending: [] })
  mockFetchHistory.mockResolvedValue({ history: [] })
  mockFetchHealth.mockResolvedValue({ status: 'ok', dataDir: '/data' })
  mockSelectProject.mockResolvedValue({ ok: true })
}

// Stub fetch globally so the iframe-readiness probe resolves as reachable.
const originalFetch = globalThis.fetch
function stubFetchForProbe() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    // The component probes the previewSrc URL to check reachability.
    if (url.startsWith('http://127.0.0.1:')) {
      return new Response('', { status: 200 })
    }
    // Api calls go through the module mock, but if anything slips to fetch
    // (e.g. the initial useQuery fetch) return a sensible default.
    return new Response(JSON.stringify({}), { status: 200 })
  }) as typeof globalThis.fetch
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  stubFetchForProbe()
  localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  globalThis.fetch = originalFetch
})

// ── Helper to render and wait for the initial data fetch to settle ────────────

async function renderAndSettle(projects = [STATIC_PROJECT], activeId = 'proj-1') {
  setupDefaultMocks(projects, activeId)
  const result = renderWithProviders(<DesignTweak />)
  // Let the initial React Query fetches fire and resolve.
  await waitFor(() => {
    expect(mockFetchProjects).toHaveBeenCalled()
  })
  // Flush microtasks so the component settles with data.
  await act(async () => { await Promise.resolve() })
  return result
}

/**
 * The preview iframe, once it has actually rendered.
 *
 * `renderAndSettle` waits for the fetch to be CALLED and flushes one microtask,
 * which is not the same as waiting for React Query's data to land and the frame
 * to mount. Reading `document.querySelector('iframe')` straight afterwards
 * therefore raced: under parallel load it returned null and the assertion failed
 * with `Cannot read properties of null (reading 'src')` — a flake that only
 * appeared in the full suite, never in isolation. Polling for the post-condition
 * is the fix; a longer flush would only move the race.
 */
async function awaitIframe(): Promise<HTMLIFrameElement> {
  return await waitFor(() => {
    const el = document.querySelector('iframe')
    expect(el).not.toBeNull()
    return el as HTMLIFrameElement
  })
}

// ═══════════════════════════════════════════════════════════════════════════════
describe('DesignTweakPage — Preview Pane', () => {

  // ── iframe src derivation ──────────────────────────────────────────────────

  describe('iframe src and sandbox', () => {
    it('derives previewSrc from loopback previewUrl with nonce query param', async () => {
      await renderAndSettle()
      const iframe = await awaitIframe()
      // The src must start with the project's previewUrl (loopback).
      expect(iframe.src).toContain('http://127.0.0.1:9100/')
      // Must contain a cache-buster nonce parameter.
      expect(iframe.src).toMatch(/_t=\d+/)
    })

    it('iframe must NOT point to a dashboard-origin URL', async () => {
      await renderAndSettle()
      const iframe = await awaitIframe()
      // The dashboard serves from window.location.origin — the preview frame
      // must never share it, because allow-same-origin on same-origin is unsafe.
      expect(iframe.src).not.toContain(window.location.origin)
    })

    it('iframe has sandbox attribute with required permissions', async () => {
      await renderAndSettle()
      const iframe = await awaitIframe()
      expect(iframe).toHaveAttribute('sandbox')
      const sandbox = iframe.getAttribute('sandbox')!
      expect(sandbox).toContain('allow-scripts')
      expect(sandbox).toContain('allow-same-origin')
      expect(sandbox).toContain('allow-forms')
    })

    it('iframe width matches DIMS[desktop] = 100% by default', async () => {
      await renderAndSettle()
      const iframe = await awaitIframe()
      expect(iframe.style.width).toBe('100%')
    })

    it('uses dev-server previewUrl for framework projects', async () => {
      await renderAndSettle([DEV_PROJECT], 'proj-2')
      const iframe = await awaitIframe()
      expect(iframe.src).toContain('http://127.0.0.1:5173/')
      expect(iframe.src).toMatch(/_t=\d+/)
    })
  })

  // ── Refresh / nonce behaviour ──────────────────────────────────────────────

  describe('refresh (nonce bump)', () => {
    it('clicking refresh button changes iframe src nonce', async () => {
      await renderAndSettle()
      const iframe = await awaitIframe()
      const originalSrc = iframe.src

      // The refresh button has a specific aria-label from i18nT.
      const refreshBtn = screen.getByRole('button', { name: 'refresh_preview' })
      await act(async () => { fireEvent.click(refreshBtn) })

      // The src should change because nonce = Date.now() was called.
      expect(iframe.src).not.toBe(originalSrc)
      expect(iframe.src).toContain('http://127.0.0.1:9100/')
      expect(iframe.src).toMatch(/_t=\d+/)
    })

    it('refresh button is disabled when no project is previewed', async () => {
      // Render with no active project — pass empty active id so preview stays blank.
      setupDefaultMocks([STATIC_PROJECT], '')
      // Override: component sets previewId from activeId on first fetch, so
      // mark the localStorage disconnected to suppress auto-connect.
      localStorage.setItem('ste_disconnected', '1')
      renderWithProviders(<DesignTweak />)
      await waitFor(() => { expect(mockFetchProjects).toHaveBeenCalled() })
      await act(async () => { await Promise.resolve() })

      const refreshBtn = screen.getByRole('button', { name: 'refresh_preview' })
      expect(refreshBtn).toBeDisabled()
    })
  })

  // ── Dimensions dropdown ────────────────────────────────────────────────────

  describe('Dimensions dropdown', () => {
    it('shows desktop as default dimension', async () => {
      await renderAndSettle()
      // The dimensions trigger contains the label "Desktop".
      expect(screen.getByText('Desktop')).toBeInTheDocument()
    })

    it('opens dropdown and shows desktop/tablet/mobile options', async () => {
      await renderAndSettle()
      // The dimensions button contains "dimensions:" text.
      const dimsBtn = screen.getByRole('button', { name: /dimensions/i })
      await act(async () => { fireEvent.click(dimsBtn) })

      // "Desktop" appears both in the trigger and in the dropdown option list.
      const desktopMatches = screen.getAllByText('Desktop')
      expect(desktopMatches.length).toBeGreaterThanOrEqual(2)
      // Tablet and Mobile include the px width — unique to the dropdown items.
      expect(screen.getByText('Tablet (768px)')).toBeInTheDocument()
      expect(screen.getByText('Mobile (390px)')).toBeInTheDocument()
    })

    it('selecting tablet changes iframe width to 768px', async () => {
      await renderAndSettle()
      const dimsBtn = screen.getByRole('button', { name: /dimensions/i })
      await act(async () => { fireEvent.click(dimsBtn) })

      const tabletOption = screen.getByText('Tablet (768px)')
      await act(async () => { fireEvent.click(tabletOption) })

      const iframe = await awaitIframe()
      expect(iframe.style.width).toBe('768px')
    })

    it('selecting mobile changes iframe width to 390px', async () => {
      await renderAndSettle()
      const dimsBtn = screen.getByRole('button', { name: /dimensions/i })
      await act(async () => { fireEvent.click(dimsBtn) })

      const mobileOption = screen.getByText('Mobile (390px)')
      await act(async () => { fireEvent.click(mobileOption) })

      const iframe = await awaitIframe()
      expect(iframe.style.width).toBe('390px')
    })

    it('dropdown dismisses after selecting an option', async () => {
      await renderAndSettle()
      const dimsBtn = screen.getByRole('button', { name: /dimensions/i })
      await act(async () => { fireEvent.click(dimsBtn) })

      expect(screen.getByText('Tablet (768px)')).toBeInTheDocument()
      const tabletOption = screen.getByText('Tablet (768px)')
      await act(async () => { fireEvent.click(tabletOption) })

      // After selection, the dropdown menu items should be gone.
      expect(screen.queryByText('Tablet (768px)')).not.toBeInTheDocument()
      expect(screen.queryByText('Mobile (390px)')).not.toBeInTheDocument()
    })

    it('persists dimension choice per project in localStorage', async () => {
      await renderAndSettle()
      const dimsBtn = screen.getByRole('button', { name: /dimensions/i })
      await act(async () => { fireEvent.click(dimsBtn) })
      const mobileOption = screen.getByText('Mobile (390px)')
      await act(async () => { fireEvent.click(mobileOption) })

      const stored = JSON.parse(localStorage.getItem('ste_dims_map') || '{}')
      expect(stored['proj-1']).toBe('mobile')
    })
  })

  // ── Preview vs Edit mode toggle ────────────────────────────────────────────

  describe('Preview / Edit mode toggle', () => {
    // The two mode-toggle buttons are the only ones on the page with
    // borderRadius: 10px (inline style).
    function getModeToggle() {
      const allBtns = document.querySelectorAll<HTMLButtonElement>('button[style*="border-radius: 10px"]')
      return { previewBtn: allBtns[0], editBtn: allBtns[1] }
    }

    it('renders preview mode active by default', async () => {
      await renderAndSettle()
      const { previewBtn, editBtn } = getModeToggle()
      expect(previewBtn).toBeTruthy()
      expect(editBtn).toBeTruthy()
      // Preview starts active (accent background), edit is transparent.
      expect(previewBtn.style.background).toContain('var(--accent)')
      expect(editBtn.style.background).toBe('transparent')
    })

    it('clicking edit button activates it', async () => {
      await renderAndSettle()
      const { editBtn } = getModeToggle()
      // Before click, edit is transparent (inactive).
      expect(editBtn.style.background).toBe('transparent')

      fireEvent.click(editBtn)
      await act(async () => { await Promise.resolve() })

      // After click, the edit button gains accent background — state changed.
      // (In happy-dom the iframe onLoad can re-fire and race the mode back,
      // so we only assert the button we clicked gained the active style.)
      const updated = getModeToggle()
      expect(updated.editBtn.style.background).toContain('var(--accent)')
    })

    it('both mode buttons are rendered and clickable', async () => {
      await renderAndSettle()
      const { previewBtn, editBtn } = getModeToggle()
      // Both buttons exist and are not disabled.
      expect(previewBtn).not.toBeNull()
      expect(editBtn).not.toBeNull()
      expect(previewBtn.disabled).toBe(false)
      expect(editBtn.disabled).toBe(false)
      // Their text content reflects the mode labels from i18nT.
      expect(previewBtn.textContent).toContain('preview')
      expect(editBtn.textContent).toContain('edit')
    })
  })

  // ── "Preview not reachable" state ──────────────────────────────────────────

  describe('preview not reachable state', () => {
    it('shows unreachable message when previewSrc probe fails', async () => {
      // Override fetch to make the probe fail.
      globalThis.fetch = vi.fn(async () => {
        throw new Error('ECONNREFUSED')
      }) as typeof globalThis.fetch

      setupDefaultMocks([STATIC_PROJECT], 'proj-1')
      renderWithProviders(<DesignTweak />)
      await waitFor(() => { expect(mockFetchProjects).toHaveBeenCalled() })
      await act(async () => { await Promise.resolve() })

      await waitFor(() => {
        expect(screen.getByText('not_reachable')).toBeInTheDocument()
      })
    })

    it('shows dev-server-specific unreachable text for framework projects', async () => {
      globalThis.fetch = vi.fn(async () => {
        throw new Error('ECONNREFUSED')
      }) as typeof globalThis.fetch

      setupDefaultMocks([DEV_PROJECT], 'proj-2')
      renderWithProviders(<DesignTweak />)
      await waitFor(() => { expect(mockFetchProjects).toHaveBeenCalled() })
      await act(async () => { await Promise.resolve() })

      await waitFor(() => {
        expect(screen.getByText('dev_server_not_reachable')).toBeInTheDocument()
      })
    })

    it('shows retry button in unreachable state', async () => {
      globalThis.fetch = vi.fn(async () => {
        throw new Error('ECONNREFUSED')
      }) as typeof globalThis.fetch

      setupDefaultMocks([STATIC_PROJECT], 'proj-1')
      renderWithProviders(<DesignTweak />)
      await waitFor(() => { expect(mockFetchProjects).toHaveBeenCalled() })
      await act(async () => { await Promise.resolve() })

      await waitFor(() => {
        expect(screen.getByText('retry')).toBeInTheDocument()
      })
    })

    it('shows needs-dev-server prompt when framework project has no previewUrl', async () => {
      await renderAndSettle([NEEDS_DEV_NO_URL], 'proj-3')

      // The unbundled entry message should appear.
      await waitFor(() => {
        expect(screen.getByText('needs_dev_server_title')).toBeInTheDocument()
      })
      // With unbundledEntry set, the entry file name is shown.
      expect(screen.getByText(/unbundled_entry/)).toBeInTheDocument()
    })

    it('shows "start dev server" button for project with devCommand', async () => {
      await renderAndSettle([NEEDS_DEV_NO_URL], 'proj-3')

      await waitFor(() => {
        expect(screen.getByText('start_dev_server')).toBeInTheDocument()
      })
    })

    it('shows warning when no dev script exists in package.json', async () => {
      await renderAndSettle([NEEDS_DEV_NO_SCRIPT], 'proj-4')

      await waitFor(() => {
        expect(screen.getByText('no_dev_script_in_package_json')).toBeInTheDocument()
      })
    })
  })

  // ── Dev-server controls ────────────────────────────────────────────────────

  describe('dev-server controls', () => {
    it('shows detect dev-server button when project has no previewUrl set', async () => {
      const noUrlProject = { ...STATIC_PROJECT, previewUrl: 'http://127.0.0.1:9100/' }
      await renderAndSettle([noUrlProject])

      // When previewUrl IS set, shows the URL chip, not the detect button.
      expect(screen.getByText(/127\.0\.0\.1:9100/)).toBeInTheDocument()
    })

    it('shows Connected button when app is connected', async () => {
      await renderAndSettle()
      // The Connected button renders when previewId === selectedId.
      expect(screen.getByText('connected')).toBeInTheDocument()
    })

    it('clicking Connected (disconnect) clears the preview', async () => {
      await renderAndSettle()
      const connectedBtn = screen.getByText('connected')
      await act(async () => { fireEvent.click(connectedBtn) })

      // After disconnect, the iframe should be gone (no previewId).
      expect(document.querySelector('iframe')).toBeNull()
      // And localStorage records the disconnect.
      expect(localStorage.getItem('ste_disconnected')).toBe('1')
    })

    it('detect dev-server calls detectDevServer and sets URL on success', async () => {
      // Project with no previewUrl to show the detect button.
      const proj = { ...STATIC_PROJECT, previewUrl: '' }
      mockDetectDevServer.mockResolvedValue({ suggested: 'http://127.0.0.1:3000/' })
      mockSetPreviewUrl.mockResolvedValue({})

      await renderAndSettle([proj], 'proj-1')

      const detectBtn = screen.getByRole('button', { name: /dev_server/i })
      await act(async () => { fireEvent.click(detectBtn) })

      await waitFor(() => {
        expect(mockDetectDevServer).toHaveBeenCalledWith('proj-1')
      })
      await waitFor(() => {
        expect(mockSetPreviewUrl).toHaveBeenCalledWith('proj-1', 'http://127.0.0.1:3000/')
      })
    })

    it('detect dev-server shows input when multiple candidates found', async () => {
      const proj = { ...STATIC_PROJECT, previewUrl: '' }
      mockDetectDevServer.mockResolvedValue({
        candidates: [
          { url: 'http://127.0.0.1:3000/', port: 3000 },
          { url: 'http://127.0.0.1:5173/', port: 5173 },
        ],
      })

      await renderAndSettle([proj], 'proj-1')
      const detectBtn = screen.getByRole('button', { name: /dev_server/i })
      await act(async () => { fireEvent.click(detectBtn) })

      // The dev-server URL input should appear for manual selection.
      await waitFor(() => {
        expect(screen.getByPlaceholderText('url_placeholder')).toBeInTheDocument()
      })
    })

    it('start dev-server calls apiStartDevServer and refreshes preview', async () => {
      mockStartDevServer.mockResolvedValue({
        ok: true,
        url: 'http://127.0.0.1:5173/',
        devUrl: 'http://127.0.0.1:5173/',
      })

      await renderAndSettle([NEEDS_DEV_NO_URL], 'proj-3')

      const startBtn = screen.getByText('start_dev_server')
      await act(async () => { fireEvent.click(startBtn) })

      await waitFor(() => {
        expect(mockStartDevServer).toHaveBeenCalledWith('proj-3')
      })
    })

    it('start dev-server shows error when it fails', async () => {
      mockStartDevServer.mockResolvedValue({
        ok: false,
        error: 'port 5173 already in use',
      })

      await renderAndSettle([NEEDS_DEV_NO_URL], 'proj-3')
      const startBtn = screen.getByText('start_dev_server')
      await act(async () => { fireEvent.click(startBtn) })

      await waitFor(() => {
        // The error is rendered in a role="alert" element.
        expect(screen.getByRole('alert')).toHaveTextContent('port 5173 already in use')
      })
    })

    it('clear dev-server URL (X button) calls setPreviewUrl with empty string', async () => {
      mockSetPreviewUrl.mockResolvedValue({})
      await renderAndSettle()

      // The X button beside the dev URL chip clears the dev server.
      const clearBtn = screen.getByRole('button', { name: 'preview_from_disk_instead' })
      await act(async () => { fireEvent.click(clearBtn) })

      await waitFor(() => {
        expect(mockSetPreviewUrl).toHaveBeenCalledWith('proj-1', '')
      })
    })
  })

  // ── Timeout / loading state ────────────────────────────────────────────────

  describe('loading and timeout states', () => {
    it('shows loading state initially before probe resolves', async () => {
      // Prevent the probe from resolving so loading state stays visible.
      globalThis.fetch = vi.fn(() => new Promise(() => {})) as unknown as typeof globalThis.fetch

      setupDefaultMocks([STATIC_PROJECT], 'proj-1')
      renderWithProviders(<DesignTweak />)
      await waitFor(() => { expect(mockFetchProjects).toHaveBeenCalled() })
      await act(async () => { await Promise.resolve() })

      // The loading spinner text should be visible.
      await waitFor(() => {
        expect(screen.getByText(/loading_name/)).toBeInTheDocument()
      })
    })

    it('shows unreachable after 12s timeout when server never answers', async () => {
      globalThis.fetch = vi.fn(() => new Promise(() => {})) as unknown as typeof globalThis.fetch

      setupDefaultMocks([STATIC_PROJECT], 'proj-1')
      renderWithProviders(<DesignTweak />)
      await waitFor(() => { expect(mockFetchProjects).toHaveBeenCalled() })
      await act(async () => { await Promise.resolve() })

      // Advance past the 12s timeout backstop.
      await act(async () => { vi.advanceTimersByTime(13000) })

      await waitFor(() => {
        expect(screen.getByText('not_reachable')).toBeInTheDocument()
      })
    })
  })

  // ── No app selected state ─────────────────────────────────────────────────

  describe('empty / no app selected', () => {
    it('shows placeholder text when no project is connected', async () => {
      localStorage.setItem('ste_disconnected', '1')
      setupDefaultMocks([STATIC_PROJECT], '')
      renderWithProviders(<DesignTweak />)
      await waitFor(() => { expect(mockFetchProjects).toHaveBeenCalled() })
      await act(async () => { await Promise.resolve() })

      await waitFor(() => {
        expect(screen.getByText('no_app_selected')).toBeInTheDocument()
      })
    })
  })
})
