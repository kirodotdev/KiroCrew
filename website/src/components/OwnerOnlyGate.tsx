import type { ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { useGuestIdentityError, useIsGuest, useRetryGuestIdentity } from '../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../i18n/t'
import { Btn } from './ui'
import { isOwnerOnlyPath } from '../lib/ownerOnly'
import ErrorNotice from './ErrorNotice'
import OwnerOnlyState from './OwnerOnlyState'

/** Shows the Owner-only state instead of a page a Shared Crew guest cannot use. */
export default function OwnerOnlyGate({ children }: { children: ReactNode }) {
  const isGuest = useIsGuest()
  const identityError = useGuestIdentityError()
  const retryIdentity = useRetryGuestIdentity()
  const { pathname } = useLocation()
  if (isGuest && isOwnerOnlyPath(pathname)) return <OwnerOnlyState />
  return (
    <>
      {/* A failed identity read is shown, not read as "not a guest": in words, with the server's
          own answer on hover and in the agent hand-off, and a way to ask again. */}
      {identityError && (
        <div className="m-2 flex flex-wrap items-center gap-2">
          <ErrorNotice
            variant="inline"
            message={i18nT('sharedCrew.identity_check_failed')}
            messageTooltip={identityError}
            askAgent
            testId="guest-identity-error"
          />
          <Btn type="button" onClick={retryIdentity} data-testid="guest-identity-retry">{i18nT('components.mobileConnect.try_again')}</Btn>
        </div>
      )}
      {children}
    </>
  )
}
