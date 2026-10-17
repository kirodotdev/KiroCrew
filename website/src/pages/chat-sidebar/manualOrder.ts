/** The person's manual order of every session (sort "Manual"): kept on the
 *  gateway (each slot row carries `manual_rank`), drawn below the pinned
 *  section, and written by a drop between two rows in any sort or, under
 *  Manual, by Alt+Arrow on an unpinned row. Every write is ONE operation the gateway applies to the
 *  order it holds -- a seed while nothing is saved, then moves of one row -- so
 *  two browsers never write over each other's changes. */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import type { ManualOrderWrite } from '../../api/client/chatOrganization'
import { MOVE_UNDO_MS } from '../../components/MoveUndoBar'
import useHeldWindow from '../../hooks/useHeldWindow'
import { haptic } from '../../lib/haptic'
import { i18nT } from '../../i18n/t'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { fetchSlots, setManualOrderError, setManualRanks, setPendingManualOrder } from '../../store/dashboardSlice'
import { movePinnedSession } from '../../utils/pinnedSessionOrder'
import { compareBySort, compareManual, SORT_LABEL_KEY, type SortKey } from '../chat/sessionOrder'
import { keyboardReorderTarget } from './pinnedOrder'
import type { Slot } from './types'

/** Keys of rows that have a place in the manual order, in that order. */
export function rankedManualKeys(rows: readonly { key: string; manual_rank?: number | null }[]): string[] {
  return rows
    .filter((row): row is { key: string; manual_rank: number } => typeof row.manual_rank === 'number')
    .sort((a, b) => a.manual_rank - b.manual_rank)
    .map(row => row.key)
}

/** The unpinned local rows as the manual order draws them with *rank*: rows
 *  with no place first (a new session shows at the top), then by place. */
export function manualDrawnOrder(
  rows: readonly Slot[],
  pinned: ReadonlySet<string>,
  rank: ReadonlyMap<string, number>,
): string[] {
  return rows
    .filter(slot => !pinned.has(slot.key))
    .sort((a, b) => compareManual(a, b, item => rank.get(item.key)))
    .map(slot => slot.key)
}

/** The manual order after *key* moves next to *anchor*, computed the way the
 *  gateway computes it (`pinned_session_order.after_manual_move`): the rows
 *  named in *seat* that have no place take the top first, then *key* goes
 *  right after *anchor* (right before it when *after* is false; the very top
 *  for a `null` anchor). An anchor with no place leaves the order as it is. */
export function afterManualMove(
  order: readonly string[],
  key: string,
  anchor: string | null,
  after: boolean,
  seat: readonly string[],
): string[] {
  const placed = new Set(order)
  const next = [...seat.filter(k => !placed.has(k) && k !== key), ...order.filter(k => k !== key)]
  if (anchor === null) return [key, ...next]
  const at = next.indexOf(anchor)
  if (at < 0) return [...order]
  next.splice(at + (after ? 1 : 0), 0, key)
  return next
}

/** What the person reads when a write did not take, naming the sort the list
 *  shows after it (*shown*). A 409 means the sessions changed under it (one
 *  closed, or another browser moved first). *unsaved* says the gateway holds
 *  no order that ranks the open sessions, so Manual draws them unranked. */
export function manualMoveFailureText(err: unknown, shown: SortKey, unsaved = false): string {
  if (shown === 'manual' && unsaved) return i18nT('pages.chatSidebar.manual_order_unsaved')
  const cause = (err as { status?: number } | null)?.status === 409 ? 'conflict' : 'failed'
  return shown === 'manual'
    ? i18nT(cause === 'conflict' ? 'pages.chatSidebar.manual_order_move_conflict_saved' : 'pages.chatSidebar.manual_order_move_failed_saved')
    : i18nT(cause === 'conflict' ? 'pages.chatSidebar.manual_order_move_conflict_sort' : 'pages.chatSidebar.manual_order_move_failed_sort',
      { sort: i18nT(SORT_LABEL_KEY[shown]) })
}

type ManualAnswer = { order: string[]; rev: number }

/** The stored order a 409 carries, with its code, or null. */
function heldOrderOf(err: unknown): (ManualAnswer & { code: string }) | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  try {
    const body = JSON.parse(err.body) as { code?: unknown; order?: unknown; rev?: unknown }
    if (!Array.isArray(body.order) || typeof body.rev !== 'number') return null
    const order = body.order.filter((key): key is string => typeof key === 'string')
    return { code: typeof body.code === 'string' ? body.code : '', order, rev: body.rev }
  } catch {
    return null
  }
}

function moveWrite(key: string, anchor: string | null, after: boolean, seat: string[], created: string | undefined): ManualOrderWrite {
  const extra = { seat, ...(created ? { expected_created: created } : {}) }
  return anchor === null || after
    ? { op: 'move', key, after: anchor, ...extra }
    : { op: 'move', key, before: anchor, ...extra }
}

/** How an undo puts the dragged row back: after the very first drag, by
 *  clearing the order while it is still at the revision that drag wrote (and
 *  otherwise by moving the row back); after a later drag, by moving the row
 *  back after the session it followed (the top for null). */
type UndoPlan = { kind: 'clear'; rev: number; after: string | null } | { kind: 'move'; after: string | null }

/** The "Sorted by hand" offer one drop arms: the row, the sort it was dropped
 *  in, and how to put it back. One at a time; a newer drop replaces it. */
export interface ManualSortOffer {
  id: number
  key: string
  prevSort: SortKey
  plan: UndoPlan
}

let nextOfferId = 1

/** One write queue per dashboard store, shared by every sidebar mount, so the
 *  gateway applies the writes in gesture order. */
const manualOrderQueues = new WeakMap<object, { tail: Promise<unknown> }>()
function manualOrderQueue(store: object): { tail: Promise<unknown> } {
  let queue = manualOrderQueues.get(store)
  if (!queue) {
    queue = { tail: Promise.resolve() }
    manualOrderQueues.set(store, queue)
  }
  return queue
}

export function useManualSessionOrder({ localSlots, pinned, sortKey, setSortKey }: {
  localSlots: Slot[]
  pinned: ReadonlySet<string>
  sortKey: SortKey
  setSortKey: (key: SortKey) => void
}) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const { mutateAsync: mutateManualOrder } = useMutation({
    mutationFn: (body: ManualOrderWrite) => api.setManualOrder(body),
  })
  // Local rows only, as for the pinned order: a peer row has no place here.
  const stored = useMemo(() => rankedManualKeys(localSlots), [localSlots])
  // While writes are queued the latest gesture outranks the rows' places: a
  // frame serialized before those writes carries older ones.
  const pending = useAppSelector(state => state.dashboard.pendingManualOrder ?? null)
  const manualOrder = pending ?? stored
  const manualRank = useMemo(() => new Map(manualOrder.map((key, index) => [key, index])), [manualOrder])
  // The unpinned rows in the order they are drawn now, whatever the sort: a
  // drop lands in this order, and a first drag seeds it.
  const drawnOrder = useMemo(() => (sortKey === 'manual'
    ? manualDrawnOrder(localSlots, pinned, manualRank)
    : localSlots.filter(slot => !pinned.has(slot.key)).sort((a, b) => compareBySort(a, b, sortKey)).map(slot => slot.key)),
  [sortKey, localSlots, pinned, manualRank])
  const drawnIndex = useMemo(() => new Map(drawnOrder.map((key, index) => [key, index])), [drawnOrder])
  const manualOrderError = useAppSelector(state => state.dashboard.manualOrderError ?? '')
  const setManualOrderErrorText = useCallback((message: string) => { dispatch(setManualOrderError(message)) }, [dispatch])
  // Read by the async continuations (a 409's retry, an undo), which run after
  // the render that started them.
  const latest = useRef({ localSlots, pinned, manualOrder, known: manualOrder.length > 0 })
  latest.current = { localSlots, pinned, manualOrder, known: manualOrder.length > 0 }

  const applyAnswer = useCallback((res: unknown) => {
    const answer = res as Partial<ManualAnswer> | null | undefined
    if (Array.isArray(answer?.order) && typeof answer?.rev === 'number') {
      dispatch(setManualRanks({ order: answer.order, rev: answer.rev }))
    }
  }, [dispatch])
  /** Send one write after every earlier one, painting *painted* until it
   *  settles. A conflict's follow-up stays in this queue link, ahead of later
   *  gestures. A 409's stored order is drawn; any other failure re-reads the
   *  slots. Only the last queued write hands the drawing back to the rows. */
  const write = useCallback((body: ManualOrderWrite, painted: string[],
    onConflict?: (held: ManualAnswer & { code: string }) => { body: ManualOrderWrite; painted: string[] } | undefined,
  ): Promise<ManualAnswer> => {
    dispatch(setManualOrderError(''))
    dispatch(setPendingManualOrder(painted))
    dispatch(setManualRanks(painted))
    const queue = manualOrderQueue(store)
    const request = queue.tail.catch(() => undefined).then(async () => {
      try {
        return await mutateManualOrder(body)
      } catch (err: unknown) {
        const held = heldOrderOf(err)
        const followUp = held && onConflict?.(held)
        if (!followUp) throw err
        applyAnswer(held)
        if (isLatest()) dispatch(setPendingManualOrder(followUp.painted))
        dispatch(setManualRanks(followUp.painted))
        return mutateManualOrder(followUp.body)
      }
    })
    queue.tail = request
    const isLatest = () => queue.tail === request
    return request.then(
      res => {
        applyAnswer(res)
        if (isLatest()) dispatch(setPendingManualOrder(null))
        return res
      },
      (err: unknown) => {
        const held = heldOrderOf(err)
        if (held) applyAnswer(held)
        if (isLatest()) {
          dispatch(setPendingManualOrder(null))
          if (!held) dispatch(fetchSlots())
        }
        throw err
      },
    )
  }, [dispatch, store, applyAnswer, mutateManualOrder])
  // The sort the list shows: the last one this hook asked for, or a newer one
  // from anywhere else. Read when a write fails, after the render that sent it.
  const shownSort = useRef(sortKey)
  useLayoutEffect(() => { shownSort.current = sortKey }, [sortKey])
  const showSort = useCallback((key: SortKey) => {
    shownSort.current = key
    setSortKey(key)
  }, [setSortKey])
  const fail = useCallback((err: unknown, { unsaved = false }: { unsaved?: boolean } = {}) => {
    setManualOrderErrorText(manualMoveFailureText(err, shownSort.current, unsaved))
  }, [setManualOrderErrorText])

  /** Prepare a move against *order*, also for a conflict's in-queue follow-up. */
  const prepareMove = useCallback((order: readonly string[], key: string, anchor: string | null, after: boolean) => {
    const { localSlots: rows, pinned: pins } = latest.current
    const rank = new Map(order.map((k, index) => [k, index]))
    const drawn = manualDrawnOrder(rows, pins, rank)
    const seat = drawn.filter(k => !rank.has(k) && k !== key)
    const created = rows.find(slot => slot.key === key)?.created
    const undoAfter = drawn[drawn.indexOf(key) - 1] ?? null
    return {
      body: moveWrite(key, anchor, after, seat, created),
      painted: afterManualMove(order, key, anchor, after, seat),
      undoAfter,
    }
  }, [])
  /** Move *key* next to *anchor* in *order*, the manual order this write applies to. */
  const moveIn = useCallback((order: readonly string[], key: string, anchor: string | null, after: boolean) => {
    const move = prepareMove(order, key, anchor, after)
    return write(move.body, move.painted).then(() => move.undoAfter)
  }, [prepareMove, write])

  const [offer, setOffer] = useState<ManualSortOffer | null>(null)
  const offerRef = useRef(offer)
  offerRef.current = offer
  const arm = useCallback((key: string, prevSort: SortKey, plan: UndoPlan) => {
    setOffer({ id: nextOfferId++, key, prevSort, plan })
  }, [])
  // Counts drops, sort picks and undos, so a drop that fails late can tell
  // whether a newer gesture has decided the sort since.
  const gesture = useRef(0)
  // The sort a drop switched away from, while no write since that switch has
  // landed: what a failed drop puts back.
  const pendingSwitch = useRef<{ from: SortKey } | null>(null)

  /** A drop of *activeKey* onto *overKey* in any sort, or Alt+Arrow past it.
   *  The row lands where the drop showed it, by the same rule the pinned rows
   *  use: below the target when it moved down, above it when it moved up. */
  const reorderManual = useCallback((activeKey: string, overKey: string) => {
    const from = drawnIndex.get(activeKey) ?? -1
    const to = drawnIndex.get(overKey) ?? -1
    if (from < 0 || to < 0 || from === to) return
    haptic('light')
    const after = from < to
    const prevSort = sortKey
    const mine = ++gesture.current
    if (sortKey !== 'manual') {
      pendingSwitch.current = { from: sortKey }
      showSort('manual')
    }
    const landed = () => { pendingSwitch.current = null }
    // Nothing has landed since a drop switched to Manual: that switch is put
    // back, unless a newer gesture has decided the sort since. The notice then
    // names the sort the list is left in.
    const failDrop = (err: unknown, unsaved = false) => {
      const pendingFrom = pendingSwitch.current?.from
      if (gesture.current === mine && pendingFrom !== undefined) {
        landed()
        showSort(pendingFrom)
      }
      fail(err, { unsaved })
    }
    if (latest.current.known) {
      moveIn(manualOrder, activeKey, overKey, after)
        .then(undoAfter => { landed(); arm(activeKey, prevSort, { kind: 'move', after: undoAfter }) }, err => failDrop(err))
      return
    }
    // Nothing saved yet: the first drag seeds the order on screen with this
    // move in it, and lands only while nothing is saved.
    const seed = movePinnedSession(drawnOrder, activeKey, overKey)
    const undoAfter = drawnOrder[from - 1] ?? null
    let retryPlan: UndoPlan | undefined
    write({ op: 'seed', keys: seed }, seed, held => {
      // Another browser seeded first: its order stands, and this drag moves
      // one row in it before any later gesture writes.
      if (held.code !== 'manual_order_exists') return
      const move = prepareMove(held.order, activeKey, overKey, after)
      retryPlan = { kind: 'move', after: move.undoAfter }
      return move
    })
      // With no follow-up prepared the seed itself failed, so nothing is
      // stored; a failed follow-up leaves another browser's order standing.
      .then(res => { landed(); arm(activeKey, prevSort, retryPlan ?? { kind: 'clear', rev: res.rev, after: undoAfter }) },
        err => failDrop(err, retryPlan === undefined))
  }, [drawnIndex, drawnOrder, manualOrder, sortKey, showSort, moveIn, prepareMove, write, arm, fail])

  /** A sort menu pick. Picking Manual with nothing saved seeds the order on
   *  screen first, so the list does not jump; a stored order, even one saved
   *  by another browser a moment ago, wins while it ranks a live row. */
  const pickSort = useCallback((key: SortKey) => {
    gesture.current += 1
    pendingSwitch.current = null
    if (key === 'manual' && sortKey !== 'manual' && !latest.current.known && drawnOrder.length > 0) {
      const shown = drawnOrder
      write({ op: 'seed', keys: shown }, shown, held => {
        // A stored order of closed sessions only would draw every row
        // unranked: the order on screen goes on top of it instead.
        if (held.code !== 'manual_order_exists') return
        const { localSlots: rows, pinned: pins } = latest.current
        const live = new Set(rows.filter(slot => !pins.has(slot.key)).map(slot => slot.key))
        if (held.order.some(k => live.has(k))) return
        const [top, ...seat] = shown.filter(k => live.has(k))
        if (top === undefined) return
        const created = rows.find(slot => slot.key === top)?.created
        return { body: moveWrite(top, null, true, seat, created), painted: afterManualMove(held.order, top, null, true, seat) }
      }).catch((err: unknown) => {
        // Sent only while no open row is ranked, and nothing that failed
        // changed that.
        if (heldOrderOf(err)?.code !== 'manual_order_exists') fail(err, { unsaved: true })
      })
    }
    showSort(key)
  }, [sortKey, drawnOrder, write, fail, showSort])

  const undo = useCallback((offerId: number) => {
    const current = offerRef.current
    if (!current || current.id !== offerId) return
    gesture.current += 1
    pendingSwitch.current = null
    setOffer(null)
    if (current.prevSort !== 'manual') showSort(current.prevSort)
    const { key, plan } = current
    const moveBack = (order: readonly string[]) => { moveIn(order, key, plan.after, true).catch(fail) }
    if (plan.kind === 'move') { moveBack(latest.current.manualOrder); return }
    write({ op: 'clear', rev: plan.rev }, [], held => {
      // The order changed since the first drag wrote it: clearing would undo
      // that change too, so only this row goes back.
      if (held.code === 'manual_order_changed') return prepareMove(held.order, key, plan.after, true)
    }).catch(fail)
  }, [showSort, moveIn, prepareMove, write, fail])

  const dismiss = useCallback(() => setOffer(null), [])
  // Retire an offer whose row has closed: there is nothing to put back.
  useEffect(() => {
    if (offer && !localSlots.some(slot => slot.key === offer.key)) setOffer(null)
  }, [offer, localSlots])
  // The undo window, on the same held clock as the folder-move bar.
  const [heldOffer, setHeldOffer] = useState<number | null>(null)
  const { remainingMs, paused } = useHeldWindow(offer?.id ?? null, MOVE_UNDO_MS, heldOffer === offer?.id, dismiss)
  const currentId = offer?.id ?? null
  const onHoldChange = useCallback((held: boolean) => setHeldOffer(held ? currentId : null), [currentId])

  return {
    manualRank, drawnIndex, drawnOrder, reorderManual, pickSort, manualOrderError, setManualOrderError: setManualOrderErrorText,
    offer, undo, dismiss, bar: { remainingMs, paused, onHoldChange },
  }
}

/** Alt+Arrow on an unpinned row under Manual (the row offers it only then):
 *  moves it past the next row of its own list and container, the way a drop
 *  does. Off while a search ranks the list. */
export function useManualKeyboardReorder({ searchRanked, drawnOrder, slotFolders, reorderManual }: {
  searchRanked: Map<string, number> | null
  drawnOrder: string[]
  slotFolders: Record<string, string>
  reorderManual: (activeKey: string, overKey: string) => void
}) {
  // Through a ref, for the reason `usePinnedKeyboardReorder` gives: the
  // handler is in the `actions` every row compares.
  const inputs = useRef({ searchRanked, drawnOrder, slotFolders, reorderManual })
  inputs.current = { searchRanked, drawnOrder, slotFolders, reorderManual }
  // A row that moves down is re-inserted into the list, which drops its focus.
  // The next commits put focus back on it (only while nothing else took focus,
  // and for a moment), so a held Alt+Arrow keeps moving the same row.
  const refocus = useRef<{ identity: string; scope: string; until: number } | null>(null)
  useLayoutEffect(() => {
    const wanted = refocus.current
    if (!wanted) return
    if (Date.now() > wanted.until) { refocus.current = null; return }
    const active = document.activeElement
    if (active && active !== document.body) return
    const el = Array.from(document.querySelectorAll<HTMLElement>('[data-session-row]'))
      .find(node => node.dataset.sessionRow === wanted.identity && node.dataset.sessionScope === wanted.scope)
    el?.focus()
  })
  return useCallback((key: string, container: string, delta: -1 | 1, row: HTMLElement) => {
    const { searchRanked: ranked, drawnOrder: order, slotFolders: folders, reorderManual: reorder } = inputs.current
    if (ranked) return
    const target = keyboardReorderTarget(order, key, container, delta, row, folders)
    if (!target) return
    refocus.current = { identity: row.dataset.sessionRow ?? key, scope: row.dataset.sessionScope ?? '', until: Date.now() + 1000 }
    reorder(key, target)
  }, [])
}
