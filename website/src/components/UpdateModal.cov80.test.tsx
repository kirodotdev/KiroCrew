import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { useQuery } from '@tanstack/react-query'
import { renderWithProviders } from '../test/helpers'
import UpdateModal from './UpdateModal'
import { i18nT } from '../i18n/t'

type UpdateState = {
  state: 'checking' | 'available' | 'downloading' | 'downloaded' | 'installing' | 'not-available' | 'error'
  version?: string
  notes?: string
  phase?: 'check' | 'download' | 'install'
  installHandoff?: 'windows-installer' | 'automatic-relaunch'
  armed?: boolean
  updatePrompts?: 'edition'
}

const install = vi.fn<() => Promise<unknown>>()

/**
 * React Query flushes cache notifications on a microtask, so seeding
 * ['update-state'] only reaches the component after an async act() tick --
 * a synchronous assertion right after setQueryData always sees no dialog.
 */
async function mount(initial?: UpdateState) {
  const rendered = renderWithProviders(<UpdateModal />)
  const push = async (next: UpdateState) => {
    await act(async () => { rendered.queryClient.setQueryData(['update-state'], next) })
  }
  if (initial) await push(initial)
  return { ...rendered, push }
}

const dialog = () => screen.queryByRole('dialog')
const byName = (key: string) => screen.getByRole('button', { name: i18nT(key) })
const downloaded: UpdateState = { state: 'downloaded', version: '9.9.9' }

describe('UpdateModal', () => {
  beforeEach(() => {
    install.mockReset()
    install.mockResolvedValue(undefined)
    ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
      armsOnQuit: true,
      install,
    }
  })

  afterEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('renders nothing without an update state', async () => {
    const { container } = await mount()
    expect(container.firstChild).toBeNull()
  })

  it("waits while What's new is open, then takes its turn", async () => {
    const rendered = renderWithProviders(<UpdateModal held />)
    await act(async () => { rendered.queryClient.setQueryData(['update-state'], downloaded) })
    expect(dialog()).toBeNull()
    rendered.rerender(<UpdateModal />)
    expect(dialog()).toBeInTheDocument()
  })

  it('renders nothing while the download is still in flight', async () => {
    const { container } = await mount({ state: 'downloading', version: '9.9.9' })
    expect(container.firstChild).toBeNull()
  })

  it('shows the version and the trimmed release notes once downloaded', async () => {
    await mount({ ...downloaded, notes: '  zzq changelog  ' })
    expect(dialog()).toBeInTheDocument()
    expect(screen.getByText('9.9.9')).toBeInTheDocument()
    expect(screen.getByText('zzq changelog')).toBeInTheDocument()
    // Install only arms: the copy promises an install on quit, never a restart.
    expect(dialog()?.textContent).toContain(i18nT('components.updateModal.install_on_quit_timing'))
    expect(dialog()?.textContent).not.toMatch(/relaunch automatically/i)
    expect(dialog()?.textContent).not.toMatch(/installation progress/i)
  })

  it('omits the notes paragraph when the notes are blank', async () => {
    await mount({ ...downloaded, notes: '   ' })
    expect(dialog()).toBeInTheDocument()
    expect(screen.queryByText(/zzq/)).not.toBeInTheDocument()
  })

  it('sets the Windows installer-window expectation from the main-process handoff', async () => {
    await mount({ ...downloaded, installHandoff: 'windows-installer' })
    expect(dialog()?.textContent).toMatch(/Windows installation window/i)
    expect(dialog()?.textContent).toMatch(/progress/i)
  })

  it('the header close button dismisses it', async () => {
    await mount(downloaded)
    fireEvent.click(byName('components.updateModal.dismiss'))
    expect(dialog()).not.toBeInTheDocument()
  })

  it('the Later button dismisses it', async () => {
    await mount(downloaded)
    fireEvent.click(byName('components.updateModal.later'))
    expect(dialog()).not.toBeInTheDocument()
  })

  it('a click on the backdrop itself dismisses, a click inside does not', async () => {
    await mount(downloaded)
    const backdrop = byName('components.updateModal.dismiss_update_dialog')
    fireEvent.click(dialog()!)
    expect(dialog()).toBeInTheDocument()
    fireEvent.click(backdrop)
    expect(dialog()).not.toBeInTheDocument()
  })

  it('Enter on the backdrop dismisses, a bubbled key press does not', async () => {
    await mount(downloaded)
    const backdrop = byName('components.updateModal.dismiss_update_dialog')
    fireEvent.keyDown(dialog()!, { key: 'Enter' })
    expect(dialog()).toBeInTheDocument()
    fireEvent.keyDown(backdrop, { key: ' ' })
    expect(dialog()).not.toBeInTheDocument()
  })

  it('Escape dismisses it', async () => {
    await mount(downloaded)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(dialog()).not.toBeInTheDocument()
  })

  it('ignores an unrelated key', async () => {
    await mount(downloaded)
    fireEvent.keyDown(window, { key: 'a' })
    expect(dialog()).toBeInTheDocument()
  })

  it('re-opens for a newer version after a dismissal', async () => {
    const { push } = await mount(downloaded)
    fireEvent.click(byName('components.updateModal.later'))
    expect(dialog()).not.toBeInTheDocument()
    await push({ state: 'downloaded', version: '9.9.10' })
    // The re-open runs through a render-phase setState, so it lands one
    // commit later than the cache write itself.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('9.9.10')).toBeInTheDocument()
  })

  it('installs and stays disabled after the dispatch resolves', async () => {
    await mount({ state: 'downloaded', version: '9.9.11' })
    fireEvent.click(
      await screen.findByRole('button', { name: i18nT('components.updateModal.install_on_quit') }),
    )
    await waitFor(() => expect(install).toHaveBeenCalledTimes(1))

    // The dispatch resolved but the main process has not yet said the update is
    // armed: the control holds its label and stays disabled.
    expect(byName('components.updateModal.install_on_quit')).toBeDisabled()
    expect(byName('components.updateModal.later')).toBeDisabled()

    // Escape and the Later button must not dismiss while the install runs.
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(byName('components.updateModal.later'))
    expect(dialog()).toBeInTheDocument()
  })

  it('reports an install request the main process rejected, instead of silently re-arming', async () => {
    // No update state is pushed for a rejected IPC, so nothing else on screen
    // would say the click failed.
    install.mockRejectedValue(new Error('ipc failed'))
    await mount({ state: 'downloaded', version: '9.9.11' })
    fireEvent.click(
      await screen.findByRole('button', { name: i18nT('components.updateModal.install_on_quit') }),
    )

    const notice = await screen.findByTestId('update-modal-install-error')
    expect(notice.textContent).toContain(i18nT('pages.settings.aboutPanel.update_error_install_unknown'))
    // The header says it once, and stops claiming "ready".
    expect(dialog()).toHaveAccessibleName(i18nT('components.updateModal.install_failed'))
    expect(within(dialog()!).getByText(i18nT('components.updateModal.install_failed'))).toBeInTheDocument()
    expect(within(dialog()!).queryByText(i18nT('components.updateModal.update_ready'))).toBeNull()
    // The failure is the only status claim: no "ready to install", no "will close
    // and relaunch", and the primary action says it retries.
    expect(screen.queryByText(i18nT('components.updateModal.is_downloaded_and_ready_to_install'), { exact: false })).toBeNull()
    expect(screen.queryByText(i18nT('components.updateModal.install_on_quit_timing'), { exact: false })).toBeNull()
    expect(byName('pages.settings.aboutPanel.try_again')).not.toBeDisabled()
  })

  it('re-arms Install for a newer build after the main process refused the stale one', async () => {
    // The freshness gate refuses a superseded stage and pursues the newest
    // instead: `install` resolves, the app keeps running, and the newer build
    // downloads. The one-way latch must not leave that build's button disabled.
    const { push } = await mount({ state: 'downloaded', version: '9.9.11' })
    fireEvent.click(
      await screen.findByRole('button', { name: i18nT('components.updateModal.install_on_quit') }),
    )
    await waitFor(() => expect(install).toHaveBeenCalledTimes(1))

    await push({ state: 'available', version: '9.9.12' })
    await push({ state: 'downloaded', version: '9.9.12' })

    // The label no longer changes while the arm is in flight, so wait for the latch to release.
    await waitFor(() => expect(byName('components.updateModal.install_on_quit')).not.toBeDisabled())
  })

  describe('installing overlay', () => {
    const installing: UpdateState = { state: 'installing', version: '9.9.9' }
    const overlay = () => screen.queryByRole('alert')

    it('takes over the screen on an installing push', async () => {
      await mount(installing)
      // Like the re-open case above, the first paint can land one commit
      // after the cache write -- wait for the overlay rather than asserting
      // synchronously (the sync form races on slow CI runners).
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      expect(
        screen.getByText(i18nT('components.updateModal.installing_update')),
      ).toBeInTheDocument()
      expect(
        screen.getByText(i18nT('components.updateModal.installing_update_body')),
      ).toBeInTheDocument()
      expect(screen.getByRole('alert').textContent).toMatch(/several minutes/i)
      // The quit-time apply exits without relaunching, so the copy must not promise one.
      expect(screen.getByRole('alert').textContent).not.toMatch(/relaunch/i)
      expect(screen.getByRole('alert').textContent).toMatch(/open it again/i)
    })

    it('repeats the Windows installer-window handoff before the app closes', async () => {
      await mount({ ...installing, installHandoff: 'windows-installer' })
      const text = (await screen.findByRole('alert')).textContent
      expect(text).toMatch(/Windows installation window/i)
      expect(text).toMatch(/progress/i)
    })

    it('offers no dismiss affordance: no buttons, and Escape is inert', async () => {
      await mount(installing)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      expect(screen.queryAllByRole('button')).toHaveLength(0)
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(overlay()).toBeInTheDocument()
    })

    it('replaces the classic modal once the install dispatches', async () => {
      const { push } = await mount(downloaded)
      expect(await screen.findByRole('dialog')).toBeInTheDocument()
      await push(installing)
      // Like the re-open case above, the swap can land one commit after the
      // cache write itself — wait for the overlay rather than asserting
      // synchronously.
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      expect(dialog()).not.toBeInTheDocument()
    })

    it('a failed install renders the failure state in place, not a silent vanish', async () => {
      const { push } = await mount(installing)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      await push({ state: 'error', phase: 'install', version: '9.9.9' })
      expect(
        await screen.findByText(i18nT('components.updateModal.install_failed')),
      ).toBeInTheDocument()
      expect(
        screen.getByText(i18nT('components.updateModal.install_failed_body')),
      ).toBeInTheDocument()
      // The failure is an error, so it renders through ErrorNotice with the
      // agent hand-off.
      expect(screen.getByTestId('update-modal-install-failed').textContent)
        .toContain(i18nT('components.updateModal.install_failed_body'))
      expect(screen.getByRole('button', { name: new RegExp(i18nT('components.askAgent.ask_the_agent'), 'i') })).toBeInTheDocument()
      // The failure state IS dismissable (unlike the busy state): the main
      // process restores the gateway, so the dashboard behind is usable.
      fireEvent.click(
        screen.getByRole('button', { name: i18nT('components.updateModal.back_to_dashboard') }),
      )
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('an install refused because the gateway kept running names the command that stops it', async () => {
      const { push } = await mount(installing)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      await push({
        state: 'error',
        phase: 'install',
        code: 'gateway-still-running',
        stopCommand: 'kirocrew stop --port 5476',
        version: '9.9.9',
      })
      expect(
        await screen.findByText(i18nT('pages.settings.aboutPanel.update_error_gateway_still_running')),
      ).toBeInTheDocument()
      // The generic body says "retry from Settings > About", which this
      // refusal repeats until the gateway is stopped, so it must not render.
      expect(screen.queryByText(i18nT('components.updateModal.install_failed_body'))).not.toBeInTheDocument()
      // The refusal is an error, so it renders through ErrorNotice with the
      // agent hand-off; nothing on this overlay is unsaved input.
      const notice = screen.getByTestId('update-modal-gateway-refusal')
      expect(notice.textContent).toContain(i18nT('pages.settings.aboutPanel.update_error_gateway_still_running'))
      expect(screen.getByRole('button', { name: new RegExp(i18nT('components.askAgent.ask_the_agent'), 'i') })).toBeInTheDocument()
      expect(screen.getByTestId('update-modal-gateway-stop-command').textContent).toBe('kirocrew stop --port 5476')
    })

    it('a gateway-still-running refusal without a command keeps the generic failure body', async () => {
      const { push } = await mount(installing)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      await push({ state: 'error', phase: 'install', code: 'gateway-still-running', version: '9.9.9' })
      expect(
        await screen.findByText(i18nT('components.updateModal.install_failed_body')),
      ).toBeInTheDocument()
      expect(screen.queryByTestId('update-modal-gateway-stop-command')).not.toBeInTheDocument()
    })

    it('closing the ready dialog does not hide a later install takeover or its failure', async () => {
      const { push } = await mount(downloaded)
      expect(await screen.findByRole('dialog')).toBeInTheDocument()
      fireEvent.click(byName('components.updateModal.later'))
      expect(dialog()).not.toBeInTheDocument()
      // The install is then started from Settings > About.
      await push(installing)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      await push({ state: 'error', phase: 'install', version: '9.9.9' })
      expect(await screen.findByTestId('update-modal-install-failed')).toBeInTheDocument()
    })

    it('a retry after Back to dashboard shows the takeover again', async () => {
      const { push } = await mount(installing)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      await push({ state: 'error', phase: 'install', version: '9.9.9' })
      fireEvent.click(
        await screen.findByRole('button', { name: i18nT('components.updateModal.back_to_dashboard') }),
      )
      expect(overlay()).not.toBeInTheDocument()
      await push(installing)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      await push({ state: 'error', phase: 'install', version: '9.9.9' })
      expect(await screen.findByTestId('update-modal-install-failed')).toBeInTheDocument()
    })

    it('a non-install error does not hijack the screen with the failure overlay', async () => {
      const { container } = await mount({ state: 'error', phase: 'check', version: '9.9.9' })
      expect(container.firstChild).toBeNull()
    })

    it("a 'downloaded' state still renders the classic modal, not the overlay", async () => {
      await mount(downloaded)
      expect(await screen.findByRole('dialog')).toBeInTheDocument()
      expect(overlay()).not.toBeInTheDocument()
    })
  })
})

describe('aborted install recovery', () => {
  beforeEach(() => {
    install.mockReset()
    install.mockResolvedValue(undefined)
    ;(window as unknown as { updateAPI?: unknown }).updateAPI = { armsOnQuit: true, install }
  })
  afterEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('an aborted install does not brick the modal for the next downloaded version', async () => {
    const { push } = await mount({ state: 'downloaded', version: '9.9.9' })
    fireEvent.click(
      await screen.findByRole('button', { name: i18nT('components.updateModal.install_on_quit') }),
    )
    await waitFor(() => expect(install).toHaveBeenCalledTimes(1))
    // Main process aborts the handoff (stage invalidated) — error/install state.
    await push({ state: 'error', phase: 'install', version: '9.9.9' })
    // The superseding version is later downloaded: the modal must reopen LIVE.
    await push({ state: 'downloaded', version: '9.9.10' })
    await waitFor(() => expect(byName('components.updateModal.install_on_quit')).toBeEnabled())
    fireEvent.click(byName('components.updateModal.install_on_quit'))
    await waitFor(() => expect(install).toHaveBeenCalledTimes(2))
  })
})

describe('armed update', () => {
  const relaunch = vi.fn<() => Promise<{ ok: boolean; error?: string }>>()
  const armedState: UpdateState = { state: 'downloaded', version: '9.9.9', armed: true }
  beforeEach(() => {
    install.mockReset()
    install.mockResolvedValue({ ok: true })
    relaunch.mockReset()
    relaunch.mockResolvedValue({ ok: true })
    ;(window as unknown as { updateAPI?: unknown }).updateAPI = { armsOnQuit: true, install, relaunch }
  })
  afterEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('does not stay stuck on Restarting after the click arms: it says it installs on quit and offers Restart now', async () => {
    const { push } = await mount({ state: 'downloaded', version: '9.9.9' })
    fireEvent.click(await screen.findByRole('button', { name: i18nT('components.updateModal.install_on_quit') }))
    await waitFor(() => expect(install).toHaveBeenCalledTimes(1))
    // The lane re-emits the same version, now armed.
    await push(armedState)

    expect(await screen.findByTestId('update-modal-armed')).toHaveTextContent(
      i18nT('components.updateModal.armed_installs_on_quit', { version: '9.9.9' }),
    )
    expect(screen.queryByRole('button', { name: i18nT('components.updateModal.restarting') })).toBeNull()
    expect(screen.queryByRole('button', { name: i18nT('components.updateModal.install_on_quit') })).toBeNull()
    expect(byName('components.updateModal.restart_now')).toBeEnabled()
    // Later, the close button and Escape work again.
    expect(byName('components.updateModal.later')).toBeEnabled()
    expect(byName('components.updateModal.dismiss')).toBeEnabled()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(dialog()).not.toBeInTheDocument()
  })

  it('Restart now calls updateAPI.relaunch', async () => {
    await mount(armedState)
    fireEvent.click(await screen.findByRole('button', { name: i18nT('components.updateModal.restart_now') }))
    await waitFor(() => expect(relaunch).toHaveBeenCalledTimes(1))
    expect(install).not.toHaveBeenCalled()
    expect(await screen.findByRole('button', { name: i18nT('components.updateModal.restarting') })).toBeDisabled()
  })

  it('says so when the restart is refused, and the update stays armed', async () => {
    relaunch.mockResolvedValue({ ok: false, error: 'relaunch is only available from the local dashboard' })
    await mount(armedState)
    fireEvent.click(await screen.findByRole('button', { name: i18nT('components.updateModal.restart_now') }))
    const notice = await screen.findByTestId('update-modal-restart-error')
    // Offers the agent hand-off like every other failure notice in the modal.
    expect(within(notice).getByText(i18nT('components.askAgent.ask_the_agent'))).toBeInTheDocument()
    expect(byName('components.updateModal.restart_now')).toBeEnabled()
    expect(screen.getByTestId('update-modal-armed')).toBeInTheDocument()
  })

  it('offers no Restart now on a shell without relaunch, but still says it installs on quit', async () => {
    ;(window as unknown as { updateAPI?: unknown }).updateAPI = { armsOnQuit: true, install }
    await mount(armedState)
    expect(await screen.findByTestId('update-modal-armed')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: i18nT('components.updateModal.restart_now') })).toBeNull()
  })
})

describe('a shell that predates arming', () => {
  it('never offers Install when I quit, because that shell installs at once', async () => {
    ;(window as unknown as { updateAPI?: unknown }).updateAPI = { install }
    await mount(downloaded)
    expect(await screen.findByTestId('update-modal-app-too-old')).toHaveTextContent(
      i18nT('components.updateModal.armed_app_too_old'),
    )
    expect(screen.queryByRole('button', { name: i18nT('components.updateModal.install_on_quit') })).toBeNull()
    expect(install).not.toHaveBeenCalled()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })
})

/**
 * Prints the ['update-state'] payload UpdateModal reads, so a test can wait
 * until a push has reached the component before asserting what it left closed.
 */
function StateProbe() {
  const { data } = useQuery<UpdateState | null>({
    queryKey: ['update-state'], queryFn: () => null, enabled: false, staleTime: Infinity,
  })
  return <span data-testid="update-state-probe">{data ? `${data.state} ${data.version ?? ''}${data.armed ? ' armed' : ''}` : ''}</span>
}

describe('an edition update widget that owns the prompts', () => {
  const probe = () => screen.getByTestId('update-state-probe')
  beforeEach(() => {
    install.mockReset()
    install.mockResolvedValue({ ok: true })
    ;(window as unknown as { updateAPI?: unknown }).updateAPI = { armsOnQuit: true, install }
  })
  afterEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('opens no ready dialog for a build its lane readied or for the arm the widget made', async () => {
    const rendered = renderWithProviders(<><UpdateModal /><StateProbe /></>)
    const push = async (next: UpdateState) => {
      await act(async () => { rendered.queryClient.setQueryData(['update-state'], next) })
    }
    await push({ ...downloaded, updatePrompts: 'edition' })
    await waitFor(() => expect(probe()).toHaveTextContent('downloaded 9.9.9'))
    expect(dialog()).toBeNull()
    await push({ ...downloaded, armed: true, updatePrompts: 'edition' })
    await waitFor(() => expect(probe()).toHaveTextContent('downloaded 9.9.9 armed'))
    expect(dialog()).toBeNull()
    expect(install).not.toHaveBeenCalled()
  })

  it('still shows the install takeover while the quit applies the update', async () => {
    await mount({ state: 'installing', version: '9.9.9', updatePrompts: 'edition' })
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})
