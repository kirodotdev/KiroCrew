import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Bot } from 'lucide-react'
import AppIcon from '../AppIcon'
import ContextBar, { contextTip, contextColor, composeContextReadout, contextPctClamped, fmtTokens } from '../ContextBar'
import ErrorNotice from '../ErrorNotice'
import HarnessMark from '../HarnessMark'
import { Btn, Slider } from '../ui'
import { acpBackendName } from '../../api/acpBackend'
import { useAnchorRemeasure } from '../../hooks/useAnchorRemeasure'
import { effortLabel } from '../../lib/effort'
import { fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { ROUTING_PREFIX_RE } from '../../providers/modelRegistry'
import type { GatewayToolsState } from '../../lib/mcpSessionReport'
import type { ComposerControl } from '../composerControl'
import type { ChatInputProps } from './props'
import type { useAutoCompactThreshold } from './autoCompact'

/** Catalog KEY per gateway-tools state, a flat Record of full literal keys
 *  indexed inline at the `i18nT()` call so the key checker can resolve it
 *  (the `EFFORT_LABEL_KEY` shape). A missing state's key is its notice's sentence. */
const GATEWAY_TOOLS_LABEL_KEY: Record<GatewayToolsState, string> = {
  connected: 'components.chatInput.gateway_tools_connected',
  sent: 'components.chatInput.gateway_tools_sent',
  pending: 'components.chatInput.gateway_tools_pending',
  not_sent: 'components.chatInput.gateway_tools_not_sent',
  failed: 'components.chatInput.gateway_tools_failed',
}

/** A session that did not receive Kiro Crew's own tools: the agent chip warns. */
export function gatewayToolsMissing(state: GatewayToolsState | null | undefined): state is 'not_sent' | 'failed' {
  return state === 'not_sent' || state === 'failed'
}

/* The context shelf under the composer: its measured width (which collapses
   the chips to icons) and the controls that stand on it -- app session
   controls, the agent chip, the context readout with its popover, and the
   model chip. The shelf row and the project pill are the composer's own. */
export function useShelfMeasure() {
  // Shelf responsiveness: measure the shelf row width and collapse chips to
  // icon-only (agent/project) + drop the model effort label when space is tight.
  // Truncation handles the in-between cases.
  const [shelfWidth, setShelfWidth] = useState(9999)
  // Border-box height of the shelf, handed to `.glass-shelf::before` as
  // `--glass-shelf-h`: the fade under the shelf is positioned against the
  // dock's input-area wrapper (so it spans exactly the dock root, which already
  // stops short of the scrollbar gutter), not against the shelf, so it has to
  // be told how tall the shelf is to start at the pane's bottom edge. 32px is
  // the one-row shelf (pt-1 + h-7) for the first paint and for environments
  // without ResizeObserver.
  const [shelfHeight, setShelfHeight] = useState(32)
  const shelfRoRef = useRef<ResizeObserver | null>(null)
  const shelfRef = useCallback((el: HTMLDivElement | null) => {
    shelfRoRef.current?.disconnect()
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width
      if (typeof w === 'number') setShelfWidth(w)
      const h = entries[0]?.borderBoxSize?.[0]?.blockSize ?? entries[0]?.target.getBoundingClientRect().height
      if (typeof h === 'number' && h > 0) setShelfHeight(h)
    })
    ro.observe(el)
    shelfRoRef.current = ro
  }, [])
  // Below ~340px the labels no longer fit comfortably alongside the context bar
  // + model chip, so collapse the chips (agent/project) to icon-only.
  const shelfCompact = shelfWidth < 340
  // A two-column split can leave under 200px per composer. The effort level on
  // the model chip is the only at-a-glance readout of what a turn runs at, so
  // it survives the compact collapse and drops only when even a short word has
  // no room (the picker the chip opens always shows the level in force).
  const shelfTiny = shelfWidth < 220
  return { shelfRef, shelfHeight, shelfCompact, shelfTiny }
}

/** The context popover: open state and its outside-click dismissal. */
export function useContextPopover() {
  const [ctxPopoverOpen, setCtxPopoverOpen] = useState(false)
  const ctxWrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ctxPopoverOpen) return
    const handler = (e: MouseEvent) => {
      if (ctxWrapRef.current && !ctxWrapRef.current.contains(e.target as Node)) setCtxPopoverOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ctxPopoverOpen])
  return { ctxPopoverOpen, setCtxPopoverOpen, ctxWrapRef }
}

/** App-contributed session controls (`contributes.sessionControls`). */
export function SessionControlChips({ sessionControls, shelfCompact, onSessionControlClick }: {
  sessionControls: NonNullable<ChatInputProps['sessionControls']>
  shelfCompact: boolean
  onSessionControlClick: ChatInputProps['onSessionControlClick']
}) {
  return (
    <div className="flex items-center gap-2 min-w-0 shrink-0 pr-2 border-r border-border">
  {(sessionControls || []).map(sc => {
    /* State must not be carried by colour alone: `ok` and `warn` differ
       only by tint, which a colourblind user cannot separate and a
       screen reader never sees at all. Fold it into the accessible name,
       and APPEND the app's own tooltip rather than replacing the label —
       the label is what identifies the control, so it has to survive
       whatever the app reports about it. */
    const stateWord =
      sc.state === 'warn'
        ? i18nT('components.chatInput.session_control_needs_attention')
        : sc.state === 'ok'
          ? i18nT('components.chatInput.session_control_ready')
          : ''
    // The app's `detail` follows the manifest label on the chip itself, so
    // the label still names the control whatever the app reports.
    const shown = sc.detail
      ? i18nT('components.chatInput.session_control_chip_label', {
          label: sc.label,
          detail: sc.detail,
        })
      : sc.label
    const detail = sc.statusTooltip || stateWord
    const chipName = detail
      ? i18nT('components.chatInput.session_control_chip_label', {
          label: shown,
          detail,
        })
      : shown
    return (
    <button
      key={sc.key}
      /* No `font-mono`: same reasoning as the agent chip below — a
         control label is a label, not code, and pinning `var(--mono)`
         would make the shelf ignore the user's Font Family setting. */
      className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer ${
        /* Open wins, so the chip you are pointing at always reads as
           the active one; otherwise the app's own state colours it. */
        sc.active
          ? 'text-accent'
          : sc.state === 'ok'
            ? 'text-ok'
            : sc.state === 'warn'
              ? 'text-warn'
              : 'text-muted hover:text-text'
      }`}
      onClick={e => onSessionControlClick?.(sc.key, e.currentTarget.getBoundingClientRect(), e.currentTarget)}
      // Marks the chip as part of its own popover for dismissal
      // purposes: mousedown fires before click, so without this the
      // host's outside-click closes the popover and the chip's toggle
      // then re-opens it — a flicker instead of a dismissal.
      data-session-control-chip=""
      title={chipName}
      aria-label={chipName}
    >
      <AppIcon icon={sc.icon} size={13} />
      {!shelfCompact && <span className="truncate max-w-[140px]">{shown}</span>}
    </button>
    )
  })}
    </div>
  )
}

/** The agent chip. Chrome type: an agent name is a label, not code. `font-mono`
 *  would pin `var(--mono)`, which Settings → Display → Font Family never
 *  writes, so it would make the shelf ignore the user's typeface. */
export function AgentChip({ agentName, agentLabel, agentIsInheritedDefault, agentSource, gatewayTools, isRunning, shelfCompact, onAgentClick }: {
  agentName: string
  agentLabel?: string
  agentIsInheritedDefault?: boolean
  agentSource?: string
  gatewayTools?: GatewayToolsState | null
  isRunning: boolean
  shelfCompact: boolean
  onAgentClick: NonNullable<ChatInputProps['onAgentClick']>
}) {
  // Inherited default: explain what the ` . default` marker means, on
  // hover (title) AND keyboard focus / screen readers (aria-label),
  // because the marker alone reads as opaque (#8770 UX). No glyph, no
  // layout change -- text on demand. A pinned chip keeps the plain
  // switch hint; it has nothing to explain.
  const switchHint = isRunning
    ? i18nT('components.chatInput.stop_the_current_response_to_switch_agents')
    : agentIsInheritedDefault
      ? i18nT('components.chatInput.agent_inherited_default', { name: agentName })
      : i18nT('components.chatInput.agent', { name: agentName })
  // A session without Kiro Crew's own tools fails every tool the agent is told
  // it has, so the chip says so before the first turn rather than after it.
  const toolsMissing = gatewayToolsMissing(gatewayTools)
  const chipName = toolsMissing
    ? `${switchHint} · ${i18nT('components.chatInput.gateway_tools_missing')}`
    : switchHint
  return (
    <button
      className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent ${agentSource === 'package' ? 'text-[var(--aim)] hover:text-[var(--aim)]' : 'text-muted hover:text-text disabled:hover:text-muted'}`}
      onClick={e => onAgentClick(e.currentTarget.getBoundingClientRect(), e.currentTarget)}
      disabled={isRunning}
      title={chipName}
      aria-label={chipName}
    >
      <Bot size={13} className="shrink-0 opacity-70" />
      {!shelfCompact && <span className="truncate max-w-[160px]">{agentLabel ?? agentName}</span>}
      {toolsMissing && <AlertTriangle size={12} className="shrink-0 text-warn" aria-hidden="true" data-testid="agent-chip-tools-missing" />}
      {/* Spelled out on every shelf width: a phone has no hover to read the title. */}
      {toolsMissing && <span className="shrink-0 text-warn" aria-hidden="true">{i18nT('components.chatInput.gateway_tools_missing_short')}</span>}
    </button>
  )
}

/** The context-window readout and its popover (usage, model, the per-session
 *  auto-compact threshold). */
export function ContextUsageControl({ contextPct, contextUsedTokens, contextWindowTokens, showContextPct, showContextTokens, shelfCompact, modelName, sessionBackend, gatewayTools, ctxPopoverOpen, setCtxPopoverOpen, ctxWrapRef, autoCompactThreshold }: {
  contextPct: number
  contextUsedTokens?: number
  contextWindowTokens?: number
  showContextPct?: boolean
  showContextTokens?: boolean
  shelfCompact: boolean
  modelName?: string
  sessionBackend?: string | null
  gatewayTools?: GatewayToolsState | null
  ctxPopoverOpen: boolean
  setCtxPopoverOpen: (update: (open: boolean) => boolean) => void
  ctxWrapRef: React.RefObject<HTMLDivElement>
  autoCompactThreshold: ReturnType<typeof useAutoCompactThreshold>
}) {
  const { autoCompactQuery, autoCompact, autoCompactError, setAutoCompactError, pushAutoCompact } = autoCompactThreshold
  const pct = Math.round(contextPct)
  const win = contextWindowTokens || 0
  const used = contextUsedTokens != null ? contextUsedTokens : (win ? Math.round((pct / 100) * win) : 0)
  const remaining = win ? Math.max(win - used, 0) : 0
  const approx = contextUsedTokens == null
  const pctColor = contextColor(contextPct)
  const showAnyReadout = !!(showContextPct || showContextTokens)
  // Graceful degrade: on a narrow shelf, collapse to the percentage
  // alone (or tokens, if that's the only segment enabled) so the
  // readout never crowds out the agent/model controls.
  const readout = shelfCompact
    ? composeContextReadout(contextPct, used, win, { approx, showPct: showContextPct, showTokens: !!showContextTokens && !showContextPct })
    : composeContextReadout(contextPct, used, win, { approx, showPct: showContextPct, showTokens: showContextTokens })
  const [ctxRect, setCtxRect] = useState<DOMRect | null>(null)
  const measureCtx = useCallback(() => {
    if (ctxWrapRef.current) setCtxRect(ctxWrapRef.current.getBoundingClientRect())
  }, [ctxWrapRef])
  // The popover is placed from its chip's rect, which a rotation or the mobile
  // keyboard moves while it is open.
  useAnchorRemeasure(ctxPopoverOpen, measureCtx)
  // Right-aligned to the chip, then kept 8px inside the viewport: on a phone the
  // chip can sit at the shelf's left edge, where a right-aligned box opens off-screen.
  const popoverWidth = Math.min(208, window.innerWidth - 16)
  const popoverLeft = ctxRect
    ? Math.max(8, Math.min(ctxRect.right - popoverWidth, window.innerWidth - popoverWidth - 8)) - ctxRect.left
    : undefined
  // A wrapped served id breaks after its routing prefix rather than mid-name.
  const routingPrefix = modelName?.match(ROUTING_PREFIX_RE)?.[0] ?? ''
  return (
  <div ref={ctxWrapRef} className="relative flex items-center">
    <button
      className={`inline-flex items-center h-7 px-2.5 rounded-md transition-colors border-none cursor-pointer ${ctxPopoverOpen ? 'bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]' : 'bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]'}`}
      onClick={() => {
        if (!ctxPopoverOpen) measureCtx()
        setCtxPopoverOpen(o => !o)
      }}
      title={contextTip(contextPct)}
      aria-label={i18nT('components.chatInput.context_usage')}
    >
      <ContextBar pct={contextPct} width={40} height={3} />
      {showAnyReadout && <span className="text-[11px] ml-1.5 tabular-nums whitespace-nowrap" style={{ color: pctColor }}>{readout}</span>}
    </button>
    {ctxPopoverOpen && (
      <div className="absolute bottom-full mb-1 z-[60] rounded-xl border border-border bg-bg-elevated shadow-xl p-3 animate-slide-up" style={{ width: popoverWidth, left: popoverLeft }} data-testid="context-usage-popover">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[11px] font-semibold text-text">{i18nT('components.chatInput.context_window')}</span>
                  <span className="text-[12px] font-mono font-bold" style={{ color: pctColor }}>{fmtPercent(contextPctClamped(contextPct) / 100)}</span>
                </div>
                <div className="flex flex-col gap-1 text-[11px] font-mono">
                  <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.used')}</span><span className="text-text">{approx ? '~' : ''}{fmtTokens(used)}</span></div>
                  <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.remaining')}</span><span className="text-text">{approx ? '~' : ''}{fmtTokens(remaining)}</span></div>
                  <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.total')}</span><span className="text-text">{fmtTokens(win)}</span></div>
                </div>
                {(modelName || sessionBackend != null || gatewayTools) && (
                  <div className="mt-2 pt-2 border-t border-border flex flex-col gap-1 text-[11px] font-mono">
                    {modelName && (
                      // A phone has no hover for a title, so the full id wraps instead of truncating.
                      <div className="flex justify-between gap-2" data-testid="context-model"><span className="text-muted shrink-0">{i18nT('components.chatInput.model')}</span><span className="text-text text-right min-w-0 [overflow-wrap:anywhere]">{routingPrefix}<wbr />{modelName.slice(routingPrefix.length)}</span></div>
                    )}
                    {/* What the live session reports, so a config switch or a
                        member route that moved the backend reads here. A value
                        too long for its label's line wraps under it, never cut. */}
                    {sessionBackend != null && (
                      <div className="flex flex-wrap justify-between gap-x-2" data-testid="context-session-backend"><span className="text-muted whitespace-nowrap">{i18nT('components.chatInput.agent_backend')}</span><span className="ml-auto text-right text-text">{acpBackendName({ id: sessionBackend })}</span></div>
                    )}
                    {gatewayTools && !gatewayToolsMissing(gatewayTools) && (
                      <div className="flex flex-wrap justify-between gap-x-2" data-testid="context-gateway-tools"><span className="text-muted whitespace-nowrap">{i18nT('components.chatInput.gateway_tools')}</span><span className="ml-auto text-right text-text">{i18nT(GATEWAY_TOOLS_LABEL_KEY[gatewayTools])}</span></div>
                    )}
                  </div>
                )}
                {gatewayTools === 'not_sent' && (
                  <div className="mt-2 pt-2 border-t border-border">
                    {/* Not an ErrorNotice: nothing failed. The agent's tools list or a
                        project settings file withheld them, so this wears the warn status. */}
                    <div role="status" data-testid="context-gateway-tools-missing" className="flex items-start gap-1.5 rounded-md border border-warn/20 bg-warn-subtle px-2 py-1.5 text-[12px] text-warn">
                      <AlertTriangle size={14} className="shrink-0 mt-px" aria-hidden="true" />
                      <span className="min-w-0 [overflow-wrap:anywhere]">{i18nT(GATEWAY_TOOLS_LABEL_KEY[gatewayTools])}</span>
                    </div>
                  </div>
                )}
                {gatewayTools === 'failed' && (
                  <div className="mt-2 pt-2 border-t border-border">
                    {/* No hand-off: the composer draft below is unsaved. */}
                    <ErrorNotice
                      variant="inline"
                      testId="context-gateway-tools-missing"
                      message={i18nT(GATEWAY_TOOLS_LABEL_KEY[gatewayTools])}
                    />
                  </div>
                )}
                {autoCompactQuery.isLoading && (
                  <div className="mt-2 pt-2 border-t border-border" aria-hidden="true">
                    <div className="h-4 mb-1 rounded bg-bg-hover animate-pulse" />
                    <div className="h-5 rounded bg-bg-hover animate-pulse" />
                  </div>
                )}
                {autoCompactQuery.isError && !autoCompact && (
                  <div className="mt-2 pt-2 border-t border-border">
                    {/* No hand-off: the composer draft below is unsaved. */}
                    <ErrorNotice
                      variant="inline"
                      testId="auto-compact-load-error"
                      message={i18nT('components.chatInput.auto_compact_load_failed')}
                    />
                  </div>
                )}
                {autoCompactError && (
                  <div className="mt-2 pt-2 border-t border-border">
                    {/* No hand-off: same composer draft. The shared notice toast is
                        transient; the write that did not persist is reported HERE,
                        next to the slider whose value snapped back. */}
                    <ErrorNotice
                      variant="inline"
                      testId="auto-compact-write-error"
                      message={autoCompactError}
                      onDismiss={() => setAutoCompactError('')}
                    />
                  </div>
                )}
                {autoCompact && (
                  <div className="mt-2 pt-2 border-t border-border">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[11px] text-muted">{i18nT('components.chatInput.auto_compact_at')}</span>
                      <span className="text-[12px] font-mono font-bold text-accent">{fmtPercent(Math.round(autoCompact.pct ?? autoCompact.global_pct) / 100)}</span>
                    </div>
                    <Slider
                      value={autoCompact.pct ?? autoCompact.global_pct}
                      onChange={v => pushAutoCompact(v)}
                      min={autoCompact.min}
                      max={autoCompact.max}
                      step={1}
                      formatValue={v => fmtPercent(v / 100)}
                      aria-label={i18nT('components.chatInput.auto_compact_threshold')}
                    />
                    {autoCompact.pct != null ? (
                      <Btn
                        className="mt-1 px-0 py-0 border-none text-[10px] text-muted underline hover:text-text hover:bg-transparent"
                        onClick={() => pushAutoCompact(null)}
                      >
                        {i18nT('components.chatInput.reset_to_global', { pct: Math.round(autoCompact.global_pct) })}
                      </Btn>
                    ) : (
                      <div className="mt-1 text-[10px] text-muted">{i18nT('components.chatInput.following_global', { pct: Math.round(autoCompact.global_pct) })}</div>
                    )}
                  </div>
                )}
        </div>
    )}
  </div>
  )
}

/** The model chip. It names the level in force beside the model; the level is
 *  CHANGED inside the model picker the chip opens (model and effort are one
 *  control, docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md). */
export function ModelChip({ modelName, modelSelected, sessionBackend, modelIsJevRouted, modelIsInheritedDefault, modelIsAutoChosen, reasoningEffort, effortIsDefault, hasEffort, isRunning, shelfCompact, shelfTiny, composerControl, modelChipPressedFromComposerRef, onModelClick }: {
  modelName: string
  modelSelected?: string
  sessionBackend?: string | null
  modelIsJevRouted?: boolean
  modelIsInheritedDefault?: boolean
  modelIsAutoChosen?: boolean
  reasoningEffort?: string
  effortIsDefault: boolean
  hasEffort?: boolean
  isRunning: boolean
  shelfCompact: boolean
  shelfTiny: boolean
  composerControl: () => ComposerControl | null
  /** Owned by the composer, so a press outlives a remount of the chip exactly as it did. */
  modelChipPressedFromComposerRef: React.MutableRefObject<boolean>
  onModelClick: NonNullable<ChatInputProps['onModelClick']>
}) {
  // The chip shows the level in every state (running, routed, pinned
  // or inherited), so its title / accessible name carries it in every
  // state too -- one suffix, appended to each branch.
  // A default and an override show the same level on the chip; the
  // name says which one it is (the picker's own "Default · High").
  const effortShown = effortIsDefault
    ? i18nT('components.reasoningEffortDropdown.default_with_level', { level: effortLabel(reasoningEffort || '') })
    : effortLabel(reasoningEffort || '')
  const effortSuffix = hasEffort
    ? ` · ${i18nT('components.reasoningEffortDropdown.reasoning_effort')}: ${effortShown}`
    : ''
  // The backend the live session runs on, named on the chip so a session that
  // kept its harness through a config switch does not read as the new one.
  const backendName = sessionBackend != null ? acpBackendName({ id: sessionBackend }) : ''
  const backendSuffix = backendName
    ? ` · ${i18nT('components.chatInput.agent_backend')}: ${backendName}`
    : ''
  const selectedSuffix = modelSelected
    ? ` · ${i18nT('components.chatInput.model_selected', { name: modelSelected })}`
    : ''
  const modelChipLabel = `${isRunning
    ? i18nT('components.chatInput.stop_the_current_response_to_switch_model')
    : modelIsJevRouted
      ? i18nT('pages.chatPage.model_auto_jev_description')
      : modelIsInheritedDefault
        ? i18nT('components.chatInput.model_inherited_default', { name: modelName })
        : modelIsAutoChosen
          ? `${i18nT('components.chatInput.model_2', { name: modelName })} · ${i18nT('components.jobForm.auto')}`
          : i18nT('components.chatInput.model_2', { name: modelName })}${selectedSuffix}${effortSuffix}${backendSuffix}`
  return (
  <button
    className="inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] text-muted hover:text-text px-2 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-muted"
    onMouseDown={() => {
      const editor = composerControl()?.getRootElement()
      modelChipPressedFromComposerRef.current = !!editor && editor.contains(document.activeElement)
    }}
    onClick={e => {
      const composerHadFocus = modelChipPressedFromComposerRef.current
      modelChipPressedFromComposerRef.current = false
      onModelClick(e.currentTarget.getBoundingClientRect(), e.currentTarget, composerHadFocus)
    }}
    disabled={isRunning}
    data-testid="composer-model-chip"
    // Inherited default: mirror the agent chip -- ` · default` marker on
    // the label, and the explanation on hover (title) AND keyboard
    // focus / screen readers (aria-label), because a bare served id
    // reads exactly like a pin. A pinned chip keeps the plain hint.
    // The effort level rides along on both: `aria-label` REPLACES the
    // chip's content in the accessible name, so without it a screen
    // reader never hears the level the chip shows, and the tooltip is
    // the only readout left when the shelf is too narrow to show it.
    title={modelChipLabel}
    aria-label={modelChipLabel}
  >
    {/* The harness leads as its logo, which fits every shelf width, a
        phone's included; the logo's own name and tooltip say which. */}
    {sessionBackend != null && <HarnessMark backend={sessionBackend} />}
    {/* The routing prefix of a wire id (`global.anthropic.`) is dropped from
        the visible name only, as the turn footer does; the title keeps it. */}
    <span className="truncate max-w-[180px]">
      {modelIsJevRouted ? i18nT('components.modelDropdownList.auto_jev') : modelName.replace(ROUTING_PREFIX_RE, '')}
    </span>
    {/* Outside the truncating span: a long provider-prefixed id must
        ellipsize its own tail, never the marker beside it. A routed chip
        takes NO marker -- its label is already the policy, and a second
        word next to it would be a marker on a name that is not a model.
        So the two unpinned states differ by KIND (a policy vs an id with
        a marker), not by two adjectives a reader has to tell apart. */}
    {!modelIsJevRouted && (modelIsInheritedDefault || modelIsAutoChosen) && (
      <>
        <span className="opacity-30 select-none shrink-0" aria-hidden="true">·</span>
        <span className="opacity-60 shrink-0">{modelIsInheritedDefault
          ? i18nT('components.agentSelector.default')
          : i18nT('components.jobForm.auto')}</span>
      </>
    )}
    {/* A default and an override show the same level, and a glance
        at "High" alone could not tell which one set it. So the chip
        says so where there is room -- the picker's own "Default ·
        High" -- and keeps the bare level only in a compact shelf,
        where the hover / accessible name above still carries it.
        An inherited-default MODEL already put one "Default" on the
        chip; a second, meaning the effort, right after it would be
        the same word twice for two unrelated facts, so that chip
        keeps the bare level as well (the name above still says
        "Reasoning effort: Default · High"). */}
    {hasEffort && !shelfTiny && (
      <>
        <span className="opacity-30 select-none shrink-0" aria-hidden="true">·</span>
        <span className="opacity-60 shrink-0">{shelfCompact || modelIsInheritedDefault ? effortLabel(reasoningEffort || '') : effortShown}</span>
      </>
    )}
  </button>
  )
}
