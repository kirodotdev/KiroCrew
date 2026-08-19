//
// Contract under test — the gateway (non-Electron) channel switcher and the
// standalone Restart control in Settings > About.
//
// Why these exist: a wheel install cannot replace its own code, so the panel
// hands the user an installer command. Two things were missing afterwards.
//
// - There was no way to change WHICH channel is followed. The switcher offers
//   the same two lanes as the desktop, stable ⇄ insider. Nightly is a
//   deliberate pinned install (`cli.sh --channel nightly`), not a destination
//   one click away from Stable, so its segment appears only for an install
//   already on that lane — as an exit, never an entrance.
// - There was no way to RELOAD after running the command. The installer
//   replaced the code on disk while this process kept executing the old
//   version, and killing it by hand was the only route.
//
// The switcher must only appear where the backend can honour it: a git checkout
// follows a remote and a desktop bundle / container is updated by something
// else, so both report no channel and get no control.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

/** A minimal-but-valid status payload; `sseStatus` dereferences it, so never null. */
const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

/**
 * Route every request the panel makes. `posts` records POST urls + bodies so a
 * test can assert what the control actually sent, not merely that it rendered.
 */
function stubFetch(opts: {
  check?: Record<string, unknown>
  channelResponse?: Record<string, unknown>
  channelStatus?: number
} = {}) {
  const posts: { url: string; body: unknown }[] = []
  const json = (body: unknown, status = 200) => ({
    ok: status < 400,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  const spy = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null })
      if (url.includes('/api/update/channel')) {
        return json(opts.channelResponse ?? { ok: true, channel: 'nightly' }, opts.channelStatus ?? 200)
      }
      return json({ ok: true })
    }
    if (url.includes('/api/update/check')) return json(opts.check ?? {})
    if (url.includes('/api/changelog')) return json({ content: '' })
    return json({})
  })
  vi.stubGlobal('fetch', spy)
  return posts
}

function mountWeb() {
  // No window.updateAPI => isDesktop false => the gateway branch renders.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

/** Seed the background check's answer, which is what the switcher reads on mount. */
function seedStatus(extra: Record<string, unknown>) {
  store.dispatch(sseStatus({ ...BLANK_STATUS, ...extra } as never))
}

describe('AboutPanel gateway channel switcher', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('offers stable and insider only, marking the followed one selected', async () => {
    stubFetch()
    seedStatus({ update_channel: 'insider' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    for (const lane of ['Stable', 'Insider']) {
      expect(within(switcher).getByTitle(lane)).toBeTruthy()
    }
    // Nightly ships untested `main` HEAD. Following it is a deliberate install,
    // so it must not sit beside Stable as a third equal option one click away.
    expect(within(switcher).queryByTitle('Nightly')).toBeNull()
  })

  it('keeps nightly on screen for an install already following it, as an exit', async () => {
    // A two-segment control whose value is `nightly` matches nothing and shows
    // no indicator at all -- the user could not tell which lane they are on.
    // Rendering the segment keeps the answer truthful and leaves one click back
    // to Stable.
    const posts = stubFetch({ channelResponse: { ok: true, channel: 'stable', checked: true, available: false } })
    seedStatus({ update_channel: 'nightly', release_channel: 'nightly' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    expect(within(switcher).getByTitle('Nightly')).toBeTruthy()

    fireEvent.click(within(switcher).getByTitle('Stable'))
    await waitFor(() => {
      const call = posts.find(p => p.url.includes('/api/update/channel'))
      expect(call!.body).toEqual({ channel: 'stable' })
    })
  })

  it('keeps the nightly segment while the running build is still nightly', async () => {
    // The window right after the exit click: the followed lane is stable, the
    // bytes are still nightly. Dropping the segment here would strand an
    // accidental click with no way back until the install actually moved.
    stubFetch()
    seedStatus({ update_channel: 'stable', release_channel: 'nightly' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    expect(within(switcher).getByTitle('Nightly')).toBeTruthy()
  })

  it('sends the picked channel to the backend', async () => {
    const posts = stubFetch({ channelResponse: {
      ok: true,
      channel: 'insider',
      check_status: 'succeeded',
      update_available: false,
    } })
    seedStatus({ update_channel: 'stable' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    fireEvent.click(within(switcher).getByTitle('Insider'))

    await waitFor(() => {
      const call = posts.find(p => p.url.includes('/api/update/channel'))
      expect(call).toBeTruthy()
      expect(call!.body).toEqual({ channel: 'insider' })
    })
  })

  it('keeps the verdict a successful switch just found instead of discarding it', async () => {
    // The switch response IS the re-run check against the new lane. This handler
    // was left reading the pre-rename keys (`available` / `checked` /
    // `remote_version` / `self_updatable`), so a switch that FOUND an update
    // wrote false into both flags and blanked the target — throwing away the very
    // answer the switch was made to get.
    // The switcher offers stable ⇄ insider only: a nightly build reports
    // channelSwitchable=false because it is a separate pinned install.
    stubFetch({ channelResponse: {
      ok: true,
      channel: 'insider',
      check_status: 'succeeded',
      update_available: true,
      latest_version: '0.9.9',
      can_apply: false,
      update_command: 'curl -fsSL https://example.invalid/cli.sh | sh',
    } })
    seedStatus({ update_channel: 'stable' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    fireEvent.click(within(switcher).getByTitle('Insider'))

    // The found version reaches the panel...
    expect(await screen.findByText(/0\.9\.9/)).toBeTruthy()
    // ...and the install that cannot self-apply is given the command instead of
    // a button the gateway would refuse.
    expect(await screen.findByTestId('manual-update-command')).toBeTruthy()
  })

  it('does not re-send the channel already followed', async () => {
    const posts = stubFetch()
    seedStatus({ update_channel: 'stable' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    fireEvent.click(within(switcher).getByTitle('Stable'))

    // A no-op POST would drop the cached verdict for nothing and re-hit the feed.
    await new Promise(r => setTimeout(r, 20))
    expect(posts.some(p => p.url.includes('/api/update/channel'))).toBe(false)
  })

  it('surfaces a rejected switch instead of showing the new lane as selected', async () => {
    stubFetch({ channelResponse: { error: 'not applicable', code: 'channel_not_applicable_git' }, channelStatus: 409 })
    seedStatus({ update_channel: 'stable' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    fireEvent.click(within(switcher).getByTitle('Insider'))

    await waitFor(() => expect(screen.getByTestId('gateway-channel-error')).toBeTruthy())
  })

  it('includes the backend reason so a refused switch is recoverable', async () => {
    // The 409s carry the only actionable detail there is. A bare "Couldn't switch
    // channel" leaves the user with no next step.
    const reason = 'A git checkout follows its git remote, not a release channel.'
    stubFetch({ channelResponse: { error: reason, code: 'channel_not_applicable_git' }, channelStatus: 409 })
    seedStatus({ update_channel: 'stable' })
    mountWeb()

    const switcher = await screen.findByTestId('gateway-channel-switcher')
    fireEvent.click(within(switcher).getByTitle('Insider'))

    await waitFor(() => {
      expect(screen.getByTestId('gateway-channel-error').textContent).toContain('git remote')
    })
  })

  it('is absent when the layout has no channel to switch', async () => {
    // A git checkout / desktop bundle / container reports update_channel "" —
    // the backend answers 409, so offering the control would be a lie.
    stubFetch()
    seedStatus({ update_channel: '' })
    mountWeb()

    await screen.findByRole('button', { name: /check for updates/i })
    expect(screen.queryByTestId('gateway-channel-switcher')).toBeNull()
  })

  it('says out loud that a switch installs nothing, without needing the disclosure', async () => {
    // The segmented control highlights the new lane as soon as the switch
    // succeeds, so a user reasonably concludes they are ON that lane. Nothing has
    // moved until they run the command. That caveat cannot live only behind the
    // collapsed "What's the difference" toggle, because the misreading happens
    // precisely to the user who never opened it.
    stubFetch()
    // Followed channel (insider) differs from the lane the running bytes came
    // from (stable) -- exactly the window where the two disagree.
    seedStatus({
      update_channel: 'insider',
      release_channel: 'stable',
      update_command: 'curl -fsSL https://example.test/cli.sh | sh -s -- --channel insider',
    })
    mountWeb()

    await screen.findByTestId('gateway-channel-switcher')
    // Visible with the disclosure still collapsed.
    expect(screen.queryByTestId('gateway-channel-help')).toBeNull()
    expect(screen.getByTestId('gateway-channel-pending-note')).toBeTruthy()
  })

  it('withholds the switch note when there is no command for it to point at', async () => {
    // The sentence says "run the command below". With no command resolved (failed
    // check, offline host) it would dangle, the same way it did when it was gated
    // on `update_available` alone.
    stubFetch()
    seedStatus({ update_channel: 'insider', release_channel: 'stable', update_command: '' })
    mountWeb()

    await screen.findByTestId('gateway-channel-switcher')
    expect(screen.queryByTestId('gateway-channel-pending-note')).toBeNull()
  })

  it('retires the switch note once the followed lane matches the running build', async () => {
    stubFetch()
    seedStatus({ update_channel: 'stable', release_channel: 'stable' })
    mountWeb()

    await screen.findByTestId('gateway-channel-switcher')
    expect(screen.queryByTestId('gateway-channel-pending-note')).toBeNull()
  })

  it('explains the lanes it offers behind the disclosure', async () => {
    stubFetch()
    seedStatus({ update_channel: 'stable' })
    mountWeb()

    const toggle = await screen.findByTestId('gateway-channel-help-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByTestId('gateway-channel-help')).toBeNull()

    fireEvent.click(toggle)
    const help = screen.getByTestId('gateway-channel-help')
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    // Every lane the switcher offers must be explained, or the control asks for
    // a choice it never described.
    for (const label of [/stable/i, /insider/i]) {
      expect(help.textContent).toMatch(label)
    }
    // And no definition for a lane it does not offer: that reads as an
    // invitation to hunt for a missing segment. The collapsed prompt names the
    // same two lanes for the same reason.
    expect(help.textContent).not.toMatch(/nightly/i)
    expect(toggle.textContent).not.toMatch(/nightly/i)
  })

  it('explains nightly for the install that is on it', async () => {
    stubFetch()
    seedStatus({ update_channel: 'nightly', release_channel: 'nightly' })
    mountWeb()

    fireEvent.click(await screen.findByTestId('gateway-channel-help-toggle'))
    expect(screen.getByTestId('gateway-channel-help').textContent).toMatch(/nightly/i)
  })
})

describe('AboutPanel gateway restart', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('offers Restart beside the installer command a wheel install must run', async () => {
    const posts = stubFetch()
    // update_available + !can_apply is the manual-update path: the command is the
    // only way forward, and restarting is the step that used to be missing.
    seedStatus({
      update_available: true,
      update_check_status: 'succeeded',
      update_can_apply: false,
      update_command: 'curl -fsSL https://example.test/cli.sh | sh -s -- --channel stable',
      update_channel: 'stable',
    })
    mountWeb()

    await screen.findByTestId('manual-update-command')
    const restart = screen.getByTestId('gateway-restart')
    // Two-step: the first click only arms it.
    fireEvent.click(restart)
    await new Promise(r => setTimeout(r, 20))
    expect(posts.some(p => p.url.includes('/api/restart'))).toBe(false)

    fireEvent.click(screen.getByTestId('gateway-restart'))
    await waitFor(() => expect(posts.some(p => p.url.includes('/api/restart'))).toBe(true))
  })

  it('offers Restart even when no update is available', async () => {
    // The reason this is a STANDING control: a restart is how the install picks
    // up code that already changed on disk (a re-run installer, a local
    // `pip install -e .`, a config change). None of those imply a pending
    // release, so gating the button on update_available hides it exactly when a
    // developer needs it.
    const posts = stubFetch()
    seedStatus({ update_available: false, update_check_status: 'succeeded', update_channel: 'stable' })
    mountWeb()

    const row = await screen.findByTestId('gateway-restart-row')
    expect(row).toBeTruthy()
    // No update card at all in this state.
    expect(screen.queryByTestId('manual-update-command')).toBeNull()

    const btn = screen.getByTestId('gateway-restart-standing')
    fireEvent.click(btn)
    fireEvent.click(screen.getByTestId('gateway-restart-standing'))
    await waitFor(() => expect(posts.some(p => p.url.includes('/api/restart'))).toBe(true))
  })

  it('a single click never restarts, and the arm expires on its own', async () => {
    vi.useFakeTimers()
    try {
      const posts = stubFetch()
      seedStatus({ update_available: false, update_check_status: 'succeeded', update_channel: 'stable' })
      mountWeb()
      await vi.advanceTimersByTimeAsync(500)

      const btn = screen.getByTestId('gateway-restart-standing')
      fireEvent.click(btn)
      // Armed but not fired.
      expect(posts.some(p => p.url.includes('/api/restart'))).toBe(false)

      // An armed control left on screen must not stay a trap for a later click.
      await vi.advanceTimersByTimeAsync(6000)
      fireEvent.click(screen.getByTestId('gateway-restart-standing'))
      await vi.advanceTimersByTimeAsync(50)
      expect(posts.some(p => p.url.includes('/api/restart'))).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows the installer command for a lane move even with no newer version', async () => {
    // Switching from nightly back to stable is a DOWNGRADE: `update_available` is false,
    // yet the command is still the only thing that performs the move. Gating the
    // command on `update_available` alone left the switcher's own note ("run the command
    // below") pointing at nothing in exactly that case.
    stubFetch()
    seedStatus({
      update_available: false,
      update_check_status: 'succeeded',
      update_can_apply: false,
      update_command: 'curl -fsSL https://example.test/cli.sh | sh -s -- --channel stable',
      update_channel: 'stable',
      release_channel: 'nightly',
    })
    mountWeb()

    await screen.findByTestId('manual-update-command')
    // And it does not claim a new version exists, because none does.
    expect(screen.queryByText(/a new version/i)).toBeNull()
  })

  it('does not render two Restart buttons at once', async () => {
    // The manual-update card already carries a Restart wired to the same
    // mutation. Two identical buttons a few rows apart read as two different
    // actions, so the standing row stands down while that card is on screen.
    stubFetch()
    seedStatus({
      update_available: true,
      update_check_status: 'succeeded',
      update_can_apply: false,
      update_command: 'curl -fsSL https://example.test/cli.sh | sh',
      update_channel: 'stable',
    })
    mountWeb()

    await screen.findByTestId('gateway-restart')
    expect(screen.queryByTestId('gateway-restart-row')).toBeNull()
  })

  it('treats the connection drop after a restart as success, not failure', async () => {
    // os.execv replaces the process image, so the POST's connection is reset by
    // the very thing it asked for. Reporting that as an error would tell the
    // user the restart failed at the exact moment it worked.
    vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/api/restart')) throw new TypeError('Failed to fetch')
      return {
        ok: true, status: 200,
        json: async () => (url.includes('/api/update/check') ? {} : {}),
        text: async () => '{}',
        headers: new Headers({ 'content-type': 'application/json' }),
      }
    }))
    seedStatus({
      update_available: true,
      update_check_status: 'succeeded',
      update_can_apply: false,
      update_command: 'curl -fsSL https://example.test/cli.sh | sh',
      update_channel: 'stable',
    })
    mountWeb()

    await screen.findByTestId('manual-update-command')
    fireEvent.click(screen.getByTestId('gateway-restart'))
    fireEvent.click(screen.getByTestId('gateway-restart'))

    // The button reports the restart in progress rather than an error.
    await waitFor(() => {
      const btn = screen.getByTestId('gateway-restart')
      expect(btn.getAttribute('disabled')).not.toBeNull()
    })
  })
})
