import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, fireEvent, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests } from '../hooks/useNotificationSound'
import { HAPTICS_PLUGIN_NAME, HAPTICS_PLUGIN_ORIGIN, MC_MOUSE_HAPTICS_RECHECK_EVENT, MOUSE_HAPTICS_ENABLED_KEY } from '../hooks/useMouseHaptics'
import {
  __resetErrorJournalForTests, __resetNavSeamForTests, consumeChatHandoff,
  installSoftNavigate, recentErrors, TRANSPORT_REJECTION_COOLDOWN_MS,
} from '../utils/errorReport'

const STATUS_URL = `${HAPTICS_PLUGIN_ORIGIN}/v1/status`
const SOUND_SETTINGS_KEY = 'mc-notification-sound'

/** Rendered on the Sound rail item, where the Mouse haptics switch lives. */
function renderSoundPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={['/settings?tab=notifications&sub=sound']}><QueryClientProvider client={qc}><NotificationsPanel /></QueryClientProvider></MemoryRouter>)
}

/** Counts every request the page sends to the plugin, answering each as an installed plugin would. */
function installPlugin(): { requests: () => number } {
  let requests = 0
  server.use(http.get(STATUS_URL, () => {
    requests += 1
    return HttpResponse.json({ plugin: HAPTICS_PLUGIN_NAME, api: 1, events: ['turn_done', 'needs_input', 'notification'] })
  }))
  return { requests: () => requests }
}

const HAPTICS_LABEL = 'Logitech mouse haptics'
const NEEDS_SOUND_LINE = 'Turn on “Play sound on new notifications” to use mouse haptics.'
const PLUGIN_UNREACHABLE = 'The plugin could not be reached on this computer. Logi Options+ may be closed, or the plugin may not be installed yet.'
const SAVE_FAILED = 'Could not save this setting. This browser’s storage may be full or blocked.'
const hapticsSwitch = () => screen.getByRole('switch', { name: HAPTICS_LABEL })
const turnHapticsOn = () => localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '1')

/**
 * Refuses every write of the haptics key with a SecurityError, the browser's answer when storage is
 * blocked by policy. Not a quota error on purpose: that would send `safeSetItem` through its reclaim
 * path, and this pins the switch's reaction to a refusal, not the reclaim. Other keys still land.
 * Returns a function that lets writes through again; `afterEach` restores the spy regardless.
 */
function refuseHapticsWrites(): { allow: () => void } {
  let refused = true
  const realSet = Storage.prototype.setItem
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (refused && key === MOUSE_HAPTICS_ENABLED_KEY) throw new DOMException('blocked', 'SecurityError')
    realSet.call(this, key, value)
  })
  return { allow: () => { refused = false } }
}

/** Counts calls toward the plugin where the page makes them, before MSW answers any. */
function spyOnPluginFetches(): () => number {
  const fetchSpy = vi.spyOn(window, 'fetch')
  return () => fetchSpy.mock.calls.filter(([input]) => {
    const url = input instanceof Request ? input.url : String(input)
    return url.startsWith(HAPTICS_PLUGIN_ORIGIN)
  }).length
}

// The status query calls fetch as soon as it starts, and it starts while React runs effects,
// so after this flush a probe the page should not send would already have been counted.
const flushEffects = () => act(async () => {})

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  __resetForTests()
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  installSoftNavigate(() => {})
})

afterEach(() => {
  vi.useRealTimers()
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('NotificationsPanel mouse haptics', () => {
  it('names the device in the label and starts off, with no plugin status and no probe', async () => {
    installPlugin()
    const pluginFetches = spyOnPluginFetches()
    renderSoundPage()
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('false')
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBeNull()
    expect(screen.queryByTestId('mouse-haptics-status')).toBeNull()
    expect(screen.queryByText(/Plugin not found/)).toBeNull()
    await flushEffects()
    expect(pluginFetches()).toBe(0)
  })

  it('turning it on persists the choice and reports a connected plugin', async () => {
    const plugin = installPlugin()
    renderSoundPage()
    fireEvent.click(hapticsSwitch())
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBe('1')
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('true')
    expect(await screen.findByText(/Plugin connected/)).toBeTruthy()
    expect(plugin.requests()).toBe(1)
  })

  it('a refused save leaves the switch off and says so, with the key handed to the agent', async () => {
    installPlugin()
    const writes = refuseHapticsWrites()
    renderSoundPage()
    fireEvent.click(hapticsSwitch())
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('false')
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBeNull()
    expect(screen.queryByTestId('mouse-haptics-status')).toBeNull()
    const notice = screen.getByTestId('mouse-haptics-save-failed')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(SAVE_FAILED)
    expect(within(notice).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({
      source: 'system',
      message: SAVE_FAILED,
      code: 'storage_write_refused',
      detail: MOUSE_HAPTICS_ENABLED_KEY,
    })

    writes.allow()
    fireEvent.click(hapticsSwitch())
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBe('1')
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByTestId('mouse-haptics-save-failed')).toBeNull()
    expect(recentErrors()).toHaveLength(1)
    expect(await screen.findByText(/Plugin connected/)).toBeTruthy()
  })

  it('a refused save leaves the switch on, and Dismiss clears the notice', async () => {
    turnHapticsOn()
    installPlugin()
    renderSoundPage()
    await screen.findByText(/Plugin connected/)
    refuseHapticsWrites()
    fireEvent.click(hapticsSwitch())
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('true')
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBe('1')
    expect(screen.getByTestId('mouse-haptics-status')).toBeInTheDocument()
    const notice = screen.getByTestId('mouse-haptics-save-failed')
    expect(notice).toHaveTextContent(SAVE_FAILED)
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({ code: 'storage_write_refused', detail: MOUSE_HAPTICS_ENABLED_KEY })

    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('mouse-haptics-save-failed')).toBeNull()
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('true')
  })

  it('journals one report per refused save', async () => {
    installPlugin()
    refuseHapticsWrites()
    renderSoundPage()
    fireEvent.click(hapticsSwitch())
    fireEvent.click(hapticsSwitch())
    expect(screen.getAllByTestId('mouse-haptics-save-failed')).toHaveLength(1)
    expect(recentErrors()).toHaveLength(2)
    expect(recentErrors().every(r => r.code === 'storage_write_refused')).toBe(true)
  })

  it('shows a recoverable failure when a successful response body fails to arrive', async () => {
    turnHapticsOn()
    vi.spyOn(window, 'fetch').mockResolvedValueOnce(new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error(new TypeError('response body disconnected'))
      },
    }), { status: 200 }))
    renderSoundPage()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(PLUGIN_UNREACHABLE)
    expect(screen.queryByText(/Plugin not found/)).toBeNull()
    expect(within(alert).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({
      message: PLUGIN_UNREACHABLE,
      method: 'GET',
      endpoint: STATUS_URL,
      code: 'network',
    })
  })

  it('shows a recoverable failure and hands the rejected request context to the agent', async () => {
    turnHapticsOn()
    vi.spyOn(window, 'fetch').mockRejectedValueOnce(new TypeError('plugin refused connection'))
    renderSoundPage()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(PLUGIN_UNREACHABLE)
    expect(alert.closest('[role="status"]')).toBeNull()
    const handoff = within(alert).getByRole('button', { name: /Ask the agent/i })
    expect(handoff).toBeInTheDocument()
    fireEvent.click(handoff)
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain('GET')
    expect(staged).toContain(STATUS_URL)
    expect(staged).toContain('network')
    expect(staged).not.toContain('plugin refused connection')
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({
      message: PLUGIN_UNREACHABLE,
      method: 'GET',
      endpoint: STATUS_URL,
      code: 'network',
    })
    expect(recentErrors()[0].detail).toBeUndefined()
    expect(screen.getByText(/Install and uninstall plugins, then open the plugin file while that page is showing/)).toBeTruthy()
    const setupLink = screen.getByRole('link', { name: 'How to get the plugin' })
    expect(setupLink.getAttribute('href')).toBe('https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/dashboard.md#mouse-haptics')
    expect(setupLink.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('does not add another journal entry for a refused probe inside the cooldown', async () => {
    const pinnedTime = new Date('2026-01-01T00:00:00Z')
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(pinnedTime)
    turnHapticsOn()
    const fetchSpy = vi.spyOn(window, 'fetch').mockRejectedValue(new TypeError('plugin refused connection'))
    const firstRender = renderSoundPage()
    await screen.findByRole('alert')
    expect(recentErrors()).toHaveLength(1)
    firstRender.unmount()

    renderSoundPage()
    await screen.findByRole('alert')
    expect(fetchSpy).toHaveBeenCalledTimes(2)
    expect(recentErrors()).toHaveLength(1)
  })

  it('adds another journal entry for a refused probe after the cooldown', async () => {
    const pinnedTime = new Date('2026-01-01T00:00:00Z')
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(pinnedTime)
    turnHapticsOn()
    const fetchSpy = vi.spyOn(window, 'fetch').mockRejectedValue(new TypeError('plugin refused connection'))
    const firstRender = renderSoundPage()
    await screen.findByRole('alert')
    expect(recentErrors()).toHaveLength(1)
    firstRender.unmount()

    vi.setSystemTime(pinnedTime.getTime() + TRANSPORT_REJECTION_COOLDOWN_MS)
    renderSoundPage()
    await screen.findByRole('alert')
    expect(fetchSpy).toHaveBeenCalledTimes(2)
    expect(recentErrors()).toHaveLength(2)
  })

  it('shows a recoverable failure and hands the non-2xx response context to the agent', async () => {
    turnHapticsOn()
    server.use(http.get(STATUS_URL, () => new HttpResponse(null, { status: 503 })))
    renderSoundPage()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(PLUGIN_UNREACHABLE)
    fireEvent.click(within(alert).getByRole('button', { name: /Ask the agent/i }))
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain('GET')
    expect(staged).toContain(STATUS_URL)
    expect(staged).toContain('503')
    expect(staged).toContain('haptics_plugin_probe_http_error')
    expect(staged).toContain('Error: Haptics plugin probe failed with status 503')
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({
      message: PLUGIN_UNREACHABLE,
      method: 'GET',
      endpoint: STATUS_URL,
      status: 503,
      code: 'haptics_plugin_probe_http_error',
      detail: 'Error: Haptics plugin probe failed with status 503',
    })
    expect(screen.getByRole('link', { name: 'How to get the plugin' })).toBeInTheDocument()
  })

  it('shows not-found guidance without an alert when a successful answer is not the plugin', async () => {
    turnHapticsOn()
    server.use(http.get(STATUS_URL, () => HttpResponse.json({ plugin: 'SomethingElse', api: 1 })))
    renderSoundPage()
    expect(await screen.findByText(/Plugin not found/)).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('link', { name: 'How to get the plugin' })).toBeInTheDocument()
  })

  it('turning it off persists the choice and drops the plugin status', async () => {
    turnHapticsOn()
    installPlugin()
    renderSoundPage()
    await screen.findByText(/Plugin connected/)
    fireEvent.click(hapticsSwitch())
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBe('0')
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('false')
    expect(screen.queryByTestId('mouse-haptics-status')).toBeNull()
  })

  it('does not look for the plugin while the switch is off', async () => {
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '0')
    installPlugin()
    const pluginFetches = spyOnPluginFetches()
    renderSoundPage()
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('false')
    expect(screen.queryByTestId('mouse-haptics-status')).toBeNull()
    await flushEffects()
    expect(pluginFetches()).toBe(0)
  })

  it('is inert while notification sound is off, and says so on the row', async () => {
    localStorage.setItem(SOUND_SETTINGS_KEY, JSON.stringify({ enabled: false }))
    turnHapticsOn()
    installPlugin()
    const pluginFetches = spyOnPluginFetches()
    renderSoundPage()
    expect(hapticsSwitch().hasAttribute('disabled') || hapticsSwitch().getAttribute('aria-disabled') === 'true').toBe(true)
    expect(screen.getByText(NEEDS_SOUND_LINE)).toBeTruthy()
    expect(hapticsSwitch()).toHaveAccessibleDescription(NEEDS_SOUND_LINE)
    expect(screen.queryByTestId('mouse-haptics-status')).toBeNull()
    await flushEffects()
    expect(pluginFetches()).toBe(0)
  })

  it('shows no reason on the row while notification sound is on', async () => {
    installPlugin()
    renderSoundPage()
    expect(hapticsSwitch().hasAttribute('disabled') || hapticsSwitch().getAttribute('aria-disabled') === 'true').toBe(false)
    expect(screen.queryByText(NEEDS_SOUND_LINE)).toBeNull()
    expect(hapticsSwitch()).not.toHaveAccessibleDescription()
    await flushEffects()
  })

  it('tells a bridge waiting out a failed probe to look again once the plugin is found', async () => {
    turnHapticsOn()
    installPlugin()
    const recheck = vi.fn()
    window.addEventListener(MC_MOUSE_HAPTICS_RECHECK_EVENT, recheck)
    try {
      renderSoundPage()
      await screen.findByText(/Plugin connected/)
      expect(recheck).toHaveBeenCalled()
    } finally {
      window.removeEventListener(MC_MOUSE_HAPTICS_RECHECK_EVENT, recheck)
    }
  })

  describe('on a page that is not on loopback', () => {
    const happyDom = () => (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM
    let testPageUrl = ''
    beforeEach(() => {
      testPageUrl = window.location.href
      happyDom().setURL('http://my-mac.tail1234.ts.net:5476/settings?tab=notifications&sub=sound')
    })
    afterEach(() => happyDom().setURL(testPageUrl))

    it('explains where haptics work instead of probing', async () => {
      turnHapticsOn()
      installPlugin()
      const pluginFetches = spyOnPluginFetches()
      renderSoundPage()
      expect(screen.getByText(/Works only in the Kiro Crew desktop app, or in a browser that opens the dashboard at a localhost address/)).toBeTruthy()
      await flushEffects()
      expect(pluginFetches()).toBe(0)
    })
  })
})
