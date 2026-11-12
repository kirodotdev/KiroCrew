/**
 * BuiltinAppRoute — dynamic route handler for builtin app pages.
 *
 * Resolves the current URL path against the builtin component registry
 * and renders the matching page component with Suspense + ErrorBoundary.
 *
 * Used as a catch-all route for builtin app paths, eliminating the need
 * to hardcode each builtin app's <Route> in App.tsx.
 *
 * It is also where the page's app identity is published. This is the only place
 * the host knows "this route belongs to app X" while the app's first query has
 * not yet mounted, which is the moment identity has to exist for anything keyed
 * off it to work.
 */
import { Suspense } from 'react'
import { useParams, Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { apiErrorCode, isNotFoundError } from '../api/apiError'
import { api } from '../api/client'
import { getBuiltinApp, builtinRefusalReason } from './builtinRegistry'
import { AppIdentityProvider } from '../app-sdk/identity'
import { AppCacheRetention } from '../app-sdk/appQuery'
import { adoptedLegacyNav } from '../appNav'
import AskAgentButton from '../components/AskAgentButton'
import ErrorBoundary from '../components/ErrorBoundary'
import ErrorNotice from '../components/ErrorNotice'
import InstalledAppSurface from '../components/InstalledAppSurface'
import { Btn, ContentSkeleton } from '../components/ui'
import { useInstalledAppsWhen } from '../hooks/panelTabRegistry'
import { i18nT } from '../i18n/t'

/**
 * The refusal as a DIAGNOSTIC, built once and used twice.
 *
 * One `Error` with two consumers that have to agree: it is what the boundary
 * journals through `recordError`/RUM, and its `message` is what the hand-off
 * button resolves that journal entry BY (`findReport` matches on the message). A
 * second spelling of the sentence would silently cost the hand-off its structured
 * report — stack, component stack — leaving the one remaining action carrying
 * only a bare string.
 *
 * Deliberately English and unlocalised: its readers are an error log, a RUM event
 * and an agent prompt, none of which is a person reading a UI in their own
 * language. What the person reads is assembled separately, from the catalog,
 * below. Keeping the sentence inside `new Error(...)` is also what the i18n lint
 * recognises as a developer diagnostic rather than copy.
 */
function refusalError(route: string, reason: string): Error {
  return new Error(`Page ${route} was refused at registration: ${reason}`)
}

/**
 * Throws, so the refusal reaches the user through the boundary below it.
 *
 * A registration refusal cannot throw where it happens: `registerBuiltinComponents`
 * runs before App mounts, so a throw there takes the whole dashboard down over
 * one bad page. It also cannot stay a `console.warn`, which is how a downstream
 * edition shipped ten pages that all redirected to chat with no signal a user
 * could see. Throwing HERE — on the render of the route nobody can reach — is
 * the same failure delivered where it is actionable: the route-level
 * `ErrorBoundary` journals it through `recordError` and RUM, while every other
 * page keeps working.
 *
 * The throw is for the JOURNAL. What renders is the explicit `fallback` the
 * boundary is given, not the default error card — see `RefusedRouteFallback`.
 */
function RefusedRoute({ failure }: { failure: Error }): never {
  throw failure
}

/**
 * What the person staring at the empty page reads.
 *
 * The default `ErrorBoundary` card is wrong here in three specific ways, and a
 * refusal is the one failure where all three are knowable in advance:
 *
 * 1. **Its "Try Again" cannot succeed.** It clears `state.error`, React re-renders
 *    the same children, `RefusedRoute` throws the same error, and the same card
 *    comes back. A refusal is decided once at startup and is immutable for the
 *    life of the page load, so the only honest number of retry affordances is
 *    zero. "Ask the agent" stays: it is the one action that can actually move
 *    this, and it carries the diagnostic into a chat that can act on it. What it
 *    does is stated in the PAGE, not only in the button's `title`: a tooltip
 *    reaches neither keyboard nor touch, so on a page with a single affordance a
 *    reader could not tell a context-carrying hand-off from a plain new chat with
 *    a different label — they were guessing at the only thing left to try.
 * 2. **It names no page.** Nothing in the sidebar is active on a refused route,
 *    so "Something went wrong" leaves the reader unsure which page they are even
 *    on. The route is in the heading for that reason.
 * 3. **It shows only the developer sentence.** The recorded reason is a
 *    registration diagnostic — charsets, storage keys — addressed to whoever
 *    built the bundle, and a user who reads it first learns nothing they can act
 *    on. So a plain sentence leads, and the diagnostic is kept BELOW it under a
 *    label saying who it is for. Kept, not dropped: it is the only content that
 *    tells the person who CAN fix this what to fix, and it is what the reader
 *    will paste or hand off.
 *
 * Journaling is unaffected — `componentDidCatch` runs whether or not a `fallback`
 * is supplied; only what RENDERS changes.
 */
function RefusedRouteFallback(
  { route, reason, handoff }: { route: string; reason: string; handoff: string },
) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 text-center p-8">
      <div className="text-4xl"><AlertTriangle className="lucide-inline" /></div>
      <div className="text-lg font-bold text-text-strong">
        {i18nT('apps.builtinAppRoute.refused_page_title', { route })}
      </div>
      <div className="text-sm text-muted max-w-md">
        {i18nT('apps.builtinAppRoute.refused_page_lead')}
      </div>
      <div className="text-xs text-muted max-w-md text-left break-words">
        <div className="font-medium">{i18nT('apps.builtinAppRoute.refused_page_detail_label')}</div>
        <div className="mt-1 font-mono">{reason}</div>
      </div>
      {/* What the one remaining action DOES, in the page rather than in the
          button's `title`. A tooltip is unreachable by keyboard and by touch, so
          on a page whose only affordance is this hand-off, the promise that the
          diagnostic above travels with it was effectively unstated: a reader
          could not tell this apart from a plain new chat with a different label. */}
      <div className="text-xs text-muted max-w-md">
        {i18nT('apps.builtinAppRoute.refused_page_handoff_note')}
      </div>
      {/* No "Try Again" — see (1) above. `hard` for the same reason the default
          card uses it: the hand-off has to survive leaving this route. */}
      <AskAgentButton message={handoff} variant="solid" hard />
    </div>
  )
}

/** The `code` of the gateway's `GET /api/apps/{name}` 404 for a released name. */
const APP_RELEASED_CODE = 'app_released'

/**
 * Whether the gateway says *name* was uninstalled after it moved to its own
 * repository, so the wheel copy behind its compiled page serves nothing.
 *
 * Asked only for a name the loaded list holds no record for (`null` asks
 * nothing), which a host meets for a released name and practically never
 * otherwise. A record or any other 404 is a cached `false`. A failed lookup is
 * no answer: it rethrows, so it takes the shared retry ladder and is asked again
 * on the next mount rather than cached. Until an answer is `true` the compiled
 * page stays, as it does today. A lookup that still failed after its retries is
 * returned as `error`, so the route can say the page may not be backed.
 */
function useReleasedWithNoRecord(name: string | null): { released: boolean; error: unknown } {
  const { data, error } = useQuery({
    queryKey: ['app-released', name],
    queryFn: () => (typeof api?.getApp === 'function' ? api.getApp(name as string) : Promise.resolve(null)).then(
      () => false,
      (e: unknown) => {
        if (!isNotFoundError(e)) throw e
        return apiErrorCode(e) === APP_RELEASED_CODE
      },
    ),
    enabled: name !== null,
  })
  return { released: name !== null && data === true, error: name !== null ? error : null }
}

export default function BuiltinAppRoute() {
  const { builtinApp } = useParams<{ builtinApp: string }>()
  const path = `/${builtinApp || ''}`
  const entry = getBuiltinApp(path)
  // With a compiled page this only reads what the shell's read cached. With none,
  // the route asks for the list and waits for it: an adopted former built-in
  // whose page this dashboard does not compile still owns its old route.
  const { apps: installedApps, settled, loadError, retry } = useInstalledAppsWhen(!entry)

  // A former built-in whose record the gateway moved to its registry copy keeps
  // its old route, and renders that copy's own bundle here instead of the page
  // compiled into this dashboard. The record decides, not the route: the match
  // is the gateway's `adoption` row, and `adoptedLegacyNav` admits only an
  // adopted NON-builtin record that ships a bundle. The copy mounts under its own
  // origin (AppHost reads it from the record), never under the `builtin` origin
  // the compiled page below publishes.
  //
  // On a path with a compiled page, the list is only what the shell's own
  // `['apps']` read has already cached: the route never fetches it, waits for it
  // or reports it failing. Until a loaded list names an adopted record for this
  // path, the compiled page renders at once, as it does for every built-in with no
  // adoption row. The copy's boundary has its own key, so a list that arrives
  // after the compiled page mounted swaps in a fresh boundary rather than one that
  // may already hold that page's caught throw. Only a path no compiled page
  // answers asks for the list, waits for it and reports its failure (below).
  const adopted = installedApps?.find(app => adoptedLegacyNav(app)?.route === path)
  // A former built-in uninstalled after adoption has no record, and the gateway
  // registers none of its wheel copy's routes, so the compiled page would render
  // against a backend that is not there (empty lists, 404s) while the app's data
  // waits on disk for a reinstall. Only a list that LOADED and names no record
  // for this page's app asks the gateway, and only its `app_released` answer
  // swaps in the not-installed state, which points at Apps. Until then, and for
  // any other answer, the compiled page renders as before.
  const recordless = !!entry && !adopted && installedApps !== undefined
    && !installedApps.some(app => app.name === entry.appId)
  const { released, error: releasedLookupError } = useReleasedWithNoRecord(
    recordless && entry ? entry.appId : null,
  )
  if (adopted) {
    return (
      <ErrorBoundary key={`${path}#adopted`}>
        <InstalledAppSurface app={adopted} requestedName={adopted.name} />
      </ErrorBoundary>
    )
  }
  if (released && entry) {
    return (
      <ErrorBoundary key={`${path}#released`}>
        <InstalledAppSurface app={null} requestedName={entry.appId} />
      </ErrorBoundary>
    )
  }

  // Both boundaries below are keyed by `path`, and that key is load-bearing.
  // `App.tsx` serves all 23 builtin pages from ONE unkeyed
  // `<Route path="/:builtinApp/*">`, so a builtin→builtin move is a param
  // change on a mounted component, not a fresh mount. Both branches return
  // `ErrorBoundary` as their root, so without a key React updates the SAME
  // instance — and `ErrorBoundary.render()` returns its fallback before it ever
  // reads `children`, with no reset on a prop change. One caught throw would
  // then follow the user onto every builtin page they opened next, until a
  // reload: a contained crash turned into all of them. Keying by the resolved
  // path makes the move a remount, and keying by the RESOLVED path rather than
  // the full URL is what keeps an app's own sub-path navigation
  // (`/aws-control/usage`) from remounting the page on every step.
  if (!entry) {
    // Two different misses. An unknown path is a typo or a stale link and
    // belongs on the chat redirect. A path whose registration was REFUSED is a
    // defect in a shipped bundle: redirecting it teleports the user away from
    // the only place the problem is visible, which is exactly how it went
    // unnoticed. So that one renders the reason instead.
    const reason = builtinRefusalReason(path)
    if (reason) {
      // Built once: thrown for the journal, and its message read for the hand-off.
      const failure = refusalError(path, reason)
      return (
        <ErrorBoundary
          key={path}
          scope="builtin-route-refused"
          fallback={
            <RefusedRouteFallback route={path} reason={reason} handoff={failure.message} />
          }
        >
          <RefusedRoute failure={failure} />
        </ErrorBoundary>
      )
    }
    // Not yet an answer on whether an adopted app owns this path.
    if (!settled) return <ContentSkeleton />
    // A list that never loaded is no answer either: the path may still be an
    // adopted app's, so the failure stays on this route with a Retry instead of
    // the chat redirect, which would drop the address the user opened.
    if (loadError) {
      return (
        <div className="p-4 flex flex-col gap-3">
          <ErrorNotice
            title={i18nT('apps.builtinAppRoute.could_not_load_the_app_list')}
            message={loadError instanceof Error ? loadError.message : String(loadError)}
            askAgent
          />
          <div>
            <Btn onClick={retry}>{i18nT('apps.builtinAppRoute.retry')}</Btn>
          </div>
        </div>
      )
    }
    return <Navigate to="/chat" replace />
  }

  const { component: Component, appId } = entry

  return (
    <ErrorBoundary key={path}>
      {/* The page still renders, but whether its app is installed could not be
          checked, so it may be talking to a backend that is not there. */}
      {releasedLookupError != null && (
        <ErrorNotice
          message={releasedLookupError instanceof Error ? releasedLookupError.message : String(releasedLookupError)}
          askAgent
          className="m-4"
        />
      )}
      {/*
        Identity is published from this render body, NOT from an effect. React
        renders a parent before its children, so a provider here is guaranteed to
        be in place before the page's first child query mounts. An effect runs
        after that query has already gone out, so anything keyed off identity
        would miss its first read — the same ordering problem issue-radar solves
        by putting its `setQueryDefaults` call at module scope.

        `origin` is the literal 'builtin' rather than a field read from
        `/api/apps`, and the proof is registry membership: `entry` came from
        BUILTIN_COMPONENT_REGISTRY, whose contents are module code compiled into
        this bundle (the core table plus whatever the extensions.ts composition
        root registers). An external app cannot put itself there by any route —
        it is loaded through AppHost, and no data path feeds this registry.

        Reading `origin` from the `['apps']` query cache here would be strictly
        WEAKER, not stronger: that cache is populated by a fetch, so on a cold
        load the record is simply absent and identity would be refused for the
        first paint — turning a compile-time-provable claim into a network race,
        and breaking the synchronous publication above. The `origin !== 'builtin'`
        refusal lives where origin is genuinely data instead: AppHost passes the
        installed app's own origin, and `useTrustedAppId()` refuses it there.
      */}
      <AppIdentityProvider appId={appId} origin="builtin">
        {/*
          Keeps this app's cached data resident across leaving the page, so a
          return repaints instead of showing loading placeholders. A SIBLING
          ahead of the Suspense boundary rather than a wrapper around it: React
          reconciles children in order, so this renders — and registers — before
          the page below it mounts its first query, which is the ordering that
          matters. It renders nothing.
        */}
        <AppCacheRetention />
        <Suspense fallback={<ContentSkeleton />}>
          <Component />
        </Suspense>
      </AppIdentityProvider>
    </ErrorBoundary>
  )
}
