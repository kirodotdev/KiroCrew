/**
 * The App Kit seam level this dashboard provides. Mirrors
 * `src/kiro_crew/apps/app_sdk_level.py`; `test/test_app_sdk_level_mirror.py` pins
 * the two equal. An app bundle reads it (namespace import, feature-detected) to
 * tell an older host from one that has the seams it was built against. Level 8
 * adds `permissions.bots`: an app's backend reaches the Bots the owner ticks for it
 * with its app token.
 */
export const APP_SDK_LEVEL = 8
