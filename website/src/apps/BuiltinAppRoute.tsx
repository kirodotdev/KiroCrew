/**
 * BuiltinAppRoute — dynamic route handler for builtin app pages.
 *
 * Resolves the current URL path against the builtin component registry
 * and renders the matching page component with Suspense + ErrorBoundary.
 *
 * Used as a catch-all route for builtin app paths, eliminating the need
 * to hardcode each builtin app's <Route> in App.tsx.
 */
import { Suspense } from 'react'
import { useParams, Navigate } from 'react-router-dom'
import { getBuiltinComponent } from './builtinRegistry'
import ErrorBoundary from '../components/ErrorBoundary'
import { ContentSkeleton } from '../components/ui'

export default function BuiltinAppRoute() {
  const { builtinApp } = useParams<{ builtinApp: string }>()
  const path = `/${builtinApp || ''}`
  const Component = getBuiltinComponent(path)

  if (!Component) {
    return <Navigate to="/chat" replace />
  }

  return (
    <ErrorBoundary>
      <Suspense fallback={<ContentSkeleton />}>
        <Component />
      </Suspense>
    </ErrorBoundary>
  )
}
