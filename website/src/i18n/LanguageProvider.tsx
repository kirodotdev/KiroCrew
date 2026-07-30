/**
 * Language state provider — the single owner of "what language is the UI in,
 * and how does that survive a reload".
 *
 * Four things must agree, and this provider keeps them in sync:
 *   1. i18next's active language (drives every `t()` / `i18nT()` call)
 *   2. `dashboard.language` in the workspace config (server-authoritative, so
 *      the choice follows the user across browsers and the desktop app)
 *   3. `localStorage['mc-lang']` (boot fast-path — read by the inline script in
 *      index.html before React hydrates, so there is no English flash)
 *   4. `<html lang>` (screen-reader pronunciation, `:lang()` CSS, spellcheck)
 *
 * ## Why a context provider and not a plain hook
 *
 * Two independent `useState` copies of the language would desynchronise the
 * moment one of them changed: the picker in Settings would update its own state
 * and localStorage, but the remount boundary at the app root would never learn
 * about it (a `StorageEvent` does NOT fire in the tab that wrote it). One
 * provider, one state — the same shape as `ThemeProvider` and `UIModeProvider`.
 *
 * ## Why the subtree is force-re-rendered at all
 *
 * The ~250 codemod-converted call sites use the standalone `i18nT()`, which reads
 * i18next's current language but does not SUBSCRIBE to it — React has no idea
 * those components should re-render on a language change. This provider forces
 * the re-render once centrally, instead of wiring a subscription into every
 * converted call site.
 *
 * ## Why it tracks i18next's ACTIVE language, not the resolved preference
 *
 * `i18next.changeLanguage()` is ASYNCHRONOUS. Re-rendering as soon as the
 * PREFERENCE changes repaints BEFORE the catalog swap, so the tree renders the
 * OLD strings and then never re-renders (nothing else is subscribed). The visible
 * symptom: picking a language in Settings appears to do nothing until some
 * unrelated state change repaints. Tracking the `languageChanged` event means the
 * repaint happens strictly AFTER the swap — the only ordering that works.
 *
 * ## How the repaint preserves component state
 *
 * The repaint uses `cloneElement`, NOT a `key` change. Both force the subtree to
 * re-render, but a changing `key` makes React treat the element as a different
 * one and REMOUNT it — discarding component state. That is not hypothetical: the
 * theme-install URL input lives in the same Display panel as the language picker,
 * so switching language is exactly when a user would lose what they typed.
 * `cloneElement` returns a fresh element object (defeating React's
 * referential-equality bailout) while keeping type and key identical, so React
 * reconciles it as an update and state survives.
 *
 * Long-term, migrating call sites to `useTranslation()` — which subscribes
 * properly — makes this indirection unnecessary; it can be dropped once no
 * standalone `i18nT()` remains.
 */

import {
  cloneElement,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { safeSetItem } from '../utils/safeStorage'
import { changeLanguage, i18next } from './index'
import { LANG_STORAGE_KEY, readStoredLanguage, resolveLanguage } from './detect'
import { AUTO_LANGUAGE } from './languages'

interface ThemeBootData {
  language?: string
}

export interface LanguageContextValue {
  /**
   * The user's stored *choice*: a BCP-47 tag, or `''` for Auto. The picker binds
   * to this — not to the resolved language — so "Auto" stays visibly selected
   * instead of snapping to whichever concrete language it resolved to.
   */
  language: string
  /** The language actually rendering right now (never `''`). */
  resolved: string
  /**
   * What Auto would resolve to for THIS browser, regardless of the current
   * choice (never `''`).
   *
   * Deliberately independent of `resolved`: the picker annotates its Auto entry
   * with this ("Auto (follow browser) — 简体中文") to answer "what do I get if I
   * pick Auto?". Using `resolved` there made the annotation echo the current
   * selection instead — pick English and it read "— English", pick 简体中文 and
   * it read "— 简体中文", on the same browser — so it conveyed nothing and
   * actively misinformed anyone whose browser prefers a different language.
   */
  detected: string
  /** Persist a new choice (`''` = Auto) and re-render the tree. */
  setLanguage: (code: string) => void
  /**
   * True when the last attempt to persist the choice to the workspace config
   * failed, so it is browser-local only.
   *
   * This must be surfaced, not swallowed: the UI switches optimistically and
   * writes `localStorage`, so a user whose PUT failed believes the choice stuck
   * — but the next load adopts the server's (unchanged) value and overwrites the
   * local mirror, silently reverting them. There is no retry, so nothing ever
   * reconciles it on its own. Showing the failure is what makes that recoverable.
   */
  syncFailed: boolean
}

const LanguageContext = createContext<LanguageContextValue | null>(null)

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<string>(readStoredLanguage)
  const resolved = resolveLanguage(language)
  // Same resolver with the Auto sentinel, so "what would Auto give me" can never
  // drift from what Auto actually does.
  const detected = resolveLanguage(AUTO_LANGUAGE)

  // Server-authoritative value. Shares the ['theme-boot'] query key AND options
  // with useTheme, so the two read one request between them rather than two.
  // `retry: false` matches useTheme: when the gateway is unreachable we keep the
  // localStorage value instead of stalling the UI.
  //
  // Rendering deliberately does NOT wait on this query. Measured first paint:
  //   returning user (mc-lang present) → 视图   — no flash, the common case
  //   cold browser, server has zh-CN   → View → 视图
  // Only a first-ever visit in a new browser can flash, because the
  // pre-hydration script in index.html seeds the language from localStorage
  // before React mounts. Gating render on the boot response would trade that
  // one-time flash for a network round-trip blocking EVERY load — and with
  // `retry: false`, an unreachable gateway would block the first paint outright.
  // Wrong trade: the fallback is readable English, not a broken UI.
  const { data: bootData } = useQuery<ThemeBootData>({
    queryKey: ['theme-boot'],
    queryFn: () => api.themeBoot(),
    staleTime: Infinity,
    retry: false,
  })

  // Adopt the server's value ONCE, when the boot response first arrives — e.g.
  // the user picked Chinese in another browser.
  //
  // The latch is load-bearing, not an optimization. `staleTime: Infinity` means
  // `bootData.language` keeps returning the value fetched at mount, so an effect
  // that re-ran on every local change would CONTINUOUSLY re-assert it: the user
  // picks a language, this effect immediately reverts it to the stale boot value,
  // and the picker appears completely broken. (Found exactly that way — a
  // realistic boot payload, where the server always sends `language`, made every
  // switch a no-op.)
  //
  // Deliberately does NOT write back: this is the read path, and echoing here
  // would race two tabs into a write loop. Cross-tab updates arrive via the
  // StorageEvent listener below, which is the mechanism for post-boot sync.
  // Set as soon as the user picks a language. Boot adoption is skipped forever
  // after that: an explicit interaction always outranks a server value that was
  // in flight when it happened.
  //
  // Reachable, not theoretical — a probe test reproduced it: with a slow
  // /api/theme/boot, a user who picks 中文 before the response lands is reverted
  // to the server's language, localStorage included. The `adoptedServerValue`
  // latch alone did not cover this, because it only guarantees "adopt at most
  // once", not "don't adopt after the user has already chosen".
  const userChose = useRef(false)

  const adoptedServerValue = useRef(false)
  useEffect(() => {
    if (adoptedServerValue.current || userChose.current) return
    const serverLang = bootData?.language
    if (serverLang === undefined) return
    adoptedServerValue.current = true
    if (serverLang === language) return
    // Only a CONCRETE server language is adopted; `''` never overrides a local
    // choice. Two reasons, and they point the same way:
    //
    // 1. `''` is ambiguous — it means both "no workspace choice recorded" (the
    //    dataclass default, true of every fresh install) and "someone chose
    //    Auto". Adopting it would therefore ERASE a real local choice on the
    //    common path: a user who picked Chinese in this browser, on a workspace
    //    whose write never landed, is reset to auto-detect on every load with no
    //    way to make it stick.
    //
    // 2. "Auto" is inherently BROWSER-RELATIVE — it means "follow the language
    //    *this* browser asks for". Propagating it as a workspace-wide instruction
    //    is incoherent: it would tell a different browser, with a different
    //    `navigator.languages`, to discard its explicit pick in favour of a
    //    preference that resolves differently there.
    //
    // Consequence, accepted deliberately: selecting Auto in browser B does not
    // reset browser A's explicit choice. A user who wants Auto in browser A
    // selects it there — which works, and is the only place that request is
    // unambiguous. Disambiguating properly would need the backend to distinguish
    // "unset" from "explicitly auto" (a second field); not worth a schema change
    // for a case whose current behaviour is defensible.
    if (!serverLang) return
    setLanguageState(serverLang)
    safeSetItem(LANG_STORAGE_KEY, serverLang)
    // `language` is read but intentionally NOT a dependency — this must run only
    // on the boot response, never in reaction to a local change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootData?.language])

  // i18next's ACTUALLY-active language — what the repaint is driven by. See the
  // header note: changeLanguage() is async, so reacting to `resolved` would
  // repaint before the catalog swapped and render the old language.
  const [active, setActive] = useState<string>(() => i18next.language || resolved)

  useEffect(() => {
    const onChanged = (lng: string) => setActive(lng)
    i18next.on('languageChanged', onChanged)
    // i18next may already have switched between render and this subscription.
    if (i18next.language && i18next.language !== active) setActive(i18next.language)
    return () => { i18next.off('languageChanged', onChanged) }
  }, [active])

  // Apply to i18next + <html lang>. Runs on mount too, so a server-provided or
  // browser-detected language takes effect without a second render pass.
  useEffect(() => {
    void changeLanguage(resolved)
    document.documentElement.lang = resolved
  }, [resolved])

  const [syncFailed, setSyncFailed] = useState(false)

  // Tail of the persistence chain — keeps PUTs strictly ordered (see setLanguage).
  const pendingWrite = useRef<Promise<unknown>>(Promise.resolve())

  // Monotonic id for persistence writes. Concurrent PUTs can complete out of
  // order (verified: two rapid picks reached the server as ["en", "zh-CN"] —
  // reversed — leaving the UI on `en` and the config on `zh-CN`, a permanent
  // divergence). Only the newest write may report its result or be considered
  // authoritative; older in-flight ones are ignored on completion.
  const writeSeq = useRef(0)

  const setLanguage = useCallback((code: string) => {
    userChose.current = true
    setLanguageState(code)
    safeSetItem(LANG_STORAGE_KEY, code)
    setSyncFailed(false)

    const seq = ++writeSeq.current
    // Optimistic: local state + cache already reflect the choice, so a transient
    // network failure must never block the UI from switching. But the failure is
    // REPORTED rather than swallowed — see `syncFailed` above for why a silent
    // catch here strands the user on a choice that silently reverts.
    //
    // SERIALIZED: chain each write behind the previous one so the server applies
    // them in click order, and drop the result of any write that a newer pick has
    // superseded (`seq !== writeSeq.current`).
    pendingWrite.current = pendingWrite.current
      .catch(() => {})
      .then(() => api.updateThemeConfig({ language: code }))
      .then(
        () => { if (seq === writeSeq.current) setSyncFailed(false) },
        () => { if (seq === writeSeq.current) setSyncFailed(true) },
      )
  }, [])

  // Cross-tab sync: switching language in one tab updates the others without a
  // reload. Same StorageEvent pattern as useUIMode.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== LANG_STORAGE_KEY) return
      setLanguageState(e.newValue ?? AUTO_LANGUAGE)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // Re-render the subtree on a language change WITHOUT unmounting it.
  //
  // The problem: `children` is a stable element reference (main.tsx doesn't
  // re-render), and React skips re-rendering a referentially-identical element —
  // so a language change alone would repaint nothing, because the ~250
  // codemod-converted `i18nT()` call sites subscribe to nothing.
  //
  // `cloneElement` returns a NEW element object each time `active` changes, which
  // defeats that bailout. Crucially it preserves the element's TYPE and KEY, so
  // React reconciles it as an update rather than a replacement: component state
  // survives. A `key={active}` wrapper also forces the re-render, but by
  // remounting — which discards in-flight state (the theme-install URL typed in
  // this very panel, open modals, scroll position). Same repaint, no data loss.
  const refreshed = useMemo(
    () => (isValidElement(children) ? cloneElement(children) : children),
    // `active` is the dependency that matters: recompute only when i18next has
    // actually switched catalogs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [children, active],
  )

  return (
    <LanguageContext.Provider value={{ language, resolved, detected, setLanguage, syncFailed }}>
      {refreshed}
    </LanguageContext.Provider>
  )
}

/**
 * Read language state.
 *
 * Falls back to a read-only view of i18next's current state when used outside a
 * `LanguageProvider` (e.g. an isolated component test), so a missing provider
 * degrades to "language switching is inert" rather than crashing the tree.
 */
export function useLanguage(): LanguageContextValue {
  const ctx = useContext(LanguageContext)
  if (ctx) return ctx
  const stored = readStoredLanguage()
  return {
    language: stored,
    resolved: resolveLanguage(stored),
    detected: resolveLanguage(AUTO_LANGUAGE),
    setLanguage: () => {},
    syncFailed: false,
  }
}
