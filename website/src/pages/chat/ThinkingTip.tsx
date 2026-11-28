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
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import { useLanguage } from '../../i18n/LanguageProvider'
import { useIsMobile } from '../../hooks/useIsMobile'
import { clientSpeechAvailable } from '../../lib/clientSpeech'
import { useTerminalEnabled } from '../../utils/terminalRegistry'
import { useTerminalPoppedOut } from '../../utils/terminalPopout'
import { useSlotRunsHere } from '../../components/sessionEnvironmentHooks'
import { ThinkingTipRow } from './ThinkingTipView'

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

/** A uniform index in [0, n) from the platform's random source. Draws that
 *  fall in the uneven top slice of the 32-bit range are redrawn, so every
 *  index is equally likely. */
export function uniformIndex(n: number): number {
  const limit = 2 ** 32 - (2 ** 32 % n)
  const buf = new Uint32Array(1)
  for (;;) {
    crypto.getRandomValues(buf)
    if (buf[0] < limit) return buf[0] % n
  }
}

/** A random eligible, non-dismissed tip, or null when there is none.
 *  `randomIndex(n)` returns an index in [0, n). */
export function pickExploratoryTip(
  tips: readonly ExploratoryTip[],
  env: ReadonlySet<string>,
  dismissed: ReadonlySet<string>,
  randomIndex: (n: number) => number = uniformIndex,
): ExploratoryTip | null {
  const pool = tips.filter(t => !dismissed.has(t.id) && exploratoryTipEligible(t, env))
  if (pool.length === 0) return null
  return pool[randomIndex(pool.length)] ?? null
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
  // Read lazily: the recorder module is not part of the first load.
  const { data: microphone = false } = useQuery(MICROPHONE_QUERY)
  // Same read as the shell's quick-search switch: on unless stored as `false`.
  const { data: commandBar } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (cfg: { dashboard?: { command_bar_enabled?: boolean } }) => cfg.dashboard?.command_bar_enabled !== false,
  })
  return useMemo(() => {
    const env = new Set<string>()
    // A phone gets every tip whose controls it has; the desktop-only ones
    // (Split View, the Terminal rail, Focus mode) stay off.
    if (!mobile) env.add('desktop')
    if (splitSend) env.add('split_send')
    if (hasFinePointer()) env.add('keyboard')
    if (commandBar === true) env.add('command_bar')
    if (terminal) env.add('terminal')
    if (terminal && !terminalPoppedOut) env.add('terminal_docked')
    if (localSession) env.add('local_session')
    if (projectDir) env.add('project_files')
    if (microphone) env.add('microphone')
    if (clientSpeechAvailable()) env.add('speech_playback')
    return env
  }, [mobile, splitSend, terminal, terminalPoppedOut, commandBar, localSession, projectDir, microphone])
}

/** Whether this browser can record, read from the lazily loaded recorder
 *  module. One shared query: useThinkingTipEnv reads its answer, useThinkingTip
 *  its failure, so a chunk that fails to load is named, not silently dropped. */
const MICROPHONE_QUERY = {
  queryKey: ['thinkingTip', 'voiceInputSupported'],
  queryFn: async () => (await import('../../hooks/useVoiceInput')).voiceInputSupported,
  staleTime: Infinity,
  retry: false,
} as const

/**
 * The tip for the running turn of `slotKey`, or null. `blocked` hides it and
 * stops the fetch (a temporary session reads no saved state).
 */
export function useThinkingTip(running: boolean, slotKey: string | null, blocked: boolean, env: ReadonlySet<string>, turnId: string | null = null) {
  const queryClient = useQueryClient()
  // shortcut: the bank is English only, so another dashboard language shows no
  // tip; drop this once tips_exploratory.json is translated.
  const english = useLanguage().resolved.split('-')[0] === 'en'
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
    enabled: !blocked && tipsOn && english,
    staleTime: 10 * 60 * 1000,
    retry: false,
  })
  // The same config read useThinkingTipEnv makes (one shared query); only its
  // failure is read here, so a failed read is named instead of silently
  // dropping the Command Bar tip.
  const { error: configError } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: !blocked && tipsOn && english,
  })
  // Same for the microphone check: a recorder chunk that fails to load hides
  // the Voice input tip, so it is named the same way.
  const { error: microphoneError } = useQuery(MICROPHONE_QUERY)

  // A new turn key on every start of a turn (or a switch to another running
  // slot, or the slot's next turn starting straight after a queued send, which
  // `turnId` shows), so the pick below runs once per turn and never mid-turn.
  const [turn, setTurn] = useState<string | null>(null)
  const turnSeq = useRef(0)
  const active = running && !blocked && tipsOn && english && !!slotKey
  // One rule picks a turn's tip: a turn id that is not the id the slot had when
  // last seen idle (a local send runs with that old id until its own arrives),
  // and not the id the current pick was made for. That covers a local send, a
  // turn started elsewhere, a queued next turn and opening a chat already
  // running (no idle id seen). A gateway that sends no turn id gets one pick
  // per running stretch of the chat.
  const idleId = useRef<{ slot: string | null; id: string | null } | null>(null)
  useEffect(() => {
    if (!running) idleId.current = { slot: slotKey, id: turnId }
  }, [running, slotKey, turnId])
  const pickedFor = useRef<{ slot: string | null; id: string | null } | null>(null)
  useEffect(() => {
    if (!active) { pickedFor.current = null; setTurn(null); return }
    const samePick = pickedFor.current?.slot === slotKey
    if (turnId == null) {
      if (samePick) return
    } else {
      const baseline = idleId.current?.slot === slotKey ? idleId.current.id : null
      if (turnId === baseline || (samePick && pickedFor.current?.id === turnId)) return
    }
    pickedFor.current = { slot: slotKey, id: turnId }
    turnSeq.current += 1
    setTurn(`${slotKey}#${turnSeq.current}`)
  }, [active, slotKey, turnId])

  const dismissed = useRef(new Set<string>())
  const [pick, setPick] = useState<{ turn: string; tip: ExploratoryTip | null } | null>(null)
  useEffect(() => {
    if (!turn || !data || pick?.turn === turn) return
    setPick({ turn, tip: pickExploratoryTip(data.tips, env, dismissed.current) })
  }, [turn, data, env, pick])

  // A `shown` that did not reach the gateway is named beside the tip, and is
  // not counted as reported, so the next mount of this turn's tip sends it again.
  const shownTurn = useRef<string | null>(null)
  const [shownError, setShownError] = useState<{ turn: string; id: string; message: string } | null>(null)
  const onShown = useCallback(() => {
    if (!pick?.tip || shownTurn.current === pick.turn) return
    const { turn: pickTurn, tip: { id } } = pick
    shownTurn.current = pickTurn
    setShownError(null)
    api.tipsFeedback(id, 'shown').catch((err: unknown) => {
      if (shownTurn.current === pickTurn) shownTurn.current = null
      setShownError({ turn: pickTurn, id, message: err instanceof Error ? err.message : String(err) })
    })
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

  // The pick stays for the turn, but hides once the screen no longer has its
  // control (a desktop window narrowed to a phone, the terminal popped out).
  const picked = turn && pick?.turn === turn ? pick.tip : null
  const tip = picked && exploratoryTipEligible(picked, env) ? picked : null
  const forTip = (e: { turn: string; id: string; message: string } | null) =>
    tip && e?.turn === turn && e.id === tip.id ? e.message : null
  // A load that failed is named beside "Thinking..." while the turn runs. A
  // 404 is a gateway that predates the route: it has no exploratory tips, which
  // is not a failure, and naming it would repeat on every turn.
  // The tips read counts only while tips are on: a disabled query keeps its
  // last error, which must not outlive the user turning tips off.
  const envError = tipsOn ? (configError ?? microphoneError) : null
  const failedLoad = [statusError, tipsOn ? tipsError : null, envError].find(e => e && !isNotFoundError(e))
  const loadError = running && !blocked && english && !!slotKey && failedLoad
    ? (failedLoad instanceof Error ? failedLoad.message : String(failedLoad))
    : null
  // `loadError` is named beside a tip already shown too (ThinkingTipRow): a
  // refresh that fails later, or a failed config or microphone check, is
  // never silent.
  const error = forTip(dismissError) ?? forTip(shownError)
  return { tip, onShown, dismiss, dismissError: error, loadError }
}

/** Builds the thinking row with its tip (or its load failure) after `statusRow`. */
export type ThinkingTipRowFn = (statusRow: ReactNode) => ReactNode

/**
 * Runs the tip hooks for ChatTranscriptView and hands up the row ChatFooter
 * draws, or null when there is neither a tip nor a load failure. Its own export
 * so the view loads this module lazily and the tips stay out of the
 * dashboard's first load; it stays mounted while the view does, so it sees
 * the chat go idle and running as `useThinkingTip` needs. Renders nothing.
 */
export function ThinkingTipController({ running, slotKey, localKey, blocked, turnId, projectDir, splitSend, onChange }: {
  running: boolean
  slotKey: string | null
  /** The chat's key when it is not a remote executor's, else null. */
  localKey: string | null
  blocked: boolean
  turnId: string | null
  projectDir: string | null
  splitSend: boolean
  onChange: Dispatch<SetStateAction<ThinkingTipRowFn | null>>
}) {
  const runsHere = useSlotRunsHere(localKey)
  const env = useThinkingTipEnv({ projectDir, localSession: !!localKey && runsHere === true, splitSend })
  const { tip, onShown, dismiss, dismissError, loadError } = useThinkingTip(running, slotKey, blocked, env, turnId)
  useEffect(() => {
    // A function value, so it is wrapped: a bare one would be read as an updater.
    onChange(() => tip || loadError
      ? (statusRow: ReactNode) => <ThinkingTipRow statusRow={statusRow} tip={tip} loadError={loadError} onShown={onShown} onDismiss={dismiss} dismissError={dismissError} />
      : null)
  }, [onChange, tip, onShown, dismiss, dismissError, loadError])
  return null
}
