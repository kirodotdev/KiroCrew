/**
 * One Kiro Space store's sign-in: start it, show the code and the approval
 * link, and watch the store's status until it reads `signed_in`.
 *
 * Used by the store row in the Sources popover and by the sign-in card on the
 * Discover page, so both run the same flow. `sign_in` answers once; the
 * outcome arrives only through the shared status read, which is polled every
 * `SIGN_IN_POLL_MS` while a code is shown, for at most `SIGN_IN_WAIT_MS`.
 */
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { ExternalLink } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import { safeHref } from '../../utils/safeHref'
import { MARKET_REGISTRIES_QUERY_KEY, readMarketRegistries } from './marketAccount'
import type { MarketRegistryStatus, MarketSignInView } from './types'

/** How often the status is re-read while a sign-in code is shown. */
export const SIGN_IN_POLL_MS = 3000
/** How long a shown code is waited on before polling stops. */
export const SIGN_IN_WAIT_MS = 10 * 60_000

/** Re-read the store statuses and every store listing. */
export function useMarketRefresh(): () => void {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: MARKET_REGISTRIES_QUERY_KEY })
    void queryClient.invalidateQueries({ queryKey: ['market-listings'] })
    void queryClient.invalidateQueries({ queryKey: ['market-listing'] })
  }
}

export interface StoreSignIn {
  view: MarketSignInView | null
  waitUntil: number | null
  /** Start the provider's sign-in; throws what the request threw. */
  start: () => Promise<void>
  /** Forget a shown code and stop polling. */
  clear: () => void
}

export function useStoreSignIn(
  registry: string,
  state: MarketRegistryStatus['state'] | undefined,
  onSignedIn: () => void,
): StoreSignIn {
  const [view, setView] = useState<MarketSignInView | null>(null)
  const [waitUntil, setWaitUntil] = useState<number | null>(null)
  useQuery({
    queryKey: MARKET_REGISTRIES_QUERY_KEY,
    queryFn: readMarketRegistries,
    staleTime: 30_000,
    refetchInterval: () => (waitUntil !== null && Date.now() < waitUntil ? SIGN_IN_POLL_MS : false),
  })
  useEffect(() => {
    if (state === 'signed_in' && waitUntil !== null) {
      setView(null)
      setWaitUntil(null)
      onSignedIn()
    }
  }, [state]) // eslint-disable-line react-hooks/exhaustive-deps
  return {
    view,
    waitUntil,
    start: async () => {
      const next = await api.marketSignIn(registry)
      setView(next)
      setWaitUntil(next.code ? Date.now() + SIGN_IN_WAIT_MS : null)
    },
    clear: () => {
      setView(null)
      setWaitUntil(null)
    },
  }
}

/** The shown code, the approval link, and what to do when it did not finish. */
export function SignInProgress({ signIn, className = '' }: { signIn: StoreSignIn; className?: string }) {
  const { t } = useTranslation()
  const { view, waitUntil } = signIn
  const approvalPage = view?.approvalPage ? safeHref(view.approvalPage) : ''
  // The link names the site it opens, so the person knows where they approve.
  const approvalHost = approvalPage ? new URL(approvalPage).host : ''
  const ended = view?.state === 'failed' || view?.state === 'expired'
  const timedOut = waitUntil !== null && Date.now() >= waitUntil
  const showCode = !!view?.code && !ended && !timedOut
  if (!showCode && !ended && !timedOut) return null
  return (
    <div className={className}>
      {showCode && (
        <div className="text-[12.5px] flex items-center gap-3 flex-wrap">
          <span>
            <Trans
              i18nKey="components.appstore.marketStore.signin_code"
              components={{ mono: <code className="font-mono bg-bg-elevated px-1 rounded">{view.code}</code> }}
            />
          </span>
          {approvalPage && (
            <a href={approvalPage} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent">
              <ExternalLink size={12} className="lucide-inline" aria-hidden /> {t('components.appstore.marketStore.open_approval_page', { host: approvalHost })}
            </a>
          )}
        </div>
      )}
      {/* A sign-in the store answered as failed or expired is a failure, so it
          takes the error notice and its hand-off; nothing here is unsaved. */}
      {ended && (
        <ErrorNotice message={t('components.appstore.marketStore.signin_timed_out')} variant="inline" askAgent />
      )}
      {timedOut && !ended && (
        <p className="text-[12px] text-muted" role="status">{t('components.appstore.marketStore.signin_timed_out')}</p>
      )}
      <p className="mt-1 text-[12px] text-muted">{t('components.appstore.marketStore.signin_failed_hint')}</p>
    </div>
  )
}
