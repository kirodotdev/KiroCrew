/**
 * The App Kit seam level this dashboard provides. Mirrors
 * `src/kiro_crew/apps/app_sdk_level.py`; `test/test_app_sdk_level_mirror.py` pins
 * the two equal. An app bundle reads it (namespace import, feature-detected) to
 * tell an older host from one that has the seams it was built against. Level 9
 * adds `permissions.bots`: an app's backend reaches the Bots the owner ticks for it
 * with its app token. Level 10 opens a batch: the fence on an installed app's
 * `credentialMasks`, `permissions.chatLaunch`, and `permissions.browserSites` with the
 * page's `useBrowserMedia` (play, pause, seek and volume on the app's own agent's tab).
 * Level 11 adds `BrowserView`, `useBrowserView` and `useBrowserCoords`: the live view of
 * the app's own chat slot, with take over and hand back, behind `app_browser_sdk`; and
 * `appWebOriginStatus`. Level 12 adds `permissions.kiroOperations`: the gateway calls
 * the Kiro operations the owner approved for the app, with the person's Kiro sign-in;
 * and `SessionChanges` and `DiffView`.
 */
export const APP_SDK_LEVEL = 12
