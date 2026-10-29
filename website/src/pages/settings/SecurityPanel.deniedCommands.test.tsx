/**
 * SecurityPanel's Denied Commands section: built-in and custom rules, the add-pattern form
 * and its drafts, and the rule search. Split from SecurityPanel.test.tsx, whose api mock
 * this file copies; the fixtures they share are in src/test/SecurityPanelTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { DeniedCommandsData } from '../../api/client'

/* ── api client mock ───────────────────────────────────────────────────────
 * SecurityPanel drives all its mutations through the `api` client methods.  We
 * mock those methods so no network I/O happens; each returns the (mutated)
 * snapshot the panel then re-renders from.  `securityPosture` feeds the Live
 * Security Posture card, and `governancePolicy` feeds the ceiling viewer.
 */
vi.mock('../../api/client', () => ({
  // The real class, not a stub: `trustFailureMessage` branches on `instanceof
  // ApiError` to decide whether a structured body is available to read.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
  api: {
    deniedCommands: vi.fn(),
    toggleBuiltinDeniedCommand: vi.fn(),
    setDeniedCommandsDisableAll: vi.fn(),
    addUserDeniedCommand: vi.fn(),
    toggleUserDeniedCommand: vi.fn(),
    deleteUserDeniedCommand: vi.fn(),
    redactionAllowedHosts: vi.fn(),
    redactionRevokeHost: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    // Read + write for the third-party-app execution toggle. Also consumed by
    // YoloDurationCard, which tolerates an unresolved read.
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    // Read by the rail on every mount to summarise the tailnet-origin section.
    // Present here so the rail's own read is a resolved query rather than a
    // crash on an undefined queryFn; the section's behaviour is covered in
    // SecurityPanel.tailnet.test.tsx.
    tailnetStatus: vi.fn(),
    listTrustedApps: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    setTrustAllApps: vi.fn(),
    // Read + the two writes behind the registry git-identity trust section.
    listTrustedRegistries: vi.fn(),
    grantTrustedRegistry: vi.fn(),
    revokeTrustedRegistry: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'
import { i18nT } from '../../i18n/t'
import {
  PINNED_DESC,
  TOGGLE_DESC,
  USER_PATTERN,
  NOTED_PATTERN,
  USER_NOTE,
  TAILNET_OFF,
  snapshot,
  posture,
  govNoPolicy,
} from '../../test/SecurityPanelTestUtils'

/** The note field's copy keys are authored separately from this component, so
 *  resolve them the way the panel does instead of asserting literal English —
 *  the same rationale as the `T` block in SecurityPanel.test.tsx. The assertion then tracks the
 *  BEHAVIOUR (this control is wired to that key) and stays green across the
 *  catalog landing, while still failing if the panel switches keys. */
const NOTE_LABEL = () => i18nT('pages.settings.securityPanel.custom_deny_note')

/** Render the panel with the denied-commands query pre-resolved.
 *
 * Built-in rules live inside per-category accordions that are COLLAPSED by
 * default, so the rows are not in the DOM until a category is expanded. After
 * the query hydrates, click "Expand all" so every rule row is present for the
 * assertions below (mirrors what a user does to reach an individual rule). */
async function renderPanel(data: DeniedCommandsData = snapshot()) {
  ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
  // Wait for the async query to hydrate the category accordion, then expand all.
  const expandAll = await screen.findByRole('button', { name: 'Expand all' })
  fireEvent.click(expandAll)
  await screen.findByLabelText(TOGGLE_DESC)
  return utils
}

describe('SecurityPanel — denied commands', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.toggleBuiltinDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.setDeniedCommandsDisableAll as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.addUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.toggleUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.deleteUserDeniedCommand as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  it('toggling a built-in OFF opens the confirm modal and only mutates after ack', async () => {
    await renderPanel()

    // Turning a built-in OFF must NOT mutate immediately — it opens the modal.
    fireEvent.click(screen.getByRole('switch', { name: TOGGLE_DESC }))
    expect(api.toggleBuiltinDeniedCommand).not.toHaveBeenCalled()

    // Modal is open; the Disable button is gated on the ack checkbox.
    const dialog = await screen.findByRole('dialog')
    const disableBtn = within(dialog).getByRole('button', { name: 'Disable' })
    expect(disableBtn).toBeDisabled()

    // Clicking Disable while un-acked is a no-op.
    fireEvent.click(disableBtn)
    expect(api.toggleBuiltinDeniedCommand).not.toHaveBeenCalled()

    // Ack the warning, then Disable → the mutation fires with enabled=false.
    fireEvent.click(
      screen.getByLabelText("I understand this weakens Kiro Crew's protection."),
    )
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }))
    await waitFor(() =>
      expect(api.toggleBuiltinDeniedCommand).toHaveBeenCalledWith(
        'aws-destructive-cfn-delete-stack',
        false,
      ),
    )
  })

  it('toggling a built-in ON is immediate (no modal)', async () => {
    await renderPanel(
      snapshot({
        builtins: [
          {
            id: 'aws-destructive-cfn-delete-stack',
            pattern: 'aws.*cloudformation.*delete-stack.*',
            category: 'aws-destructive',
            description: TOGGLE_DESC,
            enabled: false,
            pinned: false,
          },
        ],
        user_added: [],
      }),
    )

    fireEvent.click(screen.getByRole('switch', { name: TOGGLE_DESC }))
    await waitFor(() =>
      expect(api.toggleBuiltinDeniedCommand).toHaveBeenCalledWith(
        'aws-destructive-cfn-delete-stack',
        true,
      ),
    )
    // No confirm modal for enabling.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('pinned rows render locked and never call the mutation', async () => {
    await renderPanel()

    const pinnedToggle = screen.getByRole('switch', { name: PINNED_DESC })
    // Pinned toggle is disabled (forced on, un-opt-out-able).
    expect(pinnedToggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(pinnedToggle)
    expect(api.toggleBuiltinDeniedCommand).not.toHaveBeenCalled()
    // No modal opens either.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('disable-all stays available and functional when governance-locked', async () => {
    // A policy pin on ONE rule sets governance_locked, but the backend keeps
    // pinned rules enforced under disable_all — so the disable-all control must
    // remain operable to opt every OTHER (unpinned) rule out.
    await renderPanel(snapshot({ governance_locked: true }))

    const disableAll = screen.getByRole('switch', { name: 'Disable all built-in denies' })
    expect(disableAll).toBeEnabled()
    expect(disableAll).toHaveAttribute('aria-checked', 'false')

    // Turning it ON opens the confirm modal (same guarded flow as unlocked).
    fireEvent.click(disableAll)
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(
      screen.getByLabelText("I understand this weakens Kiro Crew's protection."),
    )
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(api.setDeniedCommandsDisableAll).toHaveBeenCalledWith(true))
  })

  it('add-pattern validates the regex: invalid shows inline error, no API call', async () => {
    await renderPanel()

    const input = screen.getByLabelText('Custom deny pattern')
    fireEvent.change(input, { target: { value: '(unclosed' } })
    // Submit via Enter.
    fireEvent.keyDown(input, { key: 'Enter' })

    // Inline error surfaces and no API call is made.
    await screen.findByText(/Invalid regular expression|Unterminated group|Invalid group/i)
    expect(api.addUserDeniedCommand).not.toHaveBeenCalled()

    // A valid pattern clears the error and calls the API. The note is untouched
    // here, so it arrives as the empty string the client defaults to.
    fireEvent.change(input, { target: { value: 'rm -rf /data' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(api.addUserDeniedCommand).toHaveBeenCalledWith('rm -rf /data', ''),
    )
  })

  it('add-pattern sends the typed note through to the API and clears both drafts', async () => {
    await renderPanel()

    fireEvent.change(screen.getByLabelText('Custom deny pattern'), {
      target: { value: 'find / -delete' },
    })
    const note = screen.getByLabelText(NOTE_LABEL())
    fireEvent.change(note, { target: { value: '  Scope it with -maxdepth first.  ' } })
    // Enter on the NOTE field submits too — the note is the last thing typed, so
    // requiring a trip back to the pattern input to commit would lose it.
    fireEvent.keyDown(note, { key: 'Enter' })

    // Trimmed: this prose is read back to the agent verbatim when the rule fires,
    // so surrounding whitespace must not survive the submit.
    await waitFor(() =>
      expect(api.addUserDeniedCommand).toHaveBeenCalledWith(
        'find / -delete',
        'Scope it with -maxdepth first.',
      ),
    )
    // Both drafts clear together. A note left behind would silently attach
    // itself to the NEXT pattern the reader adds, mislabelling that refusal.
    expect(screen.getByLabelText('Custom deny pattern')).toHaveValue('')
    expect(screen.getByLabelText(NOTE_LABEL())).toHaveValue('')
  })

  it('a REJECTED add keeps both drafts so the operator can correct them', async () => {
    // The server rejects a note carrying the refusal prefix (400). Clearing on
    // submit rather than on success would discard text the operator then has to
    // retype from memory -- and the rejected note is exactly the text they need
    // in front of them to fix.
    ;(api.addUserDeniedCommand as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('note must not contain'),
    )
    await renderPanel()

    const pattern = screen.getByLabelText('Custom deny pattern')
    const note = screen.getByLabelText(NOTE_LABEL())
    fireEvent.change(pattern, { target: { value: 'find / -delete' } })
    fireEvent.change(note, { target: { value: 'Blocked by security policy: nope' } })
    fireEvent.keyDown(pattern, { key: 'Enter' })

    await waitFor(() => expect(api.addUserDeniedCommand).toHaveBeenCalled())
    // Both survive the failure.
    expect(screen.getByLabelText('Custom deny pattern')).toHaveValue('find / -delete')
    expect(screen.getByLabelText(NOTE_LABEL())).toHaveValue(
      'Blocked by security policy: nope',
    )
    // ...and the rejection is VISIBLE. Keeping the drafts without saying why would
    // leave Add looking like it silently did nothing.
    expect(await screen.findByText(/must not contain/i)).toBeInTheDocument()
  })

  it('a slow add does not erase drafts the operator typed while it was in flight', async () => {
    // The inputs stay editable while an add is pending, so an operator can start
    // the NEXT rule before the previous one resolves. Clearing unconditionally on
    // success would erase that newer text.
    let release!: (snap: DeniedCommandsData) => void
    ;(api.addUserDeniedCommand as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise<DeniedCommandsData>(resolve => { release = resolve }),
    )
    await renderPanel()

    const pattern = screen.getByLabelText('Custom deny pattern')
    const note = screen.getByLabelText(NOTE_LABEL())
    fireEvent.change(pattern, { target: { value: 'first .*' } })
    fireEvent.change(note, { target: { value: 'first note' } })
    fireEvent.keyDown(pattern, { key: 'Enter' })
    await waitFor(() => expect(api.addUserDeniedCommand).toHaveBeenCalled())

    // Operator moves on to the next rule while the first is still pending.
    fireEvent.change(pattern, { target: { value: 'second .*' } })
    fireEvent.change(note, { target: { value: 'second note' } })

    release(snapshot())

    // Flush the resolution INSIDE act: without this the assertions below can pass
    // on the first poll, before React has processed onSuccess, and the test would
    // be vacuous -- it would pass with or without the guard it exists to prove.
    await act(async () => {
      await Promise.resolve()
    })

    // The in-flight add resolving must not touch the newer drafts.
    expect(screen.getByLabelText('Custom deny pattern')).toHaveValue('second .*')
    expect(screen.getByLabelText(NOTE_LABEL())).toHaveValue('second note')
  })

  it('a note deliberately reused for the next rule is not cleared by the previous add', async () => {
    // Partial reuse: the operator submits (A, N), then retypes only the pattern
    // because the SAME rationale applies to the next rule. Clearing per-field
    // would wipe N -- correct by string equality, wrong by intent. Clearing is
    // all-or-nothing: if either field moved, neither is touched.
    let release!: (snap: DeniedCommandsData) => void
    ;(api.addUserDeniedCommand as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise<DeniedCommandsData>(resolve => { release = resolve }),
    )
    await renderPanel()

    const pattern = screen.getByLabelText('Custom deny pattern')
    const note = screen.getByLabelText(NOTE_LABEL())
    fireEvent.change(pattern, { target: { value: 'first .*' } })
    fireEvent.change(note, { target: { value: 'shared rationale' } })
    fireEvent.keyDown(pattern, { key: 'Enter' })
    await waitFor(() => expect(api.addUserDeniedCommand).toHaveBeenCalled())

    // Only the pattern changes; the note is intentionally kept.
    fireEvent.change(pattern, { target: { value: 'second .*' } })

    release(snapshot())
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.getByLabelText('Custom deny pattern')).toHaveValue('second .*')
    expect(screen.getByLabelText(NOTE_LABEL())).toHaveValue('shared rationale')
  })

  it('add-pattern with an untouched note sends an empty string, not undefined', async () => {
    await renderPanel()

    const input = screen.getByLabelText('Custom deny pattern')
    fireEvent.change(input, { target: { value: 'shutdown -h now' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // The note is optional to the READER, but the argument is not optional on the
    // wire: the client posts whatever it is handed, and `undefined` would drop
    // the field from the body instead of stating "no note". Assert the exact
    // empty string rather than an arity-free match, so a regression to
    // `addUserDeniedCommand(pattern)` fails here.
    await waitFor(() =>
      expect(api.addUserDeniedCommand).toHaveBeenCalledWith('shutdown -h now', ''),
    )
  })

  it('a user rule with a note renders it under its own pattern', async () => {
    await renderPanel()

    // Scoped to the row: the note explains one specific pattern, so "somewhere
    // on the card" is not the claim being made.
    const noted = screen.getByText(NOTED_PATTERN).parentElement!
    expect(within(noted).getByText(USER_NOTE)).toBeInTheDocument()
  })

  it('a user rule without a note renders no note paragraph', async () => {
    await renderPanel()

    // `user-1` carries no note, so the optional slot must emit nothing at all —
    // an empty <p> would still claim vertical space under the pattern.
    expect(screen.getByText(USER_PATTERN).parentElement!.querySelector('p')).toBeNull()
    // Paired with the noted rule from the SAME render, so this cannot pass by the
    // panel having stopped rendering notes altogether.
    expect(screen.getByText(NOTED_PATTERN).parentElement!.querySelector('p')).not.toBeNull()
  })

  it('delete is only available on user rows', async () => {
    await renderPanel()

    // The delete affordance targets the user pattern specifically.
    const del = screen.getByLabelText(`Delete pattern ${USER_PATTERN}`)
    fireEvent.click(del)
    await waitFor(() =>
      expect(api.deleteUserDeniedCommand).toHaveBeenCalledWith('user-1'),
    )
    // Built-in rows have no delete affordance.
    expect(screen.queryByLabelText(`Delete pattern ${TOGGLE_DESC}`)).not.toBeInTheDocument()
  })

  it('chevron reveals the built-in pattern text', async () => {
    await renderPanel()

    // Pattern is hidden until the row is expanded.
    expect(screen.queryByText('aws.*cloudformation.*delete-stack.*')).not.toBeInTheDocument()
    const showButtons = screen.getAllByLabelText('Show pattern')
    fireEvent.click(showButtons[0])
    expect(screen.getByText('aws.*cloudformation.*delete-stack.*')).toBeInTheDocument()
  })
})

describe('SecurityPanel — rule search', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  const SEARCH = 'Search rules, patterns, categories…'

  async function renderRules() {
    renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
    return screen.findByLabelText(SEARCH)
  }

  it('matches on description and reveals the hit without expanding anything', async () => {
    const box = await renderRules()
    // Categories are collapsed on arrival, so the rule row is not in the DOM yet.
    expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    // A filter whose hits stay folded away is a filter that did nothing.
    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.queryByLabelText(PINNED_DESC)).not.toBeInTheDocument()
  })

  it('matches on the pattern text, not just the description', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'terminate-instances' } })

    expect(await screen.findByLabelText(PINNED_DESC)).toBeInTheDocument()
    expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument()
  })

  it('a category-name match keeps the whole category', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'aws destr' } })

    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.getByLabelText(PINNED_DESC)).toBeInTheDocument()
  })

  it('the raw category KEY matches the whole category (aws-destructive)', async () => {
    // The display label is "Aws Destructive"; the id-shaped spelling users copy
    // from config and audit logs is `aws-destructive`. Both must land.
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'aws-destructive' } })

    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.getByLabelText(PINNED_DESC)).toBeInTheDocument()
    expect(screen.getByText(/2 \/ 2 rules/)).toBeInTheDocument()
  })

  it('a full rule id matches exactly that rule', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'aws-destructive-ec2-terminate-instances' } })

    expect(await screen.findByLabelText(PINNED_DESC)).toBeInTheDocument()
    expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument()
  })

  it('a dotted <category>.<slug> id spelling matches the same rule', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'aws-destructive.ec2-terminate-instances' } })

    expect(await screen.findByLabelText(PINNED_DESC)).toBeInTheDocument()
    expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument()
  })

  it('an id fragment matches every rule whose id contains it', async () => {
    // `cfn-delete-stack` appears only in the id: the description says
    // "CloudFormation" and the pattern says "cloudformation", neither "cfn".
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'cfn-delete' } })

    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.queryByLabelText(PINNED_DESC)).not.toBeInTheDocument()
  })

  it('custom patterns match on their id too', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'user-2' } })

    expect(await screen.findByText(NOTED_PATTERN)).toBeInTheDocument()
    expect(screen.queryByText(USER_PATTERN)).not.toBeInTheDocument()
  })

  it('the category badge keeps the SHIPPED denominator while filtered', async () => {
    // The load-bearing assertion of this feature: a filter must never make the
    // gate read as smaller than it is. Showing "1/1" for a single hit inside a
    // 2-rule category would tell the reader a rule is not enforced.
    const box = await renderRules()
    expect(await screen.findByText('2/2')).toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()
    expect(screen.getByText('2/2')).toBeInTheDocument()
    expect(screen.queryByText('1/1')).not.toBeInTheDocument()
    // The match count is reported as a ratio against the shipped total instead.
    expect(screen.getByText(/1 \/ 2 rules/)).toBeInTheDocument()
  })

  it('says when the filter is what emptied the custom-pattern card', async () => {
    // Otherwise an empty Card B reads as "you have no custom patterns" while one
    // is configured and enforced.
    const box = await renderRules()
    expect(await screen.findByText(USER_PATTERN)).toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    expect(screen.queryByText(USER_PATTERN)).not.toBeInTheDocument()
    expect(
      screen.getByText('Your custom patterns are hidden by the current filter.'),
    ).toBeInTheDocument()
  })

  it('reports a miss instead of an empty list', async () => {
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'zzzz' } })

    expect(await screen.findByText('No rules match “zzzz”.')).toBeInTheDocument()
  })

  it('clearing the filter restores the collapsed accordion state', async () => {
    // The filter force-opens its hits; clearing it must hand the accordion back
    // to whatever the user had, not leave every category expanded.
    const box = await renderRules()
    fireEvent.change(box, { target: { value: 'cloudformation' } })
    expect(await screen.findByLabelText(TOGGLE_DESC)).toBeInTheDocument()

    fireEvent.change(box, { target: { value: '' } })

    await waitFor(() => expect(screen.queryByLabelText(TOGGLE_DESC)).not.toBeInTheDocument())
  })
})
