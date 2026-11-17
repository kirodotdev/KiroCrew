/**
 * A lazily loaded component whose chunk can fail to load, with a retry that
 * loads it again.
 *
 * React surfaces a rejected `import()` as a render throw, and `lazy()` caches
 * the rejection: clearing a boundary's error only renders the same rejection
 * again, with no new fetch. Each Try Again here builds a new `lazy()` and
 * remounts the boundary, so the next attempt runs `import()` again. That new
 * `import()` is also what fires `vite:preloadError`, whose guard in main.tsx
 * reloads a tab whose browser keeps the failed fetch in its module map. Reload
 * page sits beside it for when that guard has given up.
 *
 * The failure notice renders where the component would have:
 * - `page` centres it in the area the component fills (a route page, the crew
 *   window's cover).
 * - `block` makes it one more banner in a column of notices, as tall as its
 *   text, so an optional part of a pane never takes the pane's height.
 * - `inline` makes it one line in a row of controls (the composer's chips) or
 *   under a message (the share dialog): the title and the two buttons, with
 *   the raw error read out to assistive tech and shown on hover. It offers no
 *   agent hand-off, because that line sits beside an unsaved draft.
 *
 * `page` and `block` offer the agent hand-off unless `askAgent` is `false`. A
 * host drawn over the chat that hand-off opens (the crew window's cover) passes
 * `onHandoff` to close once the hand-off has gone ahead, so the chat is not left
 * under it.
 */
import { lazy, useState, Suspense, type ComponentType, type ReactNode } from 'react'
import ErrorBoundary from './ErrorBoundary'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { i18nT } from '../i18n/t'

export interface LazyRetryOptions {
  layout: 'page' | 'block' | 'inline'
  /** The failure notice's title, read at render so a language switch re-resolves it. */
  title: () => string
  /** Attributes a caught throw to its region in the error journal. */
  scope: string
  /**
   * `false` withholds the `page` and `block` hand-off, for a part beside a draft
   * the hand-off would discard; the call site names that draft in a
   * `No hand-off:` comment. On by default.
   */
  askAgent?: boolean
}

/** Props the host passes besides the loaded component's own. */
export interface LazyRetryHostProps {
  /** Stands in while the chunk loads. Nothing by default. */
  loading?: ReactNode
  /**
   * Mounted beside the failure notice: what the loading stand-in holds that a
   * failed load must keep holding, because the host still draws the area.
   */
  keepOnFailure?: ReactNode
  /**
   * `useLanguageGeneration()` from a parent the React Compiler compiles, which
   * would otherwise hand back the cached element after a language switch and
   * leave the failure notice in the previous language.
   */
  languageGeneration?: number
  /**
   * Runs once the failure notice's agent hand-off has gone ahead, for a host
   * drawn over the chat that hand-off opens. The `inline` layout offers no
   * hand-off, so it never runs there.
   */
  onHandoff?: () => void
}

const FRAME_CLASS: Record<LazyRetryOptions['layout'], string> = {
  page: 'flex h-full items-center justify-center p-8',
  block: 'mx-4 mt-2 mb-0',
  // Its own bordered group, so its two buttons never join a row's others, and
  // the first to give up width, so the composer row's working chips keep theirs.
  // The notice's title truncates from here, so the notice's own root carries
  // no `truncate` (inlineNoticeTruncate.test.ts says why that spelling is out).
  inline: 'inline-flex min-w-0 max-w-full shrink-[1000] items-center gap-1.5 rounded-md border border-danger/40 bg-danger/10 px-1.5 py-0.5 [&_strong]:truncate',
}

/**
 * `lazy(load)` typed as the component it resolves to. `lazy()`'s own type
 * spells the props through conditional types TypeScript cannot resolve while
 * `P` is generic, so it would refuse the very props the loaded component takes.
 */
function lazyComponent<P extends object>(load: () => Promise<{ default: ComponentType<P> }>): ComponentType<P> {
  return lazy(load) as unknown as ComponentType<P>
}

export function lazyRetry<P extends object>(
  load: () => Promise<{ default: ComponentType<P> }>,
  { layout, title, scope, askAgent = true }: LazyRetryOptions,
): ComponentType<P & LazyRetryHostProps> {
  // Shared by every mount, so a component whose chunk already loaded renders
  // on the next mount without suspending again. Replaced only by a retry.
  let shared = lazyComponent(load)
  function LazyRetry({ loading = null, keepOnFailure = null, languageGeneration, onHandoff, ...props }: P & LazyRetryHostProps) {
    const [{ Loaded, attempt }, setLoadState] = useState(() => ({ Loaded: shared, attempt: 0 }))
    const loadedProps = props as unknown as P
    const buttonClass = layout === 'inline' ? 'shrink-0 px-1.5 py-0 text-[11px]' : undefined
    const retryButtons = (
      <>
        <Btn className={buttonClass} onClick={() => {
          shared = lazyComponent(load)
          setLoadState(current => ({ Loaded: shared, attempt: current.attempt + 1 }))
        }}>
          {i18nT('components.errorBoundary.try_again')}
        </Btn>
        <Btn className={buttonClass} onClick={() => window.location.reload()}>
          {i18nT('components.errorBoundary.reload_page')}
        </Btn>
      </>
    )
    return (
      <ErrorBoundary
        key={attempt}
        scope={scope}
        languageGeneration={languageGeneration}
        fallback={(error) => (
          <>
            {keepOnFailure}
            {layout === 'inline' ? (
              <span className={FRAME_CLASS.inline} title={error.message}>
                {/* No hand-off: the unsent composer draft beside this line (the chat's,
                    or a ChatEmbed's around a message) is unsaved. */}
                {/* One line, never taller than the row: a raw chunk URL would
                    take the row's width, so it is read out and shown on hover,
                    and the title truncates when the row is narrow. */}
                <ErrorNotice
                  variant="inline"
                  title={title()}
                  message={error.message}
                  messageClassName="sr-only"
                  className="min-w-0"
                />
                {retryButtons}
              </span>
            ) : (
              <div className={FRAME_CLASS[layout]}>
                <ErrorNotice
                  title={title()}
                  message={error.message}
                  askAgent={askAgent}
                  onHandoff={onHandoff}
                  footer={<div className="flex items-center gap-2">{retryButtons}</div>}
                />
              </div>
            )}
          </>
        )}
      >
        <Suspense fallback={loading}><Loaded {...loadedProps} /></Suspense>
      </ErrorBoundary>
    )
  }
  return LazyRetry
}
