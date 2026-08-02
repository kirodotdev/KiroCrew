/**
 * Language-change subscription for memoized components.
 *
 * ## The problem
 *
 * `React.memo` bails out when props are shallow-equal. Standalone `i18nT()`
 * reads the current catalog but does NOT subscribe to language changes, so a
 * memoized subtree whose props haven't changed keeps its stale strings after a
 * switch.
 *
 * ## The fix
 *
 * This hook reads the `active` field from `LanguageContext`. When the language
 * changes, `active` changes, the context value changes, and React re-renders
 * the consuming component - including its memoized boundary. The hook's return
 * value is intentionally unused; the side effect of subscribing to the context
 * is what matters.
 *
 * ## Usage
 *
 * Add a single line at the top of any `memo()`-wrapped component that uses
 * `i18nT()`:
 *
 * ```tsx
 * const MyComponent = memo(function MyComponent(props) {
 *   useI18nRevision()
 *   // ... existing code with i18nT() calls
 * })
 * ```
 *
 * This does NOT remove memoization - the component still skips renders when its
 * own props are unchanged AND the language is unchanged. It only adds the
 * language as an additional tracked dependency.
 */

import { useLanguage } from './LanguageProvider'

/**
 * Subscribe to language changes so `i18nT()` re-evaluates inside a memoized
 * component. Returns the active language for callers that need it, but most
 * just call it for the subscription side-effect.
 */
export function useI18nRevision(): string {
  return useLanguage().active
}
