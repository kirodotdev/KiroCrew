import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Role update (design step 4): the drawer panel for a template-hired member.
 * Reads the merge plan, offers the update only when applying would change
 * anything, keeps Apply disabled until every conflict has a side, sends the
 * chosen sides with the version the plan was made against, and detaches in
 * two steps. */

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberRoleUpdatePlan: vi.fn(),
      applyMemberRoleUpdate: vi.fn(),
      detachMember: vi.fn(),
    },
  }
})

import { api, ApiError } from '../../api/client'
import RoleUpdatePanel, { conflictsResolved, fieldValueText, reviewableFields } from './RoleUpdatePanel'

const MEMBER = {
  name: 'Pager-triage',
  slug: 'pager-triage',
  slot_key: '',
  running: false,
  kiro_agent: 'Pager-triage',
  workspace: 'default',
  memory_store: 'default',
  model: '',
  source: 'kirocrew',
  starred: false,
  display_name: 'Pager triage',
  role: 'Oncall Triage Engineer',
  template: 'oncall-pack/triage',
  template_version: '1.2.0',
} as never

const PLAN = {
  member: 'Pager-triage',
  template: 'oncall-pack/triage',
  member_version: '1.2.0',
  installed_version: '1.3.0',
  update_available: true,
  member_fingerprint: 'abc123',
  template_fingerprint: 'tpl456',
  fields: [
    { id: 'f-spec-description', field: 'spec.description', state: 'unchanged', base: 'd', mine: 'd', theirs: 'd' },
    { id: 'f-spec-prompt', field: 'spec.prompt', state: 'conflict', base: 'p0', mine: 'mine prompt', theirs: 'their prompt' },
    { id: 'f-spec-tools', field: 'spec.tools', state: 'keep', base: ['A'], mine: ['A', 'B'], theirs: ['A'] },
    { id: 'f-spec-hooks', field: 'spec.hooks', state: 'apply', theirs: { agentSpawn: [] } },
    { id: 'f-card-role', field: 'card.role', state: 'agree', base: 'R', mine: 'R2', theirs: 'R2' },
    { id: 'f-card-triggers', field: 'card.triggers', state: 'apply', base: 'incident', mine: 'incident', theirs: 'incident, sev2' },
  ],
}

beforeEach(() => {
  vi.mocked(api.memberRoleUpdatePlan).mockReset()
  vi.mocked(api.applyMemberRoleUpdate).mockReset()
  vi.mocked(api.detachMember).mockReset()
})

function render() {
  return renderWithProviders(<RoleUpdatePanel member={MEMBER} appLabel="Oncall pack" />)
}

describe('RoleUpdatePanel helpers', () => {
  it('reviews only what changes or needs a decision, and knows when every conflict has a side', () => {
    const fields = reviewableFields(PLAN.fields as never)
    expect(fields.map((f) => f.field)).toEqual(['spec.prompt', 'spec.tools', 'spec.hooks', 'card.triggers'])
    expect(conflictsResolved(fields, {})).toBe(false)
    expect(conflictsResolved(fields, { 'f-spec-prompt': 'mine' })).toBe(true)
    // The field NAME is a redacted label, never the handle: keyed by it, nothing is resolved.
    expect(conflictsResolved(fields, { 'spec.prompt': 'mine' })).toBe(false)
    expect(fieldValueText(undefined, 'absent')).toBe('absent')
    expect(fieldValueText('text', 'absent')).toBe('text')
    expect(fieldValueText(['A', 'B'], 'absent')).toBe('[\n "A",\n "B"\n]')
  })
})

describe('RoleUpdatePanel', () => {
  it('says the template is current when nothing would change', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue({ ...PLAN, update_available: false, installed_version: '1.2.0' } as never)
    render()
    expect(await screen.findByTestId('member-role-update-current')).toHaveTextContent('Template up to date (v1.2.0).')
    expect(screen.queryByTestId('member-role-update-review')).toBeNull()
  })

  it('offers the update, blocks Apply until the conflict is decided, and sends the decision with the version it saw', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue(PLAN as never)
    vi.mocked(api.applyMemberRoleUpdate).mockResolvedValue({ ok: true })
    render()
    expect(await screen.findByTestId('member-role-update-available')).toHaveTextContent(
      'Oncall pack v1.3.0 is available (this crewmate is on v1.2.0).',
    )
    fireEvent.click(screen.getByTestId('member-role-update-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Update role from Oncall pack' })
    // unchanged and agree rows are not shown; the rest carry their state.
    const list = within(dialog).getByTestId('role-update-fields')
    expect(within(list).getAllByRole('listitem')).toHaveLength(4)
    expect(within(dialog).getByTestId('role-update-field-spec.prompt')).toHaveTextContent('Both changed — choose')
    // Spec keys read as task names, not JSON keys; an unmapped key keeps its name.
    expect(within(dialog).getByTestId('role-update-field-spec.prompt')).toHaveTextContent('Instructions')
    expect(within(dialog).getByTestId('role-update-field-spec.hooks')).toHaveTextContent('Hooks')
    expect(within(dialog).getByTestId('role-update-field-spec.tools')).toHaveTextContent('You changed — kept')
    expect(within(dialog).getByTestId('role-update-field-card.triggers')).toHaveTextContent('Template changed — will apply')
    expect(within(dialog).getByTestId('role-update-field-card.triggers')).toHaveTextContent('incident, sev2')
    expect(within(dialog).queryByTestId('role-update-field-spec.description')).toBeNull()
    expect(within(dialog).queryByTestId('role-update-field-card.role')).toBeNull()
    const apply = within(dialog).getByTestId('role-update-apply')
    expect(apply).toBeDisabled()
    expect(apply).toHaveTextContent('Apply v1.3.0')
    // The disabled state says why: the undecided conflict.
    expect(apply).toHaveAttribute('title', "Choose Keep mine or Take the template's for the 1 field that changed on both sides.")
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Instructions: Keep mine' }))
    expect(apply).not.toBeDisabled()
    expect(apply).not.toHaveAttribute('title')
    fireEvent.click(apply)
    await waitFor(() => expect(api.applyMemberRoleUpdate).toHaveBeenCalledTimes(1))
    expect(api.applyMemberRoleUpdate).toHaveBeenCalledWith('Pager-triage', {
      resolutions: { 'f-spec-prompt': 'mine' },
      expected_version: '1.3.0',
      member_fingerprint: 'abc123',
      template_fingerprint: 'tpl456',
    })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update role from Oncall pack' })).toBeNull())
  })

  it('says so when the template moved since the plan, and keeps the dialog open', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue({
      ...PLAN,
      fields: PLAN.fields.filter((f) => f.state !== 'conflict'),
    } as never)
    vi.mocked(api.applyMemberRoleUpdate).mockRejectedValue(
      new ApiError(409, 'moved', JSON.stringify({ code: 'template_changed', installed_version: '1.4.0' })),
    )
    render()
    fireEvent.click(await screen.findByTestId('member-role-update-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Update role from Oncall pack' })
    fireEvent.click(within(dialog).getByTestId('role-update-apply'))
    const notice = await within(dialog).findByTestId('role-update-error')
    expect(notice).toHaveTextContent('The template moved since this plan was made. Review it again.')
    // No hand-off from inside the dialog: it holds the user's unsaved conflict
    // picks, and Ask the agent would unmount it and lose them.
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('says so when the member was edited since the plan, and drops the picks made against the old plan', async () => {
    // First read: the plan with its conflict. After the 409 the refetch returns
    // a plan whose MINE digest moved -- a different conflict set -- so the
    // "Keep mine" picked for the old prompt must not be pre-filled on the new one.
    vi.mocked(api.memberRoleUpdatePlan)
      .mockResolvedValueOnce(PLAN as never)
      .mockResolvedValue({ ...PLAN, member_fingerprint: 'def789' } as never)
    vi.mocked(api.applyMemberRoleUpdate).mockRejectedValue(
      new ApiError(409, 'changed', JSON.stringify({ code: 'member_changed_since_plan' })),
    )
    render()
    fireEvent.click(await screen.findByTestId('member-role-update-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Update role from Oncall pack' })
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Instructions: Keep mine' }))
    const apply = within(dialog).getByTestId('role-update-apply')
    expect(apply).not.toBeDisabled()
    fireEvent.click(apply)
    expect(await within(dialog).findByTestId('role-update-error')).toHaveTextContent(
      'This crewmate was edited since this plan was made. Review it again.',
    )
    // The refetched plan carries new fingerprints: the conflict is undecided again.
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: 'Instructions: Keep mine' })).not.toBeChecked())
    expect(within(dialog).getByTestId('role-update-apply')).toBeDisabled()
  })

  it('explains an unavailable template instead of offering nothing', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockRejectedValue(
      new ApiError(409, 'disabled', JSON.stringify({ code: 'app_disabled' })),
    )
    render()
    expect(await screen.findByTestId('member-role-update-unavailable')).toHaveTextContent(
      'Oncall pack is disabled; enable it to update from it.',
    )
    expect(screen.queryByTestId('member-role-update-review')).toBeNull()
    // The refusal carries the shared hand-off, like every persisted-state error.
    expect(within(screen.getByTestId('member-role-update-unavailable')).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // Detach stays available: severing is how a member leaves a broken template.
    expect(screen.getByTestId('member-detach')).toBeInTheDocument()
  })

  it('detaches only after a confirm step, explaining what stays', async () => {
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue(PLAN as never)
    vi.mocked(api.detachMember).mockResolvedValue({ ok: true })
    render()
    // The cost is said BEFORE the click, under the button.
    expect(await screen.findByTestId('member-detach-cost')).toHaveTextContent("Keeps everything; stops following Oncall pack. Cannot be undone — you'll be asked to confirm first.")
    // The button names the app it detaches from, like the Agent template row
    // and the update title: one "template" in the panel, not two.
    expect(screen.getByTestId('member-detach')).toHaveTextContent('Detach from Oncall pack')
    fireEvent.click(screen.getByTestId('member-detach'))
    expect(api.detachMember).not.toHaveBeenCalled()
    expect(screen.queryByTestId('member-detach-cost')).toBeNull()
    expect(screen.getByTestId('member-detach-explain')).toHaveTextContent('stops following Oncall pack')
    // The confirm pair is its own row: Review update does not sit beside it --
    // but it stays in place, disabled, so "v1.3.0 is available" above it is not
    // read as "updating is no longer possible from here".
    const row = screen.getByTestId('member-detach-confirm-row')
    expect(within(row).getAllByRole('button')).toHaveLength(2)
    expect(screen.getByTestId('member-role-update-review')).toBeDisabled()
    // ...and says why it is greyed out, the way Apply carries its blocked reason.
    expect(screen.getByTestId('member-role-update-review')).toHaveAttribute('title', 'Finish or cancel the detach first.')
    expect(screen.getByTestId('member-detach-explain')).toHaveTextContent('to follow Oncall pack again, hire a new crewmate from it')
    fireEvent.click(screen.getByTestId('cancel-detach-member'))
    expect(screen.getByTestId('member-role-update-review')).toBeEnabled()
    expect(screen.getByTestId('member-role-update-review')).not.toHaveAttribute('title')
    expect(screen.queryByTestId('confirm-detach-member')).toBeNull()
    fireEvent.click(screen.getByTestId('member-detach'))
    fireEvent.click(screen.getByTestId('confirm-detach-member'))
    await waitFor(() => expect(api.detachMember).toHaveBeenCalledWith('Pager-triage'))
  })

  it('tells the drawer a detach succeeded, naming the app, so the success is not silent', async () => {
    // The refetch clears `template`, which unmounts this panel; the closure
    // ("Detached from X") is the drawer's to render, from this callback.
    vi.mocked(api.memberRoleUpdatePlan).mockResolvedValue(PLAN as never)
    vi.mocked(api.detachMember).mockResolvedValue({ ok: true })
    const onDetached = vi.fn()
    renderWithProviders(<RoleUpdatePanel member={MEMBER} appLabel="Oncall pack" onDetached={onDetached} />)
    fireEvent.click(await screen.findByTestId('member-detach'))
    fireEvent.click(screen.getByTestId('confirm-detach-member'))
    await waitFor(() => expect(onDetached).toHaveBeenCalledWith('Oncall pack'))
    // A refused detach reports the failure and does not claim closure.
    onDetached.mockClear()
    vi.mocked(api.detachMember).mockRejectedValue(new Error('nope'))
    fireEvent.click(screen.getByTestId('member-detach'))
    fireEvent.click(screen.getByTestId('confirm-detach-member'))
    await screen.findByTestId('member-detach-error')
    expect(onDetached).not.toHaveBeenCalled()
  })
})
