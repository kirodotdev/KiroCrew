import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { CrewmateExportPreview } from '../../api/client'
import type { Card } from '../../api/cards'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'

const memberExportPreview = vi.fn()
const memberExportFile = vi.fn()
const memberExportLessonsDraft = vi.fn()
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberExportPreview: (...a: unknown[]) => memberExportPreview(...a),
      memberExportFile: (...a: unknown[]) => memberExportFile(...a),
      memberExportLessonsDraft: (...a: unknown[]) => memberExportLessonsDraft(...a),
    },
  }
})

const importCrewmate = vi.fn()
const pending = vi.fn()
vi.mock('../../api/cards', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/cards')>()
  return {
    ...actual,
    cardsApi: {
      ...actual.cardsApi,
      importCrewmate: (...a: unknown[]) => importCrewmate(...a),
      pending: (...a: unknown[]) => pending(...a),
    },
  }
})

// The card itself is the shared component, covered by its own tests.
vi.mock('../../cards/ChangeCard', () => ({
  default: ({ card }: { card: Card }) => (
    <div data-testid="change-card-stub" data-status={card.status}>{String(card.params.name)}</div>
  ),
}))

const downloadBlob = vi.fn()
vi.mock('../../utils/download', () => ({ downloadBlob: (...a: unknown[]) => downloadBlob(...a) }))

import CrewExportView from './CrewExportView'

/* Make a copy: the Export confirm view in its copy mode. It lists what a copy
   gets in its own words, and its press hands the export straight to Import from
   file, showing the import card it raised in place. */

const L = (key: string) => i18nT(`pages.membersPage.${key}`)

const view = (over: Partial<CrewmateExportPreview> = {}): CrewmateExportPreview => ({
  name: 'Atlas',
  filename: 'Atlas.kirobot',
  description: 'Writes the weekly digest.',
  model: 'some-model',
  reasoning_effort: 'high',
  dashboard: '',
  avatar: 'ghost',
  avatar_digest: 'avatar-shown',
  rules: 'Cite every source.',
  goals: { longTerm: 'Publish a weekly digest', keyResults: [{ text: 'Ship four digests', target: '4' }] },
  crons: [{ name: 'morning digest', cron_expr: '0 9 * * 1-5', message: 'Draft the digest.' }],
  skills: [{ name: 'triage', lessons: false, files: { 'SKILL.md': '# Triage' } }],
  mcp_servers: [{ server: 'gh', placeholders: ['env.GITHUB_TOKEN'] }],
  mcp_in_file: false,
  left_out: [
    { kind: 'cron', name: 'nightly', reason: 'script_refused' },
    { kind: 'skill', name: 'big', reason: 'bundle_full' },
    { kind: 'goals', name: '', reason: 'first_goal_only' },
    { kind: 'cron', name: 'gone', reason: 'script_missing' },
  ],
  fingerprint: 'fp-shown',
  ...over,
})

const card = (over: Partial<Card> = {}): Card => ({
  id: 'imp-1', slot_key: 'owner:crewmate-import', kind: 'crewmate.import', revision: 1, status: 'pending',
  risk: 'normal', title: 'Import Bot “Atlas 3”', changes: [], editable: ['name'],
  params: { name: 'Atlas 3', format_version: 1 }, plan: { apply: [], undo: null },
  created_at: 1_800_000_000_000, expires_at: 1_800_086_400_000,
  ...over,
})

const refusal = (status: number, code: string) => new ApiError(status, 'raw text', JSON.stringify({ error: 'raw text', code }))

function show(props: Partial<Parameters<typeof CrewExportView>[0]> = {}) {
  return renderWithProviders(<CrewExportView mode="copy" slug="atlas" member="Atlas" name="Atlas" {...props} />)
}

beforeEach(() => {
  memberExportPreview.mockReset()
  memberExportFile.mockReset()
  memberExportLessonsDraft.mockReset()
  importCrewmate.mockReset()
  pending.mockReset()
  pending.mockResolvedValue([])
  downloadBlob.mockReset()
})

describe('CrewExportView in copy mode', () => {
  it('lists what the copy gets in copy words, without model, effort, lessons or milestones', async () => {
    memberExportPreview.mockResolvedValue(view())
    show()
    await screen.findByTestId('crew-export-view')
    expect(screen.queryByTestId('crew-export-model')).toBeNull()
    expect(screen.queryByTestId('crew-export-reasoning-effort')).toBeNull()
    expect(screen.queryByTestId('crew-export-lessons')).toBeNull()
    const identity = screen.getByTestId('crew-export-identity')
    expect(identity).toHaveTextContent(L('copy_identity'))
    expect(identity).toHaveTextContent(L('export_always'))
    expect(identity.textContent).not.toContain('Atlas')
    const goals = screen.getByTestId('crew-export-goals')
    expect(goals).toHaveTextContent('Publish a weekly digest')
    expect(goals.textContent).not.toContain('Ship four digests')
    const mcp = screen.getByTestId('crew-export-mcp')
    expect(mcp).toHaveTextContent('gh')
    expect(mcp).toHaveTextContent(L('copy_mcp_not_yet'))
    expect(mcp.textContent).not.toContain('env.GITHUB_TOKEN')
    const left = screen.getByTestId('crew-export-left-out')
    expect(left).toHaveTextContent(L('copy_left_out'))
    expect(left).toHaveTextContent(L('copy_reason_script_refused'))
    expect(left).toHaveTextContent(L('copy_reason_bundle_full'))
    expect(left).toHaveTextContent(L('copy_reason_first_goal_only'))
    expect(left).toHaveTextContent(L('export_reason_script_missing'))
    expect(screen.getByTestId('crew-copy-intro')).toHaveTextContent(L('copy_intro'))
    expect(screen.queryByText(L('export_intro'))).toBeNull()
    expect(screen.queryByTestId('crew-export-not-included')).toBeNull()
    expect(screen.queryByTestId('crew-export-download')).toBeNull()
    expect(screen.getByTestId('crew-copy-make')).toHaveTextContent(L('copy_make'))
  })

  it('says what it is reading while the plan loads', () => {
    memberExportPreview.mockReturnValue(new Promise(() => {}))
    show()
    expect(screen.getByTestId('crew-export-loading')).toHaveTextContent(L('copy_loading'))
  })

  it('builds the file with the current choices and no confirm, posts it to the import, and shows the card in place', async () => {
    memberExportPreview.mockResolvedValue(view())
    const blob = new Blob(['PK'])
    memberExportFile.mockResolvedValue(blob)
    importCrewmate.mockResolvedValue(card())
    show()
    await screen.findByTestId('crew-export-view')
    fireEvent.click(within(screen.getByTestId('crew-export-rules')).getByRole('switch'))
    fireEvent.click(screen.getByTestId('crew-copy-make'))
    expect(await screen.findByTestId('change-card-stub')).toHaveTextContent('Atlas 3')
    expect(memberExportFile).toHaveBeenCalledTimes(1)
    const [slug, member, choices] = memberExportFile.mock.calls[0]
    expect([slug, member]).toEqual(['atlas', 'Atlas'])
    expect(choices).toEqual({ rules: false, goals: true, crons: true, skills: ['triage'], lessons: null })
    expect(choices).not.toHaveProperty('confirm')
    // No plan check: the plan was read once, when the view opened.
    expect(memberExportPreview).toHaveBeenCalledTimes(1)
    expect(importCrewmate).toHaveBeenCalledWith(blob)
    expect(downloadBlob).not.toHaveBeenCalled()
    // The card stands in for the list and the button, under the paragraph.
    expect(screen.getByTestId('crew-copy-intro')).toBeInTheDocument()
    expect(screen.getByTestId('crew-copy-card-hint')).toHaveTextContent(L('copy_card_hint'))
    expect(screen.getByTestId('crew-copy-card-waits')).toHaveTextContent(L('copy_card_waits'))
    expect(screen.queryByTestId('crew-export-rules')).toBeNull()
    expect(screen.queryByTestId('crew-copy-make')).toBeNull()
  })

  it('moves focus from the pressed button to the line above the card it raised', async () => {
    memberExportPreview.mockResolvedValue(view())
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    importCrewmate.mockResolvedValue(card())
    show()
    const make = await screen.findByTestId('crew-copy-make')
    make.focus()
    fireEvent.click(make)
    await screen.findByTestId('change-card-stub')
    expect(screen.getByTestId('crew-copy-card-hint')).toHaveFocus()
  })

  it('drops the waiting line once the card is no longer pending', async () => {
    memberExportPreview.mockResolvedValue(view())
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    importCrewmate.mockResolvedValue(card({ status: 'applied' }))
    show()
    fireEvent.click(await screen.findByTestId('crew-copy-make'))
    expect(await screen.findByTestId('change-card-stub')).toHaveAttribute('data-status', 'applied')
    expect(screen.getByTestId('crew-copy-card-hint')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-copy-card-waits')).toBeNull()
  })

  it.each([
    ['too_many_cards', 'copy_refused_too_many_cards'],
    ['already_exists', 'copy_refused_already_exists'],
    ['stage_failed', 'copy_failed'],
    ['duplicate_card', 'copy_failed'],
  ])('words an import refusal %s in copy words', async (code, key) => {
    memberExportPreview.mockResolvedValue(view())
    memberExportFile.mockResolvedValue(new Blob(['PK']))
    importCrewmate.mockRejectedValue(refusal(409, code))
    show()
    fireEvent.click(await screen.findByTestId('crew-copy-make'))
    const notice = await screen.findByTestId('crew-copy-error')
    expect(notice).toHaveTextContent(L(key))
    expect(notice.textContent).not.toContain('raw text')
    expect(screen.queryByTestId('change-card-stub')).toBeNull()
    expect(screen.getByTestId('crew-copy-make')).toBeEnabled()
  })

  it('words an export refusal as the copy that could not be made, and posts nothing', async () => {
    memberExportPreview.mockResolvedValue(view())
    memberExportFile.mockRejectedValue(refusal(400, 'record_unreadable'))
    show()
    fireEvent.click(await screen.findByTestId('crew-copy-make'))
    expect(await screen.findByTestId('crew-copy-error')).toHaveTextContent(L('copy_failed'))
    expect(importCrewmate).not.toHaveBeenCalled()
  })

  it('says how to fix a name too long to copy', async () => {
    memberExportPreview.mockRejectedValue(refusal(400, 'invalid_name'))
    show({ name: 'An unusually long bot name xx' })
    expect(await screen.findByTestId('crew-export-load-error')).toHaveTextContent(L('copy_name_too_long'))
  })

  it('keeps the general copy line for any other load failure', async () => {
    memberExportPreview.mockRejectedValue(refusal(400, 'invalid_name'))
    show({ name: 'Atlas' })
    const notice = await screen.findByTestId('crew-export-load-error')
    expect(notice).toHaveTextContent(L('copy_load_failed'))
    expect(notice.textContent).not.toContain(L('copy_name_too_long'))
  })
})
