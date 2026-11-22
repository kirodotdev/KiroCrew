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
 * - `menu` is for a part rendered inside a Radix dropdown menu (the phone top
 *   bar's update item): the passive notice, then Try Again, Reload page and the
 *   hand-off as menu items, because a menu's roving focus reaches items, never
 *   a button nested in one. Try Again keeps the menu open and loads the part in
 *   place.
 *
 * `page`, `block` and `menu` offer the agent hand-off unless `askAgent` is
 * `false`. A host drawn over the chat that hand-off opens (the crew window's
 * cover) passes `onHandoff` to close once the hand-off has gone ahead, so the
 * chat is not left under it.
 */
import { lazy, useEffect, useId, useRef, useState, Suspense, type ComponentType, type ReactNode } from 'react'
import ErrorBoundary from './ErrorBoundary'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import { Btn } from './ui'
import { DropdownMenuItem } from './ui/dropdown-menu'
import { i18nT } from '../i18n/t'

export interface LazyRetryOptions {
  layout: 'page' | 'block' | 'inline' | 'menu'
  /** The failure notice's title, read at render so a language switch re-resolves it. */
  title: () => string
  /** Attributes a caught throw to its region in the error journal. */
  scope: string
  /**
   * `false` withholds the `page`, `block` and `menu` hand-off, for a part
   * beside a draft the hand-off would discard; the call site names that draft
   * in a `No hand-off:` comment. On by default.
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
   * drawn over the chat that hand-off opens. It never runs for `inline`, which
   * offers no hand-off, or `menu`, whose hand-off closes the menu itself.
   */
  onHandoff?: () => void
}

const FRAME_CLASS: Record<Exclude<LazyRetryOptions['layout'], 'menu'>, string> = {
  page: 'flex h-full items-center justify-center p-8',
  block: 'mx-4 mt-2 mb-0',
  // Its own bordered group, so its two buttons never join a row's others, and
  // the first to give up width, so the composer row's working chips keep theirs.
  // The notice's title truncates from here, so the notice's own root carries
  // no `truncate` (inlineNoticeTruncate.test.ts says why that spelling is out).
  inline: 'inline-flex min-w-0 max-w-full shrink-[1000] items-center gap-1.5 rounded-md border border-danger/40 bg-danger/10 px-1.5 py-0.5 [&_strong]:truncate',
}

// A touch-sized row, as the phone menu's other items are.
const MENU_ITEM_CLASS = '[@media(hover:none)]:min-h-10'

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
    const noticeId = useId()
    // The `menu` layout's Try Again item unmounts with the failure it belongs to.
    // Focus moves to the menu once it has gone: moved any earlier, the menu's
    // roving focus hands it straight back to that item.
    const menuToFocus = useRef<HTMLElement | null>(null)
    useEffect(() => {
      menuToFocus.current?.focus()
      menuToFocus.current = null
    }, [attempt])
    const loadedProps = props as unknown as P
    const retry = () => {
      shared = lazyComponent(load)
      setLoadState(current => ({ Loaded: shared, attempt: current.attempt + 1 }))
    }
    const buttonClass = layout === 'inline' ? 'shrink-0 px-1.5 py-0 text-[11px]' : undefined
    const retryButtons = (
      <>
        <Btn className={buttonClass} onClick={retry}>
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
            {layout === 'menu' ? (
              <>
                <ErrorNotice
                  id={noticeId}
                  variant="inline"
                  className="px-2 py-1.5"
                  title={title()}
                  message={error.message}
                  messageClassName="sr-only"
                />
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={event => {
                  // Kept open so the part loads in place.
                  event.preventDefault()
                  menuToFocus.current = (event.currentTarget as HTMLElement | null)?.closest<HTMLElement>('[role="menu"]') ?? null
                  retry()
                }}>
                  {i18nT('components.errorBoundary.try_again')}
                </DropdownMenuItem>
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => window.location.reload()}>
                  {i18nT('components.errorBoundary.reload_page')}
                </DropdownMenuItem>
                {askAgent && <ErrorNoticeMenuItem Item={DropdownMenuItem} message={error.message} describedBy={noticeId} />}
              </>
            ) : layout === 'inline' ? (
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
