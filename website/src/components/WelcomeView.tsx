import { useState, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Droplet, EyeOff, Ghost, RefreshCw, Undo2, VenetianMask } from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KiroGhost } from './KiroGhost'
import { useTheme } from '../hooks/useTheme'
import { getThemeBranding } from '../themeBranding'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
interface WelcomeViewProps {
  mode?: string
  setInput: (v: string) => void
  memoryMode?: string
  onSwitchMode?: (mode: 'persistent' | 'incognito' | 'temporary') => void
  cleanMode?: boolean
  onToggleClean?: (clean: boolean) => void
}

const FALLBACK_SUGGESTIONS = [
  'Check my pipeline status',
  'Triage my on-call tickets',
  'Search code for usage examples',
  'Summarize this week\'s Slack activity',
  'Help me write a design doc',
  'Review my latest CR',
]

function SuggestedPills({ setInput }: { setInput: (v: string) => void }) {
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  const { data, isFetching } = useQuery({
    queryKey: ['suggestions'],
    queryFn: () => api.suggestions(),
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
    refetchOnWindowFocus: false,
  })

  const pills = data?.suggestions?.length ? data.suggestions : FALLBACK_SUGGESTIONS

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await api.suggestions(true)
      qc.setQueryData(['suggestions'], fresh)
    } catch {}
    setRefreshing(false)
  }

  const spinning = isFetching || refreshing

  return (
    <div className="flex gap-x-2 gap-y-1 flex-wrap justify-center max-w-[760px] mx-auto w-full items-center">
      {pills.map(s => (
        // type=button + onMouseDown preventDefault stop the pill from taking
        // keyboard focus on click. Without this the focused pill is re-activated
        // by a follow-up Enter (re-firing setInput) instead of submitting via the
        // textarea, so the prompt appears to clear instead of send.
        <button key={s} type="button" onMouseDown={e => e.preventDefault()} className="shrink-0 px-3 py-1.5 rounded-lg text-[13px] cursor-pointer transition-all relative border border-border text-muted hover:text-text bg-bg-elevated" onClick={() => setInput(s)}>
          {s}
        </button>
      ))}
      <button
        onClick={handleRefresh}
        disabled={spinning}
        className="p-1.5 rounded-lg text-muted hover:text-accent border border-transparent hover:border-border transition-all cursor-pointer bg-transparent"
        title={i18nT('components.welcomeView.refresh_suggestions')}
        aria-label={i18nT('components.welcomeView.refresh_suggestions')}
      >
        <RefreshCw size={13} className={spinning ? 'animate-spin' : ''} />
      </button>
    </div>
  )
}

export default function WelcomeView({
  mode,
  setInput,
  memoryMode,
  onSwitchMode,
  cleanMode,
  onToggleClean,
}: WelcomeViewProps) {
  const [anonOpen, setAnonOpen] = useState(false)
  const anonBtnRef = useRef<HTMLButtonElement>(null)
  const anonPopRef = useRef<HTMLDivElement>(null)

  // Close anon popover on outside click
  useEffect(() => {
    if (!anonOpen) return
    const handler = (e: MouseEvent) => {
      const t = e.target as Node
      if (anonPopRef.current?.contains(t) || anonBtnRef.current?.contains(t)) return
      setAnonOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [anonOpen])

  const currentMode = (memoryMode ?? 'persistent') as 'persistent' | 'incognito' | 'temporary'

  // Per-theme brand mark: a registered theme (via the themeBranding seam) may
  // supply its own logo — render it here too, not just in the App shell, so the
  // welcome screen matches the active theme. Falls back to the stock KiroGhost
  // when the theme registers no logo (the standalone build always does).
  const { colorTheme } = useTheme()
  const brandLogo = getThemeBranding(colorTheme)?.logo
  const brandMark = brandLogo
    ? <img src={brandLogo} alt="" aria-hidden="true" className="w-16 h-16 drop-shadow-lg shrink-0 animate-float rounded-md object-contain" />
    : <KiroGhost size={64} className="drop-shadow-lg shrink-0 animate-float" />

  return (
    <div className="flex flex-col items-center w-full gap-6 px-8">
      {mode === 'orchestrator' && brandMark}
      <div className="text-center">
        <div className="flex items-center justify-center gap-4">
          {mode !== 'orchestrator' && brandMark}
          <h2 className="text-5xl font-light text-text-strong tracking-tight">{mode === 'orchestrator' ? i18nT('components.welcomeView.autopilot') : i18nT('components.welcomeView.what_can_i_do_for_you')}</h2>
          {mode !== 'orchestrator' && <div className="w-[64px] shrink-0" />}
        </div>
        {mode === 'orchestrator' && <p className="text-[13px] text-muted mt-1">{i18nT('components.welcomeView.simple_tasks_run_instantly_complex_ones_get_a_pl')}</p>}
      </div>
      {mode === 'orchestrator' && (
        <button
          className="px-4 py-2 rounded-lg text-[13px] text-muted border border-border bg-card hover:border-accent hover:text-text transition-all cursor-pointer"
          onClick={() => setInput('Create a plan to analyze KiroCrew code package and report file count by major components')}
        >
          {i18nT('components.welcomeView.try_create_a_plan_to_analyze_kirocrew_code_packa')}
        </button>
      )}
      {(onSwitchMode || onToggleClean) && (
        <>
          {(() => {
            // Clean supersedes the memory mode, so it counts as "ephemeral" for
            // the trigger: an active clean OR a non-persistent memory mode means
            // we're in some ephemeral state and the button offers to go back.
            const ephemeralActive = cleanMode || currentMode !== 'persistent'
            return (
              <button
                ref={anonBtnRef}
                className="flex items-center gap-1.5 text-[12px] text-muted hover:text-warn transition-colors"
                onClick={() => {
                  if (!ephemeralActive) setAnonOpen(!anonOpen)
                  // Returning to default must fire exactly ONE recreation. Both
                  // handlers do create-first-then-delete, so calling both leaks a
                  // slot (two creates, one delete). Clean supersedes the memory
                  // mode, so clear clean if it's on; otherwise reset memory mode.
                  else if (cleanMode) onToggleClean?.(false)
                  else onSwitchMode?.('persistent')
                }}
              >
                {!ephemeralActive ? <Ghost size={13} /> : <Undo2 size={13} />}
                <span>{!ephemeralActive ? i18nT('components.welcomeView.switch_to_ephemeral_mode') : i18nT('components.welcomeView.switch_back_to_default_mode')}</span>
              </button>
            )
          })()}
          {anonOpen && createPortal(
            <div
              ref={anonPopRef}
              className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl p-2 flex gap-2"
              style={(() => { const r = anonBtnRef.current?.getBoundingClientRect(); return { top: r ? r.bottom + 6 : '50%', left: r ? r.left + r.width / 2 : '50%', transform: 'translateX(-50%)' } })()}
            >
              {onSwitchMode && ([
                { key: 'incognito' as const, Icon: EyeOff, label: 'Incognito', desc: 'Memory reads enabled, writes disabled', color: 'text-warn' },
                { key: 'temporary' as const, Icon: VenetianMask, label: 'Temporary', desc: 'Blank slate — no memory reads or writes', color: 'text-aim' },
              ] as const).map(t => (
                <button
                  key={t.key}
                  className="w-[220px] p-3 rounded-lg border border-border hover:border-accent hover:bg-bg-hover transition-all text-left flex flex-col gap-1.5"
                  onClick={() => { onSwitchMode(t.key); setAnonOpen(false) }}
                >
                  <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text">
                    <t.Icon size={14} className={t.color} />
                    <span>{t.label}</span>
                  </div>
                  <div className="text-[11px] text-muted leading-snug">{t.desc}</div>
                </button>
              ))}
              {/* Clean is a peer option in this group, but it is NOT a memory
                  mode — it picks no memory_mode. It supersedes them entirely:
                  the agent runs with its own identity only, no KiroCrew context
                  or MCP servers injected. */}
              {onToggleClean && (
                <button
                  className="w-[220px] p-3 rounded-lg border border-border hover:border-accent hover:bg-bg-hover transition-all text-left flex flex-col gap-1.5"
                  onClick={() => { onToggleClean(true); setAnonOpen(false) }}
                >
                  <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text">
                    <Droplet size={14} className="text-accent" />
                    <span>{i18nT('components.welcomeView.clean')}</span>
                  </div>
                  <div className="text-[11px] text-muted leading-snug">{i18nT('components.welcomeView.agent_only_no_kirocrew_context_or_mcp')}</div>
                </button>
              )}
            </div>,
            document.body
          )}
        </>
      )}
      {mode !== 'orchestrator' && <SuggestedPills setInput={setInput} />}
    </div>
  )
}
