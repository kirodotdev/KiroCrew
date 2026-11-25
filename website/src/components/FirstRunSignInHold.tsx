import { createContext, useCallback, useContext, useEffect, useReducer, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type KasBundleStatus } from '../api/client'
import type { KiroPrerequisiteStatus } from '../api/client/onboarding'
import { useKasLoginStatus } from '../hooks/useKasLogin'
import FirstRunHarnessSetup from './FirstRunHarnessSetup'
import KasLoginGate from './KasLoginGate'

const KAS_BUNDLE_QUERY_KEY = ['kas-bundle'] as const
// Its own key: an answer read before setup admits must never stand in for the hold's own read.
const EARLY_BUNDLE_QUERY_KEY = ['kas-bundle', 'first-run-paint'] as const
/** The longest the app stays hidden while the hold decides (about twice the status read measured on a dev host). */
const PAINT_WAIT_MS = 4000
const HeldContext = createContext(false)

/** Whether the first-run sign-in hides the app behind it, for the app's layers outside its own tree. */
export function useFirstRunSignInHeld(): boolean {
  return useContext(HeldContext)
}

/** Whether this page load may still hold the app behind the Kiro sign-in, and how it lets go. */
export interface SignInHold {
  /** A first run not admitted yet: the next resolved admission asks the gateway. */
  asking: boolean
  /** A "no" from the gateway, a sign-in, an unreadable sign-in status or a Skip: the app shows from now on. */
  admit: () => void
}

/**
 * The first-run Kiro sign-in hold's page-load state.
 *
 * Asking only on a page load whose first resolved status reported
 * `initial_setup_complete: false`; that is latched, so the gate's own marker
 * write later in the load does not drop it, and a returning user is never
 * held. `ownScreen` is a status the gate answers with a screen of its own (the
 * LTS paths), which neither asks nor decides the latch. Nothing is saved: an
 * admission lasts for this page load.
 */
export function useFirstRunSignInHold(
  status: KiroPrerequisiteStatus | undefined,
  ownScreen: boolean,
): SignInHold {
  const firstRun = useRef<boolean | undefined>(undefined)
  if (status && !ownScreen && firstRun.current === undefined) {
    firstRun.current = status.initial_setup_complete === false
  }
  const admitted = useRef(false)
  const [, rerender] = useReducer((n: number) => n + 1, 0)
  const admit = useCallback(() => {
    admitted.current = true
    rerender()
  }, [])
  return { asking: !admitted.current && !ownScreen && firstRun.current === true, admit }
}

/**
 * The built-in engine is on for this gateway: the saved `agent.kas_bundle`, else
 * what the build prefers. Read from `engine_choice`, the same field the Settings
 * engine switch shows; an older gateway that sends none falls back to `enabled`.
 */
export function builtInEngineOn(bundle: KasBundleStatus): boolean {
  return (bundle.engine_choice ?? (bundle.enabled ? 'engine' : 'kiro_cli')) === 'engine'
}

/**
 * The app, hidden on a first run behind one of two screens.
 *
 * With the built-in engine on (`GET /api/kas-bundle`), the first-run harness
 * setup (pick a harness, sign in, ready), which admits on "Start chatting" or
 * "Set up later". With it off, the full-screen Kiro sign-in with a Skip, only
 * while the gateway says a new chat needs it (`chat_needs_sign_in`: KAS is the
 * main harness, its next spawn takes the bundle route, no usable sign-in is
 * stored, the caller is the owner).
 *
 * The bundle status is read only on a `resolved` admission (the status
 * answered, the agent known), and only an answer fetched from then on counts:
 * one cached on a setup screen or by another reader may predate what setup
 * changed. A "no" or a failed read admits. A stored sign-in, or a sign-in
 * status that cannot be read at all (unknown, not signed out), admits as a Skip
 * does. `children` stays mounted in every pass, hidden while held (its
 * portalled layers through `useFirstRunSignInHeld`), so what the user entered
 * before the answer is still there when the hold lets go.
 *
 * Until the hold has decided (`undecided`: the status or agent not known yet on
 * a browser that never finished setup, or the first-run read in flight) the app
 * is hidden too, once an early read of the same field says the built-in engine
 * is on, for at most `PAINT_WAIT_MS`. Otherwise the screen the app opens on
 * (settings import) paints for that wait and then vanishes under the setup
 * step. The early read only decides that paint, under its own key, never the
 * hold; with the engine off, or before it answers, the app paints as on main.
 */
export default function FirstRunSignInHold({ hold, status, resolved, undecided = false, children }: {
  hold: SignInHold
  status?: KiroPrerequisiteStatus
  resolved: boolean
  undecided?: boolean
  children: ReactNode
}) {
  const { asking, admit } = hold
  // A "yes" stands until an admission, so a refetch on focus cannot drop the hold.
  const needed = useRef<'' | 'setup' | 'sign-in'>('')
  const reading = asking && resolved && !needed.current
  const bundle = useQuery<KasBundleStatus>({
    queryKey: KAS_BUNDLE_QUERY_KEY,
    queryFn: () => api.kasBundleStatus(),
    enabled: reading,
    retry: false,
    // Stale at once, so starting the read fetches past any cached answer.
    staleTime: 0,
  })
  const answered = reading && bundle.fetchStatus === 'idle' && !bundle.isPending
  if (answered && bundle.isSuccess) {
    // Only someone who may do setup (the owner) gets the harness step; anyone else
    // falls through to the sign-in branch, which answers false for non-owners.
    if (builtInEngineOn(bundle.data) && status?.setup_allowed === true) needed.current = 'setup'
    else if (bundle.data.chat_needs_sign_in === true) needed.current = 'sign-in'
  }
  const held = asking && needed.current !== ''
  const signIn = useKasLoginStatus({ enabled: held && needed.current === 'sign-in' })
  const through = !held || (needed.current === 'sign-in'
    && (signIn.data?.authenticated === true || (signIn.isError && !signIn.data)))
  useEffect(() => {
    if ((answered && !held) || (held && through)) admit()
  }, [answered, held, through, admit])
  const waiting = undecided || (reading && !answered)
  const early = useQuery<KasBundleStatus>({
    queryKey: EARLY_BUNDLE_QUERY_KEY,
    queryFn: () => api.kasBundleStatus(),
    enabled: waiting,
    retry: false,
  })
  // Only a "the built-in engine is on" answer hides the app; off, unreadable or
  // not answered yet paints it as main always has.
  const engineOff = !(early.isSuccess && builtInEngineOn(early.data))
  // The wait is bounded: a status or bundle read that never answers paints the
  // app, as main does while they are pending, instead of a blank page.
  // shortcut: one fixed bound for every host; make it adaptive if a slow host
  // shows the import flash again past it.
  const [waitedOut, setWaitedOut] = useState(false)
  useEffect(() => {
    if (!waiting || waitedOut) return
    const timer = setTimeout(() => setWaitedOut(true), PAINT_WAIT_MS)
    return () => clearTimeout(timer)
  }, [waiting, waitedOut])
  const shown = through && !(waiting && !engineOff && !waitedOut)
  return (
    <>
      <HeldContext.Provider value={!shown}>
        <div style={{ display: shown ? 'contents' : 'none' }}>{children}</div>
      </HeldContext.Provider>
      {through ? null : needed.current === 'setup'
        ? <FirstRunHarnessSetup status={status} onDone={admit} />
        : <KasLoginGate onSkip={admit} />}
    </>
  )
}
