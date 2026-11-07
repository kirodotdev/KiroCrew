import type { ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { useGuestIdentityError, useIsGuest } from '../hooks/sharedCrew/useSharedCrew'
import { isOwnerOnlyPath } from '../lib/ownerOnly'
import ErrorNotice from './ErrorNotice'
import OwnerOnlyState from './OwnerOnlyState'

/** Shows the Owner-only state instead of a page a Shared Crew guest cannot use. */
export default function OwnerOnlyGate({ children }: { children: ReactNode }) {
  const isGuest = useIsGuest()
  const identityError = useGuestIdentityError()
  const { pathname } = useLocation()
  if (isGuest && isOwnerOnlyPath(pathname)) return <OwnerOnlyState />
  return (
    <>
      {/* A failed identity read is shown, not read as "not a guest"; askAgent hands the error to the agent. */}
      {identityError && <ErrorNotice variant="inline" className="m-2" message={identityError} askAgent testId="guest-identity-error" />}
      {children}
    </>
  )
}
