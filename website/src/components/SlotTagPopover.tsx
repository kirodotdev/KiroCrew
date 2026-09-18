import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { X, Check } from 'lucide-react'
import type { ChatTag } from '../types'
import { api } from '../api/client'
import { useAppSelector } from '../store'
import { useTagPopover } from '../hooks/useTagPopover'
import { useImeGuard } from '../hooks/useImeGuard'
import { isTouchDevice } from '../utils/isTouchDevice'
import { Input } from './ui'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'

/**
 * The topmost element at viewport point (x, y) that is NOT `backdrop` or one of
 * its descendants — i.e. what the pointer would hit if the backdrop were not
 * there. `null` when nothing else is under the point or the platform lacks
 * `document.elementsFromPoint` (older engines and non-browser test DOMs).
 */
export function elementBeneath(backdrop: Element, x: number, y: number): Element | null {
  if (typeof document.elementsFromPoint !== 'function') return null
  return document.elementsFromPoint(x, y).find(el => !backdrop.contains(el)) ?? null
}

const sameTagIds = (left: string[], right: string[]) => left.length === right.length
  && left.every((tag, i) => tag === right[i])

/**
 * Server tag revisions are `<16-digit epoch>.<20-digit sequence>-<hex>` and are
 * TOTALLY ordered: the epoch is a persisted counter each gateway process
 * claims at start-up (so a later process always sorts after an earlier one)
 * and the sequence orders commits within a process. Zero padding makes string
 * order equal numeric order. Returns a negative number when `left` is older
 * than `right`, positive when newer, 0 when equal, and null when either side is
 * missing or not in the orderable form (legacy/opaque revisions), in which case
 * callers fall back to equality and recorded lineage. No wall-clock value
 * participates in the order, so a clock step cannot make revisions regress.
 */
const ORDERABLE_REVISION = /^(\d{16}\.\d{20})-/
export const compareRevisions = (
  left: string | null | undefined,
  right: string | null | undefined,
): number | null => {
  if (!left || !right) return null
  const l = ORDERABLE_REVISION.exec(left)
  const r = ORDERABLE_REVISION.exec(right)
  if (!l || !r) return null
  return l[1] < r[1] ? -1 : l[1] > r[1] ? 1 : 0
}

/** True when `left` is provably older than `right` (both orderable). */
const isOlderRevision = (left: string | null | undefined, right: string | null | undefined) => {
  const order = compareRevisions(left, right)
  return order !== null && order < 0
}

/**
 * Whether an incoming frame's revision should REPLACE the accepted snapshot.
 * The accepted revision itself and any orderable-NEWER revision do; an
 * orderable-OLDER revision never does, even when this client has never seen it
 * (a delayed HTTP snapshot landing after a newer WebSocket frame, or a slow
 * reply from the pre-restart gateway process); for non-orderable revisions,
 * anything outside the recorded predecessor lineage is treated as newer. A
 * frame carrying NO revision comes from a gateway that does not mint them
 * (a pre-revision process after a reconnect); it is authoritative, and the
 * callers clear the cached lineage so nothing stale is held against it.
 */
export const shouldSeedAcceptedSnapshot = (
  incomingRevision: string | null,
  acceptedRevision: string | undefined,
  knownPredecessors: string[],
) => {
  if (!acceptedRevision || !incomingRevision || incomingRevision === acceptedRevision) return true
  const order = compareRevisions(incomingRevision, acceptedRevision)
  if (order !== null) return order > 0
  return !knownPredecessors.includes(incomingRevision)
}
interface RejectionPayload {
  code: string | null
  rejectedRevision: string | null
  restoredTags: string[] | null
  restoredRevision: string | null
}

/**
 * What a refused PUT reported in its 409 body: its `code` (`stale_base` when
 * the list was composed onto a revision the slot no longer holds, `session_gone`
 * when the save was refused), the provisional revision it rolled back
 * (`rejected_tags_revision`), and the list + revision the slot holds NOW
 * (`tags`, `tags_revision`) — which is a concurrent writer's commit when one
 * landed while this write was in flight. Duck-typed on `body` rather than
 * `instanceof ApiError` so a mocked `api/client` keeps working.
 */
const readRejection = (error: unknown): RejectionPayload => {
  const none: RejectionPayload = { code: null, rejectedRevision: null, restoredTags: null, restoredRevision: null }
  const body = (error as { body?: unknown } | null)?.body
  if (typeof body !== 'string' || !body.trim().startsWith('{')) return none
  try {
    const parsed = JSON.parse(body) as {
      code?: unknown
      rejected_tags_revision?: unknown
      tags?: unknown
      tags_revision?: unknown
    }
    const rejected = parsed.rejected_tags_revision
    const restored = parsed.tags
    const revision = parsed.tags_revision
    return {
      code: typeof parsed.code === 'string' ? parsed.code : null,
      rejectedRevision: typeof rejected === 'string' && rejected ? rejected : null,
      restoredTags: Array.isArray(restored)
        ? restored.filter((tag): tag is string => typeof tag === 'string')
        : null,
      restoredRevision: typeof revision === 'string' && revision ? revision : null,
    }
  } catch {
    return none
  }
}

/**
 * The single app-wide per-slot tag-assignment popover. Which slot's picker is
 * open comes from the ChatPage-scoped TagPopover context, so any surface (the
 * sidebar row menus or the chat-header menu) opens it via useTagPopover().open
 * and one instance renders for whichever slot is set — nothing when none is.
 */
export default function SlotTagPopover() {
  const { slotKey, close } = useTagPopover()
  const slot = useAppSelector(s => (slotKey ? s.dashboard.slots.find(x => x.key === slotKey) : undefined))
  const queryClient = useQueryClient()
  const ime = useImeGuard()
  const listRef = useRef<HTMLDivElement>(null)
  const { data: tags = [] } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags(), enabled: !!slotKey })

  const setSlotTagsMutation = useMutation({
    mutationFn: ({ slot, nextTags, baseRevision }: { slot: string; nextTags: string[]; baseRevision: string | null }) =>
      baseRevision ? api.setSlotTags(slot, nextTags, baseRevision) : api.setSlotTags(slot, nextTags),
  })
  const createTagMutation = useMutation({
    mutationFn: (name: string) => api.createChatTag(name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
  })

  // Optimistic overlay for the currently-open slot only. `pending` drives the
  // render; `pendingRef` mirrors it so `toggle` reads the latest value
  // synchronously (rapid-burst composition) without a state closure.
  const [pending, setPending] = useState<string[] | null>(null)
  const [writeError, setWriteError] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<{
    baseline: string[]
    desired: string[]
    expectedRevisions: string[]
  } | null>(null)
  const pendingRef = useRef<string[] | null>(null)
  const slotKeyRef = useRef(slotKey)
  slotKeyRef.current = slotKey
  const slotTagsRef = useRef<string[]>(slot?.tags ?? [])
  const slotTagsRevisionRef = useRef<string | null>(slot?.tags_revision ?? null)
  const writeQueuesRef = useRef<Map<string, Promise<boolean>>>(new Map())
  const lastAcceptedTagsRef = useRef<Map<string, string[]>>(new Map())
  const lastAcceptedRevisionRef = useRef<Map<string, string>>(new Map())
  const acceptedPredecessorRevisionsRef = useRef<Map<string, string[]>>(new Map())
  useEffect(() => {
    const slotTags = slot?.tags ?? []
    const tagsRevision = slot?.tags_revision ?? null
    slotTagsRef.current = slotTags
    slotTagsRevisionRef.current = tagsRevision
    if (slotKey && !pendingRef.current) {
      const acceptedRevision = lastAcceptedRevisionRef.current.get(slotKey)
      const knownPredecessors = acceptedPredecessorRevisionsRef.current.get(slotKey) ?? []
      if (shouldSeedAcceptedSnapshot(tagsRevision, acceptedRevision, knownPredecessors)) {
        lastAcceptedTagsRef.current.set(slotKey, [...slotTags])
        if (tagsRevision) {
          if (acceptedRevision && tagsRevision !== acceptedRevision) {
            acceptedPredecessorRevisionsRef.current.set(
              slotKey,
              [...new Set([...knownPredecessors, acceptedRevision])],
            )
          } else if (!acceptedRevision) {
            acceptedPredecessorRevisionsRef.current.set(slotKey, [])
          }
          lastAcceptedRevisionRef.current.set(slotKey, tagsRevision)
        } else {
          lastAcceptedRevisionRef.current.delete(slotKey)
          acceptedPredecessorRevisionsRef.current.delete(slotKey)
        }
      } else {
        // A delayed frame at a revision this slot already knows to be stale
        // arrived after the overlay retired. Rendering it would visibly reverse
        // the confirmed checkmark; re-cover it with the accepted list until the
        // store catches up (its accepted revision) or a newer writer lands.
        const acceptedTags = lastAcceptedTagsRef.current.get(slotKey)
        if (acceptedTags && !sameTagIds(slotTags, acceptedTags)) {
          const desired = [...acceptedTags]
          pendingRef.current = desired
          setPending(desired)
          setConfirmation({
            baseline: [...slotTags],
            desired,
            expectedRevisions: [...knownPredecessors],
          })
        }
      }
    }
  }, [slotKey, slot?.tags, slot?.tags_revision])
  useEffect(() => {
    if (slotKey) {
      const tagsRevision = slotTagsRevisionRef.current
      const acceptedRevision = lastAcceptedRevisionRef.current.get(slotKey)
      const knownPredecessors = acceptedPredecessorRevisionsRef.current.get(slotKey) ?? []
      if (shouldSeedAcceptedSnapshot(tagsRevision, acceptedRevision, knownPredecessors)) {
        lastAcceptedTagsRef.current.set(slotKey, [...slotTagsRef.current])
        if (tagsRevision) {
          if (acceptedRevision && tagsRevision !== acceptedRevision) {
            acceptedPredecessorRevisionsRef.current.set(
              slotKey,
              [...new Set([...knownPredecessors, acceptedRevision])],
            )
          } else if (!acceptedRevision) {
            acceptedPredecessorRevisionsRef.current.set(slotKey, [])
          }
          lastAcceptedRevisionRef.current.set(slotKey, tagsRevision)
        } else {
          lastAcceptedRevisionRef.current.delete(slotKey)
          acceptedPredecessorRevisionsRef.current.delete(slotKey)
        }
      } else {
        const acceptedTags = lastAcceptedTagsRef.current.get(slotKey)
        if (acceptedTags) {
          const desired = [...acceptedTags]
          pendingRef.current = desired
          setPending(desired)
          setConfirmation({
            baseline: [...slotTagsRef.current],
            desired,
            expectedRevisions: [...knownPredecessors],
          })
          setWriteError(null)
          return
        }
      }
    }
    pendingRef.current = null
    setPending(null)
    setConfirmation(null)
    setWriteError(null)
  }, [slotKey])
  useEffect(() => {
    if (!confirmation || pendingRef.current !== confirmation.desired) return
    const slotTags = slot?.tags ?? []
    const tagsRevision = slot?.tags_revision ?? null
    if (tagsRevision && confirmation.expectedRevisions.length > 0) {
      if (confirmation.expectedRevisions.includes(tagsRevision)) return
      // An orderable revision older than the accepted one is a delayed stale
      // snapshot this client never saw — not a concurrent winner. Keep the
      // overlay; only an equal-or-newer revision may retire it.
      const acceptedRevision = slotKey ? lastAcceptedRevisionRef.current.get(slotKey) : undefined
      if (isOlderRevision(tagsRevision, acceptedRevision)) return
    } else {
      const stillBaseline = sameTagIds(slotTags, confirmation.baseline)
      if (stillBaseline) return
    }
    if (slotKey) {
      const acceptedRevision = lastAcceptedRevisionRef.current.get(slotKey)
      const knownPredecessors = acceptedPredecessorRevisionsRef.current.get(slotKey) ?? []
      lastAcceptedTagsRef.current.set(slotKey, [...slotTags])
      if (tagsRevision) {
        if (acceptedRevision && tagsRevision !== acceptedRevision) {
          acceptedPredecessorRevisionsRef.current.set(
            slotKey,
            [...new Set([...knownPredecessors, acceptedRevision])],
          )
        }
        lastAcceptedRevisionRef.current.set(slotKey, tagsRevision)
      } else {
        lastAcceptedRevisionRef.current.delete(slotKey)
        acceptedPredecessorRevisionsRef.current.delete(slotKey)
      }
    }
    pendingRef.current = null
    setPending(null)
    setConfirmation(null)
  }, [confirmation, slotKey, slot?.tags, slot?.tags_revision])

  // Move focus into the tag list on open so keyboard users can operate it
  // (skip on touch to avoid hijacking focus). Deferred a tick so the list has
  // painted its options.
  useEffect(() => {
    if (!slotKey || isTouchDevice()) return
    const t = window.setTimeout(() => {
      listRef.current?.querySelector<HTMLElement>('[data-option]')?.focus()
    }, 0)
    return () => clearTimeout(t)
  }, [slotKey])

  // A right-click on the backdrop is the user pointing at something ELSE (most
  // often another session row), not a request for the browser's own menu. The
  // full-viewport backdrop would otherwise swallow the `contextmenu`: the row's
  // Radix ContextMenuTrigger never sees it and the OS/Electron menu pops instead.
  // So: suppress the native menu, dismiss the picker, and re-issue the same
  // gesture to the element under the pointer so its own Kiro Crew context menu
  // opens fresh. Only the backdrop itself does this — a right-click inside the
  // dialog (e.g. on the "New tag" input) keeps its native menu.
  const onBackdropContextMenu = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return
    e.preventDefault()
    const beneath = elementBeneath(e.currentTarget, e.clientX, e.clientY)
    close()
    if (!beneath) return
    beneath.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, composed: true, view: window,
      clientX: e.clientX, clientY: e.clientY, screenX: e.screenX, screenY: e.screenY,
      button: 2, buttons: 0,
      ctrlKey: e.ctrlKey, shiftKey: e.shiftKey, altKey: e.altKey, metaKey: e.metaKey,
    }))
  }

  if (!slotKey) return null
  const currentTags = new Set(pending ?? slot?.tags ?? [])
  const toggle = (tagId: string) => {
    const base = pendingRef.current ?? slot?.tags ?? []
    const baselineTags = [...slotTagsRef.current]
    const baselineRevision = slotTagsRevisionRef.current
    // The click is a one-tag delta. The overlay applies it to what the user
    // SEES right now; the write applies the same delta, when its turn in the
    // slot's queue comes, to the latest ACCEPTED list (the predecessor write's
    // confirmed result or a newer authoritative frame). A click therefore never
    // ships a stale absolute list: an intent made while an earlier write was
    // still in flight — or after the picker was closed and reopened onto an
    // older frame — composes onto that write's outcome rather than over it.
    const add = !base.includes(tagId)
    const applyDelta = (list: string[]) => add
      ? (list.includes(tagId) ? [...list] : [...list, tagId])
      : list.filter(t => t !== tagId)
    const displayedTags = applyDelta(base)
    pendingRef.current = displayedTags
    setPending(displayedTags)
    setConfirmation(null)
    setWriteError(null)
    const targetSlot = slotKey
    if (!lastAcceptedTagsRef.current.has(targetSlot)) {
      lastAcceptedTagsRef.current.set(targetSlot, [...baselineTags])
      if (baselineRevision) {
        lastAcceptedRevisionRef.current.set(targetSlot, baselineRevision)
        acceptedPredecessorRevisionsRef.current.set(targetSlot, [])
      }
    }
    const priorWrite = writeQueuesRef.current.get(targetSlot) ?? Promise.resolve(true)
    let queuedWrite!: Promise<boolean>
    queuedWrite = priorWrite.then(async canContinue => {
      if (!canContinue) return false
      let previousTags = [...(lastAcceptedTagsRef.current.get(targetSlot) ?? baselineTags)]
      let nextTags = applyDelta(previousTags)
      let acceptedBeforeWriteRevision = lastAcceptedRevisionRef.current.get(targetSlot) ?? null
      // Every revision this client already knows to be stale (the accepted
      // lineage behind the write) is an expected frame, not a concurrent winner:
      // a delayed predecessor frame must neither retire the overlay nor become
      // the next toggle's base and overwrite newer tags.
      const knownPredecessorsBeforeWrite = acceptedPredecessorRevisionsRef.current.get(targetSlot) ?? []
      const expectedRevisions = [...new Set(
        [baselineRevision, acceptedBeforeWriteRevision, ...knownPredecessorsBeforeWrite]
          .filter((revision): revision is string => Boolean(revision)),
      )]
      // The PUT names the revision the list was composed onto. If another
      // client committed since — a frame this client has not received yet —
      // the server refuses under its tag lock and returns the list it holds;
      // the click's delta is re-applied onto THAT list and sent again, so a
      // concurrent tag is never overwritten by an absolute list built on a
      // snapshot that predates it. Bounded: a contended slot converges in a
      // few rounds, and a pathological one surfaces as an ordinary failure.
      const write = async () => {
        for (let attempt = 0; ; attempt++) {
          try {
            return await setSlotTagsMutation.mutateAsync({
              slot: targetSlot, nextTags, baseRevision: acceptedBeforeWriteRevision,
            })
          } catch (error) {
            const rejection = readRejection(error)
            if (
              rejection.code !== 'stale_base' || !rejection.restoredTags || !rejection.restoredRevision
              || attempt >= 4
            ) throw error
            if (acceptedBeforeWriteRevision && !expectedRevisions.includes(acceptedBeforeWriteRevision)) {
              expectedRevisions.push(acceptedBeforeWriteRevision)
            }
            previousTags = [...rejection.restoredTags]
            nextTags = applyDelta(previousTags)
            acceptedBeforeWriteRevision = rejection.restoredRevision
            lastAcceptedTagsRef.current.set(targetSlot, [...previousTags])
            lastAcceptedRevisionRef.current.set(targetSlot, rejection.restoredRevision)
            acceptedPredecessorRevisionsRef.current.set(
              targetSlot,
              [...new Set([...(acceptedPredecessorRevisionsRef.current.get(targetSlot) ?? []), ...expectedRevisions])]
                .filter(revision => revision !== rejection.restoredRevision),
            )
          }
        }
      }
      try {
        const result = await write()
        if (slotKeyRef.current === targetSlot) setWriteError(null)
        const response = result as {
          tags?: unknown
          tags_revision?: unknown
          prior_tags_revision?: unknown
        } | undefined
        const responseTags = response?.tags
        const confirmedTags = Array.isArray(responseTags)
          ? responseTags.filter((tag): tag is string => typeof tag === 'string')
          : nextTags
        const confirmedRevision = typeof response?.tags_revision === 'string'
          ? response.tags_revision
          : null
        const serverPriorRevision = typeof response?.prior_tags_revision === 'string'
          ? response.prior_tags_revision
          : null
        if (serverPriorRevision && !expectedRevisions.includes(serverPriorRevision)) {
          expectedRevisions.push(serverPriorRevision)
        }
        lastAcceptedTagsRef.current.set(targetSlot, [...confirmedTags])
        if (confirmedRevision) {
          lastAcceptedRevisionRef.current.set(targetSlot, confirmedRevision)
          acceptedPredecessorRevisionsRef.current.set(targetSlot, [...expectedRevisions])
        } else {
          lastAcceptedRevisionRef.current.delete(targetSlot)
          acceptedPredecessorRevisionsRef.current.delete(targetSlot)
        }

        // `slotTagsRef`/`slotTagsRevisionRef` track the slot whose picker is
        // OPEN, not necessarily this write's target. If the picker has moved to
        // another slot, classifying them here would cache that slot's tags and
        // revision under the target and a later reopen would persist them. The
        // PUT response already recorded above is the target's accepted
        // snapshot; a frame for the target is reconciled when it is reopened.
        if (slotKeyRef.current !== targetSlot) return true

        // Classify the store's current frame FIRST, regardless of who owns the
        // overlay: a revision outside this write's expected set is a concurrent
        // writer's commit and becomes the accepted snapshot — the base every
        // later queued delta composes onto. Doing this before the ownership
        // return below is what stops a click made while this write was in
        // flight from composing onto a stale list and silently dropping that
        // writer's tag.
        const currentSlotTags = slotTagsRef.current
        const currentSlotRevision = slotTagsRevisionRef.current
        const canCompareRevision = Boolean(currentSlotRevision && expectedRevisions.length > 0)
        const isConfirmed = confirmedRevision && currentSlotRevision
          ? currentSlotRevision === confirmedRevision
          : sameTagIds(currentSlotTags, confirmedTags)
        const olderThanConfirmed = isOlderRevision(currentSlotRevision, confirmedRevision)
        const isExpectedPredecessor = canCompareRevision
          ? expectedRevisions.includes(currentSlotRevision as string) || olderThanConfirmed
          : sameTagIds(currentSlotTags, baselineTags)
            || sameTagIds(currentSlotTags, previousTags)
        if (!isExpectedPredecessor) {
          const acceptedRevision = lastAcceptedRevisionRef.current.get(targetSlot)
          const knownPredecessors = acceptedPredecessorRevisionsRef.current.get(targetSlot) ?? []
          lastAcceptedTagsRef.current.set(targetSlot, [...currentSlotTags])
          if (currentSlotRevision) {
            if (acceptedRevision && currentSlotRevision !== acceptedRevision) {
              acceptedPredecessorRevisionsRef.current.set(
                targetSlot,
                [...new Set([...knownPredecessors, acceptedRevision])],
              )
            }
            lastAcceptedRevisionRef.current.set(targetSlot, currentSlotRevision)
          } else {
            lastAcceptedRevisionRef.current.delete(targetSlot)
            acceptedPredecessorRevisionsRef.current.delete(targetSlot)
          }
        }

        // A newer click owns the overlay. This write still commits in order for
        // its own slot, but it must not overwrite that newer visible intent.
        if (pendingRef.current !== displayedTags) return true
        if (isConfirmed || !isExpectedPredecessor) {
          pendingRef.current = null
          setPending(null)
          setConfirmation(null)
          return true
        }

        // The store still carries an expected predecessor revision/list. Keep
        // the overlay — now the server-confirmed list, which may differ from
        // what the click displayed if the write composed onto a newer base —
        // until the matching result or a concurrent winner arrives.
        const overlay = [...confirmedTags]
        pendingRef.current = overlay
        setPending(overlay)
        setConfirmation({
          baseline: [...currentSlotTags],
          desired: overlay,
          expectedRevisions,
        })
        return true
      } catch (error) {
        // The server may have exposed this write's provisional revision in a
        // slots broadcast before refusing the save. It names that revision in
        // the rejection; treat it as known-stale lineage for the TARGET slot —
        // whether or not that slot's picker is still open — so a frame carrying
        // it (now, later, or on reopen) reads as an expected predecessor to roll
        // back from, never as a newer writer's commit to accept and re-toggle from.
        const { rejectedRevision, restoredTags, restoredRevision } = readRejection(error)
        if (rejectedRevision) {
          if (!expectedRevisions.includes(rejectedRevision)) expectedRevisions.push(rejectedRevision)
          const knownPredecessors = acceptedPredecessorRevisionsRef.current.get(targetSlot) ?? []
          if (lastAcceptedRevisionRef.current.has(targetSlot)) {
            acceptedPredecessorRevisionsRef.current.set(
              targetSlot,
              [...new Set([...knownPredecessors, rejectedRevision])],
            )
          }
        }
        // The 409 also carries the list the slot holds AFTER rollback. That is
        // the authoritative base for anything queued or clicked next: if a
        // concurrent writer committed while this write was in flight, the
        // server restored THEIR list, and composing a retry onto this client's
        // pre-write baseline would silently drop their tags. Seed the accepted
        // snapshot from it now, before the rollback frame has arrived.
        if (restoredTags) {
          lastAcceptedTagsRef.current.set(targetSlot, [...restoredTags])
          if (restoredRevision) {
            const previousAccepted = lastAcceptedRevisionRef.current.get(targetSlot)
            const knownPredecessors = acceptedPredecessorRevisionsRef.current.get(targetSlot) ?? []
            lastAcceptedRevisionRef.current.set(targetSlot, restoredRevision)
            acceptedPredecessorRevisionsRef.current.set(
              targetSlot,
              [...new Set([
                ...knownPredecessors,
                ...(previousAccepted && previousAccepted !== restoredRevision ? [previousAccepted] : []),
                ...expectedRevisions,
              ].filter(revision => revision !== restoredRevision))],
            )
          }
        }
        if (slotKeyRef.current === targetSlot) {
          setWriteError(error instanceof Error && error.message
            ? error.message
            : i18nT('pages.chatPage.unknown_error'))
        } else {
          return false
        }
        // A failed predecessor invalidates every later intent already queued
        // behind it. Roll the active slot back, then propagate cancellation.
        const currentSlotTags = slotTagsRef.current
        const currentSlotRevision = slotTagsRevisionRef.current
        const canCompareRevision = Boolean(currentSlotRevision && expectedRevisions.length > 0)
        // Any store revision older than this write's (rejected) provisional
        // revision predates it and is a predecessor to roll back from.
        const olderThanRejected = isOlderRevision(currentSlotRevision, rejectedRevision)
        const isExpectedPredecessor = canCompareRevision
          ? expectedRevisions.includes(currentSlotRevision as string) || olderThanRejected
          : sameTagIds(currentSlotTags, baselineTags)
            || sameTagIds(currentSlotTags, previousTags)
        if (!isExpectedPredecessor) {
          const acceptedRevision = lastAcceptedRevisionRef.current.get(targetSlot)
          const knownPredecessors = acceptedPredecessorRevisionsRef.current.get(targetSlot) ?? []
          lastAcceptedTagsRef.current.set(targetSlot, [...currentSlotTags])
          if (currentSlotRevision) {
            if (acceptedRevision && currentSlotRevision !== acceptedRevision) {
              acceptedPredecessorRevisionsRef.current.set(
                targetSlot,
                [...new Set([...knownPredecessors, acceptedRevision])],
              )
            }
            lastAcceptedRevisionRef.current.set(targetSlot, currentSlotRevision)
          } else {
            lastAcceptedRevisionRef.current.delete(targetSlot)
            acceptedPredecessorRevisionsRef.current.delete(targetSlot)
          }
          pendingRef.current = null
          setPending(null)
          setConfirmation(null)
          return false
        }

        const fallbackTags = [...(lastAcceptedTagsRef.current.get(targetSlot) ?? baselineTags)]
        if (sameTagIds(currentSlotTags, fallbackTags)) {
          pendingRef.current = null
          setPending(null)
          setConfirmation(null)
          return false
        }
        pendingRef.current = fallbackTags
        setPending(fallbackTags)
        setConfirmation({
          baseline: [...currentSlotTags],
          desired: fallbackTags,
          // The store's current frame and every revision this write already
          // knows to be stale (including its own rejected provisional one)
          // must keep the rollback overlay; only a genuinely new revision, or
          // a list matching the fallback, may retire it.
          expectedRevisions: [...new Set(
            [currentSlotRevision, ...expectedRevisions]
              .filter((revision): revision is string => Boolean(revision)),
          )],
        })
        return false
      }
    }).finally(() => {
      if (writeQueuesRef.current.get(targetSlot) === queuedWrite) {
        writeQueuesRef.current.delete(targetSlot)
      }
    })
    writeQueuesRef.current.set(targetSlot, queuedWrite)
  }

  return (
    <div role="button" tabIndex={0} aria-label={i18nT('components.slotTagPopover.close_tag_picker')}
      className="fixed inset-0 z-[9999]"
      onClick={e => { if (e.target === e.currentTarget) close() }}
      onContextMenu={onBackdropContextMenu}
      onKeyDown={e => {
        // Only handle keys originating directly on the backdrop — events
        // bubbling up from inner dialog buttons/inputs must not dismiss it.
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'Escape') { e.preventDefault(); close() }
      }}>
      {/* Both handlers below belong to the modal container, not to a control the
          user activates: `stopPropagation` keeps a click inside the dialog from
          reaching the backdrop's dismiss, and `onKeyDown` gives Escape a home
          when focus sits on an inner button whose events the backdrop ignores. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- dialog-level dismissal plumbing; the operable controls are the menuitems and the input inside */}
      <div role="dialog" aria-modal="true" aria-label={i18nT('components.slotTagPopover.assign_tags')} data-testid="slot-tag-picker"
        className="absolute bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px]"
        style={{ left: '50%', top: '30%', transform: 'translate(-50%, 0)' }}
        onClick={e => e.stopPropagation()}
        onKeyDown={e => { if (e.key === 'Escape') close() }}>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-semibold text-muted uppercase tracking-wider px-1">{i18nT('components.slotTagPopover.assign_tags')}</span>
          <button type="button" className="text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none" onClick={close} aria-label={i18nT('components.slotTagPopover.close')}><X size={13} /></button>
        </div>
        {/* tabIndex=-1: the roving-focus menu owns the arrow keys, so it must be
            able to hold focus itself (and be a focus target when it has no
            options yet) without ever joining the tab order — the menuitems are
            reached with the arrows, not with Tab. */}
        <div ref={listRef} role="menu" tabIndex={-1} aria-label={i18nT('components.slotTagPopover.tags')} className="flex flex-col gap-0.5 max-h-[260px] overflow-y-auto"
          onKeyDown={e => {
            // Roving focus across the tag options. Deliberately does NOT handle
            // Tab (keeps the "New tag" input reachable) or Escape (the dialog
            // handles that); Enter/Space activate the focused button natively.
            const opts = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[data-option]') ?? [])
            if (opts.length === 0) return
            const i = opts.indexOf(document.activeElement as HTMLElement)
            if (e.key === 'ArrowDown') { e.preventDefault(); (opts[i + 1] ?? opts[0]).focus() }
            else if (e.key === 'ArrowUp') { e.preventDefault(); (opts[i - 1] ?? opts[opts.length - 1]).focus() }
            else if (e.key === 'Home') { e.preventDefault(); opts[0].focus() }
            else if (e.key === 'End') { e.preventDefault(); opts[opts.length - 1].focus() }
          }}>
          {tags.length === 0 && <div className="text-muted px-2 py-1 text-[12px]">{i18nT('components.slotTagPopover.no_tags_yet_create_one_below')}</div>}
          {[...tags].sort((a, b) => a.order - b.order).map(t => {
            const on = currentTags.has(t.id)
            return (
              <button key={t.id} role="menuitemcheckbox" aria-checked={on} type="button" data-option tabIndex={-1}
                className={`flex items-center gap-2 px-2 py-1 rounded text-left cursor-pointer bg-transparent border-none transition-all ${on ? 'bg-accent-subtle text-text-strong' : 'text-text hover:bg-bg-hover'}`}
                onClick={() => toggle(t.id)}>
                <span className="w-3 h-3 rounded-sm border border-border shrink-0" style={{ background: t.color }} />
                <span className="flex-1 truncate">{t.name}</span>
                {on && <span className="text-accent"><Check size={11} /></span>}
              </button>
            )
          })}
        </div>
        {/* No hand-off: the adjacent new-tag input is an uncontrolled draft, so
            navigating away for agent hand-off would discard unsaved text. */}
        <ErrorNotice
          message={writeError}
          variant="inline"
          onDismiss={() => setWriteError(null)}
          className="mt-2 px-1"
        />
        <div className="mt-2 border-t border-border pt-2 flex items-center gap-1">
          <Input
            className="flex-1 text-[12px] py-1"
            placeholder={i18nT('components.slotTagPopover.new_tag')}
            {...ime.bindEnter<HTMLInputElement>({
              onEnter: () => {
                const el = document.activeElement as HTMLInputElement | null
                const name = (el?.value || '').trim()
                if (!name) return
                createTagMutation.mutate(name)
                if (el) el.value = ''
              },
              onEscape: close,
              onBlur: () => {},
            })}
          />
        </div>
      </div>
    </div>
  )
}
