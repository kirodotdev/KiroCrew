/**
 * The single rename commit path, shared by the sidebar row editor and the ChatPage
 * header editor.
 *
 * ONE commit per slot at a time. A rename is a PATCH of one field, so two of them
 * racing on the wire let the server apply the older one last, and no client
 * bookkeeping can reorder what already arrived — so the second is refused while
 * the first is in flight. That removes the ordering problem at the source, which
 * is why there are no sequence numbers and no per-rename ack cache here.
 *
 * What serialization does NOT answer is whether the server spoke at all while the
 * one request was open: a pushed title event can carry the very string that was
 * typed, so comparing values cannot tell an independent confirmation from this
 * client's own guess. The generation counters answer that, and only that.
 *
 * The in-flight set is MODULE-level, not `useRef`: a slot can be renamed from
 * either surface, and per-component state would let each start one.
 */
import { useCallback, useRef } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppStore } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import { api } from '../api/client'
import { findReport, type ErrorReport } from '../utils/errorReport'
import { i18nT } from '../i18n/t'

const inFlight = new Set<string>()

/**
 * A rename that never settles would hold its slot's lock for the life of the tab,
 * since `onSettled` is the only release. Aborting bounds the wait but not the
 * server's work, so ordering rests on the server's title epoch, not this lock.
 */
const RENAME_TIMEOUT_MS = 30_000

export function __resetRenameSlotStateForTests(): void {
  inFlight.clear()
}

export type RenameVars = { key: string; next: string }
export type RenameCtx = RenameVars & { before: string; gen: number; titleGen: number }

export function useRenameSlot(onFailure: (message: string, subject: string, report?: ErrorReport) => void) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const queryClient = useQueryClient()

  // The callers pass an inline closure, so read it through a ref: depending on its
  // identity would hand the memoized rows a fresh commit function every render.
  const onFailureRef = useRef(onFailure)
  onFailureRef.current = onFailure

  const { mutate } = useMutation({
    mutationFn: ({ key, next }: RenameVars) => {
      const abort = new AbortController()
      const timer = setTimeout(() => abort.abort(), RENAME_TIMEOUT_MS)
      return api.renameSlot(key, next, abort.signal).finally(() => clearTimeout(timer))
    },
    onMutate: ({ key, next }: RenameVars): RenameCtx => {
      const d = store.getState().dashboard
      const before = d.slots.find(s => s.key === key)?.title ?? ''
      inFlight.add(key)
      // `updateSlot`, not `sseSlotTitle`: a client guess must not advance the
      // counter that tells this rollback the server has spoken.
      dispatch(updateSlot({ key, title: next }))
      return {
        key,
        next,
        before,
        gen: d.slotsGeneration ?? 0,
        titleGen: d.slotTitleGenerations?.[key] ?? 0,
      }
    },
    onSuccess: (data, { key, next }, ctx) => {
      // The endpoint normalizes (strip, then 200 chars), so what was typed is not
      // necessarily what was stored — repaint before a lost title event can.
      const acked = typeof (data as { title?: unknown } | undefined)?.title === 'string'
        ? (data as { title: string }).title
        : next
      const d = store.getState().dashboard
      const live = d.slots.find(s => s.key === key)?.title
      // Repaint only where the server stayed quiet during the flight. A frame
      // carrying ctx.before may be NEWER than this response, not stale.
      const ours = !spokeSince(d, ctx) && (live === ctx?.next || live === ctx?.before)
      if (ours && live !== acked) dispatch(updateSlot({ key, title: acked }))
    },
    onSettled: (_data, _error, { key }) => {
      inFlight.delete(key)
    },
    onError: (e, _vars, ctx) => {
      // Compare-and-set, plus the stand-down: restore only where the row still
      // shows this rename's optimistic value and no server update has landed.
      if (ctx && !spokeSince(store.getState().dashboard, ctx)) {
        const live = store.getState().dashboard.slots.find(s => s.key === ctx.key)?.title
        if (live === ctx.next) dispatch(updateSlot({ key: ctx.key, title: ctx.before }))
      }
      onFailureRef.current(
        // The raw `e.message` is transport internals ("Failed to fetch"), so it
        // goes to the journal lookup rather than to the reader.
        i18nT('pages.chatPage.rename_reverted_try_again'),
        ctx?.next ?? '',
        findReport(errMessage(e)),
      )
      queryClient.invalidateQueries({ queryKey: ['chat-slots'] })
    },
  })

  // Returns false when the commit was REFUSED, so the caller can keep its editor
  // open and the typed draft alive rather than closing over a lost edit.
  return useCallback(({ key, next }: RenameVars): boolean => {
    if (inFlight.has(key)) {
      onFailureRef.current(i18nT('pages.chatPage.rename_already_saving'), next)
      return false
    }
    // Marked here, not in `onMutate`: that runs a microtask later, so a second
    // commit in the same tick would slip past the check above.
    inFlight.add(key)
    mutate({ key, next })
    return true
  }, [mutate])
}

type Dash = { slotsGeneration?: number; slotTitleGenerations?: Record<string, number> }

/** True when a full frame or a title event for this slot landed after `ctx`. */
function spokeSince(d: Dash, ctx?: RenameCtx): boolean {
  if (!ctx) return false
  return (d.slotsGeneration ?? 0) !== ctx.gen
    || (d.slotTitleGenerations?.[ctx.key] ?? 0) !== ctx.titleGen
}

function errMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e ?? '')
}
