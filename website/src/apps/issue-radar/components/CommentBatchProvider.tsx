// CommentBatchProvider — the shared state behind the timeline's comment boxes.
//
// One issue or PR timeline can have several boxes open at once: the box at its
// head for a new comment, and an inline box under every comment being replied
// to. They all feed ONE pending batch, so this provider owns the drafts, the
// batch (persisted per item, see lib/commentBatch) and the forge calls, and the
// boxes are thin views over it. A pending comment is edited in its own row, so
// it never leaves the place the user is looking at.
//
// Mount it with `key` set to the item so switching items starts from that item's
// stored batch and drops the drafts of the item the pane just left.

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'

import { i18nT } from '../../../i18n/t'
import { CommentsRefusedError, issueRadarApi, type CommentBatchResponse, type TimelineEvent } from '../api'
import { useIssueRadar } from '../context'
import { isGitlab, repoScopeKey } from '../lib/links'
import {
  MAX_PENDING, anchorKeyOf, isHeld, isLocked, isSubmitting, isUnsaved, newPendingId, pendingStorageKey,
  quoteReply, readBatch, remainingAfter, replyTargetOf, setHeld, setSubmitting, toBatchItems, unmarkedInStorage,
  clearUnconfirmed, getBatchError, markUnconfirmed, setBatchErrorFor, withBatchWrite, withSubmitLock, writeBatch,
  type CommentTarget, type PendingComment,
} from '../lib/commentBatch'

/** The draft key of the box at the head of the timeline. */
export const TOP = '__top__'

export interface Draft {
  text: string
  /** The thread the reply goes into, when the forge has one for this comment. */
  thread_id: string | null
  /** Who it answers, for the pending row. Null for a new top-level comment. */
  reply_to: string | null
  /** Bumped whenever the box should take focus again (a second Reply click). */
  focusSeq: number
  busy: boolean
  error: string | null
}

/** Where a pending row is shown: the list at the head of the timeline, or under
 * the comment it answers. A pending reply is shown in both. */
export type RowPlace = 'list' | 'inline'

/** A pending comment open for editing in one of its rows. The stored comment is
 * unchanged until the edit is saved. */
export interface RowEdit {
  text: string
  place: RowPlace
  /** The comment as it was when the edit opened. */
  item: PendingComment
  /** Save is waiting on the batch write: the text is fixed until it settles, so
   * nothing typed meanwhile is dropped when the editor closes. */
  saving?: boolean
}

/** Fired with a storage key whenever that item's batch changed, so every mounted
 * pane showing the item re-reads it. */
const PENDING_UPDATED_EVENT = 'issue-radar:pending-updated'

const EMPTY_TOP: Draft = {
  text: '', thread_id: null, reply_to: null, focusSeq: 0, busy: false, error: null,
}

interface CommentBatchValue {
  canWrite: boolean
  nativeReview: boolean
  pending: PendingComment[]
  batchFull: boolean
  /** The batch is held only in this page: storage refused it, so a reload loses it. */
  batchUnsaved: boolean
  batchBusy: boolean
  batchError: string | null
  clearBatchError: () => void
  /** Whether a pending comment is being sent by a submit, or is open in a row
   * editor, in this pane or another one on the same item. */
  isLocked: (id: string) => boolean
  drafts: Record<string, Draft>
  /** Open (or refocus) the inline reply box under a timeline comment. */
  openReply: (ev: TimelineEvent) => void
  setText: (key: string, text: string) => void
  clearError: (key: string) => void
  /** Close an inline box. */
  close: (key: string) => void
  post: (key: string) => Promise<void>
  addToBatch: (key: string) => void
  /** This pane's open row edits, by pending comment id. */
  rowEdits: Record<string, RowEdit>
  startEdit: (item: PendingComment, place: RowPlace) => void
  setEditText: (id: string, text: string) => void
  saveEdit: (id: string) => void
  cancelEdit: (id: string) => void
  removePending: (id: string) => void
  /** Put an unconfirmed comment back in line, once the user saw it did not post. */
  readdPending: (id: string) => void
  discardAll: () => void
  submitBatch: () => Promise<void>
}

const Ctx = createContext<CommentBatchValue | null>(null)

export function useCommentBatch(): CommentBatchValue {
  const value = useContext(Ctx)
  if (!value) throw new Error('useCommentBatch must be used inside CommentBatchProvider')
  return value
}

/** Same as `useCommentBatch`, but null outside a provider (cards rendered alone). */
export function useOptionalCommentBatch(): CommentBatchValue | null {
  return useContext(Ctx)
}

export default function CommentBatchProvider({
  target, number, onPosted, children,
}: {
  target: CommentTarget
  number: number
  /** Called after anything reached the forge, so the pane re-reads its timeline. */
  onPosted: () => void
  children: ReactNode
}) {
  const { active, canWrite } = useIssueRadar()
  const storageKey = pendingStorageKey(repoScopeKey(active), target, number)
  const nativeReview = target === 'pull' && isGitlab(active)

  // This mount's view of the item's batch. The page's copy (lib/commentBatch) is
  // the authority: the same item can be open in two panes at once (the workspace
  // and the reference sheet), so every change reads the current batch, writes it
  // back and tells the other mounts, instead of saving this mount's snapshot.
  const [batch, setBatch] = useState<PendingComment[]>(() => readBatch(storageKey))
  const [drafts, setDrafts] = useState<Record<string, Draft>>({ [TOP]: EMPTY_TOP })
  const [rowEdits, setRowEdits] = useState<Record<string, RowEdit>>({})
  // Read from the page, not this mount: a pane reopened while its batch is still
  // being sent must not offer Submit, or the same comments would post twice.
  const [batchBusy, setBatchBusy] = useState(() => isSubmitting(storageKey))
  // Read from the page as well: a submit's answer can arrive after its pane closed,
  // and the next pane on the item has to show what it said.
  const [batchError, setErrorState] = useState<string | null>(() => getBatchError(storageKey))
  // Bumped when another pane's lock changes, so locked rows re-render here.
  const [, setLockSeq] = useState(0)

  useEffect(() => {
    const onUpdate = (e: Event) => {
      if ((e as CustomEvent<string>).detail !== storageKey) return
      setBatch(readBatch(storageKey))
      setBatchBusy(isSubmitting(storageKey))
      setErrorState(getBatchError(storageKey))
      setLockSeq((n) => n + 1)
    }
    // Another tab's write reaches this one only as a storage event.
    const onStorage = (e: StorageEvent) => {
      if (e.key === storageKey) setBatch(readBatch(storageKey))
    }
    window.addEventListener(PENDING_UPDATED_EVENT, onUpdate)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(PENDING_UPDATED_EVENT, onUpdate)
      window.removeEventListener('storage', onStorage)
    }
  }, [storageKey])

  const announce = useCallback(() => {
    window.dispatchEvent(new CustomEvent(PENDING_UPDATED_EVENT, { detail: storageKey }))
  }, [storageKey])

  /** The item's submit error, kept for the page and shown in every pane on it. */
  const setBatchError = useCallback((message: string | null) => {
    setBatchErrorFor(storageKey, message)
    setErrorState(message)
    announce()
  }, [storageKey, announce])

  // A pane closed mid-edit releases its rows, or they would stay locked in every
  // other pane for the rest of the page's life.
  const editsRef = useRef(rowEdits)
  editsRef.current = rowEdits
  useEffect(() => () => {
    for (const id of Object.keys(editsRef.current)) setHeld(storageKey, id, false)
    if (Object.keys(editsRef.current).length) announce()
  }, [storageKey, announce])

  /** One read-modify-write of the batch, under the lock every tab shares. `change`
   * returns null to leave the batch as it is (it is full, say); `after` runs only
   * once a change has been written, so a box is never cleared for a comment that
   * did not make it into the batch. */
  const mutate = useCallback((
    change: (items: PendingComment[]) => PendingComment[] | null,
    after?: () => void,
  ) => withBatchWrite(storageKey, () => {
    const next = change(readBatch(storageKey))
    if (next === null) return
    writeBatch(storageKey, next)
    setBatch(next)
    announce()
    after?.()
  }), [storageKey, announce])

  const patch = useCallback((key: string, change: Partial<Draft>) => {
    setDrafts((all) => (all[key] ? { ...all, [key]: { ...all[key], ...change } } : all))
  }, [])

  /** After a post or an add: the top box empties, an inline box goes away. */
  const finish = useCallback((key: string) => {
    setDrafts((all) => {
      if (key === TOP) return { ...all, [TOP]: { ...EMPTY_TOP, focusSeq: all[TOP].focusSeq } }
      const { [key]: _gone, ...rest } = all
      return rest
    })
  }, [])

  const openReply = useCallback((ev: TimelineEvent) => {
    const key = anchorKeyOf(ev)
    const reply = replyTargetOf(ev)
    setDrafts((all) => {
      const existing = all[key]
      if (existing) return { ...all, [key]: { ...existing, focusSeq: existing.focusSeq + 1 } }
      return {
        ...all,
        [key]: {
          // Without a thread the reply is a new comment quoting this one.
          text: reply.thread_id ? '' : quoteReply(reply),
          thread_id: reply.thread_id,
          reply_to: reply.author ?? 'ghost',
          focusSeq: 1,
          busy: false,
          error: null,
        },
      }
    })
  }, [])

  /** The current batch, also refreshed into this mount's view. Checks run against
   * it, because another tab may have changed the batch since this mount read it. */
  const current = () => {
    const items = readBatch(storageKey)
    setBatch(items)
    return items
  }

  const post = async (key: string) => {
    const d = drafts[key]
    if (!d || d.busy || !d.text.trim()) return
    patch(key, { busy: true, error: null })
    try {
      const result = await issueRadarApi.postComments(active, number, target, [
        { body: d.text.trim(), thread_id: d.thread_id },
      ])
      if (result.unknown?.length) {
        // The forge may have taken it. The text stays so nothing is lost, the
        // timeline reloads so the user can see whether it landed, and the box says
        // to look before sending it again.
        patch(key, {
          busy: false,
          error: i18nT('apps.issueRadar.components.timelineComposer.post_unknown', {
            error: result.failed?.error ?? '',
          }),
        })
        onPosted()
        return
      }
      finish(key)
      onPosted()
    } catch (e) {
      // The draft stays: a transient failure must not cost the user a paragraph.
      // Without an answer from the gateway the comment may have posted, so the
      // box says to look before sending it again.
      const message = e instanceof CommentsRefusedError
        ? e.message
        : i18nT('apps.issueRadar.components.timelineComposer.post_unknown', { error: (e as Error).message })
      patch(key, { busy: false, error: message })
      if (!(e instanceof CommentsRefusedError)) onPosted()
    }
  }

  const addToBatch = (key: string) => {
    const d = drafts[key]
    if (!d || d.busy || !d.text.trim()) return
    const item: PendingComment = {
      id: newPendingId(),
      body: d.text.trim(),
      thread_id: d.thread_id,
      reply_to: d.reply_to,
      anchor: key === TOP ? null : key,
    }
    // The cap is checked on the batch as it is under the lock, and a full batch
    // keeps the draft in its box: nothing is cleared that was not stored.
    // The box is read-only until the write settles: it can wait for the cross-tab
    // lock, and text typed meanwhile would be cleared without being stored.
    patch(key, { busy: true })
    const written = mutate(
      (items) => (items.length >= MAX_PENDING ? null : [...items, item]),
      () => finish(key),
    )
    void Promise.resolve(written).then(() => patch(key, { busy: false }))
  }

  const release = (id: string) => {
    setHeld(storageKey, id, false)
    setRowEdits(({ [id]: _gone, ...rest }) => rest)
    announce()
  }

  const startEdit = (item: PendingComment, place: RowPlace) => {
    if (rowEdits[item.id]) {
      setRowEdits((all) => ({ ...all, [item.id]: { ...all[item.id], place } }))
      return
    }
    if (isLocked(storageKey, item.id)) return
    // Every other pane locks the row while it is edited here, so no pane removes,
    // submits or edits the comment underneath this edit.
    setHeld(storageKey, item.id, true)
    setRowEdits((all) => ({ ...all, [item.id]: { text: item.body, place, item } }))
    announce()
  }

  const setSaving = (id: string, saving: boolean) => setRowEdits((all) => (
    all[id] ? { ...all, [id]: { ...all[id], saving } } : all
  ))

  const saveEdit = (id: string) => {
    const edit = rowEdits[id]
    if (!edit?.text.trim() || edit.saving) return
    const body = edit.text.trim()
    setSaving(id, true)
    // Another tab, which this page's row locks do not reach, may have removed or
    // sent the comment meanwhile: the edit then goes back in as a pending comment
    // of its own, unless the batch is full, and then the editor stays open.
    void Promise.resolve(mutate((all) => {
      if (all.some((p) => p.id === id)) return all.map((p) => (p.id === id ? { ...p, body } : p))
      if (all.length >= MAX_PENDING) return null
      return [...all, { ...edit.item, id: newPendingId(), body }]
    }, () => release(id))).finally(() => setSaving(id, false))
  }

  const submitBatch = async () => {
    // A comment open in a row editor is saved or cancelled first, so Submit never
    // sends a text the user is still changing, nor skips it without saying so.
    if (batchBusy || isSubmitting(storageKey) || Object.keys(rowEdits).length) return
    setBatchError(null)
    const ran = await withSubmitLock(storageKey, () => sendBatch())
    if (!ran) setBatchError(i18nT('apps.issueRadar.components.timelineComposer.batch_busy_elsewhere'))
  }

  /** The submit itself, run holding the item's cross-tab submit lock. */
  const sendBatch = async () => {
    // Read only now, under the lock, from the stored batch rather than this mount's
    // view: another tab may have submitted part of it a moment ago. A comment
    // another pane is editing stays out of it.
    // An unconfirmed comment may already be on the forge, so it waits for the user.
    const sent = current().filter((p) => !isHeld(storageKey, p.id) && !p.unconfirmed)
    if (!sent.length) return
    setSubmitting(storageKey, sent.map((p) => p.id))
    setBatchBusy(true)
    // Every other pane on this item locks the comments being sent, so none of them
    // can be edited or removed while the forge is taking them.
    announce()
    try {
      // Stored as unconfirmed before the request leaves, so a reload while it is in
      // flight finds them marked and does not send them again. The answer clears
      // the mark on whatever it shows did not post.
      await withBatchWrite(storageKey, () => {
        writeBatch(storageKey, markUnconfirmed(readBatch(storageKey), sent))
      })
      if (unmarkedInStorage(storageKey, sent)) {
        // Storage kept an older copy without the mark, so a reload could resend:
        // nothing is sent. Storage that holds nothing at all loses the batch on
        // reload instead, and cannot resend it.
        await withBatchWrite(storageKey, () => {
          writeBatch(storageKey, clearUnconfirmed(readBatch(storageKey), sent))
        })
        setBatchError(i18nT('apps.issueRadar.components.timelineComposer.batch_not_saved_not_sent'))
        return
      }
      let result: CommentBatchResponse
      try {
        result = await issueRadarApi.postComments(active, number, target, toBatchItems(sent))
      } catch (e) {
        if (e instanceof CommentsRefusedError) {
          // The gateway's own error: nothing was posted, so all of it can go again.
          await withBatchWrite(storageKey, () => {
            writeBatch(storageKey, clearUnconfirmed(readBatch(storageKey), sent))
          })
          setBatchError(e.message)
          return
        }
        // No answer the gateway vouches for: the batch may have been posted, so
        // every comment it sent stays marked and is not sent again on its own.
        setBatchError(i18nT('apps.issueRadar.components.timelineComposer.batch_no_answer', {
          error: (e as Error).message,
        }))
        onPosted()
        return
      }
      // Reconciled into the item's batch, not this component's state: the user may
      // have left the item while the request was in flight, and this mount's state
      // is gone then. Every mount of the item re-reads the batch on the event.
      // Under the batch write lock, so a comment another tab adds meanwhile is in
      // the batch this reads and is kept.
      await withBatchWrite(storageKey, () => {
        writeBatch(storageKey, remainingAfter(readBatch(storageKey), sent, result))
      })
      if (result.unknown?.length) {
        // Only a merge request's batch is a review with drafts to check; elsewhere
        // the place to look is the timeline itself.
        const error = result.failed?.error ?? ''
        setBatchError(nativeReview
          ? i18nT('apps.issueRadar.components.timelineComposer.batch_unknown', { error })
          : i18nT('apps.issueRadar.components.timelineComposer.batch_unknown_posted', { error }))
      } else if (result.failed) {
        setBatchError(i18nT('apps.issueRadar.components.timelineComposer.batch_partial', {
          posted: result.posted.length, total: sent.length, error: result.failed.error,
        }))
      }
      if (result.posted.length) onPosted()
    } catch (e) {
      // A failure of this page's own storage step, not of the forge write.
      setBatchError((e as Error).message)
    } finally {
      setSubmitting(storageKey, null)
      setBatchBusy(false)
      announce()
    }
  }

  const value: CommentBatchValue = {
    canWrite,
    nativeReview,
    pending: batch,
    batchFull: batch.length >= MAX_PENDING,
    // Read on every render: `batch` changes on each write, so this follows it.
    batchUnsaved: batch.length > 0 && isUnsaved(storageKey),
    batchBusy,
    batchError,
    clearBatchError: () => setBatchError(null),
    isLocked: (id) => !rowEdits[id] && isLocked(storageKey, id),
    drafts,
    openReply,
    // Text typed while a post is in flight would be cleared by its success
    // without having been sent, so the box is read-only until the post settles.
    setText: (key, text) => setDrafts((all) => (
      all[key] && !all[key].busy ? { ...all, [key]: { ...all[key], text } } : all
    )),
    clearError: (key) => patch(key, { error: null }),
    close: (key) => { if (key !== TOP) finish(key) },
    post,
    addToBatch,
    rowEdits,
    startEdit,
    setEditText: (id, text) => setRowEdits((all) => (
      all[id] && !all[id].saving ? { ...all, [id]: { ...all[id], text } } : all
    )),
    saveEdit,
    cancelEdit: (id) => { if (rowEdits[id] && !rowEdits[id].saving) release(id) },
    removePending: (id) => {
      if (isLocked(storageKey, id)) return
      mutate((items) => items.filter((p) => p.id !== id))
    },
    readdPending: (id) => {
      if (isLocked(storageKey, id)) return
      mutate((items) => items.map((p) => (p.id === id ? { ...p, unconfirmed: false } : p)))
    },
    discardAll: () => mutate((items) => items.filter((p) => isLocked(storageKey, p.id))),
    submitBatch,
  }

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
