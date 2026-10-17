/**
 * Search Everywhere's routed-but-not-in-rail destinations, as pure data.
 *
 * `pagesProvider.ts` renders these (adding an icon per key); the find_ui index
 * generator (`scripts/gen-ui-index.mjs`) reads the same entries, so the palette
 * and the agent's location index cannot list a different set of extra pages.
 */
import { PREVIEW_WEBHOOKS } from '../../../utils/previewFlags'

/**
 * Routed-but-not-in-rail destinations (see `App.tsx` route table). Kept here —
 * never the rail — so the rail stays sourced exclusively from the registry.
 * Routes that redirect (e.g. /mc-agents, /instances) still navigate to
 * the right place via the router.
 *
 * Titles live in {@link EXTRA_PAGE_TITLE_KEY}, not here: the entry's `title` is
 * what the palette both DISPLAYS and fuzzy-matches against, so it has to be
 * resolved per search (`collectPages` in pagesProvider.ts) rather than frozen at import.
 *
 * `previewFlag` mirrors the registry field of the same name. The
 * `getAdvertisedSurfaces()` loop in `collectPages` applies that gate for
 * REGISTRY surfaces, but these extras bypass the registry entirely, so a
 * preview-gated one has to carry and be filtered on its own flag — otherwise
 * hiding a surface from the rail would smuggle it back in through ⌘K.
 */
export const EXTRA_PAGES: readonly { key: string; route: string; previewFlag?: string }[] = [
  // The App Store surface is `hiddenFromNav` (it renders as the Apps-header
  // "Explore" accent link, not a rail row), so it must be listed here to
  // stay reachable from the palette.
  { key: 'apps', route: '/apps' },
  // Library is its own page after the App Store split; the rail row exists,
  // but the palette resolves entries from this list, so it needs its own row.
  { key: 'apps-library', route: '/apps/library' },
  // Inbound webhooks is `hiddenFromNav` too (reached from Settings → Webhooks),
  // so the registry no longer offers it and the palette needs it from here. It
  // is ALSO preview-gated, so it carries `previewFlag` and stays out of the
  // palette until the operator turns it on — `hiddenFromNav` moved it out of
  // the registry's reach, which is where that gate would otherwise be applied.
  // Distinct from the `hooks` entry below (the agent-hooks page) in BOTH title
  // and icon: the two sit adjacent on a "hooks" query, and a shared glyph left
  // the route as the only thing telling them apart. The inbound arrow also says
  // which direction this one runs.
  { key: 'webhooks', route: '/webhooks', previewFlag: PREVIEW_WEBHOOKS },
  { key: 'logs', route: '/logs' },
  { key: 'developer', route: '/developer' },
  { key: 'tasks', route: '/tasks' },
  { key: 'mc-agents', route: '/mc-agents' },
  { key: 'instances', route: '/instances' },
]

/**
 * Catalog KEY for each {@link EXTRA_PAGES} title, by entry key.
 *
 * Flat `Record` of full literal keys, indexed inline at the `i18nT()` call, so
 * `scripts/check-i18n-keys.mjs` can resolve every member statically. Deliberately
 * NOT a `titleKey` field on the entries themselves: `i18nT(p.titleKey)` is a
 * member access the gate cannot resolve, and would add a second entry to
 * `dynamic-keys-baseline.json` — a ratchet that only goes down.
 */
export const EXTRA_PAGE_TITLE_KEY: Record<string, string> = {
  // Reuses the sidebar's own labels so the palette and the rail cannot
  // disagree on what the pages are called (the pre-split "Explore" title
  // survived the rail's rename to Discover exactly this way).
  apps: 'nav.discover',
  'apps-library': 'nav.library',
  // Reuses strings that already exist in every catalog rather than adding new
  // ones. Titled "Inbound webhooks", not "Webhooks", to stay distinguishable
  // from the `hooks` entry (the agent-hooks page) that sits beside it.
  webhooks: 'pages.settings.webhooksPanel.inbound_webhooks',
  logs: 'components.commandPalette.providers.pagesProvider.logs',
  developer: 'components.commandPalette.providers.pagesProvider.developer',
  tasks: 'components.commandPalette.providers.pagesProvider.tasks',
  'mc-agents': 'components.commandPalette.providers.pagesProvider.kirocrew_agents',
  instances: 'components.commandPalette.providers.pagesProvider.remote_crew',
}
