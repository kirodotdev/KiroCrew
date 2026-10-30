import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import WindowsTitlebarMenu from '../components/WindowsTitlebarMenu'

type MenuAPI = {
  getAppMenuItems: ReturnType<typeof vi.fn>
  executeAppMenuItem: ReturnType<typeof vi.fn>
}

function installMenuAPI() {
  const api: MenuAPI = {
    getAppMenuItems: vi.fn(async (id: string) => id === 'file-menu'
      ? [
          { type: 'normal', index: 0, label: 'Settings…', accelerator: 'Alt+,', enabled: true, gated: false, checked: false },
          { type: 'separator', index: 1 },
          { type: 'normal', index: 2, label: 'Exit', accelerator: '', enabled: true, gated: false, checked: false },
        ]
      : [{ type: 'normal', index: 0, label: 'Reload', accelerator: 'CmdOrCtrl+R', enabled: true, gated: false, checked: false }]),
    // Resolves the dispatch outcome, matching the preload's
    // `ipcRenderer.invoke("app-menu:execute")`: `true` dispatched, `false`
    // refused by the per-action gate. The renderer branches on it, so a stub
    // that answered nothing would model a channel that no longer exists.
    executeAppMenuItem: vi.fn(async () => true),
  }
  ;(window as Window & { electronAPI?: MenuAPI }).electronAPI = api
  return api
}

/**
 * Dispatch a key from whatever actually has focus, the way a browser does.
 *
 * Firing directly on the <nav> is what let a real focus bug hide: if opening the
 * menu leaves focus on <body>, a browser's keydown never reaches the nav handler
 * at all, but a test that targets the nav still passes. Going through
 * document.activeElement makes these assertions fail in exactly that case.
 */
function pressKey(key: string) {
  const active = document.activeElement
  expect(active).not.toBe(document.body)
  fireEvent.keyDown(active as Element, { key })
}

describe('WindowsTitlebarMenu', () => {
  afterEach(() => {
    delete (window as Window & { electronAPI?: unknown }).electronAPI
    delete document.documentElement.dataset.mode
  })

  it('rests as a hamburger and expands into the application menu labels', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)

    const hamburger = screen.getByRole('button', { name: 'Open menu' })
    expect(screen.queryByText('File')).toBeNull()
    fireEvent.click(hamburger)

    expect(screen.getAllByRole('button').map(item => item.textContent)).toEqual([
      'File',
      'Edit',
      'View',
      'Connection',
      'Window',
      'Help',
    ])
    expect(api.getAppMenuItems).toHaveBeenCalledWith('file-menu')
    expect(await screen.findByRole('menuitem', { name: /Settings/ })).toBeTruthy()
  })

  it('switches the active submenu when another label is hovered', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const view = screen.getByText('View')
    vi.spyOn(view, 'getBoundingClientRect').mockReturnValue({
      x: 91,
      y: 4,
      left: 91,
      top: 4,
      right: 137,
      bottom: 32,
      width: 46,
      height: 28,
      toJSON: () => ({}),
    })
    vi.spyOn(view.closest('header') as HTMLElement, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 800,
      bottom: 42,
      width: 800,
      height: 42,
      toJSON: () => ({}),
    })

    fireEvent.mouseEnter(view)

    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('view-menu'))
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toBeTruthy()
    expect(screen.getByText('File')).toBeTruthy()
    expect(view.getAttribute('aria-expanded')).toBe('true')
  })

  it('collapses to the hamburger on Escape', () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('collapses when the user clicks outside the menu session', () => {
    installMenuAPI()
    render(<div><header><WindowsTitlebarMenu /></header><button type="button">Outside</button></div>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }))

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('executes a selected command in Electron and collapses', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    expect(api.executeAppMenuItem).toHaveBeenCalledWith('file-menu', 0)
    await waitFor(() => expect(screen.queryByText('File')).toBeNull())
  })

  // A row can be painted enabled and still be refused: `app-menu:items` paints
  // from the last positive without waiting on a re-probe, so for one open after
  // a foreign process takes the gateway port the rows look live. The refusal is
  // the only moment the renderer can learn that, and before the channel
  // answered it learned nothing — the popup collapsed and the click vanished
  // with no explanation anywhere on screen (GPT 5.6 lane, 1f3a603c46).
  //
  // So a refusal must keep the popup OPEN and refetch: the second `items`
  // answer comes from a probe that has now proven the foreign holder, which
  // greys the gated rows and renders the footer that names the reason.
  it('keeps the menu open and refetches its rows when a click is refused', async () => {
    const api = installMenuAPI()
    api.executeAppMenuItem.mockResolvedValue(false)
    api.getAppMenuItems
      .mockResolvedValueOnce([
        { type: 'normal', index: 0, label: 'Settings…', accelerator: 'Alt+,', enabled: true, gated: false, checked: false },
      ])
      .mockResolvedValueOnce([
        { type: 'normal', index: 0, label: 'Settings…', accelerator: '', enabled: false, gated: true, checked: false },
      ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    // The footer is the explainer; it renders only on a `gated` row, so its
    // presence proves both the refetch and the re-paint.
    expect(await screen.findByRole('note')).toBeTruthy()
    expect(api.getAppMenuItems).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('menuitem', { name: /Settings/ })).toBeDisabled()
    // Still the same menu session — collapsing would discard the explanation
    // in the same frame it arrived.
    expect(screen.getByText('File')).toBeTruthy()
  })

  // The refetch is not always an explanation. Inside the paint grace a refusal
  // whose probe could not NAME the port's holder leaves the positive standing,
  // so `app-menu:items` answers with the same enabled, ungated rows: nothing
  // greys, no row is `gated`, the footer cannot render, and the click is wholly
  // silent. `errors-use-error-notice` (website/AUTOSDE.yaml, blocking) wants a
  // failure surfaced to the user to render through `ErrorNotice`, and a refused
  // dispatch is the outcome of something that FAILED. GPT 6.1 lane finding F2
  // on 934946a45a, adjudication UPHOLD reason=autosde-blocking-rule.
  it('names the failure when a refused click repaints the same enabled rows', async () => {
    const api = installMenuAPI()
    api.executeAppMenuItem.mockResolvedValue(false)
    api.getAppMenuItems.mockResolvedValue([
      { type: 'normal', index: 0, label: 'Settings…', accelerator: 'Alt+,', enabled: true, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    // `ErrorNotice` renders `role="alert"`, and it must name the row the user
    // actually clicked — "something failed" is not an account of which thing.
    expect(await screen.findByRole('alert')).toHaveTextContent(/Settings/)
    // Proves the notice is doing the work and not the footer: no row is gated
    // in this path, so `role="note"` cannot be what explained it.
    expect(screen.queryByRole('note')).toBeNull()
  })

  // A desktop shell older than this bundle still carries the fire-and-forget
  // bridge — `executeAppMenuItem: (id, index) => ipcRenderer.send(...)`, which
  // answers `undefined` SYNCHRONOUSLY. That pairing is ordinary, not exotic:
  // the shell loads this SPA from the gateway it connects to
  // (`gateway-supervisor.js` `loadURL(dashboardEntryUrl(targetBackendUrl, …))`),
  // so a gateway newer than the shell serves this bundle into that older
  // preload. Reading its `undefined` as a refusal reported EVERY click as
  // failed while the old main process had in fact run the command, and nothing
  // cleared it until the shell itself was upgraded.
  //
  // So the answer is classified by SHAPE, not by value: a non-thenable return
  // is the legacy void bridge and carries no verdict to test. Opus 5.5 and
  // GPT 6.1 both blocked on this independently at 7ac18db1c2, adjudication
  // UPHOLD reason=harm-warrants-remedy.
  it('treats a legacy void bridge as dispatched rather than refused', async () => {
    const api = installMenuAPI()
    api.executeAppMenuItem.mockReturnValue(undefined)
    api.getAppMenuItems.mockResolvedValue([
      { type: 'normal', index: 0, label: 'Settings…', accelerator: 'Alt+,', enabled: true, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    // Dispatched, so the popup closes the way a native click closes it.
    await waitFor(() => expect(screen.queryByText('File')).toBeNull())
    // And nothing on screen claims a command that ran did not run.
    expect(screen.queryByRole('alert')).toBeNull()
    // A dispatched click has nothing to re-paint, so the refusal refetch must
    // not fire either — that second read is what greys rows on a real refusal.
    expect(api.getAppMenuItems).toHaveBeenCalledTimes(1)
  })

  // The companion to the rule above, and the reason it keys on the METHOD
  // rather than only on the answer: a bridge with no execute method at all
  // also yields `undefined` through optional chaining, but there nothing ran.
  // Classifying that as the legacy bridge would collapse the popup silently
  // and swallow the click — the very defect the refusal notice exists to end.
  // Only `getAppMenuItems` gates whether rows paint, so a shell missing just
  // this method can still put an enabled row under the cursor.
  it('refuses a click when the bridge carries no execute method', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValue([
      { type: 'normal', index: 0, label: 'Settings…', accelerator: 'Alt+,', enabled: true, gated: false, checked: false },
    ])
    delete (window.electronAPI as { executeAppMenuItem?: unknown }).executeAppMenuItem
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/Settings/)
    expect(screen.getByText('File')).toBeTruthy()
  })

  // The notice belongs to the menu session that produced the refusal. Left
  // standing it would outlive its own cause and account for a click the user
  // has since moved past — the same "an old session decides for the current
  // one" error the generation guards exist to prevent.
  it('clears the refusal notice when a new menu session opens', async () => {
    const api = installMenuAPI()
    api.executeAppMenuItem.mockResolvedValue(false)
    api.getAppMenuItems.mockResolvedValue([
      { type: 'normal', index: 0, label: 'Settings…', accelerator: 'Alt+,', enabled: true, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))
    expect(await screen.findByRole('alert')).toBeTruthy()

    // A fresh session. Not the hamburger — a refusal deliberately leaves the
    // bar EXPANDED, so that button is unmounted; the user's route to a new
    // session from here is another top-level label.
    fireEvent.click(screen.getByText('Edit'))

    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  // The mirror of the test above, for the SUCCESS answer. Both halves need the
  // same generation check and only the refusal half had it: a slow click that
  // eventually dispatched would collapse whatever menu the user had moved on
  // to, destroying a popup they never activated an item in.
  //
  // Reachable only because the collapse is now deferred behind the await. While
  // this was fire-and-forget the collapse ran synchronously, before the user
  // could move anywhere.
  it('does not collapse a newly opened menu when an earlier success lands late', async () => {
    const api = installMenuAPI()
    let answerDispatch = (_value: boolean) => {}
    api.executeAppMenuItem.mockImplementation(
      () => new Promise<boolean>(resolve => { answerDispatch = resolve }),
    )
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    // The user moves to another top-level menu while the click is still in
    // flight, which bumps the request generation.
    fireEvent.mouseEnter(screen.getByText('View'))
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('view-menu'))
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toBeTruthy()

    await act(async () => { answerDispatch(true) })

    expect(screen.getByText('View')).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: /Reload/ })).toBeTruthy()
  })

  // The row stays mounted and enabled for as long as the probe takes, so a
  // second activation can reach a command whose first one has not answered.
  // Each invoke joins the same probe and then dispatches independently, so the
  // command runs twice — and for a toggle like Keep on Top that reads as the
  // click having done nothing at all.
  //
  // Also a consequence of deferring the collapse: the synchronous collapse this
  // replaced unmounted the popup before a second click was possible.
  it('dispatches a command once when the row is clicked twice before it answers', async () => {
    const api = installMenuAPI()
    let answerDispatch = (_value: boolean) => {}
    api.executeAppMenuItem.mockImplementation(
      () => new Promise<boolean>(resolve => { answerDispatch = resolve }),
    )
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const item = await screen.findByRole('menuitem', { name: /Settings/ })

    fireEvent.click(item)
    fireEvent.click(item)

    expect(api.executeAppMenuItem).toHaveBeenCalledTimes(1)
    await act(async () => { answerDispatch(true) })
  })

  // The latch must bound ONE menu session, not the component. Keyed on the
  // request generation rather than a bare boolean, because a pending probe from
  // a session the user has already left would otherwise swallow their next
  // click entirely — the same "an old session decides for the current one"
  // mistake as the late-collapse above, in a third place. One mechanism, the
  // generation, governs every cross-session decision here.
  it('still dispatches a click in a new menu session while an earlier one is pending', async () => {
    const api = installMenuAPI()
    const pendingAnswers: ((value: boolean) => void)[] = []
    api.executeAppMenuItem.mockImplementation(
      () => new Promise<boolean>(resolve => { pendingAnswers.push(resolve) }),
    )
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))
    expect(api.executeAppMenuItem).toHaveBeenCalledTimes(1)

    // Escape ends the session while its probe is still in flight.
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    expect(api.executeAppMenuItem).toHaveBeenCalledTimes(2)
    await act(async () => { pendingAnswers.forEach(resolve => resolve(true)) })
  })

  // The other half of keying the latch: releasing it must also be per-session.
  // An older session's answer settling must not hand the current session's
  // latch back, or the double-dispatch window this latch exists to close
  // reopens for exactly the session the user is in.
  it("an earlier session's answer does not release the current session's latch", async () => {
    const api = installMenuAPI()
    const pendingAnswers: ((value: boolean) => void)[] = []
    api.executeAppMenuItem.mockImplementation(
      () => new Promise<boolean>(resolve => { pendingAnswers.push(resolve) }),
    )
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))
    expect(api.executeAppMenuItem).toHaveBeenCalledTimes(2)

    // The FIRST session's answer settles. Its `finally` must leave the second
    // session's latch alone.
    await act(async () => { pendingAnswers[0](true) })

    fireEvent.click(screen.getByRole('menuitem', { name: /Settings/ }))

    expect(api.executeAppMenuItem).toHaveBeenCalledTimes(2)
    await act(async () => { pendingAnswers[1](true) })
  })

  // The refetch must not resurrect a menu the user has already left: Escape (or
  // any collapse) bumps the request generation, and a refusal that lands after
  // it has to stay silent rather than re-open the popup under the cursor.
  it('does not re-open a collapsed menu when the refusal lands late', async () => {
    const api = installMenuAPI()
    let answerRefusal = (_value: boolean) => {}
    api.executeAppMenuItem.mockImplementation(
      () => new Promise<boolean>(resolve => { answerRefusal = resolve }),
    )
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    expect(screen.queryByText('File')).toBeNull()

    answerRefusal(false)
    await waitFor(() => expect(api.executeAppMenuItem).toHaveBeenCalledTimes(1))

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.queryByRole('note')).toBeNull()
    expect(api.getAppMenuItems).toHaveBeenCalledTimes(1)
  })

  it('collapses when the already-open label is clicked again', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    // File opens with the hamburger, so its label is the active one.
    expect(await screen.findByRole('menuitem', { name: /Settings/ })).toBeTruthy()

    fireEvent.click(screen.getByText('File'))

    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('collapses rather than stranding an empty popup when the IPC read fails', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockRejectedValueOnce(new Error('ipc gone'))
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    // A failed read must not leave the labels expanded over an empty popup —
    // the menu session ends and the hamburger comes back.
    await waitFor(() => expect(screen.queryByText('File')).toBeNull())
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('walks top-level menus with ArrowRight / ArrowLeft, wrapping at both ends', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    // Opening moves focus into the row; wait for it, because pressing a key
    // before focus lands is not the state a real keyboard user is ever in.
    await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

    // File (index 0) is active -> ArrowRight lands on Edit and opens it.
    pressKey('ArrowRight')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('edit-menu'))
    expect(screen.getByText('Edit')).toHaveFocus()

    // ArrowLeft twice from Edit wraps past File round to Help.
    pressKey('ArrowLeft')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('file-menu'))
    pressKey('ArrowLeft')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('help-menu'))
    expect(screen.getByText('Help')).toHaveFocus()
  })

  it('ignores keys that are not menu navigation', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await screen.findByRole('menuitem', { name: /Settings/ })
    const callsBefore = api.getAppMenuItems.mock.calls.length

    pressKey('a')

    expect(api.getAppMenuItems.mock.calls).toHaveLength(callsBefore)
    expect(screen.getByRole('menu')).toBeTruthy()
  })

  it('moves focus from the labels into the popup on ArrowDown', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const settings = await screen.findByRole('menuitem', { name: /Settings/ })

    pressKey('ArrowDown')

    // First enabled item, skipping the separator.
    expect(settings).toHaveFocus()
  })

  it('cycles popup items with ArrowDown / ArrowUp / Home / End', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const settings = await screen.findByRole('menuitem', { name: /Settings/ })
    const exit = screen.getByRole('menuitem', { name: /Exit/ })
    const popup = screen.getByRole('menu')

    // Enter the popup the way a user does — ArrowDown from the labels — so the
    // cycling below starts from a real focused item rather than from the
    // container itself.
    pressKey('ArrowDown')
    expect(settings).toHaveFocus()

    // The separator is not focusable, so Settings and Exit are the only stops.
    fireEvent.keyDown(popup, { key: 'ArrowDown' })
    expect(exit).toHaveFocus()

    // Wraps forward off the end, and back off the front.
    fireEvent.keyDown(popup, { key: 'ArrowDown' })
    expect(settings).toHaveFocus()
    fireEvent.keyDown(popup, { key: 'ArrowUp' })
    expect(exit).toHaveFocus()

    fireEvent.keyDown(popup, { key: 'Home' })
    expect(settings).toHaveFocus()
    fireEvent.keyDown(popup, { key: 'End' })
    expect(exit).toHaveFocus()
  })

  it('renders a checkbox item with its check state and a rewritten accelerator', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      { type: 'checkbox', index: 0, label: 'Keep on Top', accelerator: '', enabled: true, gated: false, checked: true },
      { type: 'normal', index: 1, label: 'Zoom In', accelerator: 'CommandOrControl+Plus', enabled: true, gated: false, checked: false },
      // Natively disabled — NOT gate-caused. Left as-is so the checkbox test does
      // not inadvertently exercise the footer.
      { type: 'normal', index: 2, label: 'Unavailable', accelerator: '', enabled: false, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const toggle = await screen.findByRole('menuitemcheckbox', { name: /Keep on Top/ })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    // The Electron accelerator token is rewritten to the cap the user reads.
    expect(screen.getByRole('menuitem', { name: /Zoom In/ })).toHaveTextContent('Ctrl+Plus')
    expect(screen.getByRole('menuitem', { name: /Unavailable/ })).toBeDisabled()
  })

  // A row greyed by the per-action LOCAL_ONLY gate carries NO per-row
  // decoration: no keyboard glyph, no struck-out accelerator caption. Greying
  // states the row is inert from this window and the footer names the remedy,
  // which is the whole of what the renderer knows — the payload carries no
  // accelerator-registration field, so there is nothing per-row to draw.
  //
  // This test is the fence. Both accelerator shapes appear below — one whose
  // chord Electron routes natively and one display-only — and each renders a
  // plain caption, so a decoration that returns for either fails here.
  it('renders no per-row decoration on gated rows, whatever the accelerator state', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      // Gated, accelerator DOES route through Electron's native dispatch.
      { type: 'normal', index: 0, label: 'Paste', accelerator: 'CommandOrControl+V', enabled: false, gated: true, checked: false },
      // Gated with a display-only chord. windows-menu-model suppresses the
      // caption for this case, so the wire carries an empty accelerator and
      // the row shows a label alone.
      { type: 'normal', index: 1, label: 'Settings…', accelerator: '', enabled: false, gated: true, checked: false },
      // Enabled control row.
      { type: 'normal', index: 2, label: 'Zoom In', accelerator: 'CommandOrControl+Plus', enabled: true, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const paste = await screen.findByRole('menuitem', { name: /Paste/ })
    expect(paste).toBeDisabled()
    expect(paste).toHaveTextContent('Ctrl+V')

    const settings = screen.getByRole('menuitem', { name: /Settings/ })
    expect(settings).toBeDisabled()
    // No caption at all: the footer says the shortcuts shown still work, and
    // this row's chord does not, so showing one would be the single false
    // claim in the menu. Absence is the honest rendering, not a marker.
    expect(settings).not.toHaveTextContent('Alt+,')
    expect(settings).toHaveTextContent('Settings…')

    // No glyph anywhere in the popup, for either accelerator state.
    expect(document.querySelectorAll('[aria-label="Shortcut still available"]')).toHaveLength(0)

    // No struck caption anywhere, for either accelerator state.
    for (const row of [paste, settings, screen.getByRole('menuitem', { name: /Zoom In/ })]) {
      for (const span of Array.from(row.querySelectorAll('span'))) {
        expect(span.className).not.toContain('line-through')
      }
    }

    // The footer carries two true statements: the rows are unavailable here,
    // and the captions that remain are chords that still fire. It can make
    // the second claim without hedging because windows-menu-model suppresses
    // the caption on any gated row whose chord cannot fire, so "the shortcuts
    // shown" names a set the user can see rather than one they must guess at.
    expect(screen.getByText(/Some items are unavailable here/)).toBeInTheDocument()
    expect(screen.getByText(/The shortcuts shown still work/)).toBeInTheDocument()
    // Never the vague form: "some shortcuts work" names a distinction the
    // user cannot resolve without trying each row.
    expect(screen.queryByText(/Some keyboard shortcuts still work/)).toBeNull()
  })

  // Footer must render BELOW the menu items, not above them. Rendering the
  // footer at the top of the popup pushes the first menu row (e.g.
  // "Settings…") ~70px down versus the same menu in the main window, so
  // gated menus read as a different, shifted surface — UX Review "footer
  // sits above the items" watch on b1efe7fc9. The reviewer's own suggested
  // fix, which this test pins: render the footer below the mapped items so
  // menu-row geometry stays stable and the footer becomes a legend line the
  // user reaches AFTER scanning the rows they came for.
  it('renders the disabled-row footer below the menu items', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      { type: 'normal', index: 0, label: 'Settings…', accelerator: 'Alt+,', enabled: false, gated: true, checked: false },
      { type: 'normal', index: 1, label: 'Exit', accelerator: '', enabled: true, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const firstItem = await screen.findByRole('menuitem', { name: /Settings/ })
    const footer = screen.getByText(/Some items are unavailable here/)

    // The footer's position relative to the FIRST menu row is what the user
    // sees: if it's before Settings, Settings gets pushed down. Assert the
    // footer follows Settings in document order.
    expect(
      firstItem.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  // The footer's claim — "shortcuts still work here" — is only accurate for
  // ROWS THE GATE DISABLED. A natively-disabled row (e.g. an `undo` role
  // with an empty history, or a `paste` role when the selection focus is
  // gone) is disabled for its OWN runtime reasons, and its accelerator does
  // NOT fire either. If the footer fired on those, the claim would be false.
  // So the trigger is `item.gated`, not `!item.enabled`. This test pins that
  // separation: a menu of only natively-disabled rows leaves the footer off.
  it('omits the disabled-menu footer for natively disabled rows (not gate-caused)', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      // Natively disabled — Electron's own dispatch marked it so (undo with
      // no history, paste with no selection focus). NOT gate-caused.
      { type: 'normal', index: 0, label: 'Undo', accelerator: 'CommandOrControl+Z', enabled: false, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const undo = await screen.findByRole('menuitem', { name: /Undo/ })
    expect(undo).toBeDisabled()
    // No footer — the claim would misrepresent a native disable.
    expect(
      screen.queryByText(/Some items are unavailable here/),
    ).toBeNull()
  })

  it('omits the disabled-menu footer when every row is enabled', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      { type: 'normal', index: 0, label: 'Zoom In', accelerator: 'CommandOrControl+Plus', enabled: true, gated: false, checked: false },
      { type: 'normal', index: 1, label: 'Zoom Out', accelerator: 'CommandOrControl+Minus', enabled: true, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await screen.findByRole('menuitem', { name: /Zoom In/ })
    // The footer only fires when there's something to explain — a fully-
    // enabled menu stays clean of the extra line.
    expect(
      screen.queryByText(/Some items are unavailable here/),
    ).toBeNull()
  })

  it('shows Settings as Alt+, on Windows, never a Ctrl chord (CJK IME comma, #9824)', async () => {
    // On Windows this component IS the application menu: the submenu is drawn
    // here rather than by Menu.popup(), so this row is the rendered caption a
    // Windows user actually reads. electron/app-menu.js sends `Alt+,` off macOS
    // with registerAccelerator false, which claims no key — Ctrl+, has to stay
    // free because it is how a Chinese or Japanese IME types a comma. This
    // assertion is what makes the caption a tested surface rather than one that
    // only a screenshot could vouch for.
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const settings = await screen.findByRole('menuitem', { name: /Settings/ })
    expect(settings).toHaveTextContent('Alt+,')
    expect(settings).not.toHaveTextContent('Ctrl+,')
  })

  it('opens a different menu when its label is clicked without a hover first', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await screen.findByRole('menuitem', { name: /Settings/ })

    // Touch and some AT drivers deliver a click with no preceding mouseEnter,
    // so the click handler has to open a non-active menu on its own.
    fireEvent.click(screen.getByText('View'))

    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('view-menu'))
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toBeTruthy()
  })

  describe('focus is handed back to the hamburger on close', () => {
    // Collapsing unmounts the labels AND the portal popup, so without an
    // explicit restore the focused element is destroyed and a keyboard user
    // lands on <body> — they have to Tab in from the top of the header again.
    // The restore is deferred to a rAF because the hamburger does not exist
    // until the render that drops `expanded`, so these await it rather than
    // stubbing rAF synchronously (which would fire before that render and
    // defeat the very deferral under test).
    const hamburger = () => screen.getByRole('button', { name: 'Open menu' })
    const expectFocusRestored = () => waitFor(() => expect(hamburger()).toHaveFocus())

    it('restores focus on Escape from the label row', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await screen.findByRole('menuitem', { name: /Settings/ })

      pressKey('Escape')

      await expectFocusRestored()
    })

    it('restores focus on Escape from inside the popup', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

      // Descend into the popup first, so Escape is pressed from where a real
      // user would press it — with focus on an item, not on the container.
      pressKey('ArrowDown')
      expect(await screen.findByRole('menuitem', { name: /Settings/ })).toHaveFocus()

      pressKey('Escape')

      await expectFocusRestored()
    })

    it('restores focus after a command is picked', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())

      fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

      await expectFocusRestored()
    })

    it('restores focus when the open label is clicked shut', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await screen.findByRole('menuitem', { name: /Settings/ })

      fireEvent.click(screen.getByText('File'))

      await expectFocusRestored()
    })

    it('restores focus when an IPC failure collapses a keyboard-opened menu', async () => {
      const api = installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

      // Focus is inside the menu, so a failure that tears it down must hand
      // focus back rather than dropping the user on <body>.
      api.getAppMenuItems.mockRejectedValueOnce(new Error('ipc gone'))
      pressKey('ArrowRight')

      await expectFocusRestored()
    })

    it('does NOT steal focus when the failing open was hover-initiated', async () => {
      // The containment guard: on hover the user's focus is still wherever they
      // were typing, so an IPC failure must not yank it into the titlebar.
      const api = installMenuAPI()
      render(
        <div>
          <header><WindowsTitlebarMenu /></header>
          <input aria-label="Message" />
        </div>,
      )
      fireEvent.click(hamburger())
      await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

      const input = screen.getByLabelText('Message')
      input.focus()
      expect(input).toHaveFocus()

      api.getAppMenuItems.mockRejectedValueOnce(new Error('ipc gone'))
      fireEvent.mouseEnter(screen.getByText('View'))

      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
      expect(input).toHaveFocus()
      expect(hamburger()).not.toHaveFocus()
    })

    it('does NOT pull focus back on an outside click or a window blur', async () => {
      installMenuAPI()
      render(
        <div>
          <header><WindowsTitlebarMenu /></header>
          <button type="button">Outside</button>
        </div>,
      )

      // Outside click: the user is deliberately elsewhere, so yanking focus
      // into the titlebar would fight them.
      fireEvent.click(hamburger())
      fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }))
      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
      expect(hamburger()).not.toHaveFocus()

      // Window blur: same, and the listener must not leak its Event object in
      // as a truthy restore flag.
      fireEvent.click(hamburger())
      fireEvent(window, new Event('blur'))
      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
      expect(hamburger()).not.toHaveFocus()
    })
  })

  it('moves focus into the label row when the menu is opened', async () => {
    // Expanding replaces the hamburger with the label row, unmounting the
    // element that was just activated. If focus is not moved in, it lands on
    // <body>, the nav's onKeyDown stops receiving anything, and a keyboard user
    // cannot drive the menu they just opened.
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    await waitFor(() => expect(screen.getByText('File')).toHaveFocus())
  })

  it('is fully operable from the keyboard end to end', async () => {
    // The whole chord, each key dispatched from whatever actually has focus:
    // open -> traverse -> descend into the popup -> Escape back out.
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

    pressKey('ArrowRight')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('edit-menu'))
    expect(screen.getByText('Edit')).toHaveFocus()

    pressKey('ArrowDown')
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toHaveFocus()

    pressKey('Escape')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open menu' })).toHaveFocus())
  })

  it('does not dispatch a disabled item', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      // Natively disabled — this test's intent is that a disabled item does not
      // dispatch, independent of gate state.
      { type: 'normal', index: 0, label: 'Unavailable', accelerator: '', enabled: false, gated: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const item = await screen.findByRole('menuitem', { name: /Unavailable/ })

    fireEvent.click(item)

    expect(api.executeAppMenuItem).not.toHaveBeenCalled()
  })
})
