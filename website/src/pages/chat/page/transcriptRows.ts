import { useCallback, useLayoutEffect, useMemo, useRef, type MutableRefObject } from 'react'

import { useLatchedRunning } from '../../../hooks/useLatchedRunning'
import { useSlotDeferredValue } from '../../../hooks/useSlotDeferredValue'
import { mcpAppKey } from '../../../store/chatSlice'
import type { ChatMessage } from '../../../types'
import { anchorAltIdFor, stableAnchorIdFor, uniqueRowKeys, virtualKeyFor } from '../ChatPageMessageContent'
import { applyRunningState, createTurnGrouper, isTurnEnd, TURN_OPENER_ROLES } from '../groupDisplayItems'
import type { DisplayItem, TurnItem } from '../types'
import { useBubbleVanishProbe } from '../useBubbleVanishProbe'
import type { useScrollManager } from '../useScrollManager'

interface TranscriptRowsOptions {
  messages: ChatMessage[]
  slotRunning: boolean
  activeSlot: string | null
  /** Tool-call ids with a live MCP App payload in this slot. */
  appToolCallIds: ReadonlySet<string>
  /** Kept on the rendered rows for the pinned-prompt recompute. */
  displayItemsRef: MutableRefObject<DisplayItem[]>
  scrollerRef: ReturnType<typeof useScrollManager>['scrollerRef']
}

/**
 * The rows the transcript's virtualizer draws: turn grouping (identity-reusing),
 * the display layer's latched running flag, the slot-scoped deferral that keeps
 * typing responsive while a landed page renders, and the promotion that keeps an
 * MCP App iframe in one stable turn.
 */
export function useTranscriptRows({ messages, slotRunning, activeSlot, appToolCallIds, displayItemsRef, scrollerRef }: TranscriptRowsOptions) {
  // Grouping depends ONLY on `messages`; `slotRunning` decides one boolean on the
  // trailing turn. Bundling both in one memo re-ran the whole O(N) grouping pass on
  // every turn start/stop just to flip that flag, and the new identity cascaded into
  // messageToDisplayIdx / visibleIndexMap / the virtualizer. Split: group once, then
  // apply the flag in O(1).
  //
  // The grouper is the per-page identity cache (see createTurnGrouper): each
  // streaming flush replaces `messages`, so this memo re-runs per flush — the
  // grouper reconciles against the previous result so settled turns keep their
  // object identity and memo(TurnBlock) / mergeTurnThinking bail out.
  const groupTurns = useMemo(() => createTurnGrouper(), [])
  const groupedTurns = useMemo(() => groupTurns(messages), [groupTurns, messages])

  // LATCHED running for the DISPLAY layer only, scoped to the slot that raised
  // it: the flap it absorbs is one session's own broadcast, and a latch carried
  // across a switch paints the incoming transcript's steps unfolded for the
  // whole window. See useLatchedRunning.
  const runningLatched = useLatchedRunning(activeSlot, !!slotRunning)
  const displayItems = useMemo<DisplayItem[]>(
    () => applyRunningState(groupedTurns, runningLatched),
    [groupedTurns, runningLatched],
  )
  // The transcript render is the page's heaviest tree (a landed page regroups
  // 1500+ messages and remounts a window of rich rows), and rendering it at
  // urgent priority is what freezes composer input and every main-thread
  // animation during a landing. Deferring the VIRTUALIZER's input marks that
  // whole subtree as interruptible: urgent updates (typing, button states,
  // spinners) commit against the previous list, and the regrouped list renders
  // when the main thread has room. Everything that must agree with the
  // RENDERED rows (the DOM-index ref, row keys, the prefetch index) reads the
  // deferred value, so index spaces stay consistent.
  //
  // Scoped to the active slot: a plain useDeferredValue keeps returning the
  // PREVIOUS list until the background render lands, and under the page's
  // urgent churn that is hundreds of ms -- long enough that a session switch
  // painted the outgoing tab's transcript under the incoming tab's URL, and a
  // new chat's first send briefly showed the previous session's messages
  // (#8526). Only same-slot updates (streaming flushes, history landings) are
  // deferred; a switch renders the right transcript in its first commit.
  // One deferred frame carries both the rows the virtualizer draws and the
  // messages they came from, so every deferred reader (the transcript, the
  // turn minimap, the Navigation tab) sees the same snapshot by construction.
  const liveTranscript = useMemo(() => ({ messages, displayItems }), [messages, displayItems])
  const renderedTranscript = useSlotDeferredValue(activeSlot, liveTranscript)
  // MCP App payloads live outside the message list, so promote after the
  // transcript defer: the FIRST render that can draw an iframe must already use
  // the same TurnBlock subtree that later grouping will keep. Short turns are
  // emitted as loose siblings, so promote the WHOLE loose turn region rather
  // than each app row separately; otherwise a later merge reparents every app
  // after the first and reloads its iframe. The boundaries mirror the grouper:
  // opener rows start a turn, persisted assistant-final rows end one, and an
  // already-grouped turn is its own region. The app-anchor latch below gives
  // the synthetic turn the same key as its first rendered app row.
  const renderedDisplayItems = useMemo<DisplayItem[]>(() => {
    const items = renderedTranscript.displayItems
    if (appToolCallIds.size === 0) return items

    const next: DisplayItem[] = []
    let looseItems: TurnItem[] = []
    let looseHasApp = false
    let promoted = false

    const flushLooseTurn = () => {
      if (looseItems.length === 0) return
      if (looseHasApp) {
        next.push({ kind: 'turn', items: looseItems, complete: !runningLatched })
        promoted = true
      } else {
        next.push(...looseItems)
      }
      looseItems = []
      looseHasApp = false
    }

    for (const item of items) {
      if (item.kind === 'turn') {
        flushLooseTurn()
        next.push(item)
        continue
      }
      if (item.kind === 'single' && TURN_OPENER_ROLES.has(item.msg.role)) {
        flushLooseTurn()
        next.push(item)
        continue
      }

      looseItems.push(item)
      if (item.kind === 'single' && item.msg.role === 'tool') {
        const toolCallId = item.msg.meta?.tool_call_id
        if (typeof toolCallId === 'string' && appToolCallIds.has(toolCallId)) looseHasApp = true
      }
      if (item.kind === 'single' && isTurnEnd(item.msg)) flushLooseTurn()
    }
    flushLooseTurn()
    return promoted ? next : items
  }, [renderedTranscript.displayItems, appToolCallIds, runningLatched])

  // Keep the ref in sync so handleRangeChanged / updatePinnedPrompt
  // read the latest displayItems. useLayoutEffect (not useEffect): the DOM's
  // `data-display-index` attributes are updated at commit, but a scroll rAF can
  // fire before React flushes a PASSIVE effect — so with useEffect the pin
  // recompute could read fresh DOM indices against a stale list, mis-deriving
  // `pinned.idx` by one row (the row-hide is identity-keyed as a second guard,
  // see ChatPage's row map). A layout effect runs in the commit phase, before that rAF, so
  // the ref is caught up by the time the recompute reads it. Still a passive
  // side effect, not render-body mutation, so React's rules of render hold.
  useLayoutEffect(() => { displayItemsRef.current = renderedDisplayItems }, [renderedDisplayItems, displayItemsRef])

  // Opt-in #7045 diagnostic: log store-vs-render counts whenever the number of
  // mounted transcript rows drops (see useBubbleVanishProbe). Off (and free)
  // unless the localStorage flag is set.
  const messagesLenRef = useRef(0)
  useLayoutEffect(() => { messagesLenRef.current = messages.length }, [messages])
  const bubbleProbeCounts = useCallback(
    () => ({ store: messagesLenRef.current, display: displayItemsRef.current.length }),
    [displayItemsRef],
  )
  useBubbleVanishProbe(scrollerRef, bubbleProbeCounts, activeSlot)
  return { renderedTranscript, renderedDisplayItems }
}

/**
 * Row identity for the virtualizer and its height cache: stable per-message
 * keys, the anchors that survive a landing's key reshuffle, the prefetch index,
 * and the app-bearing turn's latched key.
 */
export function useStableRowKeys({ renderedDisplayItems, appToolCallIds, activeSlot }: {
  renderedDisplayItems: DisplayItem[]
  appToolCallIds: ReadonlySet<string>
  activeSlot: string | null
}) {
  // Per-message identity used to derive BOTH the inner bubble key (ChatPage's
  // renderMessage) AND the virtualizer/HeightCache key (virtualKey, below). Keeping
  // them on the SAME identity means the steer-bubble stability fix protects
  // the virtualizer + HeightCache layer too, not just the bubble:
  //   1. Prefer meta.clientTs — the steer_push echo overwrites `ts` (client→
  //      server) mid-stream; keying on `ts` alone would flip the key, orphan the
  //      cached height, revert the row to the estimate, and lurch the viewport.
  //   2. Fall back to `ts` for ordinary messages.
  //   3. For ts-less messages (e.g. an error appended on the send-failure path)
  //      DON'T fall back to the array index: truncateAfterIndex / regenerate
  //      would shift the key of every following row → mass remount + a large
  //      scroll swing. Mint a per-message-instance id instead. Object identity
  //      is stable across renders under Immer's structural sharing, and survives
  //      truncation of *later* rows, so the key is stable for the message's life.
  //      (A durable id stamped in the reducer at append would also survive a full
  //      refetch/replace.)
  const msgIdSeq = useRef(0)
  const msgIds = useRef(new WeakMap<ChatMessage, string>())
  const stableMsgKey = useCallback((m: ChatMessage): string => {
    const explicit = (m.meta?.clientTs as string | undefined) || m.ts
    if (explicit) return explicit
    let id = msgIds.current.get(m)
    if (!id) { id = `mid-${msgIdSeq.current++}`; msgIds.current.set(m, id) }
    return id
  }, [])
  const stableAnchorId = useCallback(
    (it: DisplayItem, index: number) => stableAnchorIdFor(it, index, stableMsgKey),
    [stableMsgKey],
  )
  const anchorAltId = useCallback(
    (it: DisplayItem, index: number) => anchorAltIdFor(it, index, stableMsgKey),
    [stableMsgKey],
  )
  // The prefetch contract, verbatim from the user: "start loading while I am
  // still two USER MESSAGES from the top" — messages they sent, not any two
  // display rows (a row can be a nudge, a tool group, a lone card). Resolve
  // the display index holding the SECOND user-authored message from the top of
  // the loaded transcript; the virtualizer fires the older-history fetch on
  // the downward crossing of that index.
  const prefetchStartIndex = useMemo(() => {
    const holdsUser = (t: TurnItem): boolean =>
      t.kind === 'single' ? t.msg.role === 'user' : t.msgs.some((m) => m.role === 'user')
    let seen = 0
    for (let i = 0; i < renderedDisplayItems.length; i++) {
      const it = renderedDisplayItems[i]
      const has = it.kind === 'turn' ? it.items.some(holdsUser) : holdsUser(it)
      if (has && ++seen === 2) return i
    }
    return undefined
  }, [renderedDisplayItems])
  // An inline MCP App makes one row stateful: remounting its iframe discards
  // in-canvas work. A running turn normally keys on its lead, but a later
  // reasoning burst can become that lead. Once an app payload exists, anchor
  // the turn to the first app payload observed in that turn. `appToolCallIds`
  // preserves the insertion order of chat.mcpApps, so an earlier transcript
  // row whose slower payload arrives later cannot steal the anchor and remount
  // an app already on screen. The session-scoped tool-call id selected here
  // becomes the turn's latch id: its transcript row outlives the bounded render
  // payload, so retention eviction cannot promote a later app and re-key the
  // turn. The latched value uses an `mcp-app:` namespace followed by that
  // session-scoped id. Ordinary row keys use other prefixes, so a history
  // prepend cannot collide with this key and make `uniqueRowKeys` suffix it.
  // Rebuilding the map from the rendered turns drops a latch as soon as its
  // turn disappears.
  const appAnchorByTurnId = useRef(new Map<string, string>())
  const rowKeys = useMemo(() => {
    // Preserve the ordinary transcript's original O(display rows) path. The
    // deeper turn-item scan is needed only while selecting or retaining an app
    // anchor.
    if (appAnchorByTurnId.current.size === 0 && appToolCallIds.size === 0) {
      return uniqueRowKeys(renderedDisplayItems, stableMsgKey)
    }
    const previousAnchors = appAnchorByTurnId.current
    const retainedAnchors = new Map<string, string>()
    const keys = uniqueRowKeys(renderedDisplayItems, stableMsgKey, (it) => {
      if (it.kind !== 'turn' || !activeSlot) return undefined

      // Retention can remove the payload that selected this anchor. Find the
      // turn's latch from its still-rendered tool row before consulting the
      // bounded live-payload set.
      for (const row of it.items) {
        if (row.kind !== 'single') continue
        const toolCallId = row.msg.meta?.tool_call_id
        if (typeof toolCallId !== 'string' || !toolCallId) continue
        const turnId = mcpAppKey(activeSlot, toolCallId)
        const anchor = previousAnchors.get(turnId)
        if (anchor) {
          retainedAnchors.set(turnId, anchor)
          return anchor
        }
      }

      for (const appToolCallId of appToolCallIds) {
        for (const row of it.items) {
          if (row.kind !== 'single') continue
          if (row.msg.meta?.tool_call_id === appToolCallId) {
            const turnId = mcpAppKey(activeSlot, appToolCallId)
            const anchor = `mcp-app:${turnId}`
            retainedAnchors.set(turnId, anchor)
            return anchor
          }
        }
      }
      return undefined
    })
    appAnchorByTurnId.current = retainedAnchors
    return keys
  }, [renderedDisplayItems, stableMsgKey, appToolCallIds, activeSlot])
  // Index lookup into the deduped list, so this getKey prices an item
  // correctly ONLY against the displayItems of its own render. Live consumers
  // pair getKeyRef with itemsRef from the same tick; the one stale-ITEMS
  // consumer — the prepend anchor capture — snapshots getKey ALONGSIDE the
  // previous items (see prependPrevRef in useVirtualChat). The window-shift /
  // tail-append captures read previous-commit DOM indices through the current
  // render, which stays correct in the shapes they fire on (indices before
  // the change point keep both item and bare key). The fallback covers only
  // an out-of-range probe.
  const virtualKey = useCallback(
    (it: DisplayItem, i: number) => rowKeys[i] ?? virtualKeyFor(it, i, stableMsgKey),
    [rowKeys, stableMsgKey],
  )
  return { stableMsgKey, stableAnchorId, anchorAltId, prefetchStartIndex, virtualKey }
}
