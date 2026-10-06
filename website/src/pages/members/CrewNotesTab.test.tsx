import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { ApiError } from '../../api/apiError'
import { memberBriefingQueryKey } from '../../api/membersQuery'

const memberBriefing = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: { memberBriefing: (...a: unknown[]) => memberBriefing(...a) },
  }
})

import CrewNotesTab from './CrewNotesTab'

/* The Notes body now opens as a page pushed inside the Profile card, so its
   states are pinned on the component itself rather than through the page. */

const briefing = (over: Record<string, unknown> = {}) => ({
  slug: 'oncall',
  member: 'oncall',
  supported: true,
  text: '## What I look after\n\n- The issue queue.\n',
  updated_ts: Date.now() / 1000 - 600,
  redacted: false,
  truncated: false,
  ...over,
})

const tab = (visible = true) => (
  <CrewNotesTab slug="oncall" member="oncall" header={<div data-testid="notes-header" />} visible={visible} />
)

const renderNotes = (visible = true) => renderWithProviders(tab(visible))

beforeEach(() => {
  memberBriefing.mockReset()
})

describe('CrewNotesTab (the crewmate\'s own notes)', () => {
  it('says whose notes these are before they are read, and offers nothing to change them with', async () => {
    memberBriefing.mockResolvedValue(briefing())
    renderNotes()
    const notes = screen.getByTestId('member-notes')
    const line = within(notes).getByTestId('member-notes-agent-only')
    expect(line).toHaveTextContent(
      'oncall writes these notes for itself as it works. You can read them here, but not change them.',
    )
    const body = await within(notes).findByTestId('member-notes-body')
    expect(line.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(notes).queryByRole('button', { name: /edit/i })).toBeNull()
    expect(within(notes).queryByRole('textbox')).toBeNull()
    expect(notes.querySelector('[contenteditable="true"]')).toBeNull()
  })

  it('renders the briefing as markdown, dated, keyed by slug and exact name', async () => {
    memberBriefing.mockResolvedValue(briefing())
    renderNotes()
    const body = await screen.findByTestId('member-notes-body')
    expect(within(body).getByRole('heading', { name: 'What I look after' })).toBeInTheDocument()
    expect(within(body).getByText('The issue queue.')).toBeInTheDocument()
    expect(memberBriefing).toHaveBeenCalledWith('oncall', 'oncall')
    expect(screen.getByTestId('member-notes-footer')).toHaveTextContent(/^Updated /)
    expect(screen.queryByTestId('member-notes-hidden')).toBeNull()
  })

  it('no notes yet is an EMPTY state naming the crewmate, undated — never an error', async () => {
    memberBriefing.mockResolvedValue(briefing({ text: '', updated_ts: null }))
    renderNotes()
    const empty = await screen.findByTestId('member-notes-empty')
    expect(empty).toHaveTextContent("oncall hasn't written any notes yet.")
    expect(screen.getByTestId('member-notes-agent-only')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('member-notes-footer')).toBeNull()
  })

  it('a platform that cannot read the file safely says so in one sentence, not as an alert', async () => {
    memberBriefing.mockResolvedValue(briefing({ supported: false, text: '', updated_ts: null }))
    renderNotes()
    expect(await screen.findByTestId('member-notes-unsupported')).toHaveTextContent(
      "Notes can't be read on this computer.",
    )
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a failed read renders the shared ErrorNotice under the agent-only line, never the empty state', async () => {
    memberBriefing.mockRejectedValue(new Error('boom'))
    renderNotes()
    const notice = await screen.findByTestId('member-notes-error')
    expect(notice).toHaveAttribute('role', 'alert')
    // Read the copy out of the catalog rather than typing it here, so a copy
    // edit moves the pin instead of leaving a stale literal. This notice sits
    // above an "Ask the agent" hand-off, so it must not name a possessor
    // ("this crewmate's notes") -- the notice and the hand-off would then point
    // at two different helpers. The same one-actor-word rule the docked webview
    // error follows (see CrewWebviewDocked.test.tsx).
    const notesError = (
      JSON.parse(
        readFileSync(resolve(__dirname, '../../i18n/locales/en.json'), 'utf8'),
      ) as { pages: { membersPage: { notes_error: string } } }
    ).pages.membersPage.notes_error
    expect(notesError).not.toMatch(/crewmate['’]s/i)
    expect(notesError).not.toMatch(/this [^.]*['’]s/i)
    expect(notice).toHaveTextContent(notesError)
    expect(screen.getByTestId('member-notes-agent-only')).toBeInTheDocument()
    expect(screen.queryByTestId('member-notes-empty')).toBeNull()
    expect(screen.queryByTestId('member-notes-footer')).toBeNull()
  })

  it('two crewmates sharing the slug: the read is refused and the tab says so in plain words', async () => {
    memberBriefing.mockRejectedValue(new ApiError(409, 'briefing_slug_ambiguous'))
    renderNotes()
    const line = await screen.findByTestId('member-notes-collision')
    expect(line).toHaveTextContent(/Two crewmates share this short name/)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('member-notes-footer')).toBeNull()
  })

  it('a redacted briefing says so in a visible line above the text, not a tooltip', async () => {
    memberBriefing.mockResolvedValue(briefing({ text: 'Token: [REDACTED: credential]', redacted: true }))
    renderNotes()
    const body = await screen.findByTestId('member-notes-body')
    const hidden = screen.getByTestId('member-notes-hidden')
    expect(hidden).toHaveAttribute('role', 'status')
    expect(hidden).toHaveTextContent(/looks like a secret was hidden here/)
    expect(hidden.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('a briefing cut at the cap says the rest is only in the file, above the text', async () => {
    memberBriefing.mockResolvedValue(
      briefing({ text: 'y'.repeat(200) + '\n[... briefing truncated at cap — prune it]', truncated: true }),
    )
    renderNotes()
    await screen.findByTestId('member-notes-body')
    expect(screen.getByTestId('member-notes-hidden')).toHaveTextContent(/longer than the panel shows/)
  })

  it('keys the briefing under the registry prefix, so a roster refresh revalidates cached notes', () => {
    expect(memberBriefingQueryKey('oncall', 'oncall')).toEqual([
      'kirocrew-agents',
      'member-briefing',
      'oncall',
      'oncall',
    ])
  })

  it('a refetch that fails over cached notes keeps them on screen under a "could not refresh" notice', async () => {
    memberBriefing.mockResolvedValueOnce(briefing()).mockRejectedValueOnce(new Error('boom'))
    const { rerender } = renderNotes()
    await screen.findByTestId('member-notes-body')
    // Hiding and showing the tab re-issues the read (`enabled` flips).
    rerender(tab(false))
    rerender(tab(true))
    const notice = await screen.findByTestId('member-notes-refresh-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent("Couldn't refresh these notes.")
    expect(screen.getByTestId('member-notes-body')).toBeInTheDocument()
    expect(screen.queryByTestId('member-notes-error')).toBeNull()
  })

  it('the read is gated on the tab being on screen: nothing is fetched while it is hidden', async () => {
    memberBriefing.mockResolvedValue(briefing())
    const { rerender } = renderNotes(false)
    expect(memberBriefing).not.toHaveBeenCalled()
    rerender(tab(true))
    await waitFor(() => expect(memberBriefing).toHaveBeenCalledTimes(1))
  })
})
