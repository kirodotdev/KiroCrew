/**
 * Exploratory tips: one short line beside "Thinking…" while a turn runs.
 *
 * The bank is `src/kiro_crew/data/tips_exploratory.json`, served (minus the
 * tips this user dismissed) by GET /api/tips/exploratory. It is separate from
 * the tip card above the composer (`components/TipCard.tsx`): no exploratory
 * tip enters the card and no card tip reaches this row. The two share the
 * dismissal store and the `tips.feedback` counter, which counts this row's
 * tips as `source=exploratory`.
 *
 * One tip per turn, picked at random among the eligible, non-dismissed tips
 * when the turn starts; it never changes mid-turn and goes when the turn ends.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { isNotFoundError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { voiceInputSupported } from '../../hooks/useVoiceInput'
import { clientSpeechAvailable } from '../../lib/clientSpeech'
import { useTerminalEnabled } from '../../utils/terminalRegistry'
import { useTerminalPoppedOut } from '../../utils/terminalPopout'

export interface ExploratoryTip {
  id: string
  /** One line: `**bold**` and `` `code` `` are the only markup. */
  body: string
  /** What the dashboard must have for the tip to be usable here. */
  requires: string[]
}

/** True when every requirement is in `env`. An unknown key is never in `env`,
 *  so a tip naming a key this build does not know stays hidden. */
export function exploratoryTipEligible(tip: ExploratoryTip, env: ReadonlySet<string>): boolean {
  return tip.requires.every(r => env.has(r))
}

/** A uniform draw in [0, 1) from the platform's random source. */
function uniformRandom(): number {
  return crypto.getRandomValues(new Uint32Array(1))[0] / 2 ** 32
}

/** A random eligible, non-dismissed tip, or null when there is none. */
export function pickExploratoryTip(
  tips: readonly ExploratoryTip[],
  env: ReadonlySet<string>,
  dismissed: ReadonlySet<string>,
  random: () => number = uniformRandom,
): ExploratoryTip | null {
  const pool = tips.filter(t => !dismissed.has(t.id) && exploratoryTipEligible(t, env))
  if (pool.length === 0) return null
  return pool[Math.min(pool.length - 1, Math.floor(random() * pool.length))]
}

/** Split a tip body into plain, bold and code runs. Shipped copy only, so a
 *  run is plain text: nothing here is ever parsed as HTML or markdown. */
export function tipRuns(body: string): { kind: 'text' | 'bold' | 'code'; text: string }[] {
  const out: { kind: 'text' | 'bold' | 'code'; text: string }[] = []
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g
  let last = 0
  for (let m = re.exec(body); m; m = re.exec(body)) {
    if (m.index > last) out.push({ kind: 'text', text: body.slice(last, m.index) })
    out.push(m[1] !== undefined ? { kind: 'bold', text: m[1] } : { kind: 'code', text: m[2] })
    last = m.index + m[0].length
  }
  if (last < body.length) out.push({ kind: 'text', text: body.slice(last) })
  return out
}

function hasFinePointer(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(any-pointer: fine)').matches
}

/**
 * What this chat can offer, as the requirement keys of the bank. Every key
 * here is a check this dashboard can make; a key it cannot confirm is left
 * out, which hides the tips that need it.
 */
export function useThinkingTipEnv({ projectDir, localSession, splitSend }: {
  projectDir: string | null
  /** The chat runs on this machine: not a remote executor, and not bound to an
   *  environment profile (the composer refuses `!` commands for both). */
  localSession: boolean
  /** The composer under this transcript shows the split send control (its
   *  Steer / Queue menu) during a running turn. A crew worker's composer is
   *  steer-only and has no menu. */
  splitSend: boolean
}): ReadonlySet<string> {
  const mobile = useIsMobile()
  const terminal = useTerminalEnabled()
  const terminalPoppedOut = useTerminalPoppedOut()
  // Same read as the shell's quick-search switch: on unless stored as `false`.
  const { data: commandBar } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (cfg: { dashboard?: { command_bar_enabled?: boolean } }) => cfg.dashboard?.command_bar_enabled !== false,
  })
  return useMemo(() => {
    const env = new Set<string>()
    if (mobile) return env
    env.add('desktop')
    if (splitSend) env.add('split_send')
    if (hasFinePointer()) env.add('keyboard')
    if (commandBar === true) env.add('command_bar')
    if (terminal) env.add('terminal')
    if (terminal && !terminalPoppedOut) env.add('terminal_docked')
    if (localSession) env.add('local_session')
    if (projectDir) env.add('project_files')
    if (voiceInputSupported) env.add('microphone')
    if (clientSpeechAvailable()) env.add('speech_playback')
    return env
  }, [mobile, splitSend, terminal, terminalPoppedOut, commandBar, localSession, projectDir])
}

/**
 * The tip for the running turn of `slotKey`, or null. `blocked` hides it and
 * stops the fetch (a temporary session reads no saved state).
 */
export function useThinkingTip(running: boolean, slotKey: string | null, blocked: boolean, env: ReadonlySet<string>) {
  const queryClient = useQueryClient()
  // The tips switch: Settings writes this query when the user turns tips off or on.
  const { data: status, error: statusError } = useQuery({
    queryKey: ['tipsStatus'],
    queryFn: api.tipsStatus,
    staleTime: 20 * 60 * 1000,
    retry: false,
    enabled: !blocked,
  })
  const tipsOn = !!status?.enabled_config && !status.opted_out
  const { data, error: tipsError } = useQuery({
    queryKey: ['tips-exploratory'],
    queryFn: api.tipsExploratory,
    enabled: !blocked && tipsOn,
    staleTime: 10 * 60 * 1000,
    retry: false,
  })

  // A new turn key on every start of a turn (or a switch to another running
  // slot), so the pick below runs once per turn and never mid-turn.
  const [turn, setTurn] = useState<string | null>(null)
  const turnSeq = useRef(0)
  const active = running && !blocked && tipsOn && !!slotKey
  useEffect(() => {
    if (!active) { setTurn(null); return }
    turnSeq.current += 1
    setTurn(`${slotKey}#${turnSeq.current}`)
  }, [active, slotKey])

  const dismissed = useRef(new Set<string>())
  const [pick, setPick] = useState<{ turn: string; tip: ExploratoryTip | null } | null>(null)
  useEffect(() => {
    if (!turn || !data || pick?.turn === turn) return
    setPick({ turn, tip: pickExploratoryTip(data.tips, env, dismissed.current) })
  }, [turn, data, env, pick])

  const shownTurn = useRef<string | null>(null)
  const onShown = useCallback(() => {
    if (!pick?.tip || shownTurn.current === pick.turn) return
    shownTurn.current = pick.turn
    api.tipsFeedback(pick.tip.id, 'shown').catch(() => {})
  }, [pick])

  // The tip goes only once the gateway saved the dismissal, as the card's own
  // dismiss does: a failed request leaves it on screen for another click
  // instead of hiding a tip that comes back on the next load.
  const dismissing = useRef(false)
  // Keyed by the pick it belongs to, so a late failure never lands on another
  // turn's (or another chat's) tip.
  const [dismissError, setDismissError] = useState<{ turn: string; id: string; message: string } | null>(null)
  const dismiss = useCallback(() => {
    if (!pick?.tip || dismissing.current) return
    const { turn: pickTurn, tip: { id } } = pick
    dismissing.current = true
    setDismissError(null)
    api.tipsFeedback(id, 'dismiss').then(() => {
      dismissed.current.add(id)
      // Matched by tip id, not turn: a switch to another running chat before
      // the reply may already show the same tip under a new turn key.
      setPick(cur => (cur?.tip?.id === id ? { turn: cur.turn, tip: null } : cur))
      queryClient.setQueryData<{ tips: ExploratoryTip[] }>(['tips-exploratory'], old =>
        old ? { tips: old.tips.filter(t => t.id !== id) } : old)
    }, (err: unknown) => {
      setDismissError({ turn: pickTurn, id, message: err instanceof Error ? err.message : String(err) })
    }).finally(() => { dismissing.current = false })
  }, [pick, queryClient])

  const tip = turn && pick?.turn === turn ? pick.tip : null
  const error = tip && dismissError?.turn === turn && dismissError.id === tip.id ? dismissError.message : null
  // A load that failed is named beside "Thinking..." while the turn runs. A
  // 404 is a gateway that predates the route: it has no exploratory tips, which
  // is not a failure, and naming it would repeat on every turn.
  // The tips read counts only while tips are on: a disabled query keeps its
  // last error, which must not outlive the user turning tips off.
  const failedLoad = [statusError, tipsOn ? tipsError : null].find(e => e && !isNotFoundError(e))
  const loadError = running && !blocked && !!slotKey && failedLoad
    ? (failedLoad instanceof Error ? failedLoad.message : String(failedLoad))
    : null
  return { tip, onShown, dismiss, dismissError: error, loadError }
}

/** The divider and the tip line, placed after "Thinking…" in ChatFooter. */
export function ThinkingTip({ tip, onShown, onDismiss, dismissError = null }: {
  tip: ExploratoryTip
  onShown: () => void
  onDismiss: () => void
  /** Why the last dismiss did not save; the tip stays so it can be tried again. */
  dismissError?: string | null
}) {
  const reduceMotion = useReducedMotion()
  useEffect(() => { onShown() }, [onShown, tip.id])
  // The row wraps (ChatFooter): a tip too long for the space after
  // "Thinking..." drops to its own line and wraps there, so no word is cut
  // off. The divider separates the two only while they share a line; on its
  // own line it is hidden but keeps its space, so hiding it cannot change the
  // width that decided the wrap.
  const ref = useRef<HTMLDivElement>(null)
  const [ownLine, setOwnLine] = useState(false)
  useLayoutEffect(() => {
    const el = ref.current
    const row = el?.parentElement
    if (!el || !row) return
    const measure = () => {
      const status = el.previousElementSibling as HTMLElement | null
      setOwnLine(!!status && el.offsetTop > status.offsetTop + status.offsetHeight / 2)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(row)
    return () => ro.disconnect()
  }, [tip.id])
  return (
    <motion.div
      ref={ref}
      data-testid="thinking-tip"
      data-own-line={ownLine || undefined}
      className="group flex items-start gap-2 min-w-0 max-w-full text-[13px] text-muted"
      initial={reduceMotion ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={reduceMotion ? { duration: 0 } : { duration: 0.2, ease: 'easeOut' }}
    >
      <span className={`w-px h-3.5 mt-[3px] bg-border shrink-0${ownLine ? ' invisible' : ''}`} aria-hidden="true" />
      <span className="min-w-0 break-words" data-testid="thinking-tip-text">
        {i18nT('components.markdownPanel.tip')}{' '}
        {tipRuns(tip.body).map((run, i) => run.kind === 'bold'
          ? <strong key={i} className="font-semibold text-text">{run.text}</strong>
          : run.kind === 'code'
            ? <code key={i} className="font-mono text-[12px] px-1 py-px rounded border border-border">{run.text}</code>
            : <span key={i}>{run.text}</span>)}
      </span>
      <button
        type="button"
        onClick={onDismiss}
        aria-label={i18nT('components.tipCard.dismiss_tip')}
        className="shrink-0 inline-flex items-center justify-center w-5 h-5 rounded bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 focus-visible:opacity-100 transition-opacity"
      >
        <X size={12} aria-hidden="true" />
      </button>
      {/* No hand-off: this row sits above the chat composer, whose draft is
          unsaved local state the hand-off's navigation would discard. */}
      <ErrorNotice variant="inline" message={dismissError} testId="thinking-tip-dismiss-error" />
    </motion.div>
  )
}
