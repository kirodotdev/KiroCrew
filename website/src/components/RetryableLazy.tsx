import { createElement, lazy, Suspense, useState, type ComponentType } from 'react'

import ErrorBoundary from './ErrorBoundary'

export type RetryableLazyProps<P extends object> = P & {
  boundaryRetryOnly?: boolean
}

/** Build a lazy component whose Retry action performs a fresh dynamic import. */
export function retryableLazy<P extends object>(
  load: () => Promise<{ default: ComponentType<P> }>,
): ComponentType<RetryableLazyProps<P>> {
  const Initial = lazy(load)

  return function RetryableLazy({ boundaryRetryOnly = false, ...props }) {
    const [{ Lazy, attempt }, setAttempt] = useState(() => ({ Lazy: Initial, attempt: 0 }))
    const ErasedLazy = Lazy as unknown as ComponentType<Record<string, unknown>>
    return (
      <ErrorBoundary
        key={attempt}
        retryOnly={boundaryRetryOnly}
        onRetry={() => setAttempt(current => ({
          Lazy: lazy(load),
          attempt: current.attempt + 1,
        }))}
      >
        <Suspense fallback={null}>
          {createElement(ErasedLazy, props as Record<string, unknown>)}
        </Suspense>
      </ErrorBoundary>
    )
  }
}
