/**
 * The App Kit seam level this dashboard provides. Mirrors
 * `src/kiro_crew/apps/app_sdk_level.py`; `test/test_app_sdk_level_mirror.py` pins
 * the two equal. An app bundle reads it (namespace import, feature-detected) to
 * tell an older host from one that has the seams it was built against.
 *
 * 1: the owner gate, the shared embedder, structural routes and the level-1 SDK.
 * 2: `contributes.sessionKinds` (an app-drawn session pane) and `useSessionPane()`.
 */
export const APP_SDK_LEVEL = 2
