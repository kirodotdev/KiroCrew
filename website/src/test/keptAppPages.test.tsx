import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useKeepAlive } from '../app-sdk/keepAlive'
import AppHost from '../components/AppHost'
import InstalledAppSurface from '../components/InstalledAppSurface'
import { KeptAppPagesProvider } from '../components/KeptAppPages'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { KeptAppPagesStore, MAX_KEPT_APP_PAGES } from '../components/keptAppPagesStore'
import { useLocalCrewHidden } from '../components/overlayRoot'

const { getApp, handOffToAgent, lifecycle, renders } = vi.hoisted(() => ({
  getApp: vi.fn(),
  handOffToAgent: vi.fn(),
  lifecycle: { mounts: [] as string[], unmounts: [] as string[] },
  renders: { count: 0 },
}))

vi.mock('../api/client', () => ({ api: { getApp } }))
vi.mock('../lib/appViewTelemetry', () => ({ useAppViewMetrics: () => {} }))
vi.mock('../i18n/t', () => ({
  i18nT: (key: string, params?: { name?: string; n?: string; error?: string }) => {
    if (params?.error !== undefined) return `${key}:${params.name}:${params.error}`
    const value = params?.name ?? params?.n
    return value ? `${key}:${value}` : key
  },
}))
vi.mock('../components/ui', () => ({
  Btn: ({ children, danger: _danger, ...rest }: React.ButtonHTMLAttributes<HTMLButtonElement> & { danger?: boolean }) => (
    <button type="button" {...rest}>{children}</button>
  ),
}))
vi.mock('../components/ErrorNotice', () => ({
  default: ({ message, onDismiss, testId, impact, askAgent, gate }: {
    message: string
    onDismiss?: () => void
    testId?: string
    impact?: string
    askAgent?: boolean
    gate?: (proceed: () => void) => void
  }) => (
    <div data-testid={testId} data-impact={impact} data-ask-agent={String(askAgent ?? false)}>
      <span>{message}</span>
      {askAgent && (
        <button type="button" onClick={() => (gate ? gate(handOffToAgent) : handOffToAgent())}>ask-the-agent</button>
      )}
      {onDismiss && <button type="button" onClick={onDismiss}>dismiss-status-error</button>}
    </div>
  ),
}))
// The real AppHost loads a bundle over the network. This one renders the app the
// same way the real one does for keep-alive: the control it is handed becomes the
// KeepAliveContext the app's `useKeepAlive` reads. It reloads the window on its
// app's `mc:app-reload` unless `suppressReload` is set, and shows the reload notice
// instead while the real `useReloadHeldFor` holds it, as the real host does. An app
// named `boom` throws in the host's own body.
vi.mock('../components/AppHost', async () => {
  const { KeepAliveContext } = await import('../app-sdk/keepAlive')
  const { useReloadHeldFor } = await import('../components/keptAppReloadHold')
  const { useEffect: useHostEffect, useState: useHostState } = await import('react')
  function MockAppHost({ app, keepAlive, active, suppressReload = false }: {
    app: { name: string }
    keepAlive?: never
    active?: boolean
    suppressReload?: boolean
  }) {
    const holdReload = useReloadHeldFor(app.name)
    const [heldNotice, setHeldNotice] = useHostState(false)
    useHostEffect(() => {
      if (suppressReload) return
      const onAppReload = (e: Event) => {
        if ((e as CustomEvent<{ app?: string }>).detail?.app !== app.name) return
        if (holdReload) setHeldNotice(true)
        else window.location.reload()
      }
      window.addEventListener('mc:app-reload', onAppReload)
      return () => window.removeEventListener('mc:app-reload', onAppReload)
    }, [app.name, suppressReload, holdReload])
    if (app.name === 'boom') throw new Error('host body threw')
    if (heldNotice) return <div data-testid={`replaced-notice-${app.name}`} />
    return (
      <KeepAliveContext.Provider value={keepAlive ?? null}>
        <ProbeApp name={app.name} active={active !== false} />
      </KeepAliveContext.Provider>
    )
  }
  return { default: MockAppHost }
})

function ProbeApp({ name, active }: { name: string; active: boolean }) {
  const [busy, setBusy] = useState(false)
  const status = useKeepAlive(busy)
  const offScreen = useLocalCrewHidden()
  renders.count += 1
  useEffect(() => {
    lifecycle.mounts.push(name)
    return () => { lifecycle.unmounts.push(name) }
  }, [name])
  return (
    <div data-testid={`probe-${name}`} data-status={status} data-active={String(active)} data-off-screen={String(offScreen)}>
      <button type="button" onClick={() => setBusy(b => !b)}>{`toggle-${name}`}</button>
      {name === 'duo' && <DuoCallers />}
    </div>
  )
}

/** More `useKeepAlive` callers inside one app: two that record, one that mounts
 *  idle on demand, and a remount (key change) of the first. */
function DuoCallers() {
  const [firstKey, setFirstKey] = useState(0)
  const [secondMounted, setSecondMounted] = useState(true)
  const [idleMounted, setIdleMounted] = useState(false)
  return (
    <>
      <Caller key={firstKey} id="duo-1" initialBusy={firstKey > 0} />
      {secondMounted && <Caller id="duo-2" />}
      {idleMounted && <Caller id="duo-idle" />}
      <button type="button" onClick={() => setFirstKey(k => k + 1)}>remount-duo-1</button>
      <button type="button" onClick={() => setSecondMounted(false)}>unmount-duo-2</button>
      <button type="button" onClick={() => setIdleMounted(true)}>mount-duo-idle</button>
    </>
  )
}

function Caller({ id, initialBusy = false }: { id: string; initialBusy?: boolean }) {
  const [busy, setBusy] = useState(initialBusy)
  const status = useKeepAlive(busy)
  return (
    <span data-testid={`caller-${id}`} data-status={status}>
      <button type="button" onClick={() => setBusy(b => !b)}>{`toggle-${id}`}</button>
    </span>
  )
}

const record = (name: string) => ({ name, displayName: name, enabled: true, manifest: { ui: { entry: 'index.mjs' } } })

function RoutedApp() {
  const { name = '' } = useParams()
  return <InstalledAppSurface app={record(name)} requestedName={name} />
}

function Nav() {
  const navigate = useNavigate()
  const location = useLocation()
  return (
    <>
      <span data-testid="location">{location.pathname + location.search + location.hash}</span>
      <button type="button" onClick={() => navigate('/chat')}>go-chat</button>
      <button type="button" onClick={() => navigate('/chat-panel')}>go-chat-panel</button>
      {['a', 'b', 'c'].map(n => <button key={n} type="button" onClick={() => navigate(`/apps/${n}`)}>{`go-${n}`}</button>)}
    </>
  )
}

function NavigationVeto({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  return null
}

/** Renders again for a reason of its own, as App does on the metrics poll. It builds
 *  the provider tree in its own render, so each render hands the provider a fresh
 *  element, as App does; a `children` element would be skipped as unchanged. */
function ShellRerender({ render: renderTree }: { render: () => React.ReactNode }) {
  const [, setGeneration] = useState(0)
  return (
    <>
      <button type="button" onClick={() => setGeneration(g => g + 1)}>rerender-shell</button>
      {renderTree()}
    </>
  )
}

function renderShell(start = '/apps/a', guard?: () => boolean) {
  return render(
    <MemoryRouter initialEntries={[start]}>
      <NavigationLeaveGuardProvider>
        {guard && <NavigationVeto guard={guard} />}
        <ShellRerender render={() => (
          <KeptAppPagesProvider>
            <Routes>
              <Route path="/apps/:name" element={<RoutedApp />} />
              <Route path="/chat" element={<div data-testid="chat-page" />} />
              {/* A host the kept-page layer does not render, as a chat side-panel tab's body is. */}
              <Route path="/chat-panel" element={<AppHost app={record('panel-app')} />} />
            </Routes>
            <Nav />
          </KeptAppPagesProvider>
        )} />
      </NavigationLeaveGuardProvider>
    </MemoryRouter>,
  )
}

const click = (label: string) => fireEvent.click(screen.getByText(label))

const announceReplacedFiles = (app: string) => act(() => {
  window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app } }))
})

function spyOnWindowReload() {
  const original = window.location.reload
  const reload = vi.fn()
  Object.defineProperty(window.location, 'reload', { configurable: true, value: reload })
  onTestFinished(() => {
    Object.defineProperty(window.location, 'reload', { configurable: true, value: original })
  })
  return reload
}

beforeEach(() => {
  getApp.mockImplementation((name: string) => Promise.resolve(record(name)))
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
  lifecycle.mounts.length = 0
  lifecycle.unmounts.length = 0
})

describe('kept app pages', () => {
  it('closes a page that is not busy when the person leaves it, as before', () => {
    renderShell()
    expect(screen.getByTestId('probe-a').dataset.status).toBe('off')
    click('go-chat')
    expect(screen.queryByTestId('probe-a')).toBeNull()
    expect(lifecycle.unmounts).toEqual(['a'])
    expect(screen.queryByTestId('kept-app-indicator')).toBeNull()
  })

  it('keeps a busy page mounted and hidden, names it, and Open returns to the same instance', () => {
    renderShell()
    click('toggle-a')
    expect(screen.getByTestId('probe-a').dataset.status).toBe('on')
    click('go-chat')
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    expect(lifecycle.unmounts).toEqual([])
    const probe = screen.getByTestId('probe-a')
    expect(probe.dataset.active).toBe('false')
    expect(probe.closest('[data-testid="kept-app-park"]')).not.toBeNull()
    expect(screen.getByTestId('kept-app-indicator').textContent).toContain('components.keptAppPages.running:a')

    click('components.keptAppPages.open_named:a')
    expect(screen.getByTestId('probe-a').closest('[data-testid="kept-app-slot"]')).not.toBeNull()
    expect(screen.getByTestId('probe-a').dataset.active).toBe('true')
    expect(lifecycle.mounts).toEqual(['a'])
    expect(lifecycle.unmounts).toEqual([])
    expect(screen.queryByTestId('kept-app-indicator')).toBeNull()
  })

  it('keeps the current page when its leave guard refuses Open', () => {
    const guard = vi.fn(() => false)
    renderShell('/apps/a', guard)
    click('toggle-a')
    click('go-chat')

    click('components.keptAppPages.open_named:a')

    expect(guard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('location').textContent).toBe('/chat')
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    expect(screen.getByTestId('probe-a').dataset.active).toBe('false')
    expect(screen.getByTestId('kept-app-indicator')).toBeInTheDocument()
  })

  it('unmounts the hidden page when the person presses Stop', () => {
    renderShell()
    click('toggle-a')
    click('go-chat')
    click('components.keptAppPages.stop_named:a')
    expect(screen.queryByTestId('probe-a')).toBeNull()
    expect(lifecycle.unmounts).toEqual(['a'])
    expect(screen.queryByTestId('kept-app-indicator')).toBeNull()
  })

  it('shows each hidden app as a row in the page flow with exactly Open and Stop', () => {
    renderShell()
    click('toggle-a')
    click('go-b')
    click('toggle-b')
    click('go-chat')
    const indicator = screen.getByTestId('kept-app-indicator')
    expect(indicator.className.split(/\s+/)).not.toContain('fixed')
    expect(indicator.className.split(/\s+/)).toContain('sticky')
    const rows = screen.getAllByTestId('kept-app-row')
    expect(rows).toHaveLength(2)
    for (const row of rows) {
      expect(Array.from(row.querySelectorAll('button')).map(b => b.textContent)).toEqual([
        expect.stringMatching(/^components\.keptAppPages\.open_named:/),
        expect.stringMatching(/^components\.keptAppPages\.stop_named:/),
      ])
    }
  })

  it('lets long app action labels shrink and wrap within a narrow row', () => {
    const name = 'Meeting Recorder with Live Transcription and Collaborative Notes'
    renderShell(`/apps/${encodeURIComponent(name)}`)
    click(`toggle-${name}`)
    click('go-chat')

    const open = screen.getByRole('button', { name: `components.keptAppPages.open_named:${name}` })
    const stop = screen.getByRole('button', { name: `components.keptAppPages.stop_named:${name}` })
    const actions = open.parentElement
    expect(actions).toBe(stop.parentElement)
    expect(actions).toHaveClass('min-w-0', 'max-w-full', 'flex-wrap')
    for (const button of [open, stop]) {
      expect(button).toHaveClass('min-w-0', 'max-w-full', 'whitespace-normal', 'break-words', 'text-left')
      expect(button).not.toHaveClass('whitespace-nowrap')
    }
  })

  it('takes a hidden page off screen for its own overlays and key handlers, and back when it is shown', () => {
    renderShell()
    click('toggle-a')
    expect(screen.getByTestId('probe-a').dataset.offScreen).toBe('false')
    click('go-chat')
    expect(screen.getByTestId('probe-a').dataset.offScreen).toBe('true')
    click('components.keptAppPages.open_named:a')
    expect(screen.getByTestId('probe-a').dataset.offScreen).toBe('false')
  })

  it('releases a hidden page as soon as its app says it is no longer busy', async () => {
    renderShell()
    click('toggle-a')
    click('go-chat')
    click('toggle-a')
    await waitFor(() => expect(screen.queryByTestId('probe-a')).toBeNull())
    expect(lifecycle.unmounts).toEqual(['a'])
  })

  // One app, several `useKeepAlive` callers. The page settles a commit after the
  // last holder lets go, so each "kept" check first lets that settle run.
  const settle = () => act(async () => { await Promise.resolve() })

  it('keeps a hidden page while another caller is still busy, and releases it when the last one stops', async () => {
    renderShell('/apps/duo')
    click('toggle-duo-1')
    click('toggle-duo-2')
    click('go-chat')
    click('toggle-duo-1')
    await settle()
    expect(screen.getByTestId('probe-duo')).toBeInTheDocument()
    expect(screen.getByTestId('caller-duo-2').dataset.status).toBe('on')
    expect(screen.getByTestId('kept-app-indicator')).toBeInTheDocument()

    click('toggle-duo-2')
    await waitFor(() => expect(screen.queryByTestId('probe-duo')).toBeNull())
  })

  it('keeps a hidden page when a caller mounts with busy false', async () => {
    renderShell('/apps/duo')
    click('toggle-duo-1')
    click('go-chat')
    click('mount-duo-idle')
    await settle()
    expect(screen.getByTestId('caller-duo-idle').dataset.status).toBe('off')
    expect(screen.getByTestId('caller-duo-1').dataset.status).toBe('on')
    expect(screen.getByTestId('kept-app-indicator')).toBeInTheDocument()
  })

  it('keeps a hidden page when one busy caller unmounts while another is busy', async () => {
    renderShell('/apps/duo')
    click('toggle-duo-1')
    click('toggle-duo-2')
    click('go-chat')
    click('unmount-duo-2')
    await settle()
    expect(screen.getByTestId('probe-duo')).toBeInTheDocument()
    expect(screen.getByTestId('kept-app-indicator')).toBeInTheDocument()
  })

  it('keeps a hidden page when its only busy caller remounts busy', async () => {
    renderShell('/apps/duo')
    click('toggle-duo-1')
    click('go-chat')
    // A key change: the old caller's cleanup and the new busy caller's request land in one commit.
    click('remount-duo-1')
    await settle()
    expect(screen.getByTestId('probe-duo')).toBeInTheDocument()
    expect(screen.getByTestId('caller-duo-1').dataset.status).toBe('on')
  })

  it('keeps a page on screen kept when one of two callers stops before the person leaves', async () => {
    renderShell('/apps/duo')
    click('toggle-duo-1')
    click('toggle-duo-2')
    click('toggle-duo-1')
    await settle()
    click('go-chat')
    expect(screen.getByTestId('probe-duo')).toBeInTheDocument()
    expect(screen.getByTestId('kept-app-indicator')).toBeInTheDocument()
  })

  it('does not draw kept pages again when the shell renders for a reason of its own', () => {
    renderShell()
    click('toggle-a')
    click('go-chat')
    const before = renders.count
    click('rerender-shell')
    click('rerender-shell')
    expect(renders.count).toBe(before)
  })

  it('keeps a throw in one page host inside that page', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    renderShell('/apps/boom')
    expect(screen.getByText('go-chat')).toBeInTheDocument()
    expect(screen.getByTestId('location').textContent).toBe('/apps/boom')
  })

  it(`refuses a keep-alive past ${MAX_KEPT_APP_PAGES} apps`, () => {
    renderShell()
    click('toggle-a')
    click('go-b')
    click('toggle-b')
    click('go-c')
    click('toggle-c')
    expect(screen.getByTestId('probe-a').dataset.status).toBe('on')
    expect(screen.getByTestId('probe-b').dataset.status).toBe('on')
    expect(screen.getByTestId('probe-c').dataset.status).toBe('refused')
    click('go-chat')
    expect(screen.queryByTestId('probe-c')).toBeNull()
    expect(screen.getAllByText(/^components\.keptAppPages\.running:/)).toHaveLength(2)
  })

  it.each([
    ['turned off', () => getApp.mockResolvedValue({ ...record('a'), enabled: false })],
    ['uninstalled', () => getApp.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))],
  ])('releases a hidden page whose app was %s', async (_label, answer) => {
    answer()
    renderShell()
    click('toggle-a')
    click('go-chat')
    act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
    await waitFor(() => expect(screen.queryByTestId('probe-a')).toBeNull())
    expect(getApp).toHaveBeenCalledWith('a')
  })

  it('re-reads a page the moment it is hidden, so an app turned off on screen is released', async () => {
    getApp.mockResolvedValue({ ...record('a'), enabled: false })
    renderShell()
    click('toggle-a')
    click('go-chat')
    await waitFor(() => expect(screen.queryByTestId('probe-a')).toBeNull())
    expect(getApp).toHaveBeenCalledWith('a')
  })

  it('Open after switching to another app returns to the first app, not the second', () => {
    renderShell()
    click('toggle-a')
    click('go-b')
    click('go-chat')
    click('components.keptAppPages.open_named:a')
    expect(screen.getByTestId('location').textContent).toBe('/apps/a')
    expect(lifecycle.mounts.filter(n => n === 'a')).toHaveLength(1)
  })

  it('Open returns to the URL the page opened at, query included', () => {
    getApp.mockResolvedValue(record('a'))
    renderShell('/apps/a?view=live#now')
    click('toggle-a')
    click('go-chat')
    click('components.keptAppPages.open_named:a')
    expect(screen.getByTestId('location').textContent).toBe('/apps/a?view=live#now')
  })

  it('keeps a hidden page whose app is still on', async () => {
    getApp.mockResolvedValue(record('a'))
    renderShell()
    click('toggle-a')
    click('go-chat')
    act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
    await waitFor(() => expect(getApp).toHaveBeenCalledWith('a'))
    await act(async () => { await Promise.resolve() })
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()
  })

  it('shows a partial error without stopping the page when its status check fails, then clears it after a successful re-read', async () => {
    getApp.mockRejectedValue(Object.assign(new Error('service unavailable'), { status: 503 }))
    renderShell()
    click('toggle-a')
    click('go-chat')

    const notice = await screen.findByTestId('kept-app-status-error-a')
    expect(notice).toHaveTextContent('components.keptAppPages.status_check_failed:a:service unavailable')
    expect(notice.dataset.impact).toBe('partial')
    expect(notice.dataset.askAgent).toBe('true')
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()

    getApp.mockResolvedValue(record('a'))
    act(() => { window.dispatchEvent(new Event('mc:apps-changed')) })
    await waitFor(() => expect(screen.queryByTestId('kept-app-status-error-a')).toBeNull())
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()
  })

  it('offers the agent hand-off on a failed status check, asking the current page first', async () => {
    getApp.mockRejectedValue(Object.assign(new Error('service unavailable'), { status: 503 }))
    let mayLeave = false
    renderShell('/apps/a', () => mayLeave)
    click('toggle-a')
    click('go-chat')

    const notice = await screen.findByTestId('kept-app-status-error-a')
    fireEvent.click(within(notice).getByText('ask-the-agent'))
    expect(handOffToAgent).not.toHaveBeenCalled()

    mayLeave = true
    fireEvent.click(within(notice).getByText('ask-the-agent'))
    expect(handOffToAgent).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()
  })

  it('dismisses a status-check error without stopping the hidden page', async () => {
    getApp.mockRejectedValue(Object.assign(new Error('network unavailable'), { status: 503 }))
    renderShell()
    click('toggle-a')
    click('go-chat')

    await screen.findByTestId('kept-app-status-error-a')
    click('dismiss-status-error')
    expect(screen.queryByTestId('kept-app-status-error-a')).toBeNull()
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()
  })

  it('releases a hidden page whose files were replaced instead of reloading the dashboard', () => {
    const reload = spyOnWindowReload()
    renderShell()
    click('toggle-a')
    click('go-chat')

    announceReplacedFiles('b')
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()

    announceReplacedFiles('a')
    expect(screen.queryByTestId('probe-a')).toBeNull()
    expect(lifecycle.unmounts).toEqual(['a'])
    expect(screen.queryByTestId('kept-app-indicator')).toBeNull()
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    expect(reload).not.toHaveBeenCalled()
  })

  it('still reloads for a page on screen whose files were replaced, including one shown again', () => {
    const reload = spyOnWindowReload()
    renderShell()
    announceReplacedFiles('a')
    expect(reload).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()

    click('toggle-a')
    click('go-chat')
    click('components.keptAppPages.open_named:a')
    expect(reload).toHaveBeenCalledTimes(1)
    announceReplacedFiles('a')
    expect(reload).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('probe-a').closest('[data-testid="kept-app-slot"]')).not.toBeNull()
  })

  it('shows the reload notice, not a window reload, for a page on screen while another page is kept', () => {
    const reload = spyOnWindowReload()
    renderShell()
    click('toggle-a')
    click('go-b')
    announceReplacedFiles('b')
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByTestId('replaced-notice-b')).toBeInTheDocument()
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()
  })

  it('holds the reload for a host outside the layer, such as a side-panel tab, while another page is kept', () => {
    const reload = spyOnWindowReload()
    renderShell()
    click('toggle-a')
    click('go-chat-panel')
    announceReplacedFiles('panel-app')
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByTestId('replaced-notice-panel-app')).toBeInTheDocument()
    expect(screen.getByTestId('probe-a')).toBeInTheDocument()
  })

  it('still reloads for a host outside the layer when no page is kept', () => {
    const reload = spyOnWindowReload()
    renderShell('/chat-panel')
    announceReplacedFiles('panel-app')
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('describes the Stop button with what stopping ends', () => {
    renderShell()
    click('toggle-a')
    click('go-chat')
    const stop = screen.getByRole('button', { name: 'components.keptAppPages.stop_named:a' })
    expect(stop).toHaveAccessibleDescription('components.keptAppPages.stop_hint:a')
    expect(within(screen.getByTestId('kept-app-row')).getByText('components.keptAppPages.stop_hint:a')).toBeVisible()
  })

  it('answers unsupported on a surface that cannot keep the page', () => {
    render(<ProbeApp name="panel" active />)
    click('toggle-panel')
    expect(screen.getByTestId('probe-panel').dataset.status).toBe('unsupported')
  })
})

describe('KeptAppPagesStore', () => {
  it('drops a hidden page when its app crashes and keeps a visible one on its crash notice', () => {
    const store = new KeptAppPagesStore()
    const slotA = document.createElement('div')
    const slotB = document.createElement('div')
    store.attach(record('a'), slotA, '/apps/a')
    store.attach(record('b'), slotB, '/apps/b')
    store.request('a', {}, true)
    store.request('b', {}, true)
    store.detach('a', slotA)
    store.crashed('a')
    store.crashed('b')
    expect(store.getSnapshot().map(p => [p.app.name, p.busy])).toEqual([['b', false]])
  })

  it('counts the cap per app, so a second caller of a kept app is never refused', () => {
    const store = new KeptAppPagesStore()
    for (const name of ['a', 'b', 'c']) store.attach(record(name), document.createElement('div'), `/apps/${name}`)
    expect(store.request('a', {}, true)).toBe('on')
    expect(store.request('b', {}, true)).toBe('on')
    expect(store.request('a', {}, true)).toBe('on')
    expect(store.request('c', {}, true)).toBe('refused')
  })

  it('ignores a release from a caller that holds nothing, including one refused', async () => {
    const store = new KeptAppPagesStore()
    for (const name of ['a', 'b', 'c']) store.attach(record(name), document.createElement('div'), `/apps/${name}`)
    store.request('a', {}, true)
    store.request('b', {}, true)
    const refused = {}
    expect(store.request('c', refused, true)).toBe('refused')
    expect(store.request('a', {}, false)).toBe('off')
    expect(store.request('c', refused, false)).toBe('off')
    await Promise.resolve()
    expect(store.getSnapshot().map(p => [p.app.name, p.busy])).toEqual([['a', true], ['b', true], ['c', false]])
  })

  it('starts a crashed page from no holders, so a holder from before the crash changes nothing', async () => {
    const store = new KeptAppPagesStore()
    store.attach(record('a'), document.createElement('div'), '/apps/a')
    const beforeCrash = {}
    store.request('a', beforeCrash, true)
    store.crashed('a')
    const afterRetry = {}
    store.request('a', afterRetry, true)
    store.request('a', beforeCrash, false)
    await Promise.resolve()
    expect(store.getSnapshot()[0]?.busy).toBe(true)
  })

  it('forgets a page holders when the page goes, so it comes back not busy', async () => {
    const store = new KeptAppPagesStore()
    const slot = document.createElement('div')
    store.attach(record('a'), slot, '/apps/a')
    const holder = {}
    store.request('a', holder, true)
    store.detach('a', slot)
    store.releaseHidden('a')
    store.attach(record('a'), slot, '/apps/a')
    expect(store.getSnapshot()[0]?.busy).toBe(false)
    expect(store.request('a', holder, false)).toBe('off')
    await Promise.resolve()
    store.detach('a', slot)
    expect(store.getSnapshot()).toEqual([])
  })

  it('ignores a detach from a slot the page has already left', () => {
    const store = new KeptAppPagesStore()
    const first = document.createElement('div')
    const second = document.createElement('div')
    store.attach(record('a'), first, '/apps/a')
    store.attach(record('a'), second, '/apps/a')
    store.detach('a', first)
    expect(store.getSnapshot()[0]?.slot).toBe(second)
  })
})
