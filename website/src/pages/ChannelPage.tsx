import { useState, useRef, useEffect, useCallback, useMemo, type ReactNode } from 'react'
import Clickable from '../components/Clickable'
import { Hourglass, Ear, Check, X, Wrench, Radio, VolumeX, User, MessageSquare, Users, Zap, AlertTriangle, RotateCcw } from 'lucide-react'
import { useAppSelector } from '../store'
import type { RootState } from '../store'
import { api } from '../api/client'
import ApprovalCard from '../components/ApprovalCard'
import { Btn, Input, Badge, EmptyState, PageHeader } from '../components/ui'
import MarkdownRenderer from '../components/MarkdownRenderer'
import AgentSelector from '../components/AgentSelector'
import { useAgents } from '../hooks/useAgents'
import { useImeGuard } from '../hooks/useImeGuard'
import { AnimatePresence } from 'framer-motion'
import DetailPanel from '../components/DetailPanel'

// ── Types ──

interface ChannelAgent {
  id: string
  role: string
  agentName: string
  state: 'pending' | 'working' | 'listening' | 'done' | 'failed' | 'tool_running'
  listenMode: 'all' | 'mention' | 'silent'
  approvalPolicy: 'all' | 'writes' | 'trusted'
}

interface ChannelMessage {
  id: string
  fromId: string
  fromRole: string
  content: string
  mention?: string | string[]
  msgType: 'progress' | 'mention' | 'broadcast' | 'approval' | 'done' | 'system'
  timestamp: number
  threadId?: string
  replyTo?: string
  replyCount: number
}

interface Channel {
  id: string
  topic: string
  agents: ChannelAgent[]
  messages: ChannelMessage[]
}

/* Map snake_case backend → camelCase frontend */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const mapAgent = (a: any): ChannelAgent => ({
  id: a.id, role: a.role, agentName: a.agent_name || a.agentName || '', state: a.state || 'pending',
  listenMode: a.listen_mode || a.listenMode || 'mention',
  approvalPolicy: a.approval_policy || a.approvalPolicy || 'writes',
})
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const mapMsg = (m: any): ChannelMessage => ({
  id: m.id, fromId: m.from_id || m.fromId, fromRole: m.from_role || m.fromRole,
  content: m.content, mention: m.mention, msgType: m.msg_type || m.msgType || 'progress',
  timestamp: m.timestamp ? m.timestamp * 1000 : Date.now(),
  threadId: m.thread_id || m.threadId, replyTo: m.reply_to || m.replyTo,
  replyCount: m.reply_count || m.replyCount || 0,
})
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const mapChannel = (c: any): Channel => ({
  id: c.id, topic: c.topic,
  agents: Object.values(c.members || {}).map(mapAgent),
  messages: (c.messages || []).map(mapMsg),
})

// ── Agent colors ──

const AGENT_COLORS = [
  { bg: 'bg-accent', fg: 'text-accent-fg' },
  { bg: 'bg-ok', fg: 'text-ok-fg' },
  { bg: 'bg-warn', fg: 'text-warn-fg' },
  { bg: 'bg-info', fg: 'text-info-fg' },
  { bg: 'bg-danger', fg: 'text-danger-fg' },
]

const agentColor = (idx: number) => AGENT_COLORS[idx % AGENT_COLORS.length]

const STATE_BADGE: Record<string, { variant: 'ok' | 'err' | 'warn'; label: ReactNode }> = {
  pending: { variant: 'warn', label: <><Hourglass className="lucide-inline" /> pending</> },
  working: { variant: 'ok', label: <>● working</> },
  listening: { variant: 'ok', label: <><Ear className="lucide-inline" /> listening</> },
  done: { variant: 'ok', label: <><Check className="lucide-inline" /> done</> },
  failed: { variant: 'err', label: <><X className="lucide-inline" /> failed</> },
  tool_running: { variant: 'ok', label: <><Wrench className="lucide-inline" /> running</> },
}
const LISTEN_BADGE: Record<string, { variant: 'ok' | 'warn'; label: ReactNode }> = {
  all: { variant: 'ok', label: <><Radio className="lucide-inline" /> all</> },
  mention: { variant: 'warn', label: <><Ear className="lucide-inline" /> mention</> },
  silent: { variant: 'warn', label: <><VolumeX className="lucide-inline" /> silent</> },
}

// ── Components ──

function AgentBadge({ agent, index }: { agent: ChannelAgent; index: number }) {
  const c = agentColor(index)
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[13px] font-medium ${c.bg} ${c.fg}`}>
      {agent.role}
    </span>
  )
}

function MessageBubble({ msg, agents, onReply, onOpenThread, onApprove }: {
  msg: ChannelMessage; agents: ChannelAgent[]
  onReply?: () => void; onOpenThread?: () => void; onApprove?: (action: string) => void
}) {
  const isHuman = msg.fromId === 'human'
  const approvalMode = useAppSelector((s: RootState) => s.dashboard.approvalMode)
  const agentIdx = agents.findIndex(a => a.id === msg.fromId)
  const c = isHuman ? null : agentColor(agentIdx >= 0 ? agentIdx : 0)
  const time = new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

  return (
    <div className={`flex gap-3 py-2 px-3 rounded-lg animate-rise group ${isHuman ? 'bg-accent/10' : 'hover:bg-bg-hover'}`}>
      {/* Avatar */}
      <div className={`w-8 h-8 rounded-full flex items-center justify-center text-[13px] font-bold shrink-0 ${isHuman ? 'bg-accent text-accent-fg' : `${c?.bg} ${c?.fg}`}`}>
        {isHuman ? <User className="lucide-inline" /> : msg.fromRole[0]}
      </div>
      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className="text-sm font-semibold text-text-strong">{msg.fromRole}</span>
          {msg.mention && (
            <span className="text-[13px] text-muted">→ {(Array.isArray(msg.mention) ? msg.mention : [msg.mention]).map(id => '@' + (agents.find(a => a.id === id)?.role || id)).join(', ')}</span>
          )}
          <span className="text-[13px] text-muted ml-auto">{time}</span>
        </div>
        <div className="text-sm text-text">{msg.msgType === 'approval' ? <span className="whitespace-pre-wrap">{msg.content}</span> : <MarkdownRenderer content={msg.content} />}</div>
        {/* Approval card */}
        {msg.msgType === 'approval' && onApprove && (
          <div className="mt-2">
            <ApprovalCard title={msg.fromRole} toolInput={msg.content.replace(/^⚠️ Approval needed:.*\n```\n?/, '').replace(/\n?```$/, '')} showButtons={approvalMode === 'normal'} onApprove={onApprove} />
          </div>
        )}
        {/* Thread badge + reply */}
        <div className="flex items-center gap-2 mt-1">
          {msg.replyCount > 0 && (
            <Btn onClick={onOpenThread!} className="!p-0 !border-none !rounded-none text-[13px] text-accent hover:underline">
              <MessageSquare className="lucide-inline" /> {msg.replyCount} repl{msg.replyCount === 1 ? 'y' : 'ies'}
            </Btn>
          )}
          {onReply && (
            <Btn onClick={onReply} className="!p-0 !border-none !rounded-none text-[13px] text-muted hover:text-text opacity-0 group-hover:opacity-100 transition-opacity">
              <MessageSquare className="lucide-inline" /> Reply
            </Btn>
          )}
        </div>
      </div>
    </div>
  )
}

const LISTEN_MODES: Array<ChannelAgent['listenMode']> = ['all', 'mention', 'silent']

function AgentControlRow({ agent, onDismiss, onListenChange, onClearContext }: {
  agent: ChannelAgent; onDismiss: () => void; onListenChange: (m: ChannelAgent['listenMode']) => void; onClearContext: () => void
}) {
  const [menu, setMenu] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const alive = agent.state !== 'done' && agent.state !== 'failed'

  useEffect(() => {
    if (!menu) return
    const close = (e: MouseEvent) => { if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [menu])

  return (
    <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-bg-hover group">
      <Badge variant={STATE_BADGE[agent.state]?.variant || 'warn'}>{STATE_BADGE[agent.state]?.label || agent.state}</Badge>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-text truncate">{agent.role}</div>
        {agent.agentName && <div className="text-[11px] text-muted font-mono truncate">{agent.agentName}</div>}
        <div className="relative inline-block" ref={menuRef}>
          <Btn onClick={() => setMenu(!menu)} className="!p-0 !border-none !rounded-none text-[13px] text-muted hover:text-text">
            <Badge variant={LISTEN_BADGE[agent.listenMode]?.variant || 'warn'}>{LISTEN_BADGE[agent.listenMode]?.label || agent.listenMode}</Badge>
          </Btn>
          {menu && <div role="menu" className="absolute top-full left-0 mt-1 bg-bg-elevated border border-border rounded-md shadow-lg z-10">
            {LISTEN_MODES.map(m => (
              <Btn key={m} onClick={() => { onListenChange(m); setMenu(false) }}
                className={`!rounded-none block w-full text-left px-3 py-1.5 text-[13px] !border-none ${m === agent.listenMode ? 'text-accent bg-accent/10' : 'text-text hover:bg-bg-hover'}`}>
                <Badge variant={LISTEN_BADGE[m]?.variant || 'warn'}>{LISTEN_BADGE[m]?.label || m}</Badge>
              </Btn>
            ))}
          </div>}
        </div>
      </div>
      {alive && <Btn onClick={onClearContext} aria-label="Clear context" title="Clear context"><RotateCcw className="lucide-inline" /></Btn>}
      {alive && <Btn onClick={onDismiss} aria-label="Dismiss" danger title="Dismiss"><X className="lucide-inline" /></Btn>}
    </div>
  )
}

// ── Team Presets ──

const FALLBACK_PRESETS = [
  { id: 'incident', label: 'Incident Response', agents: [
    { role: 'Orchestrator', is_orchestrator: true, task: 'Coordinate investigation of {topic}' },
    { role: 'Logs Agent', task: 'Search logs related to {topic}' },
    { role: 'Code Agent', task: 'Check recent code changes related to {topic}' },
  ]},
  { id: 'custom', label: 'Custom (empty)', agents: [] },
]

type Preset = { id: string; label: string; agents: { role: string; is_orchestrator?: boolean; task?: string }[] }

// ── New Channel Dialog (Step 2) ──

function NewChannelDialog({ onClose, onCreate, presets }: { onClose: () => void; onCreate: (topic: string, presetId: string) => void; presets: Preset[] }) {
  const [topic, setTopic] = useState('')
  const [preset, setPreset] = useState(presets[0]?.id || 'custom')

  const handleCreate = () => {
    if (!topic.trim()) return
    onCreate(topic.trim(), preset)
  }

  return (
    <Clickable className="fixed inset-0 bg-black/50 flex items-center justify-center z-[100]" onClick={onClose}>
      {/* Handlers only stop propagation so clicks/keys inside the dialog don't
          bubble to the backdrop's close handler — the dialog itself is not an
          interactive control. label-has-for is deprecated and can't detect the
          custom <Input> as a nested control; htmlFor+id (and aria-label) already
          give a real programmatic association. */}
      {/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/label-has-for */}
      <div role="dialog" aria-modal="true" aria-label="New channel" className="bg-bg-elevated border border-border rounded-xl p-5 w-96 shadow-xl" onClick={e => e.stopPropagation()} onKeyDown={e => e.stopPropagation()}>
        <h3 className="text-base font-semibold text-text-strong mb-4">New Channel</h3>
        <label htmlFor="new-channel-topic" className="block text-[13px] font-medium text-muted mb-1">Topic</label>
        <Input id="new-channel-topic" aria-label="Topic" value={topic} onChange={e => setTopic(e.target.value)} autoFocus
          className="w-full mb-4"
          placeholder="e.g. Investigate Gamma deployment failure" />
        <span id="new-channel-preset-label" className="block text-[13px] font-medium text-muted mb-1">Team Preset</span>
        <div role="radiogroup" aria-labelledby="new-channel-preset-label" className="space-y-1.5 mb-4">
          {presets.map(p => (
            <Btn key={p.id} onClick={() => setPreset(p.id)}
              className={`w-full text-left px-3 py-2 !rounded-lg text-sm ${preset === p.id ? '!border-accent bg-accent/10 text-text-strong' : '!border-border text-muted hover:bg-bg-hover'}`}>
              <span className="font-medium">{p.label}</span>
              {p.agents.length > 0 && <span className="text-[13px] text-muted ml-2">({p.agents.map(a => a.role).join(', ')})</span>}
            </Btn>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <Btn onClick={onClose}>Cancel</Btn>
          <Btn onClick={handleCreate} disabled={!topic.trim()} primary>Create</Btn>
        </div>
      </div>
      {/* eslint-enable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/label-has-for */}
    </Clickable>
  )
}

// ── @Mention Input (Step 4) ──

function MentionInput({ agents, value, onChange, onSend }: {
  agents: ChannelAgent[]; value: string; onChange: (v: string) => void; onSend: () => void
}) {
  const [show, setShow] = useState(false)
  const [filter, setFilter] = useState('')
  const [sel, setSel] = useState(0)
  const ref = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value
    onChange(v)
    const m = v.match(/@([A-Za-z][\w ]*)?$/)
    if (m) { setFilter((m[1] || '').trim().toLowerCase()); setShow(true); setSel(0) } else setShow(false)
  }

  const applyHeight = () => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, window.innerHeight * 0.3) + 'px'
  }

  useEffect(() => { applyHeight() }, [value])

  const pick = (a: ChannelAgent) => {
    onChange(value.replace(/@[\w ]*$/, `@${a.role} `))
    setShow(false)
    ref.current?.focus()
  }

  const active = agents.filter(a => a.state !== 'done' && a.state !== 'failed' && a.role.toLowerCase().includes(filter))

  return (
    <div className="relative flex-1">
      {show && active.length > 0 && (
        <div role="listbox" aria-label="Mention suggestions" className="absolute bottom-full left-0 mb-1 w-60 bg-bg-elevated border border-border rounded-lg shadow-lg z-10 py-1">
          {active.map((a, i) => (
            <Btn key={a.id} onClick={() => pick(a)}
              className={`w-full text-left px-3 py-1.5 text-sm !border-none flex items-center gap-2 ${i === sel ? '!bg-accent !text-accent-fg' : 'hover:bg-bg-hover'}`}>
              <AgentBadge agent={a} index={i} /> <Badge variant={STATE_BADGE[a.state]?.variant || 'warn'}>{STATE_BADGE[a.state]?.label || a.state}</Badge>
            </Btn>
          ))}
        </div>
      )}
      <textarea ref={ref} value={value} onChange={handleChange}
        rows={1}
        aria-label="Message the channel"
        className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none flex-1 transition-colors focus-ring resize-none"
        placeholder="Message the channel... (type @ to mention)"
        {...ime.composition}
        onKeyDown={e => {
          if (show && active.length > 0) {
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel(s => (s + 1) % active.length) }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setSel(s => (s - 1 + active.length) % active.length) }
            else if (e.key === 'Enter' && !ime.isComposing(e)) { e.preventDefault(); pick(active[sel]) }
            else if (e.key === 'Escape') { ime.reset(); setShow(false) }
          } else if (e.key === 'Enter' && !e.shiftKey && !ime.isComposing(e)) { e.preventDefault(); onSend() }
        }} />
    </div>
  )
}

// ── Add Agent Form ──

function AddAgentForm({ onAdd, onCancel }: { onAdd: (role: string, task: string, agent: string) => void; onCancel: () => void }) {
  const [role, setRole] = useState('')
  const [task, setTask] = useState('')
  const { agents, defaultAgent } = useAgents(0)
  const [agent, setAgent] = useState('')
  return (
    <div className="p-2 space-y-2 border-t border-border">
      <div>
        <span className="text-[11px] text-muted font-medium mb-1 block">Agent</span>
        <AgentSelector agents={agents} defaultAgent={defaultAgent} value={agent || defaultAgent} onChange={setAgent} />
      </div>
      <Input value={role} onChange={e => setRole(e.target.value)} placeholder="Role (e.g. Logs Agent)" aria-label="Role" autoFocus
        className="w-full text-[13px]" />
      <Input value={task} onChange={e => setTask(e.target.value)} placeholder="Task (e.g. Search CloudWatch logs)" aria-label="Task"
        className="w-full text-[13px]"
        onKeyDown={e => { if (e.key === 'Enter' && role.trim()) onAdd(role.trim(), task.trim(), agent || defaultAgent) }} />
      <div className="flex gap-1">
        <Btn onClick={() => { if (role.trim()) onAdd(role.trim(), task.trim(), agent || defaultAgent) }} disabled={!role.trim()} primary className="flex-1">Add</Btn>
        <Btn onClick={onCancel}>Cancel</Btn>
      </div>
    </div>
  )
}

// ── Sidebar ──

function ChannelListItem({ ch, active, onClick }: { ch: Channel; active: boolean; onClick: () => void }) {
  const working = ch.agents.filter(a => a.state === 'working' || a.state === 'tool_running').length
  return (
    <Btn onClick={onClick} className={`w-full text-left px-3 py-2.5 !rounded-lg !border-none ${active ? 'bg-accent/15 text-text-strong' : 'text-muted hover:bg-bg-hover hover:text-text'}`}>
      <div className="text-sm font-medium truncate">{ch.topic}</div>
      <div className="flex items-center gap-2 mt-1 text-[13px] text-muted">
        <span>{ch.agents.length} agent{ch.agents.length !== 1 ? 's' : ''}</span>
        {working > 0 && <Badge variant="ok"><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--ok)]" /> {working} active</Badge>}
      </div>
    </Btn>
  )
}

// ── Main Page ──

const apiError = (err: unknown, fallback: string) => {
  const message = err instanceof Error ? err.message : ''
  try { return JSON.parse(message)?.error || fallback } catch { return message || fallback }
}

export default function ChannelPage() {
  const [channels, setChannels] = useState<Channel[]>([])
  const [presets, setPresets] = useState<Preset[]>(FALLBACK_PRESETS)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [showAgents, setShowAgents] = useState(false)
  const [showAddAgent, setShowAddAgent] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [threadId, setThreadId] = useState<string | null>(null)
  const [threadInput, setThreadInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const channel = channels.find(c => c.id === activeId) || channels[0] || null
  const topLevelMessages = useMemo(() => channel?.messages.filter(m => !m.threadId) ?? [], [channel?.messages])

  // Load channels on mount
  const reload = useCallback(async () => {
    try {
      const res = await api.channelsList()
      const mapped = (res.channels || []).map(mapChannel)
      setChannels(mapped)
      if (!activeId && mapped.length > 0) setActiveId(mapped[0].id)
    } catch { /* empty */ }
    setLoading(false)
  }, [activeId])

  useEffect(() => { reload(); api.channelPresets().then(r => setPresets(r.presets || FALLBACK_PRESETS)).catch(() => {}) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Load full channel (with messages) when switching
  useEffect(() => {
    if (!activeId) return
    api.channelGet(activeId).then(res => {
      const full = mapChannel(res)
      setChannels(prev => prev.map(c => c.id === activeId ? full : c))
    }).catch(() => {})
  }, [activeId])

  // Channel WS events dispatched via existing useWebSocket in App.tsx
  // Listen for custom events on window
  useEffect(() => {
    const handler = (e: Event) => {
      const { type, data } = (e as CustomEvent).detail
      if (type === 'channel_message' && data.message) {
        const msg = mapMsg(data.message)
        setChannels(prev => prev.map(c => c.id === data.channel_id ? { ...c, messages: [...c.messages, msg] } : c))
      } else if (type === 'channel_agent_status') {
        setChannels(prev => prev.map(c => c.id === data.channel_id ? {
          ...c, agents: c.agents.map(a => a.id === data.agent_id ? { ...a, state: data.state } : a)
        } : c))
      } else if (type === 'channel_created') {
        const ch = mapChannel(data)
        setChannels(prev => prev.some(c => c.id === ch.id) ? prev : [ch, ...prev])
      } else if (type === 'channel_closed') {
        setChannels(prev => prev.filter(c => c.id !== data.channel_id))
      } else if (type === 'channel_agent_joined') {
        reload()
      } else if (type === 'channel_agent_left') {
        setChannels(prev => prev.map(c => c.id === data.channel_id ? {
          ...c, agents: c.agents.map(a => a.id === data.agent_id ? { ...a, state: 'done' as const } : a)
        } : c))
      } else if (type === 'channel_context_cleared' && data.scope === 'all') {
        // Another client cleared shared context — drop our stale message buffer.
        setChannels(prev => prev.map(c => c.id === data.channel_id ? { ...c, messages: [] } : c))
      }
    }
    window.addEventListener('kirocrew-channel', handler)
    return () => window.removeEventListener('kirocrew-channel', handler)
  }, [reload])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [channel?.messages.length, activeId])

  const sendMessage = async (text: string, tid?: string) => {
    if (!text.trim() || !channel) return
    const msg = text.trim()
    const mentionIds = channel.agents.filter(a => msg.toLowerCase().includes('@' + a.role.toLowerCase())).map(a => a.id)
    try { await api.channelPost(channel.id, msg, mentionIds.length ? mentionIds : undefined, tid) } catch { /* WS will deliver */ }
  }

  const handleSend = async () => {
    if (!input.trim()) return
    await sendMessage(input)
    setInput('')
  }

  const handleDismiss = async (agentId: string) => {
    if (!channel) return
    setChannels(prev => prev.map(c => c.id !== channel.id ? c : { ...c, agents: c.agents.map(a => a.id === agentId ? { ...a, state: 'done' as const } : a) }))
    try { await api.channelDismissAgent(channel.id, agentId) } catch { /* optimistic */ }
  }

  const handleListenChange = async (agentId: string, mode: ChannelAgent['listenMode']) => {
    if (!channel) return
    setChannels(prev => prev.map(c => c.id !== channel.id ? c : { ...c, agents: c.agents.map(a => a.id === agentId ? { ...a, listenMode: mode } : a) }))
    try { await api.channelUpdateAgent(channel.id, agentId, { listen: mode }) } catch { /* optimistic */ }
  }

  if (loading) return <div className="flex items-center justify-center h-full text-muted">Loading channels...</div>

  const handleCreateChannel = async (topic: string, presetId: string) => {
    setShowNew(false)
    const tmpl = presets.find(p => p.id === presetId) || presets[0]
    try {
      const res = await api.channelCreate(topic, (tmpl?.agents || []).map(a => ({
        role: a.role, task: (a.task || '{topic}').replace('{topic}', topic),
        is_orchestrator: a.is_orchestrator || false,
      })))
      if (res.channel) {
        const ch = mapChannel(res.channel)
        setChannels(prev => prev.some(c => c.id === ch.id) ? prev : [ch, ...prev])
        setActiveId(res.channel.id)
      }
    } catch (err) {
      setError(apiError(err, 'Failed to create channel'))
    }
  }

  return (
    <>
      <PageHeader title="Channels" subtitle="Multi-agent collaboration spaces" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
    <div className="flex h-full relative">
      {showNew && <NewChannelDialog onClose={() => setShowNew(false)} presets={presets} onCreate={handleCreateChannel} />}

      {/* Error modal */}
      {error && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[100]">
          <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 w-80 shadow-xl text-center">
            <div className="text-3xl mb-2"><AlertTriangle className="lucide-inline" /></div>
            <div className="text-sm font-semibold text-[var(--text-strong)] mb-2">Limit Reached</div>
            <div className="text-sm text-[var(--text)] mb-4">{error}</div>
            <Btn onClick={() => setError(null)} primary>OK</Btn>
          </div>
        </div>
      )}

      {/* Channel list sidebar */}
      <div className="w-64 shrink-0 border-r border-border flex flex-col">
        <div className="px-3 py-3 border-b border-border flex items-center justify-between">
          <span className="text-sm font-semibold text-text-strong">Channels</span>
          <Btn onClick={() => setShowNew(true)} primary title="New Channel">+ New</Btn>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {channels.length === 0 && <EmptyState icon={<MessageSquare className="lucide-inline" />} title="No channels yet" subtitle="Click + New to create one." />}
          {channels.map(ch => (
            <ChannelListItem key={ch.id} ch={ch} active={ch.id === activeId} onClick={() => setActiveId(ch.id)} />
          ))}
        </div>
      </div>

      {/* Channel content */}
      {channel ? (
        <div className="flex-1 flex flex-col min-w-0">
          <div className="border-b border-border px-4 py-2.5 flex items-center justify-between">
            <h2 className="text-base font-semibold text-text-strong truncate">{channel.topic}</h2>
            <div className="flex items-center gap-1.5 shrink-0">
              <Btn onClick={() => setShowAgents(!showAgents)}>
                <Users className="lucide-inline" /> {channel.agents.length} agent{channel.agents.length !== 1 ? 's' : ''}
                {channel.agents.some(a => a.state === 'working' || a.state === 'tool_running') && <Badge variant="ok">●</Badge>}
              </Btn>
              <Btn onClick={async () => {
                if (!confirm('This will reset conversation history for all agents. Configs are preserved.')) return
                try {
                  await api.channelClearContext(channel.id, 'all')
                  const res = await api.channelGet(channel.id)
                  setChannels(prev => prev.map(c => c.id === channel.id ? mapChannel(res) : c))
                } catch (e) { alert('Failed to clear context: ' + (e instanceof Error ? e.message : 'unknown error')) }
              }} title="Clear all context">
                <RotateCcw className="lucide-inline" /> Clear Context
              </Btn>
              <Btn onClick={async () => {
                if (!confirm('Close this channel? All agents will be dismissed.')) return
                try { await api.channelClose(channel.id) } catch { /* WS handles removal */ }
                setChannels(prev => prev.filter(c => c.id !== channel.id))
                setActiveId(null)
              }} danger title="Close channel">
                <X className="lucide-inline" /> Close
              </Btn>
            </div>
          </div>

          <div className="flex flex-1 min-h-0">
            <div className="flex-1 overflow-y-auto px-2 py-3 space-y-1">
              {topLevelMessages.length === 0 && (
                <EmptyState icon={<Zap className="lucide-inline" />} title="Setting up channel…" subtitle={`${channel.agents.length} agent${channel.agents.length !== 1 ? 's' : ''} joining`} />
              )}
              {topLevelMessages.map(msg => (
                <MessageBubble key={msg.id} msg={msg} agents={channel.agents}
                  onReply={() => setThreadId(msg.id)}
                  onOpenThread={() => setThreadId(msg.id)}
                  onApprove={msg.msgType === 'approval' ? (action) => api.channelApproveAgent(channel.id, msg.fromId, action).catch(() => {}) : undefined} />
              ))}
              {channel.agents.filter(a => a.state === 'working' || a.state === 'tool_running').map(a => (
                <div key={a.id + '-typing'} className="flex items-center gap-2 px-3 py-1.5 text-[13px] text-muted animate-pulse">
                  <Badge variant="ok">{a.state === 'tool_running' ? <Wrench className="lucide-inline" /> : '●'}</Badge> <span className="font-medium">{a.role}</span> {a.state === 'tool_running' ? 'running tool…' : 'is working…'}
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>

            {/* Thread panel */}
            <AnimatePresence>
            {threadId && (() => {
              const parent = channel.messages.find(m => m.id === threadId)
              const replies = channel.messages.filter(m => m.threadId === threadId)
              return (
                <DetailPanel key="thread-panel" title="Thread" onClose={() => { setThreadId(null); setThreadInput('') }} initialWidth={320} minWidth={260} storageKey="mc-channel-thread-width" footer={
                  <MentionInput agents={channel.agents} value={threadInput} onChange={setThreadInput} onSend={async () => {
                    if (!threadInput.trim() || !threadId) return
                    await sendMessage(threadInput, threadId)
                    setThreadInput('')
                  }} />
                }>
                  <div className="flex flex-col gap-1 -mx-3 -mt-2">
                    {parent && <MessageBubble msg={parent} agents={channel.agents}
                      onApprove={parent.msgType === 'approval' ? (action) => api.channelApproveAgent(channel.id, parent.fromId, action).catch(() => {}) : undefined} />}
                    {replies.length > 0 && <div className="border-t border-border my-2" />}
                    {replies.map(msg => (
                      <MessageBubble key={msg.id} msg={msg} agents={channel.agents}
                        onApprove={msg.msgType === 'approval' ? (action) => api.channelApproveAgent(channel.id, msg.fromId, action).catch(() => {}) : undefined} />
                    ))}
                    {channel.agents.filter(a => a.state === 'working' || a.state === 'tool_running').map(a => (
                      <div key={a.id + '-typing-t'} className="flex items-center gap-2 px-2 py-1 text-[13px] text-muted animate-pulse">
                        <Badge variant="ok">{a.state === 'tool_running' ? <Wrench className="lucide-inline" /> : '●'}</Badge> <span className="font-medium">{a.role}</span> {a.state === 'tool_running' ? 'running tool…' : 'is working…'}
                      </div>
                    ))}
                  </div>
                </DetailPanel>
              )
            })()}
            </AnimatePresence>

            {showAgents && (
              <div className="w-64 shrink-0 border-l border-border flex flex-col bg-bg-elevated">
                <div className="px-3 py-2.5 border-b border-border flex items-center justify-between">
                  <span className="text-sm font-semibold text-text-strong">Agents</span>
                  <Btn onClick={() => setShowAgents(false)} aria-label="Close agents panel" className="!p-0 !border-none !rounded-none text-muted hover:text-text text-sm"><X className="lucide-inline" /></Btn>
                </div>
                <div className="flex-1 overflow-y-auto p-2 space-y-1">
                  {channel.agents.map((agent) => (
                    <AgentControlRow key={agent.id} agent={agent}
                      onDismiss={() => handleDismiss(agent.id)}
                      onListenChange={m => handleListenChange(agent.id, m)}
                      onClearContext={async () => {
                        if (!confirm(`Reset ${agent.role}'s LLM session. The channel's shared message history is preserved.`)) return
                        try {
                          await api.channelClearContext(channel.id, 'agent', agent.id)
                          const res = await api.channelGet(channel.id)
                          setChannels(prev => prev.map(c => c.id === channel.id ? mapChannel(res) : c))
                        } catch (e) { alert('Failed to clear context: ' + (e instanceof Error ? e.message : 'unknown error')) }
                      }} />
                  ))}
                </div>
                <div className="p-2 border-t border-border">
                  {showAddAgent ? (
                    <AddAgentForm onCancel={() => setShowAddAgent(false)} onAdd={async (role, task, agent) => {
                      if (!channel) return
                      setShowAddAgent(false)
                      try { await api.channelAddAgent(channel.id, { role, task: task || channel.topic, agent }) } catch (err) { setError(apiError(err, 'Failed to add agent')) }
                    }} />
                  ) : (
                    <Btn onClick={() => setShowAddAgent(true)} primary className="w-full">+ Add Agent</Btn>
                  )}
                </div>
              </div>
            )}
          </div>

          <div className="border-t border-border px-4 py-3">
            <div className="flex gap-2">
              <MentionInput agents={channel.agents} value={input} onChange={setInput} onSend={handleSend} />
              <Btn onClick={handleSend} primary>Send</Btn>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center">
          <EmptyState icon={<Users className="lucide-inline" />} title="Create a channel to get started" />
        </div>
      )}
    </div>
      </div>
    </>
  )
}
