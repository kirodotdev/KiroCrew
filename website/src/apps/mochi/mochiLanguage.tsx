/**
 * Mochi's UI language, for the windows it opens outside the dashboard SPA.
 *
 * ## Why this exists at all
 *
 * `i18nT` reads the ACTIVE language from the ambient i18next instance. The
 * dashboard seeds that once in `src/main.tsx`; Mochi's pet, panel, settings and
 * gallery are separate Vite entries in separate Electron renderer processes, so
 * each has its OWN i18next instance and none of them runs that file. Without a
 * seed here every Mochi window would render the fallback language.
 *
 * ## Why a separate language is nearly free
 *
 * Because each window is its own renderer process, setting a language here
 * cannot affect the dashboard — there is no shared singleton to contend with.
 * Had Mochi been a tab inside the dashboard, an independent language would have
 * meant fighting the ambient instance; four windows make it a one-liner instead.
 *
 * ## The resolution rule
 *
 * `mochi.language` is a NARROWING of the app's choice, not a parallel one:
 *
 *   ''        -> follow KiroCrew (the shared `mc-lang` the dashboard persists)
 *   '<code>'  -> Mochi overrides it for its own windows only
 *
 * `initI18n(undefined)` already falls back to `readStoredLanguage()`, and Mochi's
 * windows are same-origin with the dashboard, so "follow KiroCrew" needs no
 * plumbing — it is the default path.
 *
 * NOTE this is the UI language only. What language the pet REPLIES in is not
 * configured anywhere and deliberately so: forcing it through the prompt fights
 * the conversation's own context and produces unreliable results, so replies
 * simply follow the conversation.
 */
import React, { useEffect, useState } from 'react'

// The all-languages entry: plain `../../i18n` registers English only, which would
// leave every override below falling back to it.
import { changeLanguage, i18next, initI18n } from '../../i18n/all'
import { readStoredLanguage } from '../../i18n/detect'
import { api } from './src/mochiApi'

/**
 * Seed the ambient instance before first paint.
 *
 * Called at module scope by every Mochi entry, mirroring how the dashboard seeds
 * itself: synchronous so the first frame is already in the right language rather
 * than flashing the fallback and swapping once the config request lands. With no
 * argument it resolves the shared `mc-lang`, i.e. whatever KiroCrew is set to;
 * `MochiLocalized` narrows that to Mochi's own override once the config arrives.
 */
export function initMochiI18n(): void {
  initI18n()
}

/**
 * Applies Mochi's language choice and re-renders its subtree when it changes.
 *
 * The subtree is keyed on the active language rather than wired through a
 * context: `i18nT` is a plain function call that reads the language at call
 * time and does not subscribe, so React has no idea a switch should re-render.
 * Remounting on a language change is the same trade the dashboard makes, and a
 * language change is a rare explicit action.
 */
export function MochiLocalized({
  children,
  remount = true,
}: {
  children: React.ReactNode
  /**
   * Re-mount the subtree on a language change by keying it on the active
   * language. TRUE (the default) for the panel/settings/avatar windows: they are
   * cheap to rebuild and carry lots of static `i18nT` text that only refreshes on
   * a mount. FALSE for the PET window: the pet is a LIVE stateful overlay (an open
   * WS connection, a running animation/sprite, display-activation state), and a
   * hard remount tears all of that down without re-initialising it — the pet
   * simply vanishes and does not come back. The pet has almost no static chrome
   * (its bubble text is backend-supplied and its context menu is rebuilt on each
   * open), so it applies the language for its next natural render instead of
   * destroying itself to relabel. FALSE for the CROP window too, for the same
   * shape of reason: its mount effect opens the OS capture picker, so a remount
   * would ask for a second capture mid-crop, and its only static text is one hint.
   */
  remount?: boolean
}) {
  const [active, setActive] = useState(() => i18next.language)

  useEffect(() => {
    let alive = true
    const apply = (value: unknown) => {
      const override = typeof value === 'string' ? value : ''
      // Empty means "follow KiroCrew", which is what the shared key holds.
      void changeLanguage(override || readStoredLanguage()).then(() => {
        // Only the remounting windows need the state bump (it changes the
        // subtree key). The pet renders `i18nT` at call time and is a live
        // overlay, so a state-driven re-render here just makes the sprite flash
        // for nothing — changeLanguage already updated the ambient instance for
        // its next natural render (e.g. the context menu reopening).
        if (alive && remount) setActive(i18next.language)
      })
    }
    void api?.getMochiConfig?.().then(c => apply((c as { language?: string } | undefined)?.language))
    // Same channel the other live settings use, so an override applies without a
    // restart — the settings window is a different process from the pet.
    const off = api?.onConfigUpdated?.((mochi: { language?: string }) => apply(mochi?.language))
    return () => {
      alive = false
      off?.()
    }
    // `remount` is a per-window CONSTANT — all five Mochi entries pass a literal
    // (`remount={false}` in pet/main.tsx and snip/main.tsx, the default `true` in
    // panel/settings/avatar) — so a primitive dep here re-runs on nothing and this stays
    // the mount-once subscribe it reads as. Declaring it rather than closing over it is
    // what keeps that true: were a caller ever to flip it, the surviving subscription
    // would go on consulting the mounted-with value and either flash the pet or leave a
    // remounting window stuck on its old subtree key.
  }, [remount])

  return remount ? (
    <React.Fragment key={active}>{children}</React.Fragment>
  ) : (
    <React.Fragment>{children}</React.Fragment>
  )
}
