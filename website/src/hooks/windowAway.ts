/**
 * The ONE spelling of "the user is away from this window", shared by every
 * surface that must stay quiet while the user can already see the app: the
 * OS toast for a feed note (`useNativeNotification`), the opt-in OS toast for
 * a finished chat (`chatCompleteNotify`), and the in-app banner's
 * `windowFocused` gate (`NotificationBanner`, via `shouldBannerNote`).
 *
 * "Away" needs both axes. `document.hidden` covers a minimized window, another
 * virtual desktop and a background tab; `document.hasFocus()` covers a window
 * that is fully painted but sitting behind another application, which Page
 * Visibility still reports as visible (Firefox tracks no occlusion at all,
 * and side-by-side windows always report visible). Reading either axis alone
 * produced a toast on top of the very screen it described.
 *
 * With no `document` (server-side, a worker) nobody is looking, so "away".
 */
export function isWindowAway(): boolean {
  if (typeof document === 'undefined') return true
  return document.hidden || !document.hasFocus()
}
