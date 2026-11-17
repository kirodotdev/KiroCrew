/**
 * The App Kit seam level this dashboard provides. Mirrors
 * `src/kiro_crew/apps/app_sdk_level.py`; `test/test_app_sdk_level_mirror.py` pins
 * the two equal. An app bundle reads it (namespace import, feature-detected) to
 * tell an older host from one that has the seams it was built against. Level 9
 * adds `permissions.bots`: an app's backend reaches the Bots the owner ticks for it
 * with its app token. Level 10 opens a batch: the fence on an installed app's
 * `credentialMasks`, `permissions.chatLaunch`, and `permissions.browserSites` with the
 * page's `useBrowserMedia` (play, pause, seek and volume on the app's own agent's tab).
 */
export const APP_SDK_LEVEL = 10
