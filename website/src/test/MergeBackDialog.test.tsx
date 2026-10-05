/** The merge dialog behind a fork's "Merge into parent…". It drafts before it
 *  writes anything, sends exactly the text the person approved, carries the gap
 *  sentence into that text, and handles a closed or busy parent. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createAsyncThunk } from '@reduxjs/toolkit'
import { StrictMode, useEffect } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'
import type { ChatSlot } from '../types'
import MergeBackDialog, { MAX_MERGE_SUMMARY_CHARS, charCount, draftWasCut, initialMergeText } from '../components/MergeBackDialog'
import { MergeBackDialogProvider, useMergeBackDialog } from '../hooks/useMergeBackDialog'
import { ERROR_HANDOFF_KEY, recordError, __resetErrorJournalForTests } from '../utils/errorReport'

const mocks = vi.hoisted(() => ({
  mergeBackDraft: vi.fn(),
  mergeBack: vi.fn(),
  resume: vi.fn(),
  switched: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, property: string) => (
      property in target ? target[property] : vi.fn().mockResolvedValue([])
    ),
  }),
}))

// The two thunks the dialog dispatches, replaced by real thunks over mocked
// payload creators: the store, the reducers and `unwrap()` all stay genuine.
vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return {
    ...actual,
    resumeFromHistory: createAsyncThunk('test/resumeFromHistory', (arg: unknown) => mocks.resume(arg)),
    switchSlot: createAsyncThunk('test/switchSlot', (key: unknown) => { mocks.switched(key); return key }),
  }
})

vi.mock('../pages/chat/composerFocus', () => ({ focusComposer: vi.fn() }))

/** Mirrors the gateway: every refusal is an ApiError whose body carries a code. */
function refusal(status: number, code: string, extra: Record<string, unknown> = {}) {
  return Object.assign(new Error(code), { name: 'ApiError', status, body: JSON.stringify({ error: code, code, ...extra }) })
}

const PARENT: ChatSlot = { key: 'parent', title: 'Pick a cache', messages: 6, running: false }
const FORK: ChatSlot = { key: 'fork', title: '↳ Fork of Pick a cache', messages: 4, running: false, forked_from: 'dashboard:parent' }

const DIGEST = 'ab'.repeat(32)

const draft = (over: Record<string, unknown> = {}) => ({
  ok: true, summary: 'Redis works. TTL is 300s.', through: 'mid-9', digest: DIGEST, messages: 2, remaining: 0, trimmed: false, parent: 'parent', ...over,
})

/** The sentence every opening text ends with; it is what the parent's agent reads about the gap. */
const GAP = 'Messages sent here after the fork was made were not seen by the fork.'
/** The text the dialog opens with for the default draft. */
const OPENING = `Redis works. TTL is 300s.\n\n${GAP}`

function Opener() {
  const dialog = useMergeBackDialog()
  useEffect(() => { dialog?.open('fork') }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return null
}

function mount(slots: ChatSlot[] = [PARENT, FORK], { strict = false } = {}) {
  const store = createTestStore()
  store.dispatch(sseSlots(slots))
  store.dispatch(setActiveSlot('fork'))
  const tree = (
    <MergeBackDialogProvider>
      <Opener />
      <MergeBackDialog />
    </MergeBackDialogProvider>
  )
  const view = renderWithProviders(strict ? <StrictMode>{tree}</StrictMode> : tree, { store })
  return { store, ...view }
}

const text = () => screen.getByTestId('merge-back-text') as HTMLTextAreaElement
const mergeButton = () => screen.getByTestId('merge-back-merge') as HTMLButtonElement

beforeEach(() => {
  mocks.mergeBackDraft.mockResolvedValue(draft())
  mocks.mergeBack.mockResolvedValue({ ok: true, parent: 'parent', messages: 2, deferred: false })
  mocks.resume.mockResolvedValue({ ok: true, key: 'parent', surface: '' })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('initialMergeText', () => {
  it('is the trimmed summary, a blank line, then the gap sentence', () => {
    expect(initialMergeText({ summary: '  Redis works.  ' })).toBe(`Redis works.\n\n${GAP}`)
  })

  it('always ends with the gap sentence, whatever the summary says', () => {
    // The sentence names no count, so it is true whether or not the parent moved
    // on, and it is never left out: it is how the parent's agent learns of the gap.
    for (const summary of ['', 'Redis works.', 'The fork saw everything.', 'word '.repeat(800).trim()]) {
      expect(initialMergeText({ summary }).endsWith(GAP)).toBe(true)
    }
    expect(initialMergeText({ summary: '' })).toBe(GAP)
  })

  it('makes room for the gap sentence in a summary already at the limit', () => {
    const text = initialMergeText({ summary: 'word '.repeat(800).trim() })

    expect(charCount(text)).toBeLessThanOrEqual(MAX_MERGE_SUMMARY_CHARS)
    expect(text.endsWith(`…\n\n${GAP}`)).toBe(true)
  })
})

describe('draftWasCut', () => {
  it('is what the gateway says for a summary with room for the gap sentence', () => {
    expect(draftWasCut({ summary: 'Redis works.', trimmed: false })).toBe(false)
    expect(draftWasCut({ summary: 'Redis works…', trimmed: true })).toBe(true)
  })

  it('is true for a summary the opening text has to cut, whatever the gateway says', () => {
    // 3999 characters is within the gateway's limit but leaves the gap sentence no room.
    expect(draftWasCut({ summary: 'x'.repeat(MAX_MERGE_SUMMARY_CHARS - 1), trimmed: false })).toBe(true)
    expect(draftWasCut({ summary: 'word '.repeat(800).trim(), trimmed: false })).toBe(true)
  })
})

describe('MergeBackDialog', () => {
  it('drafts on open, then merges exactly the approved text and opens the parent', async () => {
    const user = userEvent.setup()
    mount()
    expect(screen.getByTestId('merge-back-drafting')).toBeInTheDocument()
    await waitFor(() => expect(text().value).toBe(OPENING))
    expect(screen.getByRole('heading', { name: 'Merge into “Pick a cache”' })).toBeInTheDocument()
    // The counter reads the trimmed length against the gateway's limit.
    expect(screen.getByTestId('merge-back-count')).toHaveTextContent(`${charCount(OPENING)} / ${MAX_MERGE_SUMMARY_CHARS}`)
    expect(mocks.mergeBackDraft).toHaveBeenCalledWith('fork')

    await user.clear(text())
    await user.type(text(), 'Use Redis.')
    await user.click(mergeButton())

    await waitFor(() => expect(mocks.mergeBack).toHaveBeenCalledWith('fork', 'Use Redis.', 'mid-9', DIGEST))
    await waitFor(() => expect(mocks.switched).toHaveBeenCalledWith({ key: 'parent', announceOnMissing: true }))
    expect(screen.queryByTestId('merge-back-text')).not.toBeInTheDocument()
  })

  it('announces the parent switch after a successful merge', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())

    await user.click(mergeButton())

    await waitFor(() => expect(mocks.switched).toHaveBeenCalledWith({ key: 'parent', announceOnMissing: true }))
  })

  it('says how many more messages did not fit this draft and that merging again sends them', async () => {
    mocks.mergeBackDraft.mockResolvedValue(draft({ remaining: 3 }))
    mount()

    expect(await screen.findByTestId('merge-back-remaining')).toHaveTextContent(
      '3 more messages did not fit in this draft. Merge again after this one to send them.',
    )
  })

  // The label above the text and the remaining line under it must agree: a
  // label counting only the covered messages read as the whole unmerged count.
  it('labels a partial draft with the covered count out of every message not merged yet', async () => {
    mocks.mergeBackDraft.mockResolvedValue(draft({ messages: 4, remaining: 3 }))
    mount()

    await waitFor(() => expect(text()).toBeInTheDocument())
    const label = screen.getByText('Summarizes the first 4 of 7 messages not merged yet')
    expect(label.tagName).toBe('LABEL')
    expect(label).toHaveAttribute('for', text().id)
    const remaining = screen.getByTestId('merge-back-remaining')
    expect(label.compareDocumentPosition(remaining) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('labels a draft that covers everything with the covered count alone', async () => {
    mocks.mergeBackDraft.mockResolvedValue(draft({ messages: 4, remaining: 0 }))
    mount()

    await waitFor(() => expect(text()).toBeInTheDocument())
    const label = screen.getByText('Summarizes 4 messages not merged yet')
    expect(label.tagName).toBe('LABEL')
    expect(screen.queryByText(/of 4 messages/)).not.toBeInTheDocument()
  })

  it('does not show a remaining line when this draft covers everything', async () => {
    mount()

    await waitFor(() => expect(text()).toBeInTheDocument())
    expect(screen.queryByTestId('merge-back-remaining')).not.toBeInTheDocument()
  })

  it('drafts once and shows the draft under StrictMode, which mounts every effect twice', async () => {
    mount([PARENT, FORK], { strict: true })
    await waitFor(() => expect(text().value).toBe(OPENING))
    expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
  })

  it('carries the gap sentence into the text the parent receives', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(text().value).toBe(OPENING))

    await user.click(mergeButton())

    await waitFor(() => expect(mocks.mergeBack).toHaveBeenCalled())
    expect(mocks.mergeBack.mock.calls[0][1]).toBe(OPENING)
  })

  it('stays on the fork and says when a held merge will show', async () => {
    const user = userEvent.setup()
    mocks.mergeBack.mockResolvedValue({ ok: true, parent: 'parent', messages: 2, deferred: true })
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())

    await user.click(mergeButton())

    expect(await screen.findByTestId('merge-back-held')).toBeInTheDocument()
    expect(mocks.switched).not.toHaveBeenCalled()
    await user.click(screen.getByTestId('merge-back-open-parent'))
    expect(mocks.switched).toHaveBeenCalledWith({ key: 'parent', announceOnMissing: true })
  })

  it('offers to open a closed parent, then drafts again', async () => {
    const user = userEvent.setup()
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'parent_not_open'))
    mount([FORK])

    expect(await screen.findByTestId('merge-back-refused')).toHaveTextContent('The parent chat is closed')
    await user.click(screen.getByTestId('merge-back-reopen-parent'))

    await waitFor(() => expect(mocks.resume).toHaveBeenCalledWith({ key: 'dashboard:parent', title: '' }))
    await waitFor(() => expect(text().value).toBe(OPENING))
    expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(2)
  })

  // A closed parent rejected the draft request, so the shared error surface
  // carries the structured hand-off while the reopen action stays in the footer.
  it('shows a closed parent through ErrorNotice with Open the parent chat and Ask the agent', async () => {
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'parent_not_open'))
    mount([FORK])

    const notice = await screen.findByTestId('merge-back-refused')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveClass('text-danger')
    expect(notice.querySelector('.lucide-triangle-alert')).not.toBeNull()
    expect(notice.querySelector('.lucide-info')).toBeNull()
    expect(screen.getByTestId('merge-back-reopen-parent')).toHaveTextContent('Open the parent chat')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('keeps the danger notice and Ask the agent for a draft that failed', async () => {
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(502, 'merge_summary_failed'))
    mount()

    const notice = await screen.findByTestId('merge-back-refused')
    expect(notice.tagName).toBe('DIV')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('shows nothing_to_merge through ErrorNotice while keeping Close as its only recovery action', async () => {
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'nothing_to_merge'))
    mount()

    const notice = await screen.findByTestId('merge-back-refused')
    expect(notice).toHaveTextContent('already has everything')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveClass('text-danger')
    expect(notice.querySelector('.lucide-triangle-alert')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.getByTestId('merge-back-cancel')).toHaveTextContent('Close')
    expect(screen.queryByTestId('merge-back-reopen-parent')).not.toBeInTheDocument()
    expect(screen.queryByTestId('merge-back-merge')).not.toBeInTheDocument()
    expect(screen.queryByTestId('merge-back-retry')).not.toBeInTheDocument()
  })

  // The dialog knows the fork's parent key, so a refusal of a parent outside the
  // dashboard names what the parent is, in the person's words, not the mechanism.
  it.each([
    ['slack:1700000000.000001', "This chat's parent is a Slack chat."],
    ['cron:daily', "This chat's parent is a scheduled job."],
    ['hook:review-42', "This chat's parent runs outside it."],
  ])('names the parent %s when the gateway refuses a parent outside the dashboard', async (parentSession, named) => {
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'parent_not_dashboard'))
    mount([{ ...FORK, forked_from: parentSession }])

    const notice = await screen.findByTestId('merge-back-refused')
    expect(notice).toHaveTextContent(`A merge can only go into a chat that runs in the dashboard. ${named}`)
    expect(screen.queryByTestId('merge-back-merge')).not.toBeInTheDocument()
  })

  it('offers Draft again when the summary could not be written, and drafts again on it', async () => {
    const user = userEvent.setup()
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(502, 'merge_summary_failed'))
    mount()

    expect(await screen.findByTestId('merge-back-refused')).toHaveTextContent('The summary could not be written')
    expect(screen.queryByTestId('merge-back-reopen-parent')).not.toBeInTheDocument()
    expect(screen.queryByTestId('merge-back-merge')).not.toBeInTheDocument()
    const retry = screen.getByTestId('merge-back-retry')
    expect(retry).toHaveTextContent('Draft again')
    await user.click(retry)

    await waitFor(() => expect(text().value).toBe(OPENING))
    expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('merge-back-refused')).not.toBeInTheDocument()
  })

  it('counts characters as the gateway does, so an emoji is one', async () => {
    mocks.mergeBackDraft.mockResolvedValue(draft({ summary: '🚀 Redis works.' }))
    mount()

    const opening = `🚀 Redis works.\n\n${GAP}`
    await waitFor(() => expect(text().value).toBe(opening))
    expect(screen.getByTestId('merge-back-count')).toHaveTextContent(`${charCount(opening)} / ${MAX_MERGE_SUMMARY_CHARS}`)
    expect(charCount(opening)).toBe(opening.length - 1)
  })

  // The gap sentence looks like part of the draft, so the hint is what tells
  // the person the tool appended it and that it can go. It points at it by
  // where they see it, the last sentence, the same name the cut line uses, so
  // the two point at the same thing; nothing on screen is called a note.
  it('says under the text that the last sentence is part of it and all of it may be edited', async () => {
    mount()
    await waitFor(() => expect(text().value).toBe(OPENING))

    const hintId = text().getAttribute('aria-describedby')!.split(' ')[0]
    const hint = document.getElementById(hintId)!
    expect(hint).toHaveTextContent('The parent gets exactly this text, last sentence included. Edit or delete any of it before merging.')
    expect(hint).not.toHaveTextContent('note')
    // The hint sits under the text box, beside Draft again.
    expect(text().compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('opens a summary at the limit with the gap sentence still mergeable', async () => {
    mocks.mergeBackDraft.mockResolvedValue(draft({ summary: 'word '.repeat(800).trim() }))
    mount()

    await waitFor(() => expect(text().value).toContain('…'))
    expect(text().value.endsWith(GAP)).toBe(true)
    expect(mergeButton()).toBeEnabled()
  })

  it('will not merge text over the limit', async () => {
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())

    // The opening text always fits; only the person's own edits can take it over.
    fireEvent.change(text(), { target: { value: 'x'.repeat(MAX_MERGE_SUMMARY_CHARS + 1) } })

    expect(mergeButton()).toBeDisabled()
    expect(screen.getByTestId('merge-back-count')).toHaveClass('text-danger')
  })

  it('says why Merge is off while the text is over the limit, and only then', async () => {
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    expect(screen.queryByTestId('merge-back-too-long')).not.toBeInTheDocument()

    fireEvent.change(text(), { target: { value: 'x'.repeat(MAX_MERGE_SUMMARY_CHARS + 1) } })

    const reason = screen.getByTestId('merge-back-too-long')
    expect(reason).toHaveTextContent('The summary is over the length limit. Shorten it.')
    expect(reason).toHaveClass('text-danger')
    // The field error sits at the field: under the text box, where the cut line also goes.
    expect(text().compareDocumentPosition(reason) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The reason reaches a screen reader with the text box, as the counter does.
    expect(text().getAttribute('aria-describedby')!.split(' ')).toContain(reason.id)
    expect(text()).toHaveAttribute('aria-invalid', 'true')

    fireEvent.change(text(), { target: { value: 'x'.repeat(MAX_MERGE_SUMMARY_CHARS) } })

    expect(screen.queryByTestId('merge-back-too-long')).not.toBeInTheDocument()
    expect(text().getAttribute('aria-describedby')).not.toContain(reason.id)
    expect(mergeButton()).toBeEnabled()
  })

  it('asks before Draft again replaces edited text, and keeping sends no request', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(text().value).toBe(OPENING))
    await user.type(text(), ' Edited.')

    await user.click(screen.getByTestId('merge-back-redraft'))

    const question = screen.getByTestId('merge-back-redraft-confirm')
    expect(question).toHaveAttribute('role', 'alertdialog')
    expect(question).toHaveTextContent('Replace your edited text with a new draft?')
    expect(screen.getByTestId('merge-back-redraft-keep')).toHaveFocus()
    expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
    await user.click(screen.getByTestId('merge-back-redraft-keep'))

    expect(screen.queryByTestId('merge-back-redraft-confirm')).not.toBeInTheDocument()
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
    expect(mergeButton()).toBeEnabled()
  })

  it('replaces the edited text with a new draft once the person says so', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(text().value).toBe(OPENING))
    await user.type(text(), ' Edited.')
    mocks.mergeBackDraft.mockResolvedValueOnce(draft({ summary: 'Memcached works too.' }))

    await user.click(screen.getByTestId('merge-back-redraft'))
    await user.click(screen.getByTestId('merge-back-redraft-replace'))

    await waitFor(() => expect(text().value).toBe(`Memcached works too.\n\n${GAP}`))
    expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('merge-back-redraft-confirm')).not.toBeInTheDocument()
  })

  it('drafts again at once when the text is as the draft left it', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(text().value).toBe(OPENING))

    await user.click(screen.getByTestId('merge-back-redraft'))

    await waitFor(() => expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('merge-back-redraft-confirm')).not.toBeInTheDocument()
  })

  it('keeps the edited text and turns Merge off once another merge got there first', async () => {
    const user = userEvent.setup()
    mocks.mergeBack.mockRejectedValueOnce(refusal(409, 'already_merged'))
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    await user.type(text(), ' Edited.')

    await user.click(mergeButton())

    expect(await screen.findByTestId('merge-back-error')).toHaveTextContent('already has these messages')
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(mergeButton()).toBeDisabled()
  })

  it('says the parent was deleted and offers neither a reopen nor a retry', async () => {
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'parent_deleted'))
    mount()

    expect(await screen.findByTestId('merge-back-refused')).toHaveTextContent('The parent chat was deleted')
    expect(screen.queryByTestId('merge-back-reopen-parent')).not.toBeInTheDocument()
    expect(screen.queryByTestId('merge-back-retry')).not.toBeInTheDocument()
  })

  it('keeps the edited text and turns Merge off when the parent was deleted after the draft', async () => {
    const user = userEvent.setup()
    mocks.mergeBack.mockRejectedValueOnce(refusal(409, 'parent_deleted'))
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    await user.type(text(), ' Edited.')

    await user.click(mergeButton())

    expect(await screen.findByTestId('merge-back-error')).toHaveTextContent('The parent chat was deleted')
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(mergeButton()).toBeDisabled()
  })

  it('says the parent cannot be confirmed and offers neither a reopen nor a retry', async () => {
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'parent_unconfirmed'))
    mount()

    expect(await screen.findByTestId('merge-back-refused')).toHaveTextContent("The parent chat can't be confirmed")
    expect(screen.queryByTestId('merge-back-reopen-parent')).not.toBeInTheDocument()
    expect(screen.queryByTestId('merge-back-retry')).not.toBeInTheDocument()
  })

  it('keeps the edited text and turns Merge off when the parent cannot be confirmed after the draft', async () => {
    const user = userEvent.setup()
    mocks.mergeBack.mockRejectedValueOnce(refusal(409, 'parent_unconfirmed'))
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    await user.type(text(), ' Edited.')

    await user.click(mergeButton())

    expect(await screen.findByTestId('merge-back-error')).toHaveTextContent("The parent chat can't be confirmed")
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(mergeButton()).toBeDisabled()
  })

  it('keeps the edited text when a retryable merge failure comes back', async () => {
    const user = userEvent.setup()
    mocks.mergeBack.mockRejectedValueOnce(refusal(429, 'merge_queue_full'))
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    await user.type(text(), ' Edited.')

    await user.click(mergeButton())

    expect(await screen.findByTestId('merge-back-error')).toHaveTextContent('merges its agent has not read')
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(mergeButton()).toBeEnabled()
    // Handing this to the agent would unmount the edited text with the dialog.
    expect(within(screen.getByTestId('merge-back-error')).queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
  })

  it('keeps the footer to Cancel and Merge, with Draft again beside the text it replaces', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())

    const footer = mergeButton().parentElement as HTMLElement
    expect(within(footer).getAllByRole('button').map(b => b.dataset.testid)).toEqual(['merge-back-cancel', 'merge-back-merge'])
    await user.click(screen.getByTestId('merge-back-redraft'))
    await waitFor(() => expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(2))
  })

  it('keeps the edited text when drafting again fails', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    await user.type(text(), ' Edited.')
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(502, 'merge_summary_failed'))

    await user.click(screen.getByTestId('merge-back-redraft'))
    await user.click(screen.getByTestId('merge-back-redraft-replace'))

    const error = await screen.findByTestId('merge-back-error')
    expect(error).toHaveTextContent('The summary could not be written')
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(screen.queryByTestId('merge-back-refused')).not.toBeInTheDocument()
    // Handing this to the agent would unmount the edited text with the dialog.
    expect(within(error).queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
    expect(mergeButton()).toBeEnabled()
  })

  it('keeps the edited text when the fork moved past the draft, until it is drafted again', async () => {
    const user = userEvent.setup()
    mocks.mergeBack.mockRejectedValueOnce(refusal(409, 'merge_draft_stale'))
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    await user.type(text(), ' Edited.')

    await user.click(mergeButton())

    expect(await screen.findByTestId('merge-back-error')).toHaveTextContent('This fork changed since the summary was written')
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(mergeButton()).toBeDisabled()
    await user.click(screen.getByTestId('merge-back-redraft'))
    await user.click(screen.getByTestId('merge-back-redraft-replace'))
    await waitFor(() => expect(text().value).toBe(OPENING))
    expect(mergeButton()).toBeEnabled()
  })

  it('leaves Merge off when drafting again fails after the draft went out of date', async () => {
    const user = userEvent.setup()
    mocks.mergeBack.mockRejectedValueOnce(refusal(409, 'merge_point_missing'))
    mount()
    await waitFor(() => expect(text()).toBeInTheDocument())
    await user.type(text(), ' Edited.')
    await user.click(mergeButton())
    await screen.findByTestId('merge-back-error')
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(502, 'merge_summary_failed'))

    await user.click(screen.getByTestId('merge-back-redraft'))
    await user.click(screen.getByTestId('merge-back-redraft-replace'))

    await waitFor(() => expect(screen.getByTestId('merge-back-error')).toHaveTextContent('The summary could not be written'))
    expect(text().value).toBe(`${OPENING} Edited.`)
    expect(mergeButton()).toBeDisabled()
  })

  it('offers the agent a refusal when no text is on screen, closing the dialog to hand it off', async () => {
    const user = userEvent.setup()
    __resetErrorJournalForTests()
    sessionStorage.clear()
    // What the transport records for the failed request. The notice shows a
    // translated sentence instead, so the record has to travel with it.
    recordError({ source: 'api', message: 'merge_summary_failed', status: 502, endpoint: '/api/chat/slots/fork/merge-back/draft' })
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(502, 'merge_summary_failed'))
    mount()

    const notice = await screen.findByTestId('merge-back-refused')
    expect(notice).toHaveTextContent('The summary could not be written')
    await user.click(within(notice).getByRole('button', { name: /ask the agent/i }))

    await waitFor(() => expect(screen.queryByTestId('merge-back-refused')).not.toBeInTheDocument())
    const staged = sessionStorage.getItem(ERROR_HANDOFF_KEY) || ''
    expect(staged).toMatch(/\/api\/chat\/slots\/fork\/merge-back\/draft/)
    expect(staged).toMatch(/HTTP 502/)
  })

  it('says so in the refusal when the parent cannot be opened, and lets the person try again', async () => {
    const user = userEvent.setup()
    mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'parent_not_open'))
    mocks.resume.mockResolvedValueOnce({ ok: false })
    mount([FORK])
    await user.click(await screen.findByTestId('merge-back-reopen-parent'))

    await waitFor(() => expect(screen.getByTestId('merge-back-refused')).toHaveTextContent('could not be opened'))
    // Something went wrong this time, so the line is the danger notice with its hand-off.
    expect(screen.getByTestId('merge-back-refused')).not.toHaveAttribute('role', 'status')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.getByTestId('merge-back-reopen-parent')).toBeEnabled()
    expect(screen.queryByTestId('merge-back-error')).not.toBeInTheDocument()
    expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
  })

  describe('closing over edited text', () => {
    const closeQuestion = () => screen.getByTestId('merge-back-close-confirm')
    const dialog = () => screen.getByRole('dialog')

    const closePaths: [string, (user: ReturnType<typeof userEvent.setup>) => Promise<void>][] = [
      ['Escape', async user => { await user.keyboard('{Escape}') }],
      ['the X', async user => { await user.click(screen.getByRole('button', { name: 'Close' })) }],
      // Radix dismisses a modal dialog on the click that completes an outside
      // press, so a drag that starts outside and ends inside does not close it.
      ['a click outside', async () => {
        fireEvent.pointerDown(document.body, { button: 0 })
        fireEvent.pointerUp(document.body, { button: 0 })
        fireEvent.click(document.body, { button: 0 })
      }],
      ['Cancel', async user => { await user.click(screen.getByTestId('merge-back-cancel')) }],
    ]

    it.each(closePaths)('asks first on %s, and keeping leaves the text and sends nothing', async (_name, close) => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')

      await close(user)

      expect(dialog()).toBeInTheDocument()
      expect(await screen.findByTestId('merge-back-close-confirm')).toHaveAttribute('role', 'alertdialog')
      expect(closeQuestion()).toHaveTextContent('Close and lose your edited text?')
      expect(screen.getByTestId('merge-back-close-keep')).toHaveFocus()
      expect(screen.getByTestId('merge-back-close-keep')).toHaveTextContent('Keep my text')
      await user.click(screen.getByTestId('merge-back-close-keep'))

      expect(screen.queryByTestId('merge-back-close-confirm')).not.toBeInTheDocument()
      expect(dialog()).toBeInTheDocument()
      expect(text().value).toBe(`${OPENING} Edited.`)
      expect(mocks.mergeBack).not.toHaveBeenCalled()
      expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
      expect(mergeButton()).toBeEnabled()
    })

    it('closes once the person discards the edited text', async () => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')
      await user.keyboard('{Escape}')

      await user.click(screen.getByTestId('merge-back-close-discard'))

      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(mocks.mergeBack).not.toHaveBeenCalled()
    })

    it.each(closePaths)('closes at once on %s when the text is as the draft left it', async (_name, close) => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))

      await close(user)

      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(screen.queryByTestId('merge-back-close-confirm')).not.toBeInTheDocument()
    })

    it('asks nothing when a merge of the edited text lands', async () => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')

      await user.click(mergeButton())

      await waitFor(() => expect(mocks.switched).toHaveBeenCalledWith({ key: 'parent', announceOnMissing: true }))
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(screen.queryByTestId('merge-back-close-confirm')).not.toBeInTheDocument()
    })

    it('asks the one question the later request raised', async () => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')

      await user.click(screen.getByTestId('merge-back-redraft'))
      expect(screen.getByTestId('merge-back-redraft-confirm')).toBeInTheDocument()
      await user.keyboard('{Escape}')

      expect(screen.queryByTestId('merge-back-redraft-confirm')).not.toBeInTheDocument()
      expect(closeQuestion()).toBeInTheDocument()
      expect(text().value).toBe(`${OPENING} Edited.`)
    })

    it('still asks after a refused merge left the edited text on screen', async () => {
      const user = userEvent.setup()
      mocks.mergeBack.mockRejectedValueOnce(refusal(409, 'merge_draft_stale'))
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')
      await user.click(mergeButton())
      await screen.findByTestId('merge-back-error')

      await user.click(screen.getByTestId('merge-back-cancel'))

      expect(closeQuestion()).toBeInTheDocument()
      expect(text().value).toBe(`${OPENING} Edited.`)
    })
  })

  // The question is about the text Merge would send, so Merge waits for the
  // answer: a merge landing under an open question would send text the person
  // was about to replace or discard.
  describe('Merge while the edits question is open', () => {
    const questions: [string, string, string][] = [
      ['Draft again', 'merge-back-redraft', 'merge-back-redraft-keep'],
      ['a close', 'merge-back-cancel', 'merge-back-close-keep'],
    ]

    it.each(questions)('is off while the question %s asks is open, and on again once it is answered', async (_name, ask, keep) => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')
      expect(mergeButton()).toBeEnabled()

      await user.click(screen.getByTestId(ask))

      expect(mergeButton()).toBeDisabled()
      await user.click(screen.getByTestId(keep))

      expect(mergeButton()).toBeEnabled()
      expect(mocks.mergeBack).not.toHaveBeenCalled()
    })

    it('sends nothing on a submit while the question is open', async () => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')
      await user.click(screen.getByTestId('merge-back-redraft'))

      fireEvent.submit(mergeButton().closest('form')!)

      expect(mocks.mergeBack).not.toHaveBeenCalled()
      expect(screen.getByTestId('merge-back-redraft-confirm')).toBeInTheDocument()
    })
  })

  describe('Draft again after an outdated refusal', () => {
    const EDITED = `${OPENING} Edited.`
    const NEW_DIGEST = 'cd'.repeat(32)
    const newDraft = () => draft({ summary: 'Memcached works too.', through: 'mid-12', digest: NEW_DIGEST, messages: 3 })

    /** The edited text on screen after a stale merge refusal, Merge off. */
    async function refusedOverEdits(user: ReturnType<typeof userEvent.setup>) {
      mocks.mergeBack.mockRejectedValueOnce(refusal(409, 'merge_draft_stale'))
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')
      await user.click(mergeButton())
      await screen.findByTestId('merge-back-error')
      expect(mergeButton()).toBeDisabled()
    }

    function holdNextDraft() {
      let resolveDraft!: (value: ReturnType<typeof draft>) => void
      mocks.mergeBackDraft.mockReturnValueOnce(new Promise<ReturnType<typeof draft>>(resolve => {
        resolveDraft = resolve
      }))
      return resolveDraft
    }

    const closeWhileKeepRedrafting: [string, (user: ReturnType<typeof userEvent.setup>) => Promise<void>][] = [
      ['Cancel', async user => { await user.click(screen.getByTestId('merge-back-cancel')) }],
      ['Escape', async user => { await user.keyboard('{Escape}') }],
      ['the X', async user => { await user.click(screen.getByRole('button', { name: 'Close' })) }],
    ]

    it('asks a question that names the new draft Merge needs, with keeping as the focused answer', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)

      await user.click(screen.getByTestId('merge-back-redraft'))

      const question = screen.getByTestId('merge-back-redraft-outdated-confirm')
      expect(question).toHaveAttribute('role', 'alertdialog')
      expect(question).toHaveTextContent('The chats changed since this draft was written, so Merge needs a new draft. Keep your text as it is, or replace it with the new draft?')
      expect(screen.queryByTestId('merge-back-redraft-confirm')).not.toBeInTheDocument()
      const keep = screen.getByTestId('merge-back-redraft-outdated-keep')
      expect(keep).toHaveFocus()
      expect(keep).toHaveTextContent('Draft again, keep my text')
      expect(screen.getByTestId('merge-back-redraft-replace')).toHaveTextContent('Replace with new draft')
      // Asking replaced nothing and requested nothing.
      expect(text().value).toBe(EDITED)
      expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
    })

    it('shows the refusal once: the notice until the question is asked, then the question alone', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      expect(screen.getByTestId('merge-back-error')).toHaveTextContent('This fork changed since the summary was written')

      await user.click(screen.getByTestId('merge-back-redraft'))

      expect(screen.getByTestId('merge-back-redraft-outdated-confirm')).toBeInTheDocument()
      expect(screen.queryByTestId('merge-back-error')).not.toBeInTheDocument()
      expect(mergeButton()).toBeDisabled()
    })

    it('brings the notice back when the question closes without a new draft', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      await user.click(screen.getByTestId('merge-back-redraft'))
      expect(screen.queryByTestId('merge-back-error')).not.toBeInTheDocument()

      // A close request replaces the outdated question with the close question.
      await user.keyboard('{Escape}')

      expect(screen.queryByTestId('merge-back-redraft-outdated-confirm')).not.toBeInTheDocument()
      expect(screen.getByTestId('merge-back-close-confirm')).toBeInTheDocument()
      expect(screen.getByTestId('merge-back-error')).toHaveTextContent('This fork changed since the summary was written')
      await user.click(screen.getByTestId('merge-back-close-keep'))

      expect(screen.queryByTestId('merge-back-close-confirm')).not.toBeInTheDocument()
      expect(screen.getByTestId('merge-back-error')).toHaveTextContent('This fork changed since the summary was written')
      expect(text().value).toBe(EDITED)
      expect(mergeButton()).toBeDisabled()
      expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
    })

    it.each(closeWhileKeepRedrafting)('asks before %s while a keep-text redraft is pending, and keeping preserves the edits', async (_name, close) => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      const finishRedraft = holdNextDraft()

      await user.click(screen.getByTestId('merge-back-redraft'))
      await user.click(screen.getByTestId('merge-back-redraft-outdated-keep'))
      expect(screen.getByTestId('merge-back-drafting')).toBeInTheDocument()

      await close(user)

      const question = await screen.findByTestId('merge-back-close-confirm')
      expect(question).toHaveAttribute('role', 'alertdialog')
      expect(question).toHaveTextContent('Close and lose your edited text?')
      expect(screen.getByTestId('merge-back-close-keep')).toHaveFocus()
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      await user.click(screen.getByTestId('merge-back-close-keep'))

      expect(screen.queryByTestId('merge-back-close-confirm')).not.toBeInTheDocument()
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      finishRedraft(newDraft())
      await waitFor(() => expect(text().value).toBe(EDITED))
      expect(screen.getByText('Summarizes 3 messages not merged yet')).toBeInTheDocument()
      expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(2)
    })

    it('closes when the edited text is discarded during a pending keep-text redraft', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      holdNextDraft()

      await user.click(screen.getByTestId('merge-back-redraft'))
      await user.click(screen.getByTestId('merge-back-redraft-outdated-keep'))
      await user.click(screen.getByTestId('merge-back-cancel'))
      await screen.findByTestId('merge-back-close-confirm')
      await user.click(screen.getByTestId('merge-back-close-discard'))

      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    })

    it('keeps the edited text under the new draft, and Merge sends that text with the new fingerprint', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      mocks.mergeBackDraft.mockResolvedValueOnce(newDraft())

      await user.click(screen.getByTestId('merge-back-redraft'))
      await user.click(screen.getByTestId('merge-back-redraft-outdated-keep'))

      await waitFor(() => expect(mergeButton()).toBeEnabled())
      expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(2)
      expect(text().value).toBe(EDITED)
      // The covers line follows the new draft, the words stay the person's.
      expect(screen.getByText('Summarizes 3 messages not merged yet').tagName).toBe('LABEL')
      expect(screen.queryByTestId('merge-back-redraft-outdated-confirm')).not.toBeInTheDocument()
      expect(screen.queryByTestId('merge-back-error')).not.toBeInTheDocument()
      await user.click(mergeButton())

      await waitFor(() => expect(mocks.mergeBack).toHaveBeenLastCalledWith('fork', EDITED, 'mid-12', NEW_DIGEST))
    })

    it('replaces the edited text with the new draft on the other answer', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      mocks.mergeBackDraft.mockResolvedValueOnce(newDraft())

      await user.click(screen.getByTestId('merge-back-redraft'))
      await user.click(screen.getByTestId('merge-back-redraft-replace'))

      await waitFor(() => expect(text().value).toBe(`Memcached works too.\n\n${GAP}`))
      expect(mergeButton()).toBeEnabled()
      expect(screen.queryByTestId('merge-back-redraft-outdated-confirm')).not.toBeInTheDocument()
    })

    it('goes back to the edited text with Merge still off when the kept redraft fails', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      mocks.mergeBackDraft.mockRejectedValueOnce(refusal(502, 'merge_summary_failed'))

      await user.click(screen.getByTestId('merge-back-redraft'))
      await user.click(screen.getByTestId('merge-back-redraft-outdated-keep'))

      await waitFor(() => expect(screen.getByTestId('merge-back-error')).toHaveTextContent('The summary could not be written'))
      expect(text().value).toBe(EDITED)
      expect(screen.getByText('Summarizes 2 messages not merged yet')).toBeInTheDocument()
      expect(mergeButton()).toBeDisabled()
    })

    it('leaves Merge off with the over-limit reason when the kept text is over the limit', async () => {
      const user = userEvent.setup()
      await refusedOverEdits(user)
      fireEvent.change(text(), { target: { value: 'x'.repeat(MAX_MERGE_SUMMARY_CHARS + 1) } })
      mocks.mergeBackDraft.mockResolvedValueOnce(newDraft())

      await user.click(screen.getByTestId('merge-back-redraft'))
      await user.click(screen.getByTestId('merge-back-redraft-outdated-keep'))

      await waitFor(() => expect(screen.getByText('Summarizes 3 messages not merged yet')).toBeInTheDocument())
      expect(text().value).toBe('x'.repeat(MAX_MERGE_SUMMARY_CHARS + 1))
      expect(screen.getByTestId('merge-back-too-long')).toBeInTheDocument()
      expect(mergeButton()).toBeDisabled()
    })

    it('asks the usual question, with Keep my text as a plain cancel, while the draft is not outdated', async () => {
      const user = userEvent.setup()
      mount()
      await waitFor(() => expect(text().value).toBe(OPENING))
      await user.type(text(), ' Edited.')

      await user.click(screen.getByTestId('merge-back-redraft'))

      expect(screen.queryByTestId('merge-back-redraft-outdated-confirm')).not.toBeInTheDocument()
      expect(screen.getByTestId('merge-back-redraft-confirm')).toHaveTextContent('Replace your edited text with a new draft?')
      expect(screen.getByTestId('merge-back-redraft-keep')).toHaveTextContent('Keep my text')
      await user.click(screen.getByTestId('merge-back-redraft-keep'))

      expect(mocks.mergeBackDraft).toHaveBeenCalledTimes(1)
      expect(text().value).toBe(EDITED)
    })
  })

  describe('the description', () => {
    const DESCRIPTION = "Sends this summary to the parent chat's agent. This fork stays open."

    it('is read with the dialog while a draft is being written or is on screen', async () => {
      mocks.mergeBackDraft.mockReturnValueOnce(new Promise(() => {}))
      mount()

      expect(screen.getByTestId('merge-back-drafting')).toBeInTheDocument()
      const description = screen.getByText(DESCRIPTION)
      expect(screen.getByRole('dialog')).toHaveAttribute('aria-describedby', description.id)
      cleanup()
      mount()
      await waitFor(() => expect(text()).toBeInTheDocument())

      expect(screen.getByRole('dialog')).toHaveAttribute('aria-describedby', screen.getByText(DESCRIPTION).id)
    })

    it.each([
      ['the parent already has everything', () => mocks.mergeBackDraft.mockRejectedValueOnce(refusal(409, 'nothing_to_merge'))],
      ['the summary could not be written', () => mocks.mergeBackDraft.mockRejectedValueOnce(refusal(502, 'merge_summary_failed'))],
    ])('is not repeated over the status line when %s', async (_name, arrange) => {
      arrange()
      mount()

      await screen.findByTestId('merge-back-refused')
      expect(screen.queryByText(DESCRIPTION)).not.toBeInTheDocument()
      expect(screen.getByRole('dialog')).not.toHaveAttribute('aria-describedby')
    })

    it('is not repeated over the held notice', async () => {
      const user = userEvent.setup()
      mocks.mergeBack.mockResolvedValue({ ok: true, parent: 'parent', messages: 2, deferred: true })
      mount()
      await waitFor(() => expect(text()).toBeInTheDocument())

      await user.click(mergeButton())

      await screen.findByTestId('merge-back-held')
      expect(screen.queryByText(DESCRIPTION)).not.toBeInTheDocument()
      expect(screen.getByRole('dialog')).not.toHaveAttribute('aria-describedby')
    })
  })

  describe('a cut draft', () => {
    it('says so when the gateway cut the summary', async () => {
      mocks.mergeBackDraft.mockResolvedValue(draft({ summary: 'Redis works…', trimmed: true }))
      mount()

      const line = await screen.findByTestId('merge-back-cut')
      expect(line).toHaveTextContent('The summary was cut to fit the limit. It ends with “…” just above the last sentence. Check that line before you merge.')
      expect(text().getAttribute('aria-describedby')!.split(' ')).toContain(line.id)
    })

    it('reads as a warning, with its icon, so it does not pass for the edit hint under it', async () => {
      mocks.mergeBackDraft.mockResolvedValue(draft({ summary: 'Redis works…', trimmed: true }))
      mount()

      const line = await screen.findByTestId('merge-back-cut')
      expect(line).toHaveClass('text-warn')
      expect(line.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
    })

    it('says so when the opening text had to cut the summary for the gap sentence', async () => {
      mocks.mergeBackDraft.mockResolvedValue(draft({ summary: 'word '.repeat(800).trim(), trimmed: false }))
      mount()

      expect(await screen.findByTestId('merge-back-cut')).toBeInTheDocument()
      expect(text().value).toContain('…')
    })

    it('says nothing for a draft that fits', async () => {
      mount()

      await waitFor(() => expect(text().value).toBe(OPENING))
      expect(screen.queryByTestId('merge-back-cut')).not.toBeInTheDocument()
      expect(text().getAttribute('aria-describedby')).not.toContain('undefined')
    })

    it('goes away with a new draft that was not cut', async () => {
      const user = userEvent.setup()
      mocks.mergeBackDraft.mockResolvedValueOnce(draft({ summary: 'Redis works…', trimmed: true }))
      mount()
      await screen.findByTestId('merge-back-cut')

      await user.click(screen.getByTestId('merge-back-redraft'))

      await waitFor(() => expect(text().value).toBe(OPENING))
      expect(screen.queryByTestId('merge-back-cut')).not.toBeInTheDocument()
    })
  })
})
