/** The crew chat window: a session that lives on a connected crew, shown and
 *  driven through the hub's proxy onto the PEER's own chat API.
 *
 *  Nothing here is stored on the hub. The transcript, the running flag and the
 *  pending approval are the peer's, read through `/api/instances/{id}/proxy/`,
 *  and every action (send, stop, approve, continue, regenerate, rewind) calls
 *  the peer's own route for its own slot. The hub's proxy redacts every reply
 *  before it reaches this component, so peer text renders as delivered. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Server, X } from 'lucide-react'
import { api } from '../../../api/client'
import { crewPeerUrl } from '../../../api/client/instances'
import { useAppSelector } from '../../../store'
import { Btn, SendBtn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import Glass from '../../../components/Glass'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { i18nT } from '../../../i18n/t'
import { errMessage } from '../../../utils/thunkError'
import { closeCrewWindow, coverSiblings, markCrewWindowShown, readCrewDraft, subscribeCrewDraft, writeCrewDraft, type CrewWindowTarget } from './crewWindowStore'
import { mergeRecoveredDraft } from '../../../utils/chatDrafts'
import { useImeGuard } from '../../../hooks/useImeGuard'

interface PeerMessage { role?: string; content?: string; ts?: string }
interface PeerApproval { origin?: string; request_id?: string; request_mid?: string; tool?: string; tool_input?: string; tool_purpose?: string }
interface PeerSlot { key?: string; title?: string; running?: boolean; interrupted?: boolean; pending_approval_info?: PeerApproval | null }
interface PeerDetail { title?: string; running?: boolean; messages?: PeerMessage[] }

/** How long a burst of peer frames waits before one transcript re-read. */
const REFETCH_THROTTLE_MS = 300
/** How often an unmatched (or unreachable) crew's version is re-read. */
const CAPS_RETRY_MS = 5000

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
  const setDraft = (next: string | ((cur: string) => string)) => setDraftState(cur => {
    const value = typeof next === 'function' ? next(cur) : next
    writeCrewDraft(target, value)
    return value
  })
  const [rewindTs, setRewindTs] = useState<string | null>(null)
  // A rewind's edit is held beside the draft, never in it, so the session's
  // own unsent text survives a close or a rejected rewind untouched.
  const [rewindText, setRewindText] = useState('')
  // A failed send from an earlier mount of this session writes the store;
  // follow it so the reopened composer shows the recovered text.
  useEffect(() => subscribeCrewDraft(target, setDraftState), [target])
  useEffect(() => markCrewWindowShown(onClose), [onClose])
  const inputRef = useRef<HTMLTextAreaElement>(null)
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
  const ime = useImeGuard()
  const settle = () => {
    void queryClient.invalidateQueries({ queryKey: slotKeyQ })
    void queryClient.invalidateQueries({ queryKey: detailKeyQ })
  }
  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: object; message?: string; rewindTs?: string | null }) =>
      api.crewPeerPost(instanceId, path, body),
    onSettled: settle,
    // Mutation-level, not per-call: it still runs when the window closed or
    // switched while the send was in flight, so the text is never lost.
    onError: (_err, vars) => {
      if (!vars.message) return
      if (vars.rewindTs) {
        // Back into the rewind edit; the draft was never touched.
        setRewindText(vars.message)
        setRewindTs(cur => cur ?? vars.rewindTs ?? null)
        return
      }
      const next = mergeRecoveredDraft(readCrewDraft(target), vars.message)
      writeCrewDraft(target, next)
      setDraftState(next)
    },
  })
  const send = () => {
    const message = (rewindTs ? rewindText : draft).trim()
    if (!message || action.isPending) return
    const req = rewindTs
      ? { path: slotPath + '/rewind', body: { ts: rewindTs, content: message } }
      : { path: 'api/chat?ws=1', body: { message, slot: key } }
    // Cleared at dispatch so text typed while the send is in flight is never
    // wiped by its success; a failure restores it only into an empty box.
    if (rewindTs) setRewindText('')
    else setDraft('')
    setRewindTs(null)
    action.mutate({ ...req, message, rewindTs })
  }

  const running = slotQ.data?.running ?? detailQ.data?.running ?? false
  const approval = slotQ.data?.pending_approval_info
  // Only a native approval names its transcript row, and the peer's strict
  // check needs that row's id so a stale card cannot decide a newer request.
  const nativeApproval = approval?.origin === 'native' && approval.request_id && approval.request_mid ? approval : null
  const decide = (decision: 'approved' | 'rejected') => {
    if (!nativeApproval) return
    action.mutate({
      path: slotPath + '/approve',
      body: { action: decision, request_id: nativeApproval.request_id, request_mid: nativeApproval.request_mid, origin: 'native' },
    })
  }
  const messages = detailQ.data?.messages ?? []
  const lastTurn = [...messages].reverse().find(m => m.role === 'user' || m.role === 'assistant')
  const title = slotQ.data?.title || detailQ.data?.title || key
  const loadError = detailQ.error ?? slotQ.error ?? instancesQ.error ?? capsQ.error
  const versionMismatch = capsQ.data && !capsQ.data.version_match ? capsQ.data : null
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const root = rootRef.current
    const cover = root?.closest('[data-crew-cover]')
    root?.focus()
    return cover ? coverSiblings(cover) : undefined
  }, [])

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
      <div className="absolute inset-0 overflow-y-auto px-4 pt-3 flex flex-col gap-3" aria-live="polite" style={{ paddingBottom: dockH + 16 }}>
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
        {messages.map((m, i) => {
          const text = m.content || ''
          if (m.role === 'user') {
            return (
              <div key={m.ts || i} className="self-end max-w-[80%] flex flex-col items-end gap-1" data-testid="crew-window-user">
                <div className="rounded-lg bg-bg-elevated px-3 py-2 whitespace-pre-wrap break-words">{text}</div>
                {!running && m.ts && !text.includes('[REDACTED') && (
                  <Btn disabled={action.isPending} onClick={() => { if (action.isPending) return; setRewindTs(m.ts || null); setRewindText(text); inputRef.current?.focus() }}>
                    {i18nT('pages.chat.crewWindow.rewind')}
                  </Btn>
                )}
              </div>
            )
          }
          if (m.role === 'assistant' || m.role === 'chunk') {
            return <div key={m.ts || i} data-testid="crew-window-assistant"><MarkdownRenderer content={text} softBreaks readOnlyCode /></div>
          }
          return <div key={m.ts || i} className="text-muted truncate">{text}</div>
        })}
        {nativeApproval && (
          <div className="rounded-lg border border-border p-3 flex flex-col gap-2" data-testid="crew-window-approval">
            <span className="font-semibold">{i18nT('pages.chat.crewWindow.approval_title', { name, tool: nativeApproval.tool || '?' })}</span>
            {nativeApproval.tool_purpose && <span className="break-words">{nativeApproval.tool_purpose}</span>}
            <span className="break-words text-muted">{nativeApproval.tool}</span>
            {nativeApproval.tool_input && <pre className="whitespace-pre-wrap break-words text-muted">{nativeApproval.tool_input}</pre>}
            <span className="text-muted">{i18nT('pages.chat.crewWindow.approval_hint')}</span>
            <div className="flex gap-2">
              <Btn primary disabled={action.isPending} onClick={() => decide('approved')}>{i18nT('pages.chat.crewWindow.approve')}</Btn>
              <Btn danger disabled={action.isPending} onClick={() => decide('rejected')}>{i18nT('pages.chat.crewWindow.reject')}</Btn>
            </div>
            <div className="flex">
              <Btn onClick={() => action.mutate({ path: slotPath + '/stop' })}>{i18nT('pages.chat.crewWindow.stop')}</Btn>
            </div>
          </div>
        )}
        {approval?.request_id && !nativeApproval && (
          <div className="text-muted" data-testid="crew-window-approval-elsewhere">{i18nT('pages.chat.crewWindow.approval_elsewhere', { name })}</div>
        )}
        {running && !approval?.request_id && (
          <div className="flex items-center gap-2 text-muted" data-testid="crew-window-running">
            <Loader2 size={14} className="animate-spin" aria-hidden="true" />
            {i18nT('pages.chatSidebar.thinking')}
            <Btn onClick={() => action.mutate({ path: slotPath + '/stop' })}>{i18nT('pages.chat.crewWindow.stop')}</Btn>
          </div>
        )}
        {!running && slotQ.data?.interrupted && (
          <div className="flex items-center gap-2 text-muted" data-testid="crew-window-interrupted">
            {i18nT('pages.chat.crewWindow.interrupted')}
            <Btn disabled={!connected || action.isPending} onClick={() => action.mutate({ path: slotPath + '/continue' })}>{i18nT('pages.chat.crewWindow.continue')}</Btn>
          </div>
        )}
        {!running && !slotQ.data?.interrupted && lastTurn?.role === 'assistant' && (
          <div>
            <Btn disabled={!connected || action.isPending} onClick={() => action.mutate({ path: slotPath + '/regenerate' })}>{i18nT('pages.chat.crewWindow.regenerate')}</Btn>
          </div>
        )}
      </div>
      {/* Glass pins its own root to position: relative, so a plain box places
          the dock; the transcript above pays for it with padding. */}
      <div ref={dockRef} className="absolute left-0 right-0 bottom-0">
      <Glass thickness="thin" radius={0} className="border-t border-border px-4 py-3 flex flex-col gap-2">
        {/* No hand-off: the composer below may hold an unsent draft. */}
        {action.error && <ErrorNotice title={i18nT('pages.chat.crewWindow.action_failed', { name })} message={errMessage(action.error)} onDismiss={() => action.reset()} />}
        {!connected && <div className="text-muted">{i18nT('pages.chat.crewWindow.offline', { name })}</div>}
        {rewindTs && (
          <div className="flex items-center gap-2 text-muted">
            {i18nT('pages.chat.crewWindow.rewinding')}
            <Btn onClick={() => { setRewindTs(null); setRewindText('') }}>{i18nT('pages.chat.crewWindow.cancel_rewind')}</Btn>
          </div>
        )}
        <div className="flex gap-2 items-end">
          <textarea ref={inputRef} value={rewindTs ? rewindText : draft} rows={2} disabled={!connected}
            aria-label={i18nT('pages.chat.crewWindow.placeholder', { name })}
            placeholder={i18nT('pages.chat.crewWindow.placeholder', { name })}
            className="flex-1 min-w-0 resize-none rounded-lg border border-border bg-bg px-3 py-2"
            onChange={e => (rewindTs ? setRewindText : setDraft)(e.target.value)}
            {...ime.bindComposition()}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { if (ime.claimEnter(e)) send() } }} />
          <SendBtn disabled={!connected || !(rewindTs ? rewindText : draft).trim() || action.isPending} onClick={send}>{i18nT('pages.chat.crewWindow.send')}</SendBtn>
        </div>
      </Glass>
      </div>
      </div>
    </div>
  )
}
