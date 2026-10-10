/** The crew chat window: a session that lives on a connected crew, shown and
 *  driven through the hub's proxy onto the PEER's own chat API.
 *
 *  Nothing here is stored on the hub. The transcript, the running flag and the
 *  pending approval are the peer's, read through `/api/instances/{id}/proxy/`,
 *  and every action (send, stop, approve, continue, regenerate, rewind) calls
 *  the peer's own route for its own slot. The hub's proxy redacts every reply
 *  before it reaches this component, so peer text renders as delivered.
 *
 *  The rows and the composer are the dashboard's shared ones (the transcript
 *  list and row set a split pane draws, the composer the side panel mounts),
 *  fed with the peer's slot data, so tool lines, attachments and new row types
 *  reach a crew session without a copy here. What stays here is what only a
 *  peer has: the proxied reads, the peer's event feed, and the peer routes
 *  every action posts to (see `crewWindowRenderers.tsx` for the rows a hub
 *  must not draw from its own state). */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Server, X } from 'lucide-react'
import { api } from '../../../api/client'
import { crewPeerUrl } from '../../../api/client/instances'
import { useAppSelector } from '../../../store'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import Glass from '../../../components/Glass'
import ChatInput from '../../../components/ChatInput'
import ChatMessageList from '../../../app-sdk/ChatMessageList'
import ChatFooter from '../ChatFooter'
import { ReadOnlyCodeCtx } from '../../../components/markdown/contexts'
import { SlotProvider } from '../../../providers/SlotContext'
import { i18nT } from '../../../i18n/t'
import { errMessage } from '../../../utils/thunkError'
import type { ChatMessage } from '../../../types'
import { closeCrewWindow, coverSiblings, markCrewWindowShown, readCrewDraft, subscribeCrewDraft, writeCrewDraft, type CrewWindowTarget } from './crewWindowStore'
import { createCrewWindowRenderers, crewWindowSlot } from './crewWindowRenderers'
import { useCrewWindowModelPicker, type PeerModelFields } from './crewWindowModelPicker'
import { mergeRecoveredDraft } from '../../../utils/chatDrafts'
import { isHiddenInvisibleAssistantRow } from '../../../utils/invisibleText'
import { isSystemNoticeRow } from '../CompactionCard'
import { useMessageQuote } from '../../../chat-core/composer/useMessageQuote'
import type { MessageQuote } from '../../../chat-core/composer/messageQuote'

interface PeerApproval { origin?: string; request_id?: string; request_mid?: string }
interface PeerSlot extends PeerModelFields { key?: string; title?: string; running?: boolean; interrupted?: boolean; agent?: string; pending_approval_info?: PeerApproval | null }
interface PeerDetail { title?: string; running?: boolean; messages?: ChatMessage[] }

/** How long a burst of peer frames waits before one transcript re-read. */
const REFETCH_THROTTLE_MS = 300
/** How often an unmatched (or unreachable) crew's version is re-read. */
const CAPS_RETRY_MS = 5000
const NO_MESSAGES: ChatMessage[] = []

/** Whether the hub redacted part of this text (its proxy marker). */
function hasRedaction(text?: string): boolean {
  return !!text?.includes('[REDACTED')
}

/** A peer row without the records that act on THIS gateway: the decision
 *  strip's verdict thumbs (on a reply, a steered user row, a compaction
 *  notice) write its decision records, and file-change chips open its
 *  files. So no peer row may draw either. */
function withoutHubRecords(m: ChatMessage): ChatMessage {
  const meta = m.meta
  if (m.decisions_strip === undefined && meta?.decisions_strip === undefined && meta?.file_changes === undefined && meta?.file_changes_omitted_files === undefined) return m
  const { decisions_strip: _row, ...rest } = m
  if (!meta) return rest
  const { decisions_strip: _strip, file_changes: _files, file_changes_omitted_files: _omitted, ...kept } = meta
  return { ...rest, meta: kept }
}

export default function CrewChatWindow({ target, onClose = closeCrewWindow }: { target: CrewWindowTarget; onClose?: () => void }) {
  const { instanceId, key } = target
  const queryClient = useQueryClient()
  const slotPath = 'api/chat/slots/' + encodeURIComponent(key)
  const slotKeyQ = useMemo(() => ['crew-window', instanceId, key, 'slot'] as const, [instanceId, key])
  const detailKeyQ = useMemo(() => ['crew-window', instanceId, key, 'detail'] as const, [instanceId, key])
  const instancesQ = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances() })
  const name = instancesQ.data?.instances.find(i => i.id === instanceId)?.name || instanceId
  // A tunnel that is down answers `version: ""`, which reads as a mismatch;
  // re-ask until the versions match so a reconnect unlocks the window.
  const capsQ = useQuery({
    queryKey: ['instance-caps', instanceId], queryFn: () => api.instancesCapabilities(instanceId),
    refetchInterval: q => (q.state.data?.version_match === true ? false : CAPS_RETRY_MS),
  })
  const versionOk = capsQ.data?.version_match === true
  // The tunnel's own state, not the warm map: `warm` tracks only crews whose
  // dashboard pane is kept loaded, and the viewport evicts past its cap while
  // the tunnel stays up.
  const warm = useAppSelector(s => !!s.instances?.warm?.[instanceId])
  const tunnelUp = instancesQ.data?.instances.find(i => i.id === instanceId)?.status?.state === 'connected'
  const connected = (warm || tunnelUp) && versionOk

  // The peer's slot row carries `running` and the pending approval; the
  // detail carries the transcript. Both are the peer's own answers.
  const slotQ = useQuery({
    queryKey: slotKeyQ,
    queryFn: async () => ((await api.crewPeerGet(instanceId, 'api/chat/slots')) as PeerSlot[]).find(s => s.key === key) ?? null,
    enabled: versionOk,
  })
  const detailQ = useQuery({
    queryKey: detailKeyQ,
    queryFn: () => api.crewPeerGet(instanceId, slotPath + '?limit=200') as Promise<PeerDetail>,
    enabled: versionOk,
  })

  // Live updates from the peer's event feed, held ONLY while this window is
  // open: the feed carries the peer's whole broadcast, not just this session.
  const [feedLost, setFeedLost] = useState(false)
  const [feedGen, setFeedGen] = useState(0)
  useEffect(() => {
    if (!versionOk) return
    let timer: ReturnType<typeof setTimeout> | undefined
    const refetch = () => {
      if (timer) return
      timer = setTimeout(() => {
        timer = undefined
        void queryClient.refetchQueries({ queryKey: detailKeyQ }, { cancelRefetch: false })
      }, REFETCH_THROTTLE_MS)
    }
    const es = new EventSource(crewPeerUrl(instanceId, 'api/stream'))
    es.onopen = () => setFeedLost(false)
    // The proxy answers a down tunnel with JSON, which EventSource refuses
    // for good (CLOSED); a transient drop it retries by itself.
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setFeedLost(true) }
    es.addEventListener('slots', (e: MessageEvent) => {
      try {
        const rows = JSON.parse(e.data) as PeerSlot[]
        const row = Array.isArray(rows) ? rows.find(s => s.key === key) : undefined
        if (row) {
          queryClient.setQueryData(slotKeyQ, row)
          refetch()
        }
      } catch { /* a malformed frame changes nothing */ }
    })
    es.addEventListener('chat_message', (e: MessageEvent) => {
      try {
        // Finished rows only: the peer never puts streamed `chunk` rows on
        // this feed, so a re-read per frame is one per finished message.
        if ((JSON.parse(e.data) as { slot?: string }).slot === key) refetch()
      } catch { /* a malformed frame changes nothing */ }
    })
    return () => {
      es.close()
      if (timer) clearTimeout(timer)
    }
  }, [instanceId, key, queryClient, slotKeyQ, detailKeyQ, feedGen, versionOk])

  const [draft, setDraftState] = useState(() => readCrewDraft(target))
  const setDraft = useCallback((value: string) => {
    writeCrewDraft(target, value)
    setDraftState(value)
  }, [target])
  const [rewindTs, setRewindTs] = useState<string | null>(null)
  // Quote, as in the local chat: a staged whole message rides the next send
  // as its block plus `meta.quote`. Text only, so it works on the peer.
  const messageQuote = useMessageQuote({ slot: crewWindowSlot(instanceId, key) })
  // A rewind's edit is held beside the draft, never in it, so the session's
  // own unsent text survives a close or a rejected rewind untouched.
  const [rewindText, setRewindText] = useState('')
  // The held rewind edit as of now: the latest render's, or one staged since
  // in the same tick. Read when a refusal lands or another edit arrives.
  const heldRewind = useRef({ ts: rewindTs, text: rewindText })
  heldRewind.current = { ts: rewindTs, text: rewindText }
  /** Fold text into the session's ordinary draft (persisted), beside what is there. */
  const recoverIntoDraft = useCallback((text: string) => {
    const next = mergeRecoveredDraft(readCrewDraft(target), text)
    writeCrewDraft(target, next)
    setDraftState(next)
  }, [target])
  // A failed send from an earlier mount of this session writes the store;
  // follow it so the reopened composer shows the recovered text.
  useEffect(() => subscribeCrewDraft(target, setDraftState), [target])
  useEffect(() => markCrewWindowShown(onClose), [onClose])
  // Whether this window is still on screen: a refusal that lands after it
  // closed has no quote stage to restore into.
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  // The dock's height, so the transcript's last row clears the glass.
  const dockRef = useRef<HTMLDivElement>(null)
  const [dockH, setDockH] = useState(0)
  useEffect(() => {
    const el = dockRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setDockH(el.offsetHeight))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const settle = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: slotKeyQ })
    void queryClient.invalidateQueries({ queryKey: detailKeyQ })
  }, [queryClient, slotKeyQ, detailKeyQ])
  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: object; message?: string; rewindTs?: string | null; typed?: string; quote?: MessageQuote | null }) =>
      api.crewPeerPost(instanceId, path, body),
    onSettled: settle,
    // Mutation-level, not per-call: it still runs when the window closed or
    // switched while the send was in flight, so the text is never lost.
    onError: (_err, vars) => {
      if (!vars.message) return
      // Back into the rewind edit (ts and text together), unless a newer
      // rewind edit is already held: then the refused text joins the ordinary
      // draft, so neither edit is lost and no ts is paired with another
      // row's text.
      // Only an open window can hold the rewind; after close the edit goes
      // to the saved draft below instead of into unmounted state.
      if (vars.rewindTs && mounted.current && !heldRewind.current.ts) {
        heldRewind.current = { ts: vars.rewindTs, text: vars.message }
        setRewindTs(vars.rewindTs)
        setRewindText(vars.message)
        return
      }
      // A refused quoted send puts the quote back on the stage (or, when a
      // newer one is staged, its block into the draft), never a raw block alone.
      const text = vars.quote ? messageQuote.recoverInto(vars.typed ?? '', vars.quote, mounted.current) : vars.message
      if (text.trim()) recoverIntoDraft(text)
    },
  })
  const send = () => {
    const typed = (rewindTs ? rewindText : draft).trim()
    if ((!typed && (rewindTs || !messageQuote.pendingQuote)) || action.isPending || !connected) return
    // A held rewind waits for an idle peer: mid-turn the peer refuses it.
    if (rewindTs && running) return
    const { quote, text: message } = rewindTs ? { quote: null, text: typed } : messageQuote.consume(typed)
    const req = rewindTs
      ? { path: slotPath + '/rewind', body: { ts: rewindTs, content: message } }
      : { path: 'api/chat?ws=1', body: { message, slot: key, ...(quote ? { meta: { quote } } : {}) } }
    // Cleared at dispatch so text typed while the send is in flight is never
    // wiped by its success; a failure restores it only into an empty box.
    if (rewindTs) setRewindText('')
    else setDraft('')
    setRewindTs(null)
    action.mutate({ ...req, message, rewindTs, typed, quote })
  }

  const running = slotQ.data?.running ?? detailQ.data?.running ?? false
  const approval = slotQ.data?.pending_approval_info
  // Only a native approval names its transcript row, and the peer's strict
  // check needs that row's id so a stale card cannot decide a newer request.
  const nativeApproval = approval?.origin === 'native' && approval.request_id && approval.request_mid ? approval : null
  // The shared permission row answers by its `approval_id`. A peer reuses
  // request ids, so the id alone cannot say WHICH request a card showed: each
  // row's own `meta.mid` rides in the id it hands back, and only the row the
  // peer reports pending (same id AND same mid) is answered, with that mid. A
  // stale card for a reused id makes no peer call.
  const messages = useMemo(() => (detailQ.data?.messages ?? NO_MESSAGES).map(withoutHubRecords).map(m => {
    const aid = m.meta?.approval_id
    if (m.role !== 'permission' || typeof aid !== 'string') return m
    const mid = typeof m.meta?.mid === 'string' ? m.meta.mid : ''
    return { ...m, meta: { ...m.meta, approval_id: JSON.stringify([aid, mid]) } }
  }), [detailQ.data?.messages])
  const approve = useCallback((approvalId: string, decision: string) => {
    let row: unknown
    try { row = JSON.parse(approvalId) } catch { row = null }
    const [id, mid] = Array.isArray(row) ? row : []
    if (!nativeApproval || id !== nativeApproval.request_id || !mid || mid !== nativeApproval.request_mid || (decision !== 'approved' && decision !== 'rejected')) {
      return Promise.reject(new Error('approval not pending on ' + name))
    }
    return api.crewPeerPost(instanceId, slotPath + '/approve', {
      action: decision, request_id: id, request_mid: mid, origin: 'native',
    }).finally(settle)
  }, [nativeApproval, instanceId, slotPath, settle, name])
  // The newest turn row the transcript draws as a bubble: a system notice
  // (compaction, reload) and a say-nothing reply are not one.
  const lastTurn = [...messages].reverse().find(m => m.role === 'user' || (m.role === 'assistant' && !isSystemNoticeRow(m) && !isHiddenInvisibleAssistantRow(m)))
  const title = slotQ.data?.title || detailQ.data?.title || key
  const loadError = detailQ.error ?? slotQ.error ?? instancesQ.error ?? capsQ.error
  const versionMismatch = capsQ.data && !capsQ.data.version_match ? capsQ.data : null
  const actionPending = action.isPending
  const mutate = action.mutate
  // Regenerate sits on the newest reply's own hover row, as in the local chat.
  // Read at submit time, not render time: a row's editor can outlive the
  // render whose closure it holds (the list keeps finished rows).
  const rewindGate = useRef({ connected, running, actionPending })
  rewindGate.current = { connected, running, actionPending }
  const regenerateRow = !running && !slotQ.data?.interrupted && connected && !actionPending && lastTurn?.role === 'assistant' ? lastTurn : null
  const renderers = useMemo(() => createCrewWindowRenderers({
    instanceId,
    key,
    name,
    // A rewind replaces the conversation from that row on, so none is offered
    // mid-turn or while another action is in flight, and none on a row the
    // hub redacted (its text is not the peer's, so resending it would rewrite
    // the session with the redaction).
    // The editor also restores the row's pastes, which the proxy redacts too.
    canRewind: m => connected && !running && !actionPending && !!m.ts && !hasRedaction(m.content) && !hasRedaction(JSON.stringify(m.meta?.pastes ?? null)),
    // Edit & Resend posts the rewind at once; a refusal lands the edit in the
    // composer's rewind mode (see `onError`), so Send retries it. An earlier
    // refused rewind is cleared first, as `send()` does, so a second refusal
    // never pairs that row's ts with this row's text. Gated like `send()`: an
    // editor opened while idle can be submitted after a turn started.
    onRewind: (ts, content) => {
      const message = content.trim()
      if (!message) return true
      const gate = rewindGate.current
      // Busy: refuse, and the bubble keeps its editor open with the text.
      if (!gate.connected || gate.running || gate.actionPending) return false
      // A refused edit this one replaces is kept in the ordinary draft.
      const held = heldRewind.current
      if (held.ts && held.text.trim()) recoverIntoDraft(held.text)
      heldRewind.current = { ts: null, text: '' }
      setRewindTs(null)
      setRewindText('')
      mutate({ path: slotPath + '/rewind', body: { ts, content: message }, message, rewindTs: ts })
      return true
    },
    regenerateRow,
    onRegenerate: () => mutate({ path: slotPath + '/regenerate' }),
  }), [instanceId, key, name, running, actionPending, connected, slotPath, mutate, regenerateRow, recoverIntoDraft])
  const picker = useCrewWindowModelPicker({
    instanceId, slotKey: crewWindowSlot(instanceId, key), slotPath, slot: slotQ.data, caps: capsQ.data, enabled: versionOk, onWritten: settle,
  })
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const root = rootRef.current
    const cover = root?.closest('[data-crew-cover]')
    root?.focus()
    return cover ? coverSiblings(cover) : undefined
  }, [])
  const placeholder = i18nT('pages.chat.crewWindow.placeholder', { name })

  return (
    <div ref={rootRef} tabIndex={-1} className="flex flex-col h-full min-h-0 outline-none" data-testid="crew-chat-window">
      <div className="flex items-center gap-2 px-4 py-2 border-b border-border">
        <Server size={14} className="text-info shrink-0" aria-hidden="true" />
        <span className="truncate font-semibold min-w-0">{title}</span>
        <span className="text-muted truncate">{i18nT('pages.chat.crewWindow.running_on_header', { name })}</span>
        <span className="ml-auto text-muted truncate hidden sm:inline">{i18nT('pages.chat.crewWindow.close_hint', { name })}</span>
        <Btn className="shrink-0" onClick={onClose}><X size={14} aria-hidden="true" />{i18nT('pages.chat.crewWindow.close')}</Btn>
      </div>
      <div className="relative flex-1 min-h-0">
      <div className="absolute inset-0 overflow-y-auto pt-3 flex flex-col gap-1" aria-live="polite" style={{ paddingBottom: dockH + 16 }}>
        <div className="px-4 flex flex-col gap-3">
          {/* No hand-off: the composer below may hold an unsent draft. */}
          {loadError && <ErrorNotice title={i18nT('pages.chat.crewWindow.load_failed', { name })} message={errMessage(loadError)} />}
          {/* No hand-off: the composer below may hold an unsent draft. */}
          {versionMismatch && <ErrorNotice message={i18nT('pages.chat.crewWindow.version_mismatch', { peer: versionMismatch.version || '?', local: versionMismatch.local_version })} />}
          {/* No hand-off: the composer below may hold an unsent draft. */}
          {feedLost && (
            <div className="flex items-start gap-2">
              <ErrorNotice title={i18nT('pages.chat.crewWindow.feed_lost', { name })} message={i18nT('pages.chat.crewWindow.feed_lost_hint')} />
              <Btn onClick={() => { setFeedLost(false); setFeedGen(g => g + 1) }}>{i18nT('pages.chat.crewWindow.retry')}</Btn>
            </div>
          )}
          {!loadError && !detailQ.isPending && messages.length === 0 && <div className="text-muted">{i18nT('pages.chat.crewWindow.empty')}</div>}
        </div>
        {/* Every code fence here is the peer's: copy only, never Edit or Run
            in THIS machine's terminal. */}
        <ReadOnlyCodeCtx.Provider value={true}>
          <ChatMessageList messages={messages} running={running} renderers={renderers} onApprove={approve} onQuoteMessage={messageQuote.quoteMessage} />
        </ReadOnlyCodeCtx.Provider>
        <ChatFooter running={running && !approval?.request_id} stopping={false} state="" lastRole={messages[messages.length - 1]?.role ?? ''} />
        <div className="px-4 flex flex-col gap-3">
          {nativeApproval && <div className="text-muted mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }} data-testid="crew-window-approval-hint">{i18nT('pages.chat.crewWindow.approval_hint')}</div>}
          {approval?.request_id && !nativeApproval && (
            <div className="text-muted" data-testid="crew-window-approval-elsewhere">{i18nT('pages.chat.crewWindow.approval_elsewhere', { name })}</div>
          )}
          {!running && slotQ.data?.interrupted && (
            <div className="flex items-center gap-2 text-muted" data-testid="crew-window-interrupted">
              {i18nT('pages.chat.crewWindow.interrupted')}
              <Btn disabled={!connected || action.isPending} onClick={() => action.mutate({ path: slotPath + '/continue' })}>{i18nT('pages.chat.crewWindow.continue')}</Btn>
            </div>
          )}
        </div>
      </div>
      {/* Glass pins its own root to position: relative, so a plain box places
          the dock; the transcript above pays for it with padding. */}
      <div ref={dockRef} className="absolute left-0 right-0 bottom-0">
      <Glass thickness="thin" radius={0} className="border-t border-border py-3 flex flex-col gap-2">
        {/* The composer's own column, so a notice about a pick sits over the box and chip it is about. */}
        <div className="px-4 mx-auto w-full flex flex-col gap-2" style={{ maxWidth: 'var(--mc-input-width, 900px)' }}>
          {/* No hand-off: the composer below may hold an unsent draft. */}
          {action.error && <ErrorNotice title={i18nT('pages.chat.crewWindow.action_failed', { name })} message={errMessage(action.error)} onDismiss={() => action.reset()} />}
          {/* No hand-off: the composer below may hold an unsent draft. */}
          {!!picker.error && <ErrorNotice title={i18nT('pages.chat.crewWindow.action_failed', { name })} message={errMessage(picker.error)} onDismiss={picker.clearError} />}
          {/* No hand-off: same draft. The capability read retries in place. */}
          {picker.effortCapsFailed && <ErrorNotice variant="inline" message={i18nT('pages.chatPage.effort_options_unavailable')} />}
          {!connected && <div className="text-muted">{i18nT('pages.chat.crewWindow.offline', { name })}</div>}
          {rewindTs && (
            <div className="flex items-center gap-2 text-muted">
              {i18nT('pages.chat.crewWindow.rewinding')}
              <Btn onClick={() => { setRewindTs(null); setRewindText('') }}>{i18nT('pages.chat.crewWindow.cancel_rewind')}</Btn>
            </div>
          )}
        </div>
        {/* The shared composer, under a slot key no local session can carry:
            its store reads (approvals, tool log, busy mode) then find nothing
            of the hub's own sessions. Menus, the optimizer and the approval
            chrome are off, as in the side panel: each acts on a LOCAL slot.
            Offline, a disabled fieldset turns the whole composer off, as the
            RFC's "input disabled" says; the composer's own offline state would
            name the hub's gateway, not this crew. */}
        <fieldset disabled={!connected} className="contents">
        <SlotProvider slotId={crewWindowSlot(instanceId, key)}>
          <ChatInput
            value={rewindTs ? rewindText : draft}
            onChange={rewindTs ? setRewindText : setDraft}
            onSend={send}
            onStop={() => action.mutate({ path: slotPath + '/stop' })}
            isRunning={running}
            autoFocusKey={rewindTs}
            placeholder={placeholder}
            inputAriaLabel={placeholder}
            typedCommandMenus={false}
            slotApprovalChrome={false}
            promptOptimizer={false}
            // Model + effort write the PEER's slot. Attach, the agent picker
            // and the approval-mode picker are not passed, so the composer
            // does not draw them: each writes THIS gateway's state.
            {...picker.chipProps}
            pendingQuote={rewindTs ? null : messageQuote.pendingQuote}
            onRemoveQuote={messageQuote.clearQuote}
          />
        </SlotProvider>
        </fieldset>
      </Glass>
      </div>
      {picker.portal}
      </div>
    </div>
  )
}
