import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { WeixinPanel } from '../pages/settings/WeixinPanel'
import { api, type WeixinConfigData } from '../api/client'

/**
 * WeChat channel panel — DM-policy picker.
 *
 * Covers the native-<select> → SimpleSelect migration for the "Who can message
 * the bot" control. The panel's Playwright drive
 * (`scripts/test-weixin-panel.mjs`) exercises the same step, but only against a
 * built `dist/`; this keeps the contract under the unit suite too.
 *
 * The picker is a Radix Select, so a `change` event on the trigger does nothing —
 * open it, then click the option.
 */

const CONFIG: WeixinConfigData = {
  connected: true,
  connect_error: '',
  configured: true,
  read_only: false,
  credential_set: true,
  enabled: true,
  account_id: 'a5ace6fd482e@im.bot',
  dm_policy: 'open',
  allowed_user_ids: [],
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG })
})

describe('WeixinPanel — DM policy picker', () => {
  it('labels the trigger from the field caption and shows the saved policy', async () => {
    renderWithProviders(<WeixinPanel />)
    const trigger = await screen.findByRole('combobox', { name: 'Who can message the bot' })
    // The trigger renders before the config query settles (falling back to the
    // 'allowlist' default), so the saved value is an async assertion.
    await waitFor(() => expect(trigger).toHaveTextContent('Anyone who messages the bot'))
  })

  it('selecting a policy PUTs the new value', async () => {
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('combobox', { name: 'Who can message the bot' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Only allowed user IDs' }))

    await waitFor(() => expect(save).toHaveBeenCalledWith({ dm_policy: 'allowlist' }))
  })

  it('disables the picker when the config is read-only', async () => {
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, read_only: true })
    renderWithProviders(<WeixinPanel />)
    const trigger = await screen.findByRole('combobox', { name: 'Who can message the bot' })
    await waitFor(() => expect(trigger).toBeDisabled())
  })

  it('lists every policy in the popup', async () => {
    renderWithProviders(<WeixinPanel />)
    fireEvent.click(await screen.findByRole('combobox', { name: 'Who can message the bot' }))
    expect((await screen.findAllByRole('option')).map(o => o.textContent)).toEqual([
      'Anyone who messages the bot',
      'Only allowed user IDs',
      'Nobody (ignore all messages)',
    ])
  })
})


describe('WeixinPanel — enable toggle', () => {
  const ENABLE_SWITCH = { name: 'Enable the WeChat channel' }

  it('renders the shared switch, not a bare checkbox', async () => {
    renderWithProviders(<WeixinPanel />)
    const toggle = await screen.findByRole('switch', ENABLE_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    // The bare <input type="checkbox"> this replaced would satisfy no switch
    // role; the shared component also carries the visible label as the
    // accessible name, which the role+name query above already proves.
    expect(toggle.tagName).not.toBe('INPUT')
  })

  it('toggling saves the flipped enabled value immediately', async () => {
    // Same contract as every control on this panel: no Save button, so the
    // switch must persist on change.
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', ENABLE_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(toggle)

    await waitFor(() => expect(save).toHaveBeenCalledWith({ enabled: false }))
  })

  it('toggles from the keyboard', async () => {
    // The shared component owns keyboard semantics; this pins that the
    // migration kept them (the old checkbox got Space handling for free).
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', ENABLE_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.keyDown(toggle, { key: ' ' })

    await waitFor(() => expect(save).toHaveBeenCalledWith({ enabled: false }))
  })

  it('clicking the row (not the switch) saves exactly once', async () => {
    // SettingsToggle's row is a second activation surface; its inner-switch
    // stopPropagation is what keeps a row click from double-saving.
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', ENABLE_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(screen.getByText('Enable the WeChat channel'))

    await waitFor(() => expect(save).toHaveBeenCalledWith({ enabled: false }))
    expect(save).toHaveBeenCalledTimes(1)
  })

  it('is disabled when the config is read-only', async () => {
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, read_only: true })
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', ENABLE_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-disabled', 'true'))
    fireEvent.click(toggle)
    fireEvent.keyDown(toggle, { key: ' ' })
    // The row wrapper is its own activation surface and must be inert too.
    fireEvent.click(screen.getByText('Enable the WeChat channel'))

    // A disabled switch must ignore both activation paths.
    expect(save).not.toHaveBeenCalled()
  })
})

/**
 * WeChat channel panel — session folder.
 *
 * Queried the way `BotChannelPanel.sessionFolder.test.tsx` queries the other six
 * channels — `role="switch"` by accessible name, the name field by its visible
 * label — so this suite fails if the panel drifts back to hand-rolled markup
 * instead of the shared `SettingsToggle` / `SettingsInput` primitives. The
 * behaviour assertions are WeChat-specific because this panel alone has no Save
 * button and must persist on change.
 */
const FOLDER_SWITCH = { name: 'File sessions in a folder' }

describe('WeixinPanel — session folder', () => {
  it('renders the shared switch, not a checkbox', async () => {
    renderWithProviders(<WeixinPanel />)
    const toggle = await screen.findByRole('switch', FOLDER_SWITCH)
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(toggle.tagName).not.toBe('INPUT')
  })

  it('sits below the allowlist, at the bottom of the panel', async () => {
    // Placement is the actual bug: mid-panel it read as part of the
    // access-policy block, so users looking where every other channel puts it
    // concluded WeChat had no such setting.
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, dm_policy: 'allowlist' })
    renderWithProviders(<WeixinPanel />)

    const allowlist = await screen.findByTestId('weixin-allowlist')
    const folder = await screen.findByTestId('weixin-session-folder')
    expect(allowlist.compareDocumentPosition(folder)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('enabling the toggle saves the default name immediately', async () => {
    // This panel has no Save button, so a toggle that only reveals the field
    // loses the setting for every user who turns it on, sees the name already
    // filled in, and walks away.
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('switch', FOLDER_SWITCH))

    expect(await screen.findByLabelText('Folder name')).toBeTruthy()
    await waitFor(() => expect(save).toHaveBeenCalledWith({ session_folder: 'WeChat' }))
  })

  it('saves the committed name when the field blurs, not per keystroke', async () => {
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('switch', FOLDER_SWITCH))
    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'Team chat' } })
    expect(save).toHaveBeenCalledTimes(1)
    fireEvent.blur(input)

    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: 'Team chat' }))
  })

  it('commits the name on Enter', async () => {
    // Enter is the only commit affordance a keyboard user has here — the panel
    // has no Save button — so it must survive the move onto SettingsInput.
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('switch', FOLDER_SWITCH))
    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'Team chat' } })
    // Enter commits BY blurring, and jsdom only dispatches blur for the focused
    // element — without this the assertion would pass on a no-op handler.
    ;(input as HTMLInputElement).focus()
    expect(document.activeElement).toBe(input)
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: 'Team chat' }))
  })

  it("describes the folder as created on turn-on, not on a save this panel doesn't have", async () => {
    // The shared botChannelPanel copy says "when you save these settings" —
    // accurate on the six panels with a Save button, a phantom instruction on
    // this one, where everything persists on change.
    vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('switch', FOLDER_SWITCH))
    await screen.findByLabelText('Folder name')

    expect(
      screen.getByText('Created for you when you turn this on, if it does not exist yet.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/when you save these settings/i)).not.toBeInTheDocument()
  })

  it('confirms a committed rename with a transient "Saved." next to the field', async () => {
    // Without this, blur / Enter succeeds invisibly: no Save button was
    // pressed, so nothing else on the panel acknowledges the commit.
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('switch', FOLDER_SWITCH))
    const input = await screen.findByLabelText('Folder name')
    // The toggle's own save must not raise the confirmation — flipping the
    // switch is its own feedback, and "Saved." appearing before any rename
    // would teach users the field self-saves per keystroke.
    expect(screen.queryByTestId('weixin-session-folder-saved')).not.toBeInTheDocument()

    fireEvent.change(input, { target: { value: 'Team chat' } })
    fireEvent.blur(input)
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: 'Team chat' }))

    const confirmation = await screen.findByTestId('weixin-session-folder-saved')
    expect(confirmation).toHaveTextContent('Saved.')
  })

  it('withholds and clears the confirmation when the commit fails', async () => {
    // A "Saved." check next to a rejection error would read as the failed
    // value having been saved.
    let stored = 'WeChat'
    vi.spyOn(api, 'getWeixinConfig').mockImplementation(async () => ({
      ...CONFIG,
      session_folder: stored,
    }))
    const save = vi.spyOn(api, 'saveWeixinConfig').mockImplementation(async patch => {
      if (typeof patch.session_folder === 'string' && patch.session_folder.includes('/')) {
        throw new Error('Folder name cannot contain "/"')
      }
      if (typeof patch.session_folder === 'string') stored = patch.session_folder
      return { ...CONFIG }
    })
    renderWithProviders(<WeixinPanel />)

    const input = await screen.findByLabelText('Folder name')
    // A good rename first, so a confirmation is up when the bad one fails.
    fireEvent.change(input, { target: { value: 'Team chat' } })
    fireEvent.blur(input)
    await screen.findByTestId('weixin-session-folder-saved')

    fireEvent.change(input, { target: { value: 'bad/name' } })
    fireEvent.blur(input)

    await screen.findByTestId('weixin-session-folder-error')
    expect(screen.queryByTestId('weixin-session-folder-saved')).not.toBeInTheDocument()
    expect(save).toHaveBeenLastCalledWith({ session_folder: 'bad/name' })
  })

  it('ignores a slow commit that resolves after a newer one was rejected', async () => {
    // Each blur fires its own request, so completions can land out of order: a
    // delayed rename resolving after a newer rename was rejected must not paint
    // "Saved." next to the rejection — that asserts the failed draft was stored.
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, session_folder: 'WeChat' })
    let resolveSlow!: () => void
    const slow = new Promise<{ ok: boolean }>(res => {
      resolveSlow = () => res({ ok: true })
    })
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockImplementationOnce(() => slow as never)
      .mockImplementationOnce(async () => {
        throw new Error('Folder name cannot contain "/"')
      })
    renderWithProviders(<WeixinPanel />)

    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'Team chat' } })
    fireEvent.blur(input)
    fireEvent.change(input, { target: { value: 'bad/name' } })
    fireEvent.blur(input)
    await screen.findByTestId('weixin-session-folder-error')
    expect(save).toHaveBeenCalledTimes(2)

    resolveSlow()
    // Flush the stale completion's microtasks, then assert it was discarded:
    // the error stays, the confirmation never appears.
    await waitFor(() =>
      expect(screen.queryByTestId('weixin-session-folder-saved')).not.toBeInTheDocument(),
    )
    expect(screen.getByTestId('weixin-session-folder-error')).toBeInTheDocument()
  })

  it('shows the rejection even when another control saves in the same gesture', async () => {
    // Clicking any other control is what blurs the name field, so an invalid
    // rename and that control's own save land back to back. The newer,
    // unrelated save must not swallow the rename's rejection — the field would
    // silently keep a name the server refused.
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, session_folder: 'WeChat' })
    const save = vi.spyOn(api, 'saveWeixinConfig').mockImplementation(async patch => {
      if (typeof patch.session_folder === 'string' && patch.session_folder.includes('/')) {
        throw new Error('Folder name cannot contain "/"')
      }
      return { ...CONFIG }
    })
    renderWithProviders(<WeixinPanel />)

    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'bad/name' } })
    // The blur commit and the unrelated control's save fire in one gesture.
    fireEvent.blur(input)
    fireEvent.click(screen.getByRole('switch', { name: 'Enable the WeChat channel' }))

    await screen.findByTestId('weixin-session-folder-error')
    expect(save).toHaveBeenCalledWith({ session_folder: 'bad/name' })
    expect(save).toHaveBeenCalledWith({ enabled: false })
    expect(screen.queryByTestId('weixin-session-folder-saved')).not.toBeInTheDocument()
  })

  it("keeps the rejection when the other control's save resolves after it", async () => {
    // The two requests race with no ordering guarantee. When the unrelated
    // control's SUCCESS lands after the rename's rejection, it must not clear
    // an error it did not produce — pinned deterministically by holding the
    // orthogonal save open until the rejection is on screen.
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, session_folder: 'WeChat' })
    let resolveOrthogonal!: () => void
    const orthogonal = new Promise<{ ok: boolean }>(res => {
      resolveOrthogonal = () => res({ ok: true })
    })
    vi.spyOn(api, 'saveWeixinConfig').mockImplementation(patch => {
      if ('enabled' in patch) return orthogonal as never
      return Promise.reject(new Error('Folder name cannot contain "/"')) as never
    })
    renderWithProviders(<WeixinPanel />)

    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'bad/name' } })
    fireEvent.blur(input)
    fireEvent.click(screen.getByRole('switch', { name: 'Enable the WeChat channel' }))
    await screen.findByTestId('weixin-session-folder-error')

    resolveOrthogonal()
    // Flush the orthogonal completion, then assert it left the rejection alone.
    await waitFor(() =>
      expect(screen.queryByTestId('weixin-session-folder-saved')).not.toBeInTheDocument(),
    )
    expect(screen.getByTestId('weixin-session-folder-error')).toBeInTheDocument()
  })

  it('does not resurface a stale confirmation after the setting is toggled off and on', async () => {
    // The confirmation unmounts with the field when the setting is switched
    // off; if the flag survived, the next turn-on would repaint "Saved." while
    // the last completed write was the off-patch that cleared the name.
    let stored = 'WeChat'
    vi.spyOn(api, 'getWeixinConfig').mockImplementation(async () => ({
      ...CONFIG,
      session_folder: stored,
    }))
    vi.spyOn(api, 'saveWeixinConfig').mockImplementation(async patch => {
      if (typeof patch.session_folder === 'string') stored = patch.session_folder
      return { ...CONFIG }
    })
    renderWithProviders(<WeixinPanel />)

    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'Team chat' } })
    fireEvent.blur(input)
    await screen.findByTestId('weixin-session-folder-saved')

    const toggle = screen.getByRole('switch', FOLDER_SWITCH)
    fireEvent.click(toggle) // off — unmounts the field and the confirmation
    await waitFor(() => expect(screen.queryByLabelText('Folder name')).not.toBeInTheDocument())
    fireEvent.click(toggle) // back on, within the confirmation's display window

    await screen.findByLabelText('Folder name')
    expect(screen.queryByTestId('weixin-session-folder-saved')).not.toBeInTheDocument()
  })

  it('keeps a custom name when the setting is switched off and back on', async () => {
    // Off persists "" (the backend encodes off as an empty name), so the panel
    // must hold the name locally across that round trip.
    //
    // The fixture is STATEFUL on purpose: a GET that keeps replying "Team chat"
    // no matter what was saved models a server that never forgets, and passes
    // even when the panel drops the name entirely.
    let stored = 'Team chat'
    vi.spyOn(api, 'getWeixinConfig').mockImplementation(async () => ({
      ...CONFIG,
      session_folder: stored,
    }))
    const save = vi.spyOn(api, 'saveWeixinConfig').mockImplementation(async patch => {
      if (typeof patch.session_folder === 'string') stored = patch.session_folder
      return { ...CONFIG }
    })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', FOLDER_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(toggle)
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: '' }))
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    fireEvent.click(toggle)

    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: 'Team chat' }))
  })

  it('keeps a rename that the server accepted even when its refetch fails', async () => {
    // The query does not retry, so a failed refetch leaves `data` stale and the
    // seed effect never sees the new name. If the save path did not record it,
    // a later off/on would persist the superseded name over the rename.
    let stored = 'Team chat'
    let gets = 0
    vi.spyOn(api, 'getWeixinConfig').mockImplementation(async () => {
      gets += 1
      if (gets > 1) throw new Error('network')
      return { ...CONFIG, session_folder: stored }
    })
    const save = vi.spyOn(api, 'saveWeixinConfig').mockImplementation(async patch => {
      if (typeof patch.session_folder === 'string') stored = patch.session_folder
      return { ...CONFIG }
    })
    renderWithProviders(<WeixinPanel />)

    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.blur(input)
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: 'Renamed' }))

    const toggle = screen.getByRole('switch', FOLDER_SWITCH)
    fireEvent.click(toggle)
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: '' }))
    fireEvent.click(toggle)

    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: 'Renamed' }))
  })

  it('records an overlapping save\u2019s own outcome', async () => {
    // Per-call `mutate` callbacks live on the mutation observer, so a second save
    // starting before the first resolves replaces them. Clicking the toggle is
    // what blurs the name field, so this sequence is the ordinary path: the
    // rename must still be recorded, or re-enabling overwrites it.
    let stored = 'Team chat'
    let release: (() => void) | null = null
    vi.spyOn(api, 'getWeixinConfig').mockImplementation(async () => ({
      ...CONFIG,
      session_folder: stored,
    }))
    const save = vi.spyOn(api, 'saveWeixinConfig').mockImplementation(async patch => {
      if (typeof patch.session_folder === 'string') stored = patch.session_folder
      if (patch.session_folder === 'Renamed') {
        await new Promise<void>(r => { release = r })
      }
      return { ...CONFIG }
    })
    renderWithProviders(<WeixinPanel />)

    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.blur(input)
    await waitFor(() => expect(save).toHaveBeenCalledWith({ session_folder: 'Renamed' }))

    // Second save starts while the rename is still in flight.
    const toggle = screen.getByRole('switch', FOLDER_SWITCH)
    fireEvent.click(toggle)
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: '' }))
    await waitFor(() => expect(release).not.toBeNull())
    release!()
    // The recording runs in the resolved promise's continuation, several
    // microtasks after release(); clicking synchronously would read the ref
    // before it is written and test the harness rather than the panel.
    await act(async () => {})

    fireEvent.click(toggle)
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ session_folder: 'Renamed' }))
  })

  it('surfaces a rejected ENABLE, whose revert unmounts the field', async () => {
    // This is the case that makes the error node's placement load-bearing. The
    // server has no folder, so the revert after a rejected enable returns the
    // switch to off and the field unmounts — an error nested inside that block
    // would never paint.
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, session_folder: '' })
    vi.spyOn(api, 'saveWeixinConfig').mockRejectedValue(new Error('config is read-only'))
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', FOLDER_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    fireEvent.click(toggle)

    const err = await screen.findByTestId('weixin-session-folder-error')
    expect(err.textContent).toContain('read-only')
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    expect(screen.queryByLabelText('Folder name')).not.toBeInTheDocument()
  })

  it('reverts the switch when a toggle-off is rejected', async () => {
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, session_folder: 'WeChat' })
    vi.spyOn(api, 'saveWeixinConfig').mockRejectedValue(new Error('config is read-only'))
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', FOLDER_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(toggle)

    const err = await screen.findByTestId('weixin-session-folder-error')
    expect(err.textContent).toContain('read-only')
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
  })

  it('shows the server error when a folder name is rejected', async () => {
    // Reject ONLY the invalid name: a blanket reject would also fail the
    // toggle-on save, whose revert unmounts the field before it can be typed in.
    vi.spyOn(api, 'saveWeixinConfig').mockImplementation(async patch => {
      if (typeof patch.session_folder === 'string' && patch.session_folder.includes('/')) {
        throw new Error('Folder name cannot contain / or \\')
      }
      return { ...CONFIG }
    })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('switch', FOLDER_SWITCH))
    const input = await screen.findByLabelText('Folder name')
    fireEvent.change(input, { target: { value: 'a/b' } })
    fireEvent.blur(input)

    const err = await screen.findByTestId('weixin-session-folder-error')
    expect(err.textContent).toContain('cannot contain')
    // The name field must NOT be reverted on rejection — keeping the typed text
    // is what lets the user correct it.
    expect(screen.getByDisplayValue('a/b')).toBeInTheDocument()
  })

  it('disables both controls when the config is read-only', async () => {
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({
      ...CONFIG,
      read_only: true,
      session_folder: 'WeChat',
    })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByRole('switch', FOLDER_SWITCH)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-disabled', 'true'))
    expect(await screen.findByLabelText('Folder name')).toBeDisabled()
  })
})
