import { useCallback, useEffect, useReducer, useRef, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { KiroPrerequisiteStatus } from '../api/client/onboarding'
import KasLoginGate from './KasLoginGate'

const PREREQUISITE_QUERY_KEY = ['kiro-prerequisite'] as const
const KAS_LOGIN_QUERY_KEY = ['kas-login'] as const

/** Whether this page load holds the app behind the Kiro sign-in, and how it lets go. */
export interface SignInHold {
  held: boolean
  /** Called wherever the gate renders the app: an app shown unheld is never held later. */
  noteShown: () => void
  /** A sign-in, an unreadable sign-in status or a Skip: the app renders from now on. */
  admit: () => void
}

/**
 * The first-run Kiro sign-in hold, decided from the setup status alone.
 *
 * Held only on a page load whose first resolved status reported
 * `initial_setup_complete: false`; that is latched, so the gate's own marker
 * write later in the load does not drop the hold, and a returning user is never
 * held. Within such a load the server's `kiro_sign_in_needed` decides. `ownScreen`
 * is a status the gate answers with a screen of its own (the LTS paths), which
 * neither holds nor decides the latch. Nothing is saved: a Skip lasts for this
 * page load.
 */
export function useFirstRunSignInHold(
  status: KiroPrerequisiteStatus | undefined,
  ownScreen: boolean,
): SignInHold {
  const queryClient = useQueryClient()
  const firstRun = useRef<boolean | undefined>(undefined)
  if (status && !ownScreen && firstRun.current === undefined) {
    firstRun.current = status.initial_setup_complete === false
  }
  const admitted = useRef(false)
  const [, rerender] = useReducer((n: number) => n + 1, 0)
  const held = !admitted.current && !ownScreen && firstRun.current === true
    && status?.kiro_sign_in_needed === true
  const noteShown = () => {
    if (status && !held) admitted.current = true
  }
  // The status is read again so the tour card words the sign-in from a fresh field.
  const admit = useCallback(() => {
    admitted.current = true
    rerender()
    void queryClient.invalidateQueries({ queryKey: PREREQUISITE_QUERY_KEY })
  }, [queryClient])
  return { held, noteShown, admit }
}

/**
 * The app, or while `hold.held` the full-screen Kiro sign-in with a Skip.
 *
 * A stored sign-in, or a sign-in status that cannot be read at all (unknown, not
 * signed out), admits as a Skip does. `children` keeps its tree position in every
 * pass, so the app is not mounted again when the hold lets go.
 */
export default function FirstRunSignInHold({ hold, children }: { hold: SignInHold; children: ReactNode }) {
  const { held, admit } = hold
  const signIn = useQuery({
    queryKey: KAS_LOGIN_QUERY_KEY,
    queryFn: api.kasLoginStatus,
    enabled: held,
  })
  const through = !held || signIn.data?.authenticated === true || (signIn.isError && !signIn.data)
  useEffect(() => {
    if (held && through) admit()
  }, [held, through, admit])
  return (
    <>
      {through ? null : <KasLoginGate onSkip={admit} />}
      {through ? children : null}
    </>
  )
}
