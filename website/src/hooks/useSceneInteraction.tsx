import React, { useState, useCallback, useEffect, useRef, type RefObject } from 'react'
import { Circle, Pause, MessageSquare, X, Check } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import { api } from '../api/client'
import type { AgentSource } from './useAgentSync'
import { KIRO_GHOST_PIXELS } from './sceneText'

import { i18nT } from '../i18n/t'
/** Minimal agent shape for hit-testing — all scene agent types satisfy this */
export interface SceneAgent {
  id: string; name: string; x: number; y: number; running: boolean; detail: string
  kind: 'slot' | 'cron' | 'spawn'
  /** Scene accent color for this agent, used by the popover mini avatar */
  color?: string
}

/** Tiny pixel Kiro ghost used as the popover's agent identity marker */
function MiniGhost({ color }: { color: string }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = ref.current
    const ctx = cv?.getContext('2d')
    if (!cv || !ctx) return
    ctx.clearRect(0, 0, 24, 28)
    ctx.fillStyle = color
    KIRO_GHOST_PIXELS.forEach((row, y) => {
      for (let x = 0; x < row.length; x++) if (row[x] === '#') ctx.fillRect(x, y, 1, 1)
    })
    ctx.fillStyle = '#14141e'
    ctx.fillRect(11, 8, 3, 4)
    ctx.fillRect(17, 8, 3, 4)
  }, [color])
  return <canvas ref={ref} width={24} height={28} style={{ width: 18, height: 21, imageRendering: 'pixelated', flexShrink: 0 }} aria-hidden />
}

export interface SceneTooltipTheme {
  active: string   // e.g. "Grinding PRs"
  idle: string     // e.g. "Waiting for CR approval"
}

interface TooltipState {
  x: number; y: number; agent: SceneAgent
}

interface ThreadMessage { role: string; content: string }

interface ThreadViewState {
  x: number; y: number; agent: SceneAgent
  loading: boolean
  messages: ThreadMessage[]
  error: boolean
}

/** Derive the agent's true state line from its kind, running flag, and detail. */
export function agentStatusLine(agent: SceneAgent): string {
  const detail = agent.detail ? ` · ${agent.detail}` : ''
  switch (agent.kind) {
    case 'cron':
      return agent.running ? i18nT('hooks.useSceneInteraction.cron_running') : i18nT('hooks.useSceneInteraction.cron', { detail })
    case 'spawn':
      return i18nT('hooks.useSceneInteraction.subagent', { detail })
    default:
      return (agent.running ? i18nT('hooks.useSceneInteraction.working') : i18nT('hooks.useSceneInteraction.idle')) + detail
  }
}

/** Truncate a message preview to a single tooltip-friendly line. */
export function messagePreview(text: string, max = 64): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  return flat.slice(0, max - 1) + '…'
}

const THREAD_VIEW_MESSAGES = 8

/** Streaming-bookkeeping roles the main chat filters out of history */
const THREAD_SKIP_ROLES = new Set(['chunk', 'done'])

/** Clean raw slot history for the mini thread: drop streaming roles and
 *  collapse consecutive duplicates (a streamed chunk + its persisted final). */
function cleanThread(msgs: ThreadMessage[]): ThreadMessage[] {
  const out: ThreadMessage[] = []
  for (const m of msgs) {
    if (THREAD_SKIP_ROLES.has(m.role)) continue
    const prev = out[out.length - 1]
    if (prev && prev.role === m.role && prev.content === m.content) continue
    out.push(m)
  }
  return out.slice(-THREAD_VIEW_MESSAGES)
}

/**
 * Shared tooltip + mini thread view for all Worlds scenes.
 * Hover shows true state (+ real last message when sources are provided).
 * Clicking a chat-slot agent opens a mini thread popover with recent messages
 * and an "Open chat" action.
 * @param canvasRef - ref to the pixel canvas element
 * @param agentsRef - ref to the scene's agent array
 * @param W - scene width in logical pixels
 * @param H - scene height in logical pixels
 * @param theme - scene-specific tooltip messages
 * @param hitRadius - hit-test radius in logical pixels (default 10)
 * @param extraLine - optional scene-specific tooltip line
 * @param sources - live AgentSource list (enables last-message tooltip line)
 */
export function useSceneInteraction(
  canvasRef: RefObject<HTMLCanvasElement | null>,
  agentsRef: RefObject<SceneAgent[]>,
  W: number, H: number,
  theme: SceneTooltipTheme,
  hitRadius = 10,
  extraLine?: (agent: SceneAgent) => React.ReactNode,
  sources?: AgentSource[],
) {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)
  const [threadView, setThreadView] = useState<ThreadViewState | null>(null)
  const sourcesRef = useRef<AgentSource[] | undefined>(sources)
  sourcesRef.current = sources
  const popoverRef = useRef<HTMLDivElement | null>(null)

  const getAgentAt = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cv = canvasRef.current
    if (!cv) return undefined
    const rect = cv.getBoundingClientRect()
    const mx = (e.clientX - rect.left) / rect.width * W
    const my = (e.clientY - rect.top) / rect.height * H
    return agentsRef.current?.find(a => Math.abs(a.x - mx) < hitRadius && Math.abs(a.y - my) < hitRadius)
  }, [canvasRef, agentsRef, W, H, hitRadius])

  const onMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const a = getAgentAt(e)
    if (a) {
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return
      setTooltip({ x: e.clientX - rect.left + 12, y: e.clientY - rect.top - 50, agent: a })
    } else {
      setTooltip(null)
    }
  }, [getAgentAt, canvasRef])

  const onMouseLeave = useCallback(() => setTooltip(null), [])

  const openChat = useCallback((agent: SceneAgent) => {
    const slotKey = agent.id.replace(/^slot-/, '')
    dispatch(switchSlot(slotKey))
    navigate('/chat')
  }, [dispatch, navigate])

  const onClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const a = getAgentAt(e)
    if (!a || a.kind !== 'slot') {
      setThreadView(null)
      return
    }
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    // Toggle off when clicking the same agent
    if (threadView && threadView.agent.id === a.id) {
      setThreadView(null)
      return
    }
    const px = Math.min(e.clientX - rect.left + 12, rect.width - 280)
    const py = Math.min(Math.max(e.clientY - rect.top - 60, 8), rect.height - 200)
    setTooltip(null)
    setThreadView({ x: Math.max(8, px), y: py, agent: a, loading: true, messages: [], error: false })
    const slotKey = a.id.replace(/^slot-/, '')
    api.chatSlotDetail(slotKey, THREAD_VIEW_MESSAGES * 3)
      .then((d: { messages?: ThreadMessage[] }) => {
        setThreadView(tv => tv && tv.agent.id === a.id
          ? { ...tv, loading: false, messages: cleanThread(d.messages || []) }
          : tv)
      })
      .catch(() => {
        setThreadView(tv => tv && tv.agent.id === a.id ? { ...tv, loading: false, error: true } : tv)
      })
  }, [getAgentAt, canvasRef, threadView])

  // Dismiss the thread view on outside click or Escape
  useEffect(() => {
    if (!threadView) return
    const onPointerDown = (ev: PointerEvent) => {
      const el = popoverRef.current
      if (el && ev.target instanceof Node && !el.contains(ev.target) && ev.target !== canvasRef.current) {
        setThreadView(null)
      }
    }
    const onKey = (ev: KeyboardEvent) => { if (ev.key === 'Escape') setThreadView(null) }
    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [threadView, canvasRef])

  // Live refresh: poll the thread while the popover is open so agent
  // responses appear without reopening. Recently-sent messages that the
  // server hasn't persisted yet are re-appended to avoid flicker.
  const threadAgentId = threadView?.agent.id
  const lastSentRef = useRef<{ slotKey: string; content: string; at: number } | null>(null)
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!threadAgentId || !threadAgentId.startsWith('slot-')) return
    const slotKey = threadAgentId.replace(/^slot-/, '')
    const refresh = () => {
      api.chatSlotDetail(slotKey, THREAD_VIEW_MESSAGES * 3)
        .then((d: { messages?: ThreadMessage[] }) => {
          setThreadView(tv => {
            if (!tv || tv.agent.id !== threadAgentId) return tv
            let msgs = cleanThread(d.messages || [])
            const sent = lastSentRef.current
            if (sent && sent.slotKey === slotKey && Date.now() - sent.at < 8000 && !msgs.some(m => m.role === 'user' && m.content === sent.content)) {
              msgs = [...msgs, { role: 'user', content: sent.content }].slice(-THREAD_VIEW_MESSAGES)
            }
            return { ...tv, loading: false, messages: msgs }
          })
        })
        .catch(() => {})
    }
    const timer = setInterval(refresh, 2000)
    return () => clearInterval(timer)
  }, [threadAgentId])

  // Keep the mini thread pinned to the newest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ block: 'nearest' })
  }, [threadView?.messages.length, threadView?.loading])

  const sourceFor = (agent: SceneAgent): AgentSource | undefined =>
    sourcesRef.current?.find(s => s.id === agent.id)

  const [draft, setDraft] = useState('')
  const [sendState, setSendState] = useState<'idle' | 'sending' | 'sent' | 'failed'>('idle')
  const [approvalState, setApprovalState] = useState<'idle' | 'resolving' | 'failed'>('idle')

  // Reset composer state when the popover target changes
  useEffect(() => {
    setDraft(''); setSendState('idle'); setApprovalState('idle')
  }, [threadView?.agent.id])

  // Draggable popover: dragPos overrides the anchored position once the user
  // grabs the header. Resets when the popover retargets.
  const [dragPos, setDragPos] = useState<{ x: number; y: number } | null>(null)
  useEffect(() => { setDragPos(null) }, [threadView?.agent.id])

  const onHeaderPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!threadView) return
    // Buttons in the header keep their click behavior
    if (e.target instanceof Element && e.target.closest('button')) return
    e.preventDefault()
    const startX = e.clientX, startY = e.clientY
    const base = dragPos ?? { x: threadView.x, y: threadView.y }
    const parent = popoverRef.current?.offsetParent as HTMLElement | null
    const bounds = parent ? { w: parent.clientWidth, h: parent.clientHeight } : null
    const onMove = (ev: PointerEvent) => {
      let nx = base.x + (ev.clientX - startX)
      let ny = base.y + (ev.clientY - startY)
      if (bounds) {
        nx = Math.min(Math.max(nx, -180), bounds.w - 90)
        ny = Math.min(Math.max(ny, 0), bounds.h - 40)
      }
      setDragPos({ x: nx, y: ny })
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [threadView, dragPos])

  const sendToAgent = useCallback(async (agent: SceneAgent, text: string) => {
    const msg = text.trim()
    if (!msg) return
    const slotKey = agent.id.replace(/^slot-/, '')
    setSendState('sending')
    try {
      const src = sourceFor(agent)
      if (src?.running) {
        // Mid-turn: steer the running turn (backend queues if steer unavailable)
        await api.steerChat(msg, slotKey)
      } else {
        // Idle or waiting for input: start/continue the turn.
        // sendChat streams SSE — fire it and swallow the stream; the scene's
        // live slot state reflects the turn via the normal WS updates.
        await api.sendChat(msg, slotKey).then(r => { r.body?.cancel().catch(() => {}) })
      }
      setDraft('')
      setSendState('sent')
      setTimeout(() => setSendState('idle'), 1500)
      lastSentRef.current = { slotKey, content: msg, at: Date.now() }
      // Optimistically append to the mini thread
      setThreadView(tv => tv && tv.agent.id === agent.id
        ? { ...tv, messages: [...tv.messages, { role: 'user', content: msg }].slice(-THREAD_VIEW_MESSAGES) }
        : tv)
    } catch {
      setSendState('failed')
    }
  }, [])

  const resolvePendingApproval = useCallback(async (agent: SceneAgent, action: 'approve' | 'reject') => {
    const src = sourceFor(agent)
    if (!src?.pendingApproval) return
    setApprovalState('resolving')
    try {
      await api.resolveApproval(src.pendingApproval.requestId, action)
      setApprovalState('idle')
    } catch {
      setApprovalState('failed')
    }
  }, [])

  const tooltipSource = tooltip ? sourceFor(tooltip.agent) : undefined

  // In-world "New session" sign — creates a chat slot; the new agent enters
  // the scene through the live slot state. Rendered only when sources are wired.
  const [creating, setCreating] = useState(false)
  const createSession = useCallback(async () => {
    setCreating(true)
    try {
      await api.createChatSlot()
    } catch { /* button re-enables; slot list unchanged */ }
    setCreating(false)
  }, [])
  // Mirrors MAX_AGENTS in useAgentSync (kept local so tests can mock that module)
  const WORLD_CAPACITY = 8
  const worldFull = (sourcesRef.current?.length ?? 0) >= WORLD_CAPACITY
  const spawnEl = sources ? (
    <button
      onClick={createSession}
      disabled={creating || worldFull}
      title={worldFull ? i18nT('hooks.useSceneInteraction.all_slots_are_occupied') : i18nT('hooks.useSceneInteraction.create_a_new_chat_session_a_new_agent_joins_this')}
      style={{
        position: 'absolute', right: 10, bottom: 10, zIndex: 8,
        background: 'rgba(10,10,20,0.72)', border: '1.5px solid var(--accent, #f90)',
        borderRadius: 6, color: 'var(--accent, #f90)', fontSize: 11, fontWeight: 'bold',
        padding: '4px 10px', cursor: worldFull ? 'default' : 'pointer',
        opacity: creating || worldFull ? 0.45 : 0.9,
        fontFamily: 'var(--font-body, inherit)',
      }}
    >
      {creating ? i18nT('hooks.useSceneInteraction.summoning') : i18nT('hooks.useSceneInteraction.new_session')}
    </button>
  ) : null

  const tooltipEl = tooltip && !threadView ? (
    <div style={{ position: 'absolute', left: tooltip.x, top: tooltip.y, background: '#111', border: '1px solid #555', borderRadius: 4, padding: '4px 8px', fontSize: 11, color: '#ccc', pointerEvents: 'none', whiteSpace: 'nowrap', zIndex: 10, maxWidth: 300 }}>
      <div style={{ color: '#f90', fontWeight: 'bold', overflow: 'hidden', textOverflow: 'ellipsis' }}>{tooltip.agent.name}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Circle size={8} fill={tooltip.agent.running ? '#4f4' : '#ccc'} color={tooltip.agent.running ? '#4f4' : '#ccc'} aria-hidden /> {agentStatusLine(tooltip.agent)}</div>
      {tooltipSource?.pendingApproval ? (
        <div style={{ color: '#fb0', display: 'flex', alignItems: 'center', gap: 4 }}><Pause size={10} aria-hidden /> {i18nT('hooks.useSceneInteraction.needs_approval')} {tooltipSource.pendingApproval.tool}</div>
      ) : null}
      {tooltipSource?.lastMessage ? (
        <div style={{ color: '#aab', overflow: 'hidden', textOverflow: 'ellipsis', display: 'flex', alignItems: 'center', gap: 4 }}><MessageSquare size={10} style={{ flexShrink: 0 }} aria-hidden /> <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{messagePreview(tooltipSource.lastMessage)}</span></div>
      ) : null}
      <div style={{ color: '#777', fontStyle: 'italic' }}>{tooltip.agent.running ? theme.active : theme.idle}</div>
      {extraLine && extraLine(tooltip.agent)}
      {tooltip.agent.kind === 'slot' ? (
        <div style={{ color: '#666', marginTop: 2 }}>{i18nT('hooks.useSceneInteraction.click_for_thread')}</div>
      ) : null}
    </div>
  ) : null

  const threadViewEl = threadView ? (
    <div
      ref={popoverRef}
      role="dialog"
      aria-label={i18nT('hooks.useSceneInteraction.recent_messages_for', { name: threadView.agent.name })}
      style={{ position: 'absolute', left: (dragPos ?? threadView).x, top: (dragPos ?? threadView).y, width: 272, background: '#15151f', border: '1px solid #555', borderRadius: 6, fontSize: 11, color: '#ccc', zIndex: 20, boxShadow: '0 4px 16px rgba(0,0,0,0.5)', overflow: 'hidden' }}
    >
      <div
        onPointerDown={onHeaderPointerDown}
        style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px', borderBottom: '1px solid #333', cursor: 'grab', touchAction: 'none', userSelect: 'none' }}
      >
        <MiniGhost color={threadView.agent.color || '#e8ecf4'} />
        <span style={{ color: '#f90', fontWeight: 'bold', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{threadView.agent.name}</span>
        <button
          onClick={() => openChat(threadView.agent)}
          style={{ background: '#2a2a3a', border: '1px solid #555', borderRadius: 4, color: '#ddd', fontSize: 10, padding: '2px 8px', cursor: 'pointer' }}
        >
          {i18nT('hooks.useSceneInteraction.open_chat')}
        </button>
        <button
          onClick={() => setThreadView(null)}
          aria-label={i18nT('hooks.useSceneInteraction.close_thread_view')}
          style={{ background: 'transparent', border: 'none', color: '#888', fontSize: 12, cursor: 'pointer', padding: '0 2px', lineHeight: 1 }}
        >
          <X size={12} aria-hidden />
        </button>
      </div>
      <div style={{ maxHeight: 180, overflowY: 'auto', padding: '6px 8px', display: 'flex', flexDirection: 'column', gap: 5 }}>
        {threadView.loading ? (
          <div style={{ color: '#777', fontStyle: 'italic' }}>{i18nT('hooks.useSceneInteraction.loading_thread')}</div>
        ) : threadView.error ? (
          <div style={{ color: '#c66' }}>{i18nT('hooks.useSceneInteraction.couldn_t_load_messages')}</div>
        ) : threadView.messages.length === 0 ? (
          <div style={{ color: '#777', fontStyle: 'italic' }}>{i18nT('hooks.useSceneInteraction.no_messages_yet')}</div>
        ) : (
          threadView.messages.map((m, i) => (
            <div key={i} style={{ display: 'flex', gap: 5, alignItems: 'baseline' }}>
              <span style={{ color: m.role === 'user' ? '#f90' : '#7a8', flexShrink: 0, fontWeight: 'bold' }}>
                {m.role === 'user' ? 'you' : 'kiro'}
              </span>
              <span style={{ color: '#bbb', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical' }}>
                {messagePreview(m.content, 200)}
              </span>
            </div>
          ))
        )}
        {sourceFor(threadView.agent)?.running ? (
          <div style={{ color: '#7a8', fontStyle: 'italic' }}>{i18nT('hooks.useSceneInteraction.kiro_is_working')}</div>
        ) : null}
        <div ref={messagesEndRef} />
      </div>
      {(() => {
        const src = sourceFor(threadView.agent)
        return src?.pendingApproval ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px', borderTop: '1px solid #333', background: '#241d10' }}>
            <span style={{ color: '#fb0', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 4 }}>
              <Pause size={10} style={{ flexShrink: 0 }} aria-hidden /> {i18nT('hooks.useSceneInteraction.waiting_on_approval')} {src.pendingApproval.tool}
            </span>
            <button
              onClick={() => resolvePendingApproval(threadView.agent, 'approve')}
              disabled={approvalState === 'resolving'}
              style={{ background: '#1e4620', border: '1px solid #3a7a3d', borderRadius: 4, color: '#8f8', fontSize: 10, padding: '2px 8px', cursor: 'pointer' }}
            >
              {i18nT('hooks.useSceneInteraction.approve')}
            </button>
            <button
              onClick={() => resolvePendingApproval(threadView.agent, 'reject')}
              disabled={approvalState === 'resolving'}
              style={{ background: '#46201e', border: '1px solid #7a3d3a', borderRadius: 4, color: '#f88', fontSize: 10, padding: '2px 8px', cursor: 'pointer' }}
            >
              {i18nT('hooks.useSceneInteraction.deny')}
            </button>
            {approvalState === 'failed' ? <span style={{ color: '#c66' }}>{i18nT('hooks.useSceneInteraction.failed')}</span> : null}
          </div>
        ) : null
      })()}
      <div style={{ display: 'flex', gap: 6, padding: '6px 8px', borderTop: '1px solid #333' }}>
        <input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendToAgent(threadView.agent, draft) } }}
          placeholder={sourceFor(threadView.agent)?.running ? i18nT('hooks.useSceneInteraction.steer_this_agent') : i18nT('hooks.useSceneInteraction.message_this_agent')}
          aria-label={i18nT('hooks.useSceneInteraction.message', { name: threadView.agent.name })}
          style={{ flex: 1, background: '#0d0d15', border: '1px solid #444', borderRadius: 4, color: '#ddd', fontSize: 11, padding: '4px 7px', outline: 'none' }}
        />
        <button
          onClick={() => sendToAgent(threadView.agent, draft)}
          disabled={sendState === 'sending' || !draft.trim()}
          aria-label={sendState === 'sending' ? i18nT('hooks.useSceneInteraction.sending_message') : sendState === 'sent' ? i18nT('hooks.useSceneInteraction.message_sent') : sendState === 'failed' ? i18nT('hooks.useSceneInteraction.retry_sending_message') : i18nT('hooks.useSceneInteraction.send_message')}
          style={{ background: '#2a2a3a', border: '1px solid #555', borderRadius: 4, color: sendState === 'failed' ? '#f88' : '#ddd', fontSize: 10, padding: '2px 9px', cursor: 'pointer', opacity: draft.trim() ? 1 : 0.5 }}
        >
          {sendState === 'sending' ? '…' : sendState === 'sent' ? <Check size={12} aria-hidden /> : sendState === 'failed' ? i18nT('hooks.useSceneInteraction.retry') : i18nT('hooks.useSceneInteraction.send')}
        </button>
      </div>
    </div>
  ) : null

  return {
    canvasProps: { onMouseMove, onMouseLeave, onClick, style: { cursor: tooltip?.agent.kind === 'slot' ? 'pointer' : 'default' } },
    tooltipEl: (
      <>
        {tooltipEl}
        {threadViewEl}
        {spawnEl}
      </>
    ),
  }
}
