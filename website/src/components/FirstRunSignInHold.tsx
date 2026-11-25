import { createContext, useCallback, useContext, useEffect, useReducer, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type KasBundleStatus } from '../api/client'
import type { KiroPrerequisiteStatus } from '../api/client/onboarding'
import { useKasLoginStatus } from '../hooks/useKasLogin'
import KasLoginGate from './KasLoginGate'

const KAS_BUNDLE_QUERY_KEY = ['kas-bundle'] as const
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
 * The app, hidden behind the full-screen Kiro sign-in with a Skip while the
 * gateway says a sign-in is needed.
 *
 * The gateway decides, through the field the chat's sign-in prompt reads:
 * `chat_needs_sign_in` on `GET /api/kas-bundle` (KAS is the main harness, its
 * next spawn takes the bundle route, no usable sign-in is stored, the caller is
 * the owner). It is read only on a `resolved` admission (the status answered,
 * the agent known), and only an answer fetched from then on counts: one cached
 * on a setup screen or by another reader may predate what setup changed. Until
 * it answers the app renders as it would unheld, but that is not an admission;
 * a "no" or a failed read admits. A stored sign-in, or a sign-in status that
 * cannot be read at all (unknown, not signed out), admits as a Skip does.
 * `children` stays mounted in every pass, hidden while held (its portalled
 * layers through `useFirstRunSignInHeld`), so what the user entered before the
 * answer is still there when the hold lets go.
 */
export default function FirstRunSignInHold({ hold, resolved, children }: {
  hold: SignInHold
  resolved: boolean
  children: ReactNode
}) {
  const { asking, admit } = hold
  // A "yes" stands until an admission, so a refetch on focus cannot drop the hold.
  const needed = useRef(false)
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
  if (answered && bundle.isSuccess && bundle.data.chat_needs_sign_in === true) needed.current = true
  const held = asking && needed.current
  const signIn = useKasLoginStatus({ enabled: held })
  const through = !held || signIn.data?.authenticated === true || (signIn.isError && !signIn.data)
  useEffect(() => {
    if ((answered && !held) || (held && through)) admit()
  }, [answered, held, through, admit])
  return (
    <>
      <HeldContext.Provider value={!through}>
        <div style={{ display: through ? 'contents' : 'none' }}>{children}</div>
      </HeldContext.Provider>
      {through ? null : <KasLoginGate onSkip={admit} />}
    </>
  )
}
