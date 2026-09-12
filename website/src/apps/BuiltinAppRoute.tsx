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
import { getBuiltinApp } from './builtinRegistry'
import { AppIdentityProvider } from '../app-sdk/identity'
import { AppCacheRetention } from '../app-sdk/appQuery'
import ErrorBoundary from '../components/ErrorBoundary'
import { ContentSkeleton } from '../components/ui'

export default function BuiltinAppRoute() {
  const { builtinApp } = useParams<{ builtinApp: string }>()
  const path = `/${builtinApp || ''}`
  const entry = getBuiltinApp(path)

  if (!entry) {
    return <Navigate to="/chat" replace />
  }

  const { component: Component, appId } = entry

  return (
    <ErrorBoundary>
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
