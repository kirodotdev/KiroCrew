/**
 * TimelineComposer + lib/commentBatch (#19505).
 *
 * Covers: posting one comment now; a threaded reply written INLINE under its
 * comment and carrying its `thread_id`; a quote-reply pre-fill when the comment has
 * none; inline replies and the head box feeding one pending batch that survives a
 * remount; editing a pending comment in its own row; submitting the batch; a
 * partial failure keeping only the unposted items; and the read-only gate.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { CommentsRefusedError, type CommentBatchResponse, type TimelineEvent } from '../api'
import {
  anchorKeyOf, loadPending, newPendingId, pendingStorageKey, quoteReply, remainingAfter, resetBatchState,
  type PendingComment,
} from '../lib/commentBatch'

const postComments = vi.hoisted(() => vi.fn())
const ctx = vi.hoisted(() => ({
  active: { owner: 'zzq', repo: 'fabric', provider: 'github', host: 'github.com' },
  canWrite: true,
}))

vi.mock('../context', () => ({ useIssueRadar: () => ctx }))
vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return { ...mod, issueRadarApi: { ...mod.issueRadarApi, postComments } }
})

import TimelineComposer from './TimelineComposer'
import CommentBatchProvider from './CommentBatchProvider'
import InlineReply from './InlineReply'
import ReplyButton from './ReplyButton'
import { repoScopeKey } from '../lib/links'

const KEY = () => pendingStorageKey(repoScopeKey(ctx.active as never), 'pull', 7)

function ok(n: number): CommentBatchResponse {
  return {
    owner: 'zzq', repo: 'fabric', number: 7, target: 'pull',
    posted: Array.from({ length: n }, (_, index) => ({ index, id: index + 1, url: null })),
    failed: null,
  }
}

const THREADED: TimelineEvent = { kind: 'review_comment', actor: 'kim', created_at: '2026-10-01T00:00:00Z', body: 'why?', thread_id: '44' }
const FLAT: TimelineEvent = { kind: 'comment', actor: 'lee', created_at: '2026-10-02T00:00:00Z', body: 'how?', thread_id: null }

/** The head box plus two timeline comments, each with its Reply button and inline slot. */
function renderComposer(props: Partial<{ onPosted: () => void }> = {}) {
  const onPosted = props.onPosted ?? vi.fn()
  const events = [THREADED, FLAT]
  const view = render(
    <CommentBatchProvider target="pull" number={7} onPosted={onPosted}>
      <TimelineComposer />
      {events.map((ev) => (
        <div key={anchorKeyOf(ev)} data-testid={`card-${ev.actor}`}>
          <ReplyButton ev={ev} />
          <InlineReply ev={ev} />
        </div>
      ))}
    </CommentBatchProvider>,
  )
  return { ...view, onPosted }
}

const replyTo = (actor: string) =>
  fireEvent.click(within(screen.getByTestId(`card-${actor}`)).getByRole('button', { name: /reply|quote/i }))
const inlineBox = (actor: string) =>
  within(screen.getByTestId(`card-${actor}`)).getByRole('textbox') as HTMLTextAreaElement

const box = () => within(screen.getByTestId('timeline-composer-box')).getByRole('textbox') as HTMLTextAreaElement
const type = (text: string) => fireEvent.change(box(), { target: { value: text } })
const button = (name: RegExp) => screen.getByRole('button', { name })

beforeEach(() => {
  localStorage.clear()
  resetBatchState()
  postComments.mockReset()
  ctx.canWrite = true
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('commentBatch helpers', () => {
  it('quotes the original under a mention and cuts long bodies', () => {
    const body = ['a', '', 'b', 'c', 'd', 'e', 'f', 'g'].join('\n')
    expect(quoteReply({ author: 'kim', body, thread_id: null }))
      .toBe('@kim\n\n> a\n>\n> b\n> c\n> d\n> e\n> …\n\n')
  })

  it('keeps only the items the server did not post', () => {
    const items = ['a', 'b', 'c'].map((body, i) => ({ id: String(i), body, thread_id: null, reply_to: null }))
    expect(remainingAfter(items, items, { posted: [{ index: 0, id: 1, url: null }] }).map((p) => p.body))
      .toEqual(['b', 'c'])
  })

  it('keeps items the server reports as unknown, marked so they are not resent', () => {
    const items = ['a', 'b', 'c'].map((body, i) => ({ id: String(i), body, thread_id: null, reply_to: null }))
    const result = { posted: [{ index: 0, id: 1, url: null }], unknown: [1] }
    const left = remainingAfter(items, items, result)
    expect(left.map((p) => [p.body, Boolean(p.unconfirmed)])).toEqual([['b', true], ['c', false]])
  })

  it('mints ids that differ across tabs in the same millisecond', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1)
    expect(newPendingId()).not.toBe(newPendingId())
    expect(newPendingId()).toMatch(/^[0-9a-f-]{36}$/)
    vi.restoreAllMocks()
  })

  it('keeps an item added while the submit was in flight', () => {
    const sent = ['a', 'b'].map((body, i) => ({ id: String(i), body, thread_id: null, reply_to: null }))
    const current = [...sent, { id: 'new', body: 'added meanwhile', thread_id: null, reply_to: null }]
    const result = { posted: [{ index: 0, id: 1, url: null }, { index: 1, id: 2, url: null }] }
    expect(remainingAfter(current, sent, result).map((p) => p.body)).toEqual(['added meanwhile'])
  })

  it('keys two comments by the same author in the same second apart by their id', () => {
    const at = '2026-10-01T00:00:00Z'
    const a: TimelineEvent = { kind: 'comment', actor: 'kim', created_at: at, body: 'one', thread_id: 't', id: 11 }
    const b: TimelineEvent = { ...a, body: 'two', id: 12 }
    expect(anchorKeyOf(a)).not.toBe(anchorKeyOf(b))
    // Without an id the key falls back to author and time, as before.
    const { id: _a, ...noIdA } = a
    const { id: _b, ...noIdB } = b
    expect(anchorKeyOf(noIdA)).toBe(anchorKeyOf(noIdB))
  })

  it('drops corrupt stored entries instead of throwing', () => {
    localStorage.setItem('k', JSON.stringify([{ id: '1', body: 'ok', thread_id: null, reply_to: null }, { nope: 1 }]))
    expect(loadPending('k').map((p) => p.body)).toEqual(['ok'])
    localStorage.setItem('k', '{not json')
    expect(loadPending('k')).toEqual([])
  })
})

describe('TimelineComposer', () => {
  it('posts one comment now and clears the box', async () => {
    postComments.mockResolvedValue(ok(1))
    const { onPosted } = renderComposer()
    type('  looks good  ')
    fireEvent.click(button(/^comment$/i))
    await waitFor(() => expect(onPosted).toHaveBeenCalled())
    expect(postComments).toHaveBeenCalledWith(
      ctx.active, 7, 'pull', [{ body: 'looks good', thread_id: null }],
    )
    expect(box().value).toBe('')
  })

  it('keeps the draft when the post fails', async () => {
    postComments.mockRejectedValue(new CommentsRefusedError('locked'))
    renderComposer()
    type('hello')
    fireEvent.click(button(/^comment$/i))
    expect(await screen.findByText('locked')).toBeTruthy()
    expect(box().value).toBe('hello')
  })

  it('says a direct post may have landed when no answer came back', async () => {
    postComments.mockRejectedValue(new TypeError('Failed to fetch'))
    const { onPosted } = renderComposer()
    type('hello')
    await act(async () => { fireEvent.click(button(/^comment$/i)) })
    expect(box().value).toBe('hello')
    expect(screen.getByText(/could not be confirmed as posted/i)).toBeTruthy()
    expect(onPosted).toHaveBeenCalled()
  })

  it('opens the reply box inline under the comment and sends it into its thread', async () => {
    postComments.mockResolvedValue(ok(1))
    const { onPosted } = renderComposer()
    expect(screen.queryByTestId('inline-reply')).toBeNull()
    replyTo('kim')
    const reply = inlineBox('kim')
    expect(reply.value).toBe('')
    expect(box().value).toBe('')
    fireEvent.change(reply, { target: { value: 'because' } })
    fireEvent.click(within(screen.getByTestId('card-kim')).getByRole('button', { name: /^reply$/i }))
    await waitFor(() => expect(onPosted).toHaveBeenCalled())
    expect(postComments.mock.calls[0][3]).toEqual([{ body: 'because', thread_id: '44' }])
    // The inline box closes once the reply is on the forge.
    expect(screen.queryByTestId('inline-reply')).toBeNull()
  })

  it('pre-fills a quote inline when the comment cannot be threaded', () => {
    renderComposer()
    replyTo('lee')
    expect(inlineBox('lee').value).toBe('@lee\n\n> how?\n\n')
  })

  it('closes an empty inline box on Escape or Cancel', () => {
    renderComposer()
    replyTo('kim')
    fireEvent.keyDown(inlineBox('kim'), { key: 'Escape' })
    expect(screen.queryByTestId('inline-reply')).toBeNull()
    replyTo('lee')
    fireEvent.click(within(screen.getByTestId('card-lee')).getByRole('button', { name: /cancel/i }))
    expect(screen.queryByTestId('inline-reply')).toBeNull()
  })

  it('batches an inline reply under its comment and edits it in that row', () => {
    renderComposer()
    replyTo('kim')
    fireEvent.change(inlineBox('kim'), { target: { value: 'later' } })
    fireEvent.click(within(screen.getByTestId('card-kim')).getByRole('button', { name: /add to batch/i }))
    const card = screen.getByTestId('card-kim')
    expect(within(card).getByTestId('inline-pending').textContent).toContain('later')
    expect(screen.getByTestId('pending-batch').textContent).toContain('later')
    expect(loadPending(KEY())[0]).toMatchObject({ body: 'later', thread_id: '44', anchor: anchorKeyOf(THREADED) })

    fireEvent.click(within(within(card).getByTestId('inline-pending')).getByRole('button', { name: /edit/i }))
    // The row under the comment became the editor; nothing moved to another box.
    const editor = within(card).getByTestId('pending-row-editor')
    expect(within(editor).getByRole<HTMLTextAreaElement>('textbox').value).toBe('later')
    expect(screen.queryByTestId('inline-reply')).toBeNull()
    // The list copy of the same reply says where it is being edited.
    expect(screen.getByTestId('pending-batch').textContent).toMatch(/being edited/i)
  })

  it('edits a pending comment in its row: Cancel keeps it, Save replaces it in place', () => {
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b', 'c'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    renderComposer()
    const list = () => within(screen.getByTestId('pending-batch'))
    const edit = (i: number) => fireEvent.click(list().getAllByRole('button', { name: /edit/i })[i])
    const editor = () => within(screen.getByTestId('pending-row-editor'))
    edit(1)
    fireEvent.change(editor().getByRole('textbox'), { target: { value: 'b, discarded' } })
    fireEvent.click(editor().getByRole('button', { name: /cancel/i }))
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a', 'b', 'c'])
    expect(screen.queryByTestId('pending-row-editor')).toBeNull()

    edit(1)
    // The head box is untouched by an edit.
    expect(box().value).toBe('')
    fireEvent.change(editor().getByRole('textbox'), { target: { value: 'b, reworded' } })
    fireEvent.click(editor().getByRole('button', { name: /save/i }))
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a', 'b, reworded', 'c'])
  })

  it('keeps an edit whose comment another tab removed, and Save adds it back', () => {
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    renderComposer()
    fireEvent.click(within(screen.getByTestId('pending-batch')).getByRole('button', { name: /edit/i }))
    const editor = () => within(screen.getByTestId('pending-row-editor'))
    fireEvent.change(editor().getByRole('textbox'), { target: { value: 'a, reworded' } })
    // Another tab removes the comment; this tab hears of it through a storage event.
    act(() => {
      localStorage.setItem(KEY(), '[]')
      window.dispatchEvent(new StorageEvent('storage', { key: KEY() }))
    })
    // The editor and its text are still there...
    expect(editor().getByRole<HTMLTextAreaElement>('textbox').value).toBe('a, reworded')
    // ...and Save puts the edit back as a pending comment.
    fireEvent.click(editor().getByRole('button', { name: /save/i }))
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a, reworded'])
  })

  it('keeps Submit off while a row is being edited', () => {
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    renderComposer()
    const submit = () => button(/submit batch/i) as HTMLButtonElement
    expect(submit().disabled).toBe(false)
    fireEvent.click(within(screen.getByTestId('pending-batch')).getByRole('button', { name: /edit/i }))
    expect(submit().disabled).toBe(true)
    expect(screen.getByTestId('pending-batch').textContent).toMatch(/save or cancel the edit/i)
    fireEvent.keyDown(within(screen.getByTestId('pending-row-editor')).getByRole('textbox'), { key: 'Escape' })
    expect(submit().disabled).toBe(false)
  })

  it('locks a row another pane is editing, and leaves it out of that submit', async () => {
    postComments.mockResolvedValue(ok(1))
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    const paneA = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const paneB = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const inA = within(paneA.container)
    const inB = within(paneB.container)
    act(() => { fireEvent.click(within(inA.getByTestId('pending-batch')).getAllByRole('button', { name: /edit/i })[0]) })
    // B cannot edit or remove 'a' while A has it open.
    const rowsB = within(inB.getByTestId('pending-batch'))
    expect((rowsB.getAllByRole('button', { name: /edit/i })[0] as HTMLButtonElement).disabled).toBe(true)
    expect((rowsB.getAllByRole('button', { name: /remove/i })[0] as HTMLButtonElement).disabled).toBe(true)
    // B's submit sends only 'b'; 'a' stays pending for A to save.
    await act(async () => { fireEvent.click(inB.getByRole('button', { name: /submit batch/i })) })
    expect(postComments.mock.calls[0][3].map((i: { body: string }) => i.body)).toEqual(['b'])
    fireEvent.change(within(inA.getByTestId('pending-row-editor')).getByRole('textbox'), { target: { value: 'a, edited' } })
    fireEvent.click(within(inA.getByTestId('pending-row-editor')).getByRole('button', { name: /save/i }))
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a, edited'])
  })

  it('locks the rows a submit in another pane is sending', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    const paneA = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const paneB = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    fireEvent.click(within(paneA.container).getByRole('button', { name: /submit batch/i }))
    const editInB = within(within(paneB.container).getByTestId('pending-batch'))
      .getByRole('button', { name: /edit/i }) as HTMLButtonElement
    expect(editInB.disabled).toBe(true)
    await act(async () => { resolve(ok(1)) })
    expect(loadPending(KEY())).toEqual([])
  })

  it('releases a row when the pane editing it closes', () => {
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    const paneA = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const paneB = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    act(() => { fireEvent.click(within(within(paneA.container).getByTestId('pending-batch')).getByRole('button', { name: /edit/i })) })
    const editInB = () => within(within(paneB.container).getByTestId('pending-batch'))
      .getByRole('button', { name: /edit/i }) as HTMLButtonElement
    expect(editInB().disabled).toBe(true)
    act(() => { paneA.unmount() })
    expect(editInB().disabled).toBe(false)
  })

  it('keeps comments batched in two panes open on the same item', () => {
    const paneA = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const paneB = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const add = (pane: typeof paneA, text: string) => {
      const scope = within(pane.container)
      fireEvent.change(scope.getByRole('textbox'), { target: { value: text } })
      fireEvent.click(scope.getByRole('button', { name: /add to batch/i }))
    }
    add(paneB, 'from the sheet')
    add(paneA, 'from the workspace')
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['from the sheet', 'from the workspace'])
    // Each pane lists both, so neither can write back a stale copy.
    expect(within(paneB.container).getByTestId('pending-batch').textContent).toContain('from the workspace')
    act(() => {
      fireEvent.click(within(within(paneB.container).getByTestId('pending-batch')).getAllByRole('button', { name: /remove/i })[0])
    })
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['from the workspace'])
  })

  it('keeps a comment another tab added to the stored batch', () => {
    const pane = render(
      <CommentBatchProvider target="pull" number={7} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const add = (text: string) => {
      const scope = within(pane.container)
      fireEvent.change(scope.getByRole('textbox'), { target: { value: text } })
      fireEvent.click(scope.getByRole('button', { name: /add to batch/i }))
    }
    add('a')
    // Another tab reads [a] from storage and adds b; this page's copy still says [a].
    const stored = loadPending(KEY())
    localStorage.setItem(KEY(), JSON.stringify([...stored, { id: 'other-tab', body: 'b', thread_id: null, reply_to: null }]))
    add('c')
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a', 'b', 'c'])
  })

  it('checks the cap and builds a submit from the batch another tab last stored', async () => {
    const item = (id: string, body = id) => ({ id, body, thread_id: null, reply_to: null })
    localStorage.setItem(KEY(), JSON.stringify(Array.from({ length: 24 }, (_, i) => item(`s${i}`))))
    renderComposer()
    // Another tab fills the batch; this tab gets no event and still has 24.
    localStorage.setItem(KEY(), JSON.stringify(Array.from({ length: 25 }, (_, i) => item(`s${i}`))))
    type('one too many')
    fireEvent.click(button(/add to batch/i))
    // The full batch kept the draft in its box rather than dropping it on read.
    expect(box().value).toBe('one too many')
    expect(loadPending(KEY())).toHaveLength(25)

    // The other tab then submits all but one; this tab sends only what is left.
    localStorage.setItem(KEY(), JSON.stringify([item('s24', 'left')]))
    postComments.mockResolvedValueOnce(ok(1))
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(postComments.mock.calls.at(-1)![3].map((i: { body: string }) => i.body)).toEqual(['left'])
  })

  it('wraps the inline footer rather than clipping its buttons', () => {
    renderComposer()
    replyTo('kim')
    const add = within(screen.getByTestId('card-kim')).getByRole('button', { name: /add to batch/i })
    expect(add.parentElement?.className).toContain('flex-wrap')
  })

  it('holds comments as a batch that survives a remount, then submits them in order', async () => {
    postComments.mockResolvedValue(ok(2))
    const first = renderComposer()
    type('one')
    fireEvent.click(button(/add to batch/i))
    type('two')
    // Once a batch is pending, Ctrl+Enter adds to it rather than posting.
    fireEvent.keyDown(box(), { key: 'Enter', ctrlKey: true })
    expect(postComments).not.toHaveBeenCalled()
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['one', 'two'])
    first.unmount()

    const { onPosted } = renderComposer()
    expect(screen.getByTestId('pending-batch').textContent).toContain('one')
    fireEvent.click(button(/submit batch/i))
    await waitFor(() => expect(onPosted).toHaveBeenCalled())
    expect(postComments.mock.calls[0][3]).toEqual([
      { body: 'one', thread_id: null }, { body: 'two', thread_id: null },
    ])
    expect(screen.queryByTestId('pending-batch')).toBeNull()
    expect(localStorage.getItem(KEY())).toBeNull()
  })

  it('keeps only the unposted items after a partial failure', async () => {
    const stored: PendingComment[] = ['a', 'b', 'c'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))
    localStorage.setItem(KEY(), JSON.stringify(stored))
    postComments.mockResolvedValue({ ...ok(1), failed: { index: 1, error: 'rate limited' } })
    renderComposer()
    fireEvent.click(button(/submit batch/i))
    expect(await screen.findByText(/rate limited/)).toBeTruthy()
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['b', 'c'])
  })

  it('discards the batch only on a second click', () => {
    localStorage.setItem(KEY(), JSON.stringify([{ id: '1', body: 'x', thread_id: null, reply_to: null }]))
    renderComposer()
    fireEvent.click(button(/discard all/i))
    expect(screen.getByTestId('pending-batch')).toBeTruthy()
    act(() => { fireEvent.click(button(/click again/i)) })
    expect(screen.queryByTestId('pending-batch')).toBeNull()
  })

  it('reconciles a batch that lands after the pane was left', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    const first = renderComposer()
    fireEvent.click(button(/submit batch/i))
    // The user switches to another item while the request is in flight.
    first.unmount()
    await act(async () => { resolve(ok(2)) })
    expect(localStorage.getItem(KEY())).toBeNull()
    renderComposer()
    expect(screen.queryByTestId('pending-batch')).toBeNull()
  })

  it('does not offer Submit again in a pane reopened while its batch is in flight', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    const first = renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    first.unmount()
    // Back on the item before the request lands: the batch is shown but locked.
    renderComposer()
    const submit = within(screen.getByTestId('pending-batch')).getAllByRole('button')
      .find((b) => /submit|submitting|posting|publishing/i.test(b.textContent ?? ''))!
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(submit)
    expect(postComments).toHaveBeenCalledTimes(1)
    await act(async () => { resolve(ok(2)) })
    expect(screen.queryByTestId('pending-batch')).toBeNull()
  })

  it('keeps a directly posted comment whose delivery is unknown, and says to check', async () => {
    postComments.mockResolvedValue({ ...ok(0), failed: { index: 0, error: 'timed out' }, unknown: [0] })
    const { onPosted } = renderComposer()
    type('maybe sent')
    await act(async () => { fireEvent.click(button(/^comment$/i)) })
    expect(box().value).toBe('maybe sent')
    expect(screen.getByText(/could not be confirmed as posted. check the timeline/i)).toBeTruthy()
    // The timeline reloads, so the user can see whether it landed.
    expect(onPosted).toHaveBeenCalled()
  })

  it('keeps an unconfirmed comment, leaves it out of Submit, and re-adds it on request', async () => {
    postComments.mockResolvedValueOnce({ ...ok(0), failed: { index: 0, error: 'timeout' }, unknown: [0] })
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    renderComposer()
    const submit = () => button(/submit batch/i) as HTMLButtonElement
    await act(async () => { fireEvent.click(submit()) })
    // Kept with its text, flagged, and not sendable on its own.
    const list = within(screen.getByTestId('pending-batch'))
    expect(list.getByTestId('pending-unconfirmed')).toBeTruthy()
    expect(list.getByText('a')).toBeTruthy()
    expect(screen.getByText(/stay in the batch as unconfirmed/i)).toBeTruthy()
    expect(submit().disabled).toBe(true)
    // The user checked the timeline: it did not post, so it goes back in line.
    fireEvent.click(list.getByRole('button', { name: /re-add/i }))
    expect(loadPending(KEY())[0].unconfirmed).toBe(false)
    postComments.mockResolvedValueOnce(ok(1))
    await act(async () => { fireEvent.click(submit()) })
    expect(postComments.mock.calls[1][3].map((i: { body: string }) => i.body)).toEqual(['a'])
  })

  it('stores the sent comments as unconfirmed before the request leaves', async () => {
    // Never answers: a reload now must find them marked, not sendable again.
    postComments.mockReturnValue(new Promise(() => {}))
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(postComments).toHaveBeenCalled()
    expect(loadPending(KEY()).map((p) => p.unconfirmed)).toEqual([true, true])
    // In this tab the rows are in flight, so they are locked rather than labelled.
    expect(screen.queryByTestId('pending-unconfirmed')).toBeNull()
  })

  it('sends nothing when storage keeps an older copy without the mark', async () => {
    const stored = JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }])
    const real = localStorage
    vi.stubGlobal('localStorage', {
      getItem: () => stored,
      // Full: every write is refused, so the copy above stays as it is.
      setItem: () => { throw new DOMException('full', 'QuotaExceededError') },
      removeItem: () => {},
      clear: () => {},
    })
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(postComments).not.toHaveBeenCalled()
    expect(screen.getByText(/nothing was sent/i)).toBeTruthy()
    vi.stubGlobal('localStorage', real)
  })

  it('takes the mark off when the gateway refuses the batch', async () => {
    postComments.mockRejectedValue(new CommentsRefusedError('forbidden'))
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(loadPending(KEY()).map((p) => p.unconfirmed)).toEqual([false])
    expect(screen.getByText('forbidden')).toBeTruthy()
    expect((button(/submit batch/i) as HTMLButtonElement).disabled).toBe(false)
  })

  it('takes the mark off what a partial batch shows did not post', async () => {
    postComments.mockResolvedValue({ ...ok(1), failed: { index: 1, error: 'refused' } })
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(loadPending(KEY()).map((p) => [p.body, p.unconfirmed])).toEqual([['b', false]])
  })

  it('fixes the row editor while its save waits for the write lock', async () => {
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    renderComposer()
    fireEvent.click(within(screen.getByTestId('pending-batch')).getByRole('button', { name: /edit/i }))
    const editor = within(screen.getByTestId('pending-row-editor'))
    const text = () => editor.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(text(), { target: { value: 'a, edited' } })
    let grant!: () => void
    vi.stubGlobal('navigator', {
      ...navigator,
      locks: {
        request: (_n: string, ...rest: unknown[]) => {
          const cb = rest[rest.length - 1] as () => unknown
          return new Promise((done) => { grant = () => { void Promise.resolve(cb()).then(done) } })
        },
      },
    })
    fireEvent.click(editor.getByRole('button', { name: /save/i }))
    expect(text().readOnly).toBe(true)
    expect((editor.getByRole('button', { name: /cancel/i }) as HTMLButtonElement).disabled).toBe(true)
    // Typed while the write waits: refused, so the saved text is what the box shows.
    fireEvent.change(text(), { target: { value: 'a, edited, and more' } })
    expect(text().value).toBe('a, edited')
    await act(async () => { grant() })
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a, edited'])
    expect(screen.queryByTestId('pending-row-editor')).toBeNull()
  })

  it('marks every sent comment unconfirmed when the gateway does not answer', async () => {
    postComments.mockRejectedValue(new TypeError('Failed to fetch'))
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(loadPending(KEY()).map((p) => [p.body, p.unconfirmed])).toEqual([['a', true], ['b', true]])
    expect(screen.getByText(/no answer came back/i)).toBeTruthy()
  })

  it('keeps the error showing once the last unconfirmed comment is dismissed', async () => {
    postComments.mockResolvedValue({ ...ok(0), failed: { index: 0, error: 'timeout' }, unknown: [0] })
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    fireEvent.click(within(screen.getByTestId('pending-batch')).getByRole('button', { name: /remove/i }))
    // The batch is empty, so the panel is gone...
    expect(screen.queryByTestId('pending-batch')).toBeNull()
    // ...but the notice telling the user to check the forge is still there.
    expect(screen.getByText(/could not be confirmed as posted/i)).toBeTruthy()
  })

  it('shows a submit error in a pane opened after the submitting one closed', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    const first = renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    // The user closes the pane before the forge answers.
    first.unmount()
    await act(async () => { resolve({ ...ok(0), failed: { index: 0, error: 'timeout' }, unknown: [0] }) })
    renderComposer()
    expect(screen.getByText(/could not be confirmed as posted/i)).toBeTruthy()
  })

  it('adds to the batch under the cross-tab write lock, and clears the box only once written', async () => {
    const names: string[] = []
    let grant!: () => void
    vi.stubGlobal('navigator', {
      ...navigator,
      locks: {
        request: (name: string, ...rest: unknown[]) => {
          names.push(name)
          const cb = rest[rest.length - 1] as () => unknown
          return new Promise((done) => { grant = () => { void Promise.resolve(cb()).then(done) } })
        },
      },
    })
    renderComposer()
    type('held')
    fireEvent.click(button(/add to batch/i))
    expect(names.some((n) => n.startsWith('issue-radar-batch:'))).toBe(true)
    // Until the lock is granted nothing is written, so the box keeps its text.
    expect(loadPending(KEY())).toEqual([])
    expect(box().value).toBe('held')
    await act(async () => { grant() })
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['held'])
    expect(box().value).toBe('')
  })

  it('keeps the box read-only while an add waits for the write lock', async () => {
    let grant!: () => void
    vi.stubGlobal('navigator', {
      ...navigator,
      locks: {
        request: (_n: string, ...rest: unknown[]) => {
          const cb = rest[rest.length - 1] as () => unknown
          return new Promise((done) => { grant = () => { void Promise.resolve(cb()).then(done) } })
        },
      },
    })
    renderComposer()
    type('held')
    fireEvent.click(button(/add to batch/i))
    expect(box().readOnly).toBe(true)
    // Typing while the lock is pending is refused, so nothing unsent is cleared.
    type('held, plus more')
    expect(box().value).toBe('held')
    await act(async () => { grant() })
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['held'])
    expect(box().value).toBe('')
    expect(box().readOnly).toBe(false)
  })

  it('keeps the draft when another tab filled the batch before the write lock was granted', async () => {
    const item = (id: string) => ({ id, body: id, thread_id: null, reply_to: null })
    let grant!: () => void
    vi.stubGlobal('navigator', {
      ...navigator,
      locks: {
        request: (_n: string, ...rest: unknown[]) => {
          const cb = rest[rest.length - 1] as () => unknown
          return new Promise((done) => { grant = () => { void Promise.resolve(cb()).then(done) } })
        },
      },
    })
    renderComposer()
    type('late')
    fireEvent.click(button(/add to batch/i))
    // Another tab fills the batch while this one waits for the lock.
    localStorage.setItem(KEY(), JSON.stringify(Array.from({ length: 25 }, (_, i) => item(`s${i}`))))
    await act(async () => { grant() })
    expect(loadPending(KEY())).toHaveLength(25)
    expect(box().value).toBe('late')
  })

  it('does not submit while another tab holds the submit lock', async () => {
    vi.stubGlobal('navigator', {
      ...navigator,
      locks: { request: (_n: string, _o: unknown, cb: (l: null) => unknown) => Promise.resolve(cb(null)) },
    })
    localStorage.setItem(KEY(), JSON.stringify([{ id: '0', body: 'a', thread_id: null, reply_to: null }]))
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(postComments).not.toHaveBeenCalled()
    expect(screen.getByText(/another tab is submitting/i)).toBeTruthy()
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a'])
  })

  it('reads the batch only once it holds the submit lock', async () => {
    postComments.mockResolvedValue(ok(1))
    vi.stubGlobal('navigator', {
      ...navigator,
      locks: {
        // Both signatures: the submit lock passes options, the batch write does not.
        request: (name: string, a: unknown, b?: unknown) => {
          const cb = (typeof a === 'function' ? a : b) as (l: object) => unknown
          if (name.startsWith('issue-radar-submit:')) {
            // While this tab waited for the lock, another tab sent 'a'.
            localStorage.setItem(KEY(), JSON.stringify([{ id: '1', body: 'b', thread_id: null, reply_to: null }]))
          }
          return Promise.resolve(cb({ name }))
        },
      },
    })
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    renderComposer()
    await act(async () => { fireEvent.click(button(/submit batch/i)) })
    expect(postComments.mock.calls[0][3].map((i: { body: string }) => i.body)).toEqual(['b'])
  })

  it('makes Add to batch the primary button once a batch is pending', () => {
    renderComposer()
    const addBtn = () => button(/add to batch/i)
    const postBtn = () => button(/^comment$/i)
    type('first')
    // No batch yet: posting now is the primary action.
    expect(postBtn().className).toContain('bg-accent')
    expect(addBtn().className).not.toContain('bg-accent')
    fireEvent.click(addBtn())
    type('second')
    // A batch is pending, so the primary button matches Ctrl/Cmd+Enter.
    expect(addBtn().className).toContain('bg-accent')
    expect(postBtn().className).not.toContain('bg-accent')
  })

  it('says so when storage does not take the batch, and keeps the comment pending', () => {
    renderComposer()
    type('kept in memory')
    fireEvent.click(button(/add to batch/i))
    expect(screen.queryByTestId('batch-unsaved')).toBeNull()

    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('denied') },
      setItem: () => { throw new Error('quota') },
      removeItem: () => { throw new Error('denied') },
      clear: () => {},
    })
    type('not stored')
    fireEvent.click(button(/add to batch/i))
    expect(screen.getByTestId('batch-unsaved').textContent).toMatch(/didn't save the batch/i)
    expect(screen.getByTestId('pending-batch').textContent).toContain('not stored')
  })

  it('keeps the unposted comments after a partial failure with storage disabled', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('denied') },
      setItem: () => { throw new Error('denied') },
      removeItem: () => { throw new Error('denied') },
      clear: () => {},
    })
    postComments.mockResolvedValue({ ...ok(1), failed: { index: 1, error: 'rate limited' } })
    renderComposer()
    for (const body of ['alpha-1', 'bravo-2', 'charlie-3']) {
      type(body)
      fireEvent.click(button(/add to batch/i))
    }
    fireEvent.click(button(/submit batch/i))
    expect(await screen.findByText(/rate limited/)).toBeTruthy()
    const shown = screen.getByTestId('pending-batch').textContent ?? ''
    expect(shown).not.toContain('alpha-1')
    expect(shown).toContain('bravo-2')
    expect(shown).toContain('charlie-3')
  })

  it('keeps the box read-only while its post is in flight', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    renderComposer()
    type('sent')
    fireEvent.click(button(/^comment$/i))
    expect(box().readOnly).toBe(true)
    // A change event while busy is ignored, so success cannot erase unsent text.
    type('sent plus more')
    expect(box().value).toBe('sent')
    await act(async () => { resolve(ok(1)) })
    expect(box().readOnly).toBe(false)
  })

  it('renders nothing on a read-only repo', () => {
    ctx.canWrite = false
    renderComposer()
    expect(screen.queryByTestId('timeline-composer')).toBeNull()
  })
})
