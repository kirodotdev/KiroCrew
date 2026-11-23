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
 * and `SessionChanges` and `DiffView`. Level 13 adds `useBrowserSites`: the app's
 * browsing list (declared sites and the ones the owner added), and the owner's edits
 * through `/api/apps/<name>/browser-sites`, where adding a site asks the owner first.
 * Level 14 adds backend SDK names, one batch: `canonical_file_path`, the gateway's file
 * path gate (the UNC and link screens and the sensitive-path list, answered as the
 * canonical path or None); and `vet_public_url`, `UrlRejectedError` and
 * `address_is_not_public`, the gateway's outbound public-address check for a URL, or a
 * resolved address, an app's backend fetches.
 * Level 14 also adds `sniff_raster_mime` and `RASTER_SNIFF_BYTES`: the gateway's
 * raster sniffer, by reference.
 * Level 14 also adds the pinned path walk and copy (`pin_parent`,
 * `copy_file_pinned`); the dashboard gains nothing at that level.
 *
 * Level 15 adds `permissions.browserRecipes` and `useBrowserMedia().runRecipe`: fixed
 * clicks on selectors the gateway runs on the app's own agent's tab, no model asked.
 * Level 16 adds `useKeepAlive`: a routed page stays mounted, hidden, while the app
 * says it is busy, and the dashboard names it on every page with Open and Stop.
 */
export const APP_SDK_LEVEL = 16
