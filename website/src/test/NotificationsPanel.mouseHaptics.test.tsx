import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests } from '../hooks/useNotificationSound'
import { HAPTICS_PLUGIN_NAME, HAPTICS_PLUGIN_ORIGIN, MC_MOUSE_HAPTICS_RECHECK_EVENT, MOUSE_HAPTICS_ENABLED_KEY } from '../hooks/useMouseHaptics'

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

const hapticsSwitch = () => screen.getByRole('switch', { name: /Mouse haptics/i })

// A probe the page should not send would start on a later tick, so a "no request" check waits that out first.
const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 100))

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
})

describe('NotificationsPanel mouse haptics', () => {
  it('is on by default and reports a connected plugin', async () => {
    const plugin = installPlugin()
    renderSoundPage()
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('true')
    expect(await screen.findByText(/Plugin connected/)).toBeTruthy()
    expect(plugin.requests()).toBe(1)
  })

  it('tells the user where to install the plugin when nothing answers', async () => {
    renderSoundPage()
    expect(await screen.findByText(/Install and uninstall plugins, then open the plugin file while that page is showing/)).toBeTruthy()
    const setupLink = screen.getByRole('link', { name: 'How to get the plugin' })
    expect(setupLink.getAttribute('href')).toBe('https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/dashboard.md#mouse-haptics')
    expect(setupLink.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('turning it off persists the choice and drops the plugin status', async () => {
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
    const plugin = installPlugin()
    renderSoundPage()
    expect(hapticsSwitch().getAttribute('aria-checked')).toBe('false')
    expect(screen.queryByTestId('mouse-haptics-status')).toBeNull()
    await settle()
    expect(plugin.requests()).toBe(0)
  })

  it('is inert while notification sound is off, because a buzz only goes out with a chime', async () => {
    localStorage.setItem(SOUND_SETTINGS_KEY, JSON.stringify({ enabled: false }))
    const plugin = installPlugin()
    renderSoundPage()
    expect(hapticsSwitch().hasAttribute('disabled') || hapticsSwitch().getAttribute('aria-disabled') === 'true').toBe(true)
    expect(screen.queryByTestId('mouse-haptics-status')).toBeNull()
    await settle()
    expect(plugin.requests()).toBe(0)
  })

  it('tells a bridge waiting out a failed probe to look again once the plugin is found', async () => {
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
      const plugin = installPlugin()
      renderSoundPage()
      expect(screen.getByText(/Works only in the Kiro Crew desktop app, or in a browser that opens the dashboard at a localhost address/)).toBeTruthy()
      await settle()
      expect(plugin.requests()).toBe(0)
    })
  })
})
