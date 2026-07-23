import { useState, useEffect, useRef } from 'react'
import { Settings, Volume2 } from 'lucide-react'
import { createPortal } from 'react-dom'
import { useQueryClient, useQuery, useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { safeSetItem } from '../../utils/safeStorage'

export type ContentWidth = 'compact' | 'comfortable' | 'full'

/** Send-key mode: enter (Enter sends), ctrl-enter (Ctrl+Enter sends), enter-ctrl-newline (Enter sends, Ctrl+Enter = newline) */
export type SendMode = 'enter' | 'ctrl-enter' | 'enter-ctrl-newline'

export const CONTENT_WIDTH: Record<ContentWidth, { messages: string; input: string }> = {
  compact: { messages: '800px', input: '816px' },
  comfortable: { messages: '84%', input: '85%' },
  // 'full' = the widest single-pane width (keeps a small gutter so text doesn't
  // touch the window edge). Native grid panes force true edge-to-edge (100%)
  // via ChatPane's inline --mc-content-width, so this global constant stays at
  // the single-pane value — widening it here would silently change single-pane
  // "full" users who never opted into Split View.
  full: { messages: '92%', input: '93%' },
}

export interface ChatConfig {
  contentWidth: ContentWidth
  historyExpanded: boolean
  notifLimit: number
  showTimestamps: boolean
  showTurnStats: boolean
  sendOnEnter: SendMode
  collapseAllSteps: boolean
  confirmCloseSession: boolean
  simplifiedToolNames: boolean
  tagColumnsEnabled: boolean
  fileChipStyle: FileChipStyle
  followUpLayout: FollowUpLayout
  streamMode: StreamMode
  showContextPct: boolean
  defaultAutopilot: boolean
}

export type FileChipStyle = 'expanded' | 'minimal'
export type FollowUpLayout = 'multiline' | 'scroll'
/** Per-char streaming entrance animation. 'immediate' restores the pre-buffer
 *  behavior (raw chunk paint + tail glow only). */
export type StreamMode = 'immediate' | 'smooth'

const LS_KEY = 'mc-chat-config'
const DEFAULTS: ChatConfig = { historyExpanded: true, notifLimit: 50, showTimestamps: true, showTurnStats: true, sendOnEnter: 'enter', collapseAllSteps: true, confirmCloseSession: false, simplifiedToolNames: true, contentWidth: 'compact', tagColumnsEnabled: true, fileChipStyle: 'expanded', followUpLayout: 'scroll', streamMode: 'smooth', showContextPct: false, defaultAutopilot: false }

const VALID_FILE_CHIP_STYLES: ReadonlySet<FileChipStyle> = new Set(['expanded', 'minimal'])
const VALID_FOLLOW_UP_LAYOUTS: ReadonlySet<FollowUpLayout> = new Set(['multiline', 'scroll'])
const VALID_STREAM_MODES: ReadonlySet<StreamMode> = new Set(['immediate', 'smooth'])

/** Migrate legacy boolean sendOnEnter to new SendMode enum */
function migrateSendMode(raw: unknown): SendMode {
  if (raw === true) return 'enter'
  if (raw === false) return 'ctrl-enter'
  if (raw === 'enter' || raw === 'ctrl-enter' || raw === 'enter-ctrl-newline') return raw
  return 'enter'
}

export function loadChatConfig(): ChatConfig {
  try {
    const stored = JSON.parse(localStorage.getItem(LS_KEY) || '{}')
    const cfg = { ...DEFAULTS, ...stored, sendOnEnter: migrateSendMode(stored.sendOnEnter) }
    if (!(cfg.contentWidth in CONTENT_WIDTH)) cfg.contentWidth = 'compact'
    // Migrate legacy fileChipStyle values:
    //   'tooltip'                                 → 'minimal'  (renamed)
    //   'pebble' / 'full' / 'compact'             → 'expanded' (Pebble renamed; Full/Compact removed)
    //   'expanded-aurora' / 'expanded-domed'      → 'expanded' (Aurora & Domed previewed then dropped)
    const legacy = cfg.fileChipStyle as string
    if (legacy === 'tooltip') cfg.fileChipStyle = 'minimal'
    else if (legacy === 'pebble' || legacy === 'full' || legacy === 'compact'
          || legacy === 'expanded-aurora' || legacy === 'expanded-domed') cfg.fileChipStyle = 'expanded'
    if (!VALID_FILE_CHIP_STYLES.has(cfg.fileChipStyle)) cfg.fileChipStyle = 'expanded'
    if (!VALID_FOLLOW_UP_LAYOUTS.has(cfg.followUpLayout)) cfg.followUpLayout = 'scroll'
    if (!VALID_STREAM_MODES.has(cfg.streamMode)) cfg.streamMode = 'smooth'
    if (typeof cfg.showContextPct !== 'boolean') cfg.showContextPct = false
    if (typeof cfg.showTurnStats !== 'boolean') cfg.showTurnStats = true
    return cfg
  }
  catch { return { ...DEFAULTS } }
}

export function saveChatConfig(cfg: ChatConfig) {
  safeSetItem(LS_KEY, JSON.stringify(cfg))
  window.dispatchEvent(new Event('mc-config-changed'))
}

export interface DashboardConfig {
  restore_sessions: boolean
  restore_window_minutes: number
  merge_queued_messages: boolean
  widget_density: 'more' | 'less'
  quick_send: boolean
  session_grid: boolean
  tail_fork_enabled: boolean
}

export default function ChatSettings({ config, onChange }: { config: ChatConfig; onChange: (c: ChatConfig) => void }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()
  const { data: dashCfg = { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' as const, quick_send: false, session_grid: false, tail_fork_enabled: false } } = useQuery<DashboardConfig>({ queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig() })
  const dashMut = useMutation({
    mutationFn: (next: DashboardConfig) => api.updateDashboardConfig(next),
    onMutate: async (next) => {
      await queryClient.cancelQueries({ queryKey: ['dashboardConfig'] })
      const prev = queryClient.getQueryData<DashboardConfig>(['dashboardConfig'])
      queryClient.setQueryData(['dashboardConfig'], next)
      return { prev }
    },
    onError: (_err, _vars, ctx) => { if (ctx?.prev) queryClient.setQueryData(['dashboardConfig'], ctx.prev) },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['dashboardConfig'] }),
  })
  const [voiceCfg, setVoiceCfg] = useState({ enabled: false, voice: 'Ruth', engine: 'generative', rate: '100%', pitch: '+0%', autoSpeak: false, aws_profile: '', region: '' })
  const [localAwsProfile, setLocalAwsProfile] = useState('')
  const [localAwsRegion, setLocalAwsRegion] = useState('')

  useEffect(() => {
    api.voiceConfig().then(cfg => { setVoiceCfg(cfg); setLocalAwsProfile(cfg.aws_profile || ''); setLocalAwsRegion(cfg.region || '') }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      if (btnRef.current?.contains(e.target as Node)) return
      if (popoverRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    const t = setTimeout(() => document.addEventListener('click', close), 0)
    return () => { clearTimeout(t); document.removeEventListener('click', close) }
  }, [open])

  const set = <K extends keyof ChatConfig>(k: K, v: ChatConfig[K]) => {
    const next = { ...config, [k]: v }
    saveChatConfig(next)
    onChange(next)
  }

  const setDash = (patch: Partial<DashboardConfig>) => {
    dashMut.mutate({ ...dashCfg, ...patch })
  }

  const setVoice = (patch: Partial<typeof voiceCfg>) => {
    const prev = voiceCfg
    const next = { ...voiceCfg, ...patch }
    setVoiceCfg(next)
    window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: next }))
    api.updateVoiceConfig(patch).catch(() => {
      setVoiceCfg(prev)
      setLocalAwsProfile(prev.aws_profile || '')
      setLocalAwsRegion(prev.region || '')
      window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: prev }))
    })
  }

  return (
    <>
      <button ref={btnRef} className="text-[18px] text-muted cursor-pointer hover:text-text transition-all" onClick={() => setOpen(!open)} title="Chat settings" aria-label="Chat settings"><Settings className="lucide-inline" /></button>
      {open && btnRef.current && createPortal(
        <div ref={popoverRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg w-[280px] p-3 flex flex-col gap-3 animate-slide-up" style={(() => { const r = btnRef.current!.getBoundingClientRect(); const top = r.bottom + 6; const left = Math.max(8, Math.min(r.left, window.innerWidth - 288)); return { top, left } })()}>
          <div className="text-[13px] font-semibold text-text-strong border-b border-border pb-2">Chat Settings</div>
          <Toggle label="Session tag columns" hint="Enable the Trello-style column strip in the sidebar. When disabled, session list is a single flat lane." checked={config.tagColumnsEnabled} onChange={v => set('tagColumnsEnabled', v)} />
          <Toggle label="History expanded by default" checked={config.historyExpanded} onChange={v => set('historyExpanded', v)} />
          <Toggle label="Show message timestamps" checked={config.showTimestamps} onChange={v => set('showTimestamps', v)} />
          <Toggle label="Show elapsed time and credits" hint="Display per-turn usage beneath completed assistant responses" checked={config.showTurnStats} onChange={v => set('showTurnStats', v)} />
          <Toggle label="Simplified tool call names" hint="When enabled, tool pills show purpose instead of exact command" checked={config.simplifiedToolNames} onChange={v => set('simplifiedToolNames', v)} />
          <Toggle label="Show context percentage" hint="Display usage percentage next to the context bar" checked={config.showContextPct} onChange={v => set('showContextPct', v)} />
          <div className="flex items-center justify-between">
            <div className="flex flex-col gap-0.5">
              <span className="text-[13px] text-text">Send shortcut</span>
              <span className="text-[11px] text-muted">{config.sendOnEnter === 'enter' ? 'Shift+Enter for newline' : config.sendOnEnter === 'ctrl-enter' ? 'Enter for newline' : `${navigator.platform?.includes('Mac') ? '⌘' : 'Ctrl'}+Enter for newline`}</span>
            </div>
            <select className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none cursor-pointer" value={config.sendOnEnter} onChange={e => set('sendOnEnter', e.target.value as SendMode)}>
              <option value="enter">Enter sends</option>
              <option value="ctrl-enter">{navigator.platform?.includes('Mac') ? '⌘' : 'Ctrl'}+Enter sends</option>
              <option value="enter-ctrl-newline">Enter sends, {navigator.platform?.includes('Mac') ? '⌘' : 'Ctrl'}+Enter newline</option>
            </select>
          </div>
          <Toggle label="Quick Send" hint="Click a suggested reply to send it instantly. Shift+Click to select multiple." checked={dashCfg.quick_send} onChange={v => setDash({ quick_send: v })} />
          <div className="flex items-center justify-between">
            <span className="text-[13px] text-muted">Notification limit</span>
            <select className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none cursor-pointer" value={config.notifLimit} onChange={e => set('notifLimit', Number(e.target.value))}>
              {[25, 50, 100, 200].map(n => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
          <div className="border-t border-border pt-2 mt-1">
            <div className="text-[11px] font-semibold text-muted uppercase tracking-wide mb-2">Startup</div>
            <Toggle label="Restore sessions on restart" hint="Re-open chats active within the time window" checked={dashCfg.restore_sessions} onChange={v => setDash({ restore_sessions: v })} />
            {dashCfg.restore_sessions && (
              <div className="flex items-center justify-between mt-2">
                <span className="text-[13px] text-muted">Restore window</span>
                <select className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none cursor-pointer" value={dashCfg.restore_window_minutes} onChange={e => setDash({ restore_window_minutes: Number(e.target.value) })}>
                  {[15, 30, 60, 120, 360, 720, 1440].map(n => <option key={n} value={n}>{n < 60 ? `${n}m` : `${n/60}h`}</option>)}
                  <option value={0}>No limit</option>
                </select>
              </div>
            )}
          </div>
          <div className="border-t border-border pt-2 mt-1">
            <div className="text-[11px] font-semibold text-muted uppercase tracking-wide mb-2"><Volume2 className="lucide-inline" /> Voice</div>
            <Toggle label="Auto-speak responses" hint="Speak every assistant reply automatically" checked={voiceCfg.autoSpeak} onChange={v => setVoice({ autoSpeak: v, ...(v ? { enabled: true } : {}) })} />
            <div className="flex items-center justify-between mt-2">
              <span className="text-[13px] text-muted">Voice</span>
              <select className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none cursor-pointer" value={voiceCfg.voice} onChange={e => setVoice({ voice: e.target.value })}>
                {[['Ruth','Ruth (US F)'],['Matthew','Matthew (US M)'],['Arthur','Arthur (UK M)'],['Brian','Brian (UK M)'],['Amy','Amy (UK F)'],['Joanna','Joanna (US F)'],['Stephen','Stephen (US M)'],['Gregory','Gregory (US M)'],['Danielle','Danielle (US F)']].map(([v,l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            <div className="flex items-center justify-between mt-2">
              <span className="text-[13px] text-muted">Engine</span>
              <select className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none cursor-pointer" value={voiceCfg.engine} onChange={e => setVoice({ engine: e.target.value })}>
                <option value="generative">Generative</option>
                <option value="neural">Neural</option>
                <option value="long-form">Long-form</option>
                <option value="standard">Standard</option>
              </select>
            </div>
            <div className="flex items-center justify-between mt-2">
              <span className="text-[13px] text-muted">Speed</span>
              <select className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none cursor-pointer" value={voiceCfg.rate} onChange={e => setVoice({ rate: e.target.value })}>
                {['80%','90%','95%','100%','110%','120%','130%','150%'].map(r => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            <div className="flex items-center justify-between mt-2">
              <span className="text-[13px] text-muted">AWS Profile</span>
              <input aria-label="AWS Profile" className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none w-[120px]" placeholder="default" value={localAwsProfile} onChange={e => setLocalAwsProfile(e.target.value)} onBlur={e => setVoice({ aws_profile: e.target.value.trim() })} />
            </div>
            <div className="flex items-center justify-between mt-2">
              <span className="text-[13px] text-muted">AWS Region</span>
              <input aria-label="AWS Region" className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none w-[120px]" placeholder="us-east-1" value={localAwsRegion} onChange={e => setLocalAwsRegion(e.target.value)} onBlur={e => setVoice({ region: e.target.value.trim() })} />
            </div>
          </div>
        </div>,
        document.body
      )}
    </>
  )
}

function Toggle({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className="flex items-center justify-between w-full text-left bg-transparent border-none p-0 cursor-pointer group"
    >
      <div>
        <span className="text-[13px] text-muted group-hover:text-text transition-colors">{label}</span>
        {hint && <div className="text-[11px] text-muted/60">{hint}</div>}
      </div>
      <div className={`w-9 h-5 rounded-full relative transition-colors ${checked ? 'bg-accent' : 'bg-border'}`}>
        <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-4' : 'translate-x-0.5'}`} />
      </div>
    </button>
  )
}
