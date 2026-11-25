import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ContentSkeleton } from '../../components/ui'
import ErrorBoundary from '../../components/ErrorBoundary'
import type { KiroAccountUsage } from '../../components/KiroAccountModal'
import { useKasLoginStatus } from '../../hooks/useKasLogin'
import { api, type KasLoginSources } from '../../api/client'
import { ACP_BACKEND_KIRO, configuredAcpBackend, type AcpBackendConfig } from '../../api/acpBackend'
import { KiroCreditsCard } from './KiroCreditsCard'
import { signInLapsed } from './kiroSignInState'
import { useSignedInAccount } from './kiroSignedInAccount'
import { creditsShowIdentity, creditsVisible } from './kiroCreditsVisibility'

/**
 * Lazy: the Kiro sign-in card pulls in its OIDC chooser, which nothing else on
 * this tab needs, so its chunk is fetched when the header first draws.
 */
const KiroSignInCard = lazy(() =>
  import('./KiroSignInCard').then(m => ({ default: m.KiroSignInCard })),
)
/** What the sign-in unlocks, at the foot of the column once signed in. Same
 *  chunk as the card: the card imports its signed-out list from this module. */
const KiroAccountUnlocksCard = lazy(() =>
  import('./KiroAccountUnlocks').then(m => ({ default: m.KiroAccountUnlocksCard })),
)

/**
 * Settings > Kiro account: one centered column with the account header
 * (`KiroSignInCard`) on top, the Credits card under it, and what the sign-in
 * unlocks (`KiroAccountUnlocksCard`) at the foot once signed in. When the Credits
 * card shows is `creditsVisible`; whether it names the account itself is
 * `creditsShowIdentity`. A build with no Kiro sign-in shows only the Credits
 * card. Loaded lazily from SettingsPage so this tab's reads stay out of the
 * first load.
 */
export function KiroAccountPanel({ signInOffered, usage, showCredits }: {
  signInOffered: boolean
  usage: KiroAccountUsage
  showCredits: boolean
}) {
  // Same cache key as the sign-in flow, which owns the polling.
  const crewStatus = useKasLoginStatus({ enabled: signInOffered }).data
  const crewSignedIn = crewStatus?.authenticated === true && !signInLapsed(crewStatus)
  // The header's name comes from the same reading, so it is the same account.
  const headerName = useSignedInAccount().name
  // Observed only (same cache key as the card's `SOURCES_QUERY_KEY`, which owns
  // the read), so this tab fetches nothing more and the sources module stays in
  // the card's lazy chunk.
  const { data: sources } = useQuery<KasLoginSources>({
    queryKey: ['kas-login-sources'],
    queryFn: api.kasLoginSources,
    enabled: false,
  })
  // Same config read (and cache key) as the top bar's.
  const { data: cfg } = useQuery<AcpBackendConfig>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const kiroCliBackend = cfg !== undefined && configuredAcpBackend(cfg) === ACP_BACKEND_KIRO
  const kiroCliActive = sources?.active === 'kiro_cli' || kiroCliBackend
  // Signed out here while sessions run on kiro-cli's login: the header names that account.
  const kiroCliHeader = signInOffered && crewStatus?.authenticated === false && sources?.active === 'kiro_cli'
  // Once shown, the card stays while the sign-in situation is unchanged: a
  // Refresh writes its outcome (even `none`) into the shared usage query while
  // the card sets its own notice, and unmounting it would drop that notice and
  // its retry. A change of Crew sign-in or kiro-cli state re-decides.
  const visible = creditsVisible({ showCredits, signInOffered, crewSignedIn, kiroCliActive, usage })
  const [shown, setShown] = useState(visible)
  const [prevSignIn, setPrevSignIn] = useState({ crewSignedIn, kiroCliActive })
  if (prevSignIn.crewSignedIn !== crewSignedIn || prevSignIn.kiroCliActive !== kiroCliActive) {
    setPrevSignIn({ crewSignedIn, kiroCliActive })
    setShown(visible)
  } else if (visible && !shown) {
    setShown(true)
  }
  return (
    <div className="mx-auto flex w-full max-w-[560px] flex-col gap-6 pt-6 md:pt-12" data-testid="kiro-account-centered">
      {signInOffered && (
        // Isolated so a throwing card cannot take the Settings page down with
        // it. The default fallback, not `null`: a failure must show its error
        // and Try Again rather than a blank pane.
        <ErrorBoundary scope="settings-kiro-sign-in">
          <Suspense fallback={<ContentSkeleton rows={6} />}>
            <KiroSignInCard />
          </Suspense>
        </ErrorBoundary>
      )}
      {showCredits && (visible || shown) && (
        <ErrorBoundary scope="settings-kiro-credits">
          {/* No hand-off beside the sign-in card, which can hold an unsaved draft. */}
          <KiroCreditsCard
            usage={usage}
            handoff={!signInOffered}
            showIdentity={creditsShowIdentity({
              usage, signInOffered, crewSignedIn, headerName, kiroCliHeader, sourcesEmail: sources?.kiro_cli.email,
            })}
          />
        </ErrorBoundary>
      )}
      {signInOffered && (
        // Its own boundary: a failure here must not take the sign-in card down.
        // No skeleton: the card renders nothing until it is signed in.
        <ErrorBoundary scope="settings-kiro-account-unlocks">
          <Suspense fallback={null}>
            <KiroAccountUnlocksCard />
          </Suspense>
        </ErrorBoundary>
      )}
    </div>
  )
}
