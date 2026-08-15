/**
 * AppDetailPage — the branches the three focused suites leave cold.
 *
 * `AppDetailPage.test.tsx` pins built-in icon/hero resolution,
 * `AppDetailPageAutoAction.test.tsx` pins the deep-link trigger and
 * `AppDetailPageDeniedDeeplink.test.tsx` pins how a trust denial is surfaced.
 * None of them reach the screenshot lightbox, the install-log panel, the
 * client-install card, the uninstall confirmation, the per-lifecycle action sets
 * or the info grid — which is most of what a user actually sees on this page.
 *
 * Harness matches the sibling suites exactly: MemoryRouter over the real route
 * plus a QueryClientProvider, because `useTrustGate` invalidates the
 * ['trusted-apps'] / ['apps'] queries after a grant and needs a client in scope.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The resolved colour mode drives screenshot-set and hero selection, so it has
// to be switchable per test. `vi.hoisted` keeps the box defined before the mock
// factory runs during module import.
const theme = vi.hoisted(() => ({ mode: 'light' as 'light' | 'dark' }))

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const installFromRegistryStream = vi.fn()
const enableApp = vi.fn()
const disableApp = vi.fn()
const updateApp = vi.fn()
const uninstallApp = vi.fn()
const trustApp = vi.fn()
const untrustApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    installFromRegistryStream: (...a: unknown[]) => installFromRegistryStream(...a),
    enableApp: (...a: unknown[]) => enableApp(...a),
    disableApp: (...a: unknown[]) => disableApp(...a),
    updateApp: (...a: unknown[]) => updateApp(...a),
    uninstallApp: (...a: unknown[]) => uninstallApp(...a),
    trustApp: (...a: unknown[]) => trustApp(...a),
    untrustApp: (...a: unknown[]) => untrustApp(...a),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: theme.mode }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

/** Not one of the first-party names in APP_MANIFEST_KEY, so display name,
 *  description and highlights fall through to the fixture instead of a catalog. */
const NAME = 'ledger-lens'
const DENIED = 'blocked by execution policy: third-party app execution is disabled'
const TRUST_MODAL = /to run its own code\?/i

type Dict = Record<string, unknown>

/** The /api/apps/{name} shape: an installed, enabled, gateway-managed app. */
function installedApp(overrides: Dict = {}): Dict {
  return {
    name: NAME,
    displayName: 'Ledger Lens',
    version: '1.0.0',
    enabled: true,
    origin: 'registry',
    resources: 'gateway',
    lifecycle: 'gateway',
    installedAt: '2026-07-01T00:00:00Z',
    manifest: {
      displayName: 'Ledger Lens',
      description: 'Reads your books and explains them.',
      author: 'zezhexu',
    },
    ...overrides,
  }
}

/** A /api/apps/registry row for the same name. */
function registryRow(overrides: Dict = {}): Dict {
  return {
    name: NAME,
    displayName: 'Ledger Lens',
    description: 'Reads your books and explains them.',
    version: '1.0.0',
    author: 'zezhexu',
    installed: false,
    ...overrides,
  }
}

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: `/apps/detail/${NAME}` }]}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
          <Route path="/apps" element={<div>apps list</div>} />
          <Route path="/chat" element={<div>chat page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Wait until the loaded page (not the spinner) is on screen. */
async function loaded() {
  await screen.findByTestId('app-icon')
}

/** Collect `mc:apps-changed` dispatches for the duration of one test. */
function watchAppsChanged(): { count: () => number; stop: () => void } {
  let n = 0
  const onChange = () => { n += 1 }
  window.addEventListener('mc:apps-changed', onChange)
  return { count: () => n, stop: () => window.removeEventListener('mc:apps-changed', onChange) }
}

describe('AppDetailPage — uncovered surfaces', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    theme.mode = 'light'
    system.mockResolvedValue({ hostname: '' })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'arm64' } })
    getApp.mockResolvedValue(null)
    installFromRegistryStream.mockResolvedValue({ ok: true })
    enableApp.mockResolvedValue({ ok: true })
    disableApp.mockResolvedValue({ ok: true })
    updateApp.mockResolvedValue({ ok: true })
    uninstallApp.mockResolvedValue({ ok: true })
    trustApp.mockResolvedValue({ ok: true })
    untrustApp.mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    delete (window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch
  })

  // --- Not found / load failure --------------------------------------------

  it('renders the not-found page when neither the app nor the registry knows the name', async () => {
    renderDetail()

    expect(await screen.findByText('App Not Found')).toBeInTheDocument()
    expect(screen.getByText(`App "${NAME}" not found`)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /back to apps/i }))
    expect(await screen.findByText('apps list')).toBeInTheDocument()
  })

  it('surfaces a malformed registry payload instead of crashing the page', async () => {
    // Resolves (so the inline .catch does not fire) but has no `apps` — the
    // outer try/catch is the only thing between this and a blank route.
    listRegistry.mockResolvedValue(null)
    renderDetail()

    expect(await screen.findByText('App Not Found')).toBeInTheDocument()
    expect(screen.getByText(/Cannot read properties of null/)).toBeInTheDocument()
  })

  it('renders the app even when the registry and system endpoints fail', async () => {
    // Both are best-effort enrichment, so a failure must degrade to the
    // installed record rather than taking the page down with it.
    getApp.mockResolvedValue(installedApp())
    listRegistry.mockRejectedValue(new Error('registry offline'))
    system.mockRejectedValue(new Error('no system info'))
    renderDetail()
    await loaded()

    expect(screen.getByText('Reads your books and explains them.')).toBeInTheDocument()
    expect(screen.queryByText('registry offline')).not.toBeInTheDocument()
  })

  // --- Screenshot gallery + lightbox ---------------------------------------

  const WITH_SHOTS = {
    displayName: 'Ledger Lens',
    description: 'Reads your books and explains them.',
    author: 'zezhexu',
    screenshots: ['/shots/light-one.png', '/shots/light-two.png', '/shots/light-three.png'],
    screenshotsDark: ['/shots/dark-one.png', '/shots/dark-two.png'],
  }

  it('steps through the lightbox with the next and previous controls', async () => {
    getApp.mockResolvedValue(installedApp({ manifest: WITH_SHOTS }))
    renderDetail()
    await loaded()

    expect(screen.getByText('Screenshots')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open screenshot 1' }))

    const box = screen.getByRole('dialog')
    expect(within(box).getByText('1 / 3')).toBeInTheDocument()
    // On the first frame there is nothing before it.
    expect(within(box).queryByRole('button', { name: 'Previous' })).not.toBeInTheDocument()

    fireEvent.click(within(box).getByRole('button', { name: 'Next' }))
    expect(within(box).getByText('2 / 3')).toBeInTheDocument()
    fireEvent.click(within(box).getByRole('button', { name: 'Next' }))
    expect(within(box).getByText('3 / 3')).toBeInTheDocument()
    expect(within(box).queryByRole('button', { name: 'Next' })).not.toBeInTheDocument()

    fireEvent.click(within(box).getByRole('button', { name: 'Previous' }))
    expect(within(box).getByText('2 / 3')).toBeInTheDocument()

    fireEvent.click(within(box).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('drives the lightbox from the keyboard and closes on Escape', async () => {
    getApp.mockResolvedValue(installedApp({ manifest: WITH_SHOTS }))
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: 'Open screenshot 2' }))
    const box = screen.getByRole('dialog')
    expect(within(box).getByText('2 / 3')).toBeInTheDocument()

    fireEvent.keyDown(box, { key: 'ArrowRight' })
    expect(within(box).getByText('3 / 3')).toBeInTheDocument()
    // Already on the last frame: ArrowRight must not run past the end.
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'ArrowRight' })
    expect(within(screen.getByRole('dialog')).getByText('3 / 3')).toBeInTheDocument()

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'ArrowLeft' })
    expect(within(screen.getByRole('dialog')).getByText('2 / 3')).toBeInTheDocument()

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('dismisses the lightbox on the backdrop but not on the image itself', async () => {
    getApp.mockResolvedValue(installedApp({ manifest: WITH_SHOTS }))
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: 'Open screenshot 1' }))
    const box = screen.getByRole('dialog')

    // The wrapper around the full-size image stops the backdrop-dismiss, so a
    // click on the picture the user came to look at does not close it.
    fireEvent.click(box.firstElementChild as HTMLElement)
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('dialog'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('hides a screenshot thumbnail whose image fails to load', async () => {
    getApp.mockResolvedValue(installedApp({ manifest: WITH_SHOTS }))
    renderDetail()
    await loaded()

    const thumb = screen.getByAltText('Screenshot 1') as HTMLImageElement
    expect(thumb.style.display).toBe('')
    fireEvent.error(thumb)
    expect(thumb.style.display).toBe('none')
  })

  it('prefers the dark screenshot set when the resolved mode is dark', async () => {
    theme.mode = 'dark'
    getApp.mockResolvedValue(installedApp({ manifest: WITH_SHOTS }))
    renderDetail()
    await loaded()

    expect((screen.getByAltText('Screenshot 1') as HTMLImageElement).getAttribute('src'))
      .toBe('/shots/dark-one.png')
    // The dark set is shorter, so the third light shot must not leak through.
    expect(screen.queryByAltText('Screenshot 3')).not.toBeInTheDocument()
  })

  it('renders no screenshots section when the app ships none', async () => {
    getApp.mockResolvedValue(installedApp())
    renderDetail()
    await loaded()

    expect(screen.queryByText('Screenshots')).not.toBeInTheDocument()
  })

  it('hides a hero banner whose image fails to load', async () => {
    getApp.mockResolvedValue(installedApp({
      manifest: {
        displayName: 'Ledger Lens',
        description: 'Reads your books and explains them.',
        // The detail-ratio banner wins over the Browse hero and sizes its own
        // container, so both resolution arms are exercised here.
        heroImageDetail: '/hero/detail-light.png',
        heroImage: '/hero/browse-light.png',
      },
    }))
    renderDetail()
    await loaded()

    const hero = document.querySelector('img[src="/hero/detail-light.png"]') as HTMLImageElement
    expect(hero).not.toBeNull()
    expect(hero.parentElement?.className).toContain('aspect-[25/6]')
    expect(document.querySelector('img[src="/hero/browse-light.png"]')).toBeNull()

    fireEvent.error(hero)
    expect(hero.style.display).toBe('none')
  })

  // --- Install log panel ---------------------------------------------------

  it('streams install log lines and hands a failed install to the agent', async () => {
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    installFromRegistryStream.mockImplementation(async (_n: string, onLine: (l: string) => void) => {
      onLine('cloning repository')
      onLine('installing dependencies')
      return { ok: false, error: 'pip exploded' }
    })
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))

    expect(await screen.findByText('Install failed')).toBeInTheDocument()
    const log = document.querySelector('pre') as HTMLPreElement
    expect(log.textContent).toContain('cloning repository')
    expect(log.textContent).toContain('installing dependencies')
    expect(screen.getByText('pip exploded')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /fix with ai/i }))

    const launch = (window as Window & { __mc_chat_launch?: { message: string } }).__mc_chat_launch
    expect(launch?.message).toContain('installing dependencies')
    expect(launch?.message).toContain(`app-sources/${NAME}/`)
    expect(await screen.findByText('chat page')).toBeInTheDocument()
  })

  it('closes the install log once a successful install has landed', async () => {
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    installFromRegistryStream.mockImplementation(async (_n: string, onLine: (l: string) => void) => {
      onLine('done in 4s')
      return { ok: true }
    })
    const changed = watchAppsChanged()
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    expect(await screen.findByText('Install complete')).toBeInTheDocument()
    await waitFor(() => expect(changed.count()).toBeGreaterThan(0))
    changed.stop()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByText('Install complete')).not.toBeInTheDocument()
  })

  it('reports a rejected install stream with the thrown message', async () => {
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    installFromRegistryStream.mockRejectedValue(new Error('network down'))
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    expect(await screen.findByText('network down')).toBeInTheDocument()
    expect(screen.getByText('Install failed')).toBeInTheDocument()
  })

  it('aborts an in-flight install when the page unmounts', async () => {
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    let seen: AbortSignal | undefined
    installFromRegistryStream.mockImplementation(
      (_n: string, _l: unknown, signal: AbortSignal) => new Promise((_res, rej) => {
        seen = signal
        signal.addEventListener('abort', () => {
          rej(Object.assign(new Error('aborted'), { name: 'AbortError' }))
        })
      }),
    )
    const view = renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    // Both the action button and the log card title read "Installing…".
    await screen.findAllByText('Installing…')

    view.unmount()
    await waitFor(() => expect(seen?.aborted).toBe(true))
  })

  // --- Client-side install ------------------------------------------------

  it('shows the terminal instructions and copies the resolved command', async () => {
    system.mockResolvedValue({ hostname: 'dev-desk-1' })
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    installFromRegistryStream.mockResolvedValue({
      needsClientInstall: true,
      clientInstall: {
        shell: 'curl {{gateway_url}}/install.sh | sh',
        postInstall: 'open ssh://{{gateway_host}}',
      },
    })
    const writeText = vi.fn()
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    expect(await screen.findByText('Install on your Mac')).toBeInTheDocument()

    const resolved = `curl ${window.location.origin}/install.sh | sh`
    expect(screen.getByText(resolved)).toBeInTheDocument()
    expect(screen.getByText('open ssh://dev-desk-1')).toBeInTheDocument()
    // The Get button is replaced by a statement of the requirement.
    expect(screen.getByText('Requires local install')).toBeInTheDocument()
    // The streaming log panel is dropped: nothing landed on the gateway.
    expect(screen.queryByText('Install complete')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Copy command' }))
    expect(writeText).toHaveBeenCalledWith(resolved)
  })

  it('falls back to the app platform block and a host placeholder', async () => {
    // No hostname from the gateway and no clientInstall on the stream result:
    // both fallbacks fire at once.
    listRegistry.mockResolvedValue({
      apps: [registryRow({ platform: { installMode: 'client', clientInstall: { shell: 'brew install lens --host {{gateway_host}}' } } })],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    installFromRegistryStream.mockResolvedValue({ needsClientInstall: true })
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    expect(await screen.findByText('brew install lens --host <your-cloud-desktop-host>')).toBeInTheDocument()
    expect(screen.queryByText(/After installation, run/)).not.toBeInTheDocument()
  })

  it('clears the copied confirmation after two seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'darwin', arch: 'arm64' } })
      installFromRegistryStream.mockResolvedValue({
        needsClientInstall: true,
        clientInstall: { shell: 'brew install lens' },
      })
      const writeText = vi.fn()
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
      renderDetail()
      await loaded()

      fireEvent.click(screen.getByRole('button', { name: /install/i }))
      const copy = await screen.findByRole('button', { name: 'Copy command' })

      fireEvent.click(copy)
      await waitFor(() => expect(copy.querySelector('.lucide-check')).not.toBeNull())

      act(() => { vi.advanceTimersByTime(2100) })
      await waitFor(() => expect(copy.querySelector('.lucide-check')).toBeNull())
    } finally {
      vi.useRealTimers()
    }
  })

  // --- Trust consent retry -------------------------------------------------

  it('opens the consent modal when the install REJECTS with the denial code', async () => {
    // The non-streaming route answers 403 with the same code, and the client
    // keeps the payload as a JSON string on .body — so the catch arm has to
    // recognise it too, not only the resolved-payload arm.
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    installFromRegistryStream.mockRejectedValue(Object.assign(new Error(DENIED), {
      name: 'ApiError',
      status: 403,
      body: JSON.stringify({ ok: false, error: DENIED, code: 'app_execution_denied' }),
    }))
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    expect(await screen.findByText(TRUST_MODAL)).toBeInTheDocument()
    // A refusal is a consent prompt, not an error: no log panel, no red card.
    expect(screen.queryByText('Install failed')).not.toBeInTheDocument()
  })

  it('reports a second denial inline instead of closing on a silent no-op', async () => {
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    installFromRegistryStream.mockResolvedValue({ ok: false, error: DENIED, code: 'app_execution_denied' })
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    await screen.findByText(TRUST_MODAL)
    fireEvent.click(screen.getByRole('button', { name: /trust this app and enable/i }))

    // The grant did not take effect, so it is withdrawn and the modal stays up.
    await waitFor(() => expect(untrustApp).toHaveBeenCalledWith(NAME))
    expect(screen.getByText(TRUST_MODAL)).toBeInTheDocument()
  })

  it('re-runs the install after consent and closes the modal', async () => {
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    installFromRegistryStream
      .mockResolvedValueOnce({ ok: false, error: DENIED, code: 'app_execution_denied' })
      .mockResolvedValueOnce({ ok: true })
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    expect(await screen.findByText(TRUST_MODAL)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /trust this app and enable/i }))

    await waitFor(() => expect(trustApp).toHaveBeenCalledWith(NAME))
    await waitFor(() => expect(screen.queryByText(TRUST_MODAL)).not.toBeInTheDocument())
    expect(installFromRegistryStream).toHaveBeenCalledTimes(2)
  })

  it('keeps the modal open and rolls the grant back when the retry still fails', async () => {
    listRegistry.mockResolvedValue({ apps: [registryRow()], serverPlatform: { os: 'linux', arch: 'arm64' } })
    installFromRegistryStream
      .mockResolvedValueOnce({ ok: false, error: DENIED, code: 'app_execution_denied' })
      .mockResolvedValueOnce({ ok: false, error: 'still broken' })
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /install/i }))
    await screen.findByText(TRUST_MODAL)
    fireEvent.click(screen.getByRole('button', { name: /trust this app and enable/i }))

    // The retry rejected, so the grant it was written for owns nothing — the
    // gate withdraws it rather than leaving it over an absent app.
    await waitFor(() => expect(untrustApp).toHaveBeenCalledWith(NAME))
    expect(screen.getByText(TRUST_MODAL)).toBeInTheDocument()
  })

  // --- Installed-app actions ----------------------------------------------

  it('disables an installed app and announces the change', async () => {
    getApp.mockResolvedValue(installedApp())
    const changed = watchAppsChanged()
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /disable/i }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith(NAME))
    await waitFor(() => expect(changed.count()).toBeGreaterThan(0))
    changed.stop()
  })

  it('enables a disabled app through the shared enable path', async () => {
    getApp.mockResolvedValue(installedApp({ enabled: false }))
    const changed = watchAppsChanged()
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /enable/i }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith(NAME))
    await waitFor(() => expect(changed.count()).toBeGreaterThan(0))
    changed.stop()
  })

  it('syncs a gateway-managed app that has no update waiting', async () => {
    getApp.mockResolvedValue(installedApp())
    renderDetail()
    await loaded()

    expect(screen.queryByRole('button', { name: /^update$/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /sync/i }))
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith(NAME))
  })

  it('reports a failed sync inline and lets the user dismiss it', async () => {
    getApp.mockResolvedValue(installedApp())
    updateApp.mockRejectedValue(new Error('git conflict'))
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /sync/i }))
    expect(await screen.findByText('git conflict')).toBeInTheDocument()
    // A raw backend sentence is a dead end on its own, so the card offers the
    // hand-off alongside it.
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss error' }))
    expect(screen.queryByText('git conflict')).not.toBeInTheDocument()
  })

  it('offers the registry install path for a self-managed app with an update', async () => {
    getApp.mockResolvedValue(installedApp({ resources: 'app' }))
    listRegistry.mockResolvedValue({
      apps: [registryRow({ installed: true, updateAvailable: true })],
      serverPlatform: { os: 'linux', arch: 'arm64' },
    })
    renderDetail()
    await loaded()

    expect(screen.getByText('Self-managed')).toBeInTheDocument()
    expect(screen.getByText('Installed (v1.0.0)')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^update$/i }))
    await waitFor(() => expect(installFromRegistryStream).toHaveBeenCalled())
  })

  it('a locked built-in offers enable with the desktop requirement and no uninstall', async () => {
    getApp.mockResolvedValue(installedApp({
      origin: 'builtin',
      lifecycle: 'locked',
      enabled: false,
      manifest: {
        displayName: 'Ledger Lens',
        description: 'Reads your books and explains them.',
        platform: { requiresDesktopApp: true },
      },
    }))
    renderDetail()
    await loaded()

    expect(screen.getByText('Built-in')).toBeInTheDocument()
    expect(screen.getByText('Disabled')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /enable/i })).toBeInTheDocument()
    // Stated in text, not only in a hover title: a tooltip is unreachable by
    // touch or keyboard, and this page is where the decision happens.
    expect(screen.getByText(/the app's own window needs the Kiro Crew desktop app/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /uninstall/i })).not.toBeInTheDocument()
  })

  // --- Uninstall confirmation ---------------------------------------------

  /** Open the uninstall dialog on a gateway-managed installed app. */
  async function openUninstall() {
    getApp.mockResolvedValue(installedApp())
    renderDetail()
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: /uninstall/i }))
    return screen.findByRole('dialog', { name: /confirm uninstall/i })
  }

  it('keeps app data by default and cancels without calling the API', async () => {
    const box = await openUninstall()

    const keep = within(box).getByRole('checkbox', { name: /keep app data/i })
    expect(keep).toBeChecked()

    fireEvent.click(within(box).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /confirm uninstall/i })).not.toBeInTheDocument())
    expect(uninstallApp).not.toHaveBeenCalled()
  })

  it('passes the keep-data choice through and returns to the list', async () => {
    uninstallApp.mockResolvedValue({ ok: true, uninstall_log: 'removed 3 files' })
    const box = await openUninstall()

    fireEvent.click(within(box).getByRole('checkbox', { name: /keep app data/i }))
    fireEvent.click(within(box).getByRole('button', { name: 'Uninstall' }))

    await waitFor(() => expect(uninstallApp).toHaveBeenCalledWith(NAME, false))
    expect(await screen.findByText('apps list')).toBeInTheDocument()
  })

  it('keeps the data when the checkbox is left alone', async () => {
    const box = await openUninstall()

    fireEvent.click(within(box).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() => expect(uninstallApp).toHaveBeenCalledWith(NAME, true))
  })

  it('dismisses the uninstall dialog on Escape', async () => {
    const box = await openUninstall()

    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /confirm uninstall/i })).not.toBeInTheDocument())
  })

  it('closes the dialog and reports a failed uninstall', async () => {
    uninstallApp.mockRejectedValue(new Error('app is busy'))
    const box = await openUninstall()

    fireEvent.click(within(box).getByRole('button', { name: 'Uninstall' }))
    expect(await screen.findByText('app is busy')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: /confirm uninstall/i })).not.toBeInTheDocument()
  })

  // --- Info grid ----------------------------------------------------------

  it('renders features, permissions, MCP servers, tags, resources and details', async () => {
    getApp.mockResolvedValue(installedApp({
      manifest: {
        displayName: 'Ledger Lens',
        description: 'Reads your books and explains them.',
        author: 'zezhexu',
        minKiroCrewVersion: '0.2.0',
        highlights: ['Explains a balance sheet', 'Flags odd entries'],
        tags: ['finance', 'reporting'],
        agents: ['agents/auditor.json', 'agents/scribe.json'],
        skills: ['skills/ledger-read/SKILL.md'],
        crons: [{ name: 'nightly-close' }],
        permissions: {
          api: ['GET /api/ledger'],
          events: ['ledger.updated'],
          mcpTools: ['ledger_query'],
          storage: true,
          cron: true,
          network: true,
          memory: 'read',
        },
        mcpServers: {
          ledgerd: {
            url: 'http://127.0.0.1:9911/mcp',
            command: 'uvx ledgerd',
            autoApprove: ['ledger_read_only'],
          },
        },
      },
    }))
    listRegistry.mockResolvedValue({
      apps: [registryRow({ installed: true, repo: 'https://example.invalid/ledger-lens' })],
      serverPlatform: { os: 'linux', arch: 'arm64' },
    })
    renderDetail()
    await loaded()

    expect(screen.getByText('Features')).toBeInTheDocument()
    expect(screen.getByText('Explains a balance sheet')).toBeInTheDocument()

    expect(screen.getByText('Permissions')).toBeInTheDocument()
    expect(screen.getByText('API Access')).toBeInTheDocument()
    expect(screen.getByText('GET /api/ledger')).toBeInTheDocument()
    expect(screen.getByText('WebSocket Events')).toBeInTheDocument()
    expect(screen.getByText('ledger.updated')).toBeInTheDocument()
    expect(screen.getByText('MCP Tools')).toBeInTheDocument()
    expect(screen.getByText('ledger_query')).toBeInTheDocument()
    expect(screen.getByText('Storage: yes')).toBeInTheDocument()
    expect(screen.getByText('Cron: yes')).toBeInTheDocument()
    expect(screen.getByText('Network: yes')).toBeInTheDocument()
    expect(screen.getByText(/Memory:/)).toBeInTheDocument()

    expect(screen.getByText('MCP Servers')).toBeInTheDocument()
    expect(screen.getByText('ledgerd')).toBeInTheDocument()
    expect(screen.getByText('http://127.0.0.1:9911/mcp')).toBeInTheDocument()
    expect(screen.getByText('uvx ledgerd')).toBeInTheDocument()
    expect(screen.getByText('ledger_read_only')).toBeInTheDocument()

    expect(screen.getByText('Tags')).toBeInTheDocument()
    expect(screen.getByText('finance')).toBeInTheDocument()

    expect(screen.getByText('Resources')).toBeInTheDocument()
    // Agent paths are shown as bare stems, not manifest paths.
    expect(screen.getByText('auditor, scribe')).toBeInTheDocument()
    expect(screen.getByText('SKILL.md')).toBeInTheDocument()
    expect(screen.getByText('nightly-close')).toBeInTheDocument()

    expect(screen.getByText('Details')).toBeInTheDocument()
    expect(screen.getByText(/https:\/\/example\.invalid\/ledger-lens/)).toBeInTheDocument()
    expect(screen.getByText(/2026/)).toBeInTheDocument()
    expect(screen.getByText(/Origin:/)).toBeInTheDocument()
    expect(screen.getByText(/Min Kiro Crew: v0\.2\.0/)).toBeInTheDocument()
  })

  it('shows the platform list for a registry app and no resources card', async () => {
    listRegistry.mockResolvedValue({
      apps: [registryRow({ platform: { os: ['darwin', 'linux'] } })],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    renderDetail()
    await loaded()

    expect(screen.getByText(/darwin, linux/)).toBeInTheDocument()
    expect(screen.queryByText('Resources')).not.toBeInTheDocument()
    expect(screen.queryByText('Permissions')).not.toBeInTheDocument()
  })

  it('links back to the apps list from the page header', async () => {
    getApp.mockResolvedValue(installedApp())
    renderDetail()
    await loaded()

    fireEvent.click(screen.getByRole('button', { name: /back to apps/i }))
    expect(await screen.findByText('apps list')).toBeInTheDocument()
  })
})
