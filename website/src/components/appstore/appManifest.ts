import { i18nT } from '../../i18n/t'

/**
 * Localised display copy for BUILT-IN app manifest metadata.
 *
 * THE PROBLEM. `displayName`, `description`, `highlights[]` and `ui.pages[].label` are
 * owned by the Python side — `apps/builtins/<app>/app.json` -> `discovery.py` -> `GET
 * /api/apps` — and the App Store components interpolate them raw. So a Chinese user
 * gets a translated "功能" heading directly above five English sentences, and the nav
 * rail reads "Papyrus" while that app's own page header is translated. No amount of
 * frontend i18n reaches them: the value never passes through a catalog.
 *
 * WHY THE MANIFEST IS NOT TOUCHED. The obvious fix is VS Code's shape — put `%key%`
 * in `app.json` and resolve it. That was rejected: it REPLACES the English, so every
 * consumer with no catalog starts printing a raw placeholder. `kirocrew app list`
 * (`cli_commands.py`) prints `app.get('displayName')` straight to a terminal, and the
 * same field reaches Slack and the logs. Resolving there would mean a second
 * localisation stack in Python plus a request-scoped locale the backend does not have
 * (`ui_language_tag()` returns `''` whenever the user is on "follow the browser").
 *
 * So this table is ADDITIVE. `app.json` keeps its English exactly as it was, the CLI
 * is untouched BY CONSTRUCTION rather than by a fallback, and only the React render
 * path takes a detour through the catalog. The cost of two copies of the English is
 * paid by `scripts/check-app-manifest-sync.mjs`, which fails if the catalog value and
 * the manifest prose ever stop being byte-identical.
 *
 * Shape follows `CATEGORY_LABEL_KEY` in `./categories.ts` and `EFFORT_LABEL_KEY` in
 * `lib/effort.ts` for one of their reasons: keys and not strings, because the module is
 * evaluated once at import and an `i18nT()` call in the initializer would freeze the
 * boot language. An id with no entry is returned VERBATIM rather than dressed up as
 * copy, same as `categoryLabel()`.
 *
 * WHERE IT DIFFERS FROM THOSE TWO, and what that costs. They are flat
 * `Record<string, string>` tables indexed inline at the call — `i18nT(CATEGORY_LABEL_KEY[c])`
 * — which is the one form `scripts/check-i18n-keys.mjs` resolves statically. This table
 * holds an OBJECT per app and the resolvers read `i18nT(k.displayName)` off a local, so
 * `check-i18n-keys` cannot follow it: it reports `appManifest.ts: 0 -> 4` and counts them
 * under `[dynamic-keys]`, which is report-only. **None of these keys is covered by the
 * `[key-refs]` hard zero.** Do not assume they are.
 *
 * What covers them instead is `scripts/check-app-manifest-sync.mjs`, which is a hard
 * zero: it derives the same keys from each app id and fails if any is missing from
 * `en.json` or holds anything but the manifest's own prose. Between that and
 * `catalogParity.test.ts` (every key in all ten catalogs) the population is gated — by a
 * different gate than the one a reader would expect, which is why this paragraph exists.
 * Grouping per app is deliberate even so: one entry per app is what makes the coverage
 * assertion in `src/test/appManifest.test.ts` a single lookup, and what keeps a
 * highlight list and its length together.
 *
 * Coverage is first-party only, deliberately. A third-party app's copy is its author's
 * to translate, not ours — it falls through to whatever the manifest supplied. That is
 * the same provenance-before-identity rule `sourceLabel()` and `isVerified()` in
 * `./types.ts` apply, and `pickFeatured()` alongside them; see `keysFor()` below for how
 * it is enforced here. Localising installed third-party apps needs an
 * `app.nls.<locale>.json` sidecar served next to the manifest, which is a separate
 * change, not this table.
 */
type ManifestKeys = {
  displayName: string
  description: string
  /** Absent for an app that contributes no page (e.g. an overlay-only app). */
  pageLabel?: string
  highlights: string[]
  useCases: string[]
  configuration: string[]
}

export const APP_MANIFEST_KEY: Record<string, ManifestKeys> = {
  'agent-worlds': {
    displayName: 'apps.agentWorlds.manifest.display_name',
    description: 'apps.agentWorlds.manifest.description',
    pageLabel: 'apps.agentWorlds.manifest.page_label',
    highlights: [
      'apps.agentWorlds.manifest.highlight_1',
      'apps.agentWorlds.manifest.highlight_2',
      'apps.agentWorlds.manifest.highlight_3',
      'apps.agentWorlds.manifest.highlight_4',
      'apps.agentWorlds.manifest.highlight_5',
    ],
    useCases: ['apps.agentWorlds.manifest.use_case_1'],
    configuration: ['apps.agentWorlds.manifest.configuration_1'],
  },
  'auto-improvement': {
    displayName: 'apps.autoImprovement.manifest.display_name',
    description: 'apps.autoImprovement.manifest.description',
    pageLabel: 'apps.autoImprovement.manifest.page_label',
    highlights: [
      'apps.autoImprovement.manifest.highlight_1',
      'apps.autoImprovement.manifest.highlight_2',
      'apps.autoImprovement.manifest.highlight_3',
      'apps.autoImprovement.manifest.highlight_4',
      'apps.autoImprovement.manifest.highlight_5',
      'apps.autoImprovement.manifest.highlight_6',
    ],
    useCases: ['apps.autoImprovement.manifest.use_case_1'],
    configuration: ['apps.autoImprovement.manifest.configuration_1'],
  },
  'auto-research': {
    displayName: 'apps.autoResearch.manifest.display_name',
    description: 'apps.autoResearch.manifest.description',
    pageLabel: 'apps.autoResearch.manifest.page_label',
    highlights: [
      'apps.autoResearch.manifest.highlight_1',
      'apps.autoResearch.manifest.highlight_2',
      'apps.autoResearch.manifest.highlight_3',
      'apps.autoResearch.manifest.highlight_4',
      'apps.autoResearch.manifest.highlight_5',
      'apps.autoResearch.manifest.highlight_6',
      'apps.autoResearch.manifest.highlight_7',
    ],
    useCases: ['apps.autoResearch.manifest.use_case_1'],
    configuration: ['apps.autoResearch.manifest.configuration_1'],
  },
  'auto-triage-pipeline': {
    displayName: 'apps.autoTriagePipeline.manifest.display_name',
    description: 'apps.autoTriagePipeline.manifest.description',
    pageLabel: 'apps.autoTriagePipeline.manifest.page_label',
    highlights: [],
    useCases: ['apps.autoTriagePipeline.manifest.use_case_1'],
    configuration: ['apps.autoTriagePipeline.manifest.configuration_1'],
  },
  'channels': {
    displayName: 'apps.channels.manifest.display_name',
    description: 'apps.channels.manifest.description',
    pageLabel: 'apps.channels.manifest.page_label',
    highlights: [
      'apps.channels.manifest.highlight_1',
      'apps.channels.manifest.highlight_2',
      'apps.channels.manifest.highlight_3',
      'apps.channels.manifest.highlight_4',
      'apps.channels.manifest.highlight_5',
    ],
    useCases: ['apps.channels.manifest.use_case_1'],
    configuration: ['apps.channels.manifest.configuration_1'],
  },
  'code-review-sage': {
    displayName: 'apps.codeReviewSage.manifest.display_name',
    description: 'apps.codeReviewSage.manifest.description',
    pageLabel: 'apps.codeReviewSage.manifest.page_label',
    highlights: [
      'apps.codeReviewSage.manifest.highlight_1',
      'apps.codeReviewSage.manifest.highlight_2',
      'apps.codeReviewSage.manifest.highlight_3',
      'apps.codeReviewSage.manifest.highlight_4',
      'apps.codeReviewSage.manifest.highlight_5',
    ],
    useCases: ['apps.codeReviewSage.manifest.use_case_1'],
    configuration: ['apps.codeReviewSage.manifest.configuration_1'],
  },
  // Overlay-only: no `pageLabel`, because this app contributes no page.
  'command-bar': {
    displayName: 'apps.commandBar.manifest.display_name',
    description: 'apps.commandBar.manifest.description',
    highlights: [],
    useCases: ['apps.commandBar.manifest.use_case_1'],
    configuration: ['apps.commandBar.manifest.configuration_1'],
  },
  'crew-companion': {
    displayName: 'apps.crewCompanion.manifest.display_name',
    description: 'apps.crewCompanion.manifest.description',
    pageLabel: 'apps.crewCompanion.manifest.page_label',
    highlights: [
      'apps.crewCompanion.manifest.highlight_1',
      'apps.crewCompanion.manifest.highlight_2',
      'apps.crewCompanion.manifest.highlight_3',
      'apps.crewCompanion.manifest.highlight_4',
      'apps.crewCompanion.manifest.highlight_5',
    ],
    useCases: ['apps.crewCompanion.manifest.use_case_1'],
    configuration: ['apps.crewCompanion.manifest.configuration_1'],
  },
  'design-critique': {
    displayName: 'apps.designCritique.manifest.display_name',
    description: 'apps.designCritique.manifest.description',
    pageLabel: 'apps.designCritique.manifest.page_label',
    highlights: [
      'apps.designCritique.manifest.highlight_1',
      'apps.designCritique.manifest.highlight_2',
      'apps.designCritique.manifest.highlight_3',
      'apps.designCritique.manifest.highlight_4',
      'apps.designCritique.manifest.highlight_5',
      'apps.designCritique.manifest.highlight_6',
      'apps.designCritique.manifest.highlight_7',
      'apps.designCritique.manifest.highlight_8',
    ],
    useCases: ['apps.designCritique.manifest.use_case_1'],
    configuration: ['apps.designCritique.manifest.configuration_1'],
  },
  // `design-tweak` ships no `highlights` (matching `spec-builder`'s precedent
  // below): app.json declares no `highlights` field, so the App Store card
  // shows displayName/description/pageLabel only.
  'design-tweak': {
    displayName: 'apps.designTweak.manifest.display_name',
    description: 'apps.designTweak.manifest.description',
    pageLabel: 'apps.designTweak.manifest.page_label',
    highlights: [],
    useCases: ['apps.designTweak.manifest.use_case_1'],
    configuration: ['apps.designTweak.manifest.configuration_1'],
  },
  'dev-fleet': {
    displayName: 'apps.devFleet.manifest.display_name',
    description: 'apps.devFleet.manifest.description',
    pageLabel: 'apps.devFleet.manifest.page_label',
    highlights: [
      'apps.devFleet.manifest.highlight_1',
      'apps.devFleet.manifest.highlight_2',
      'apps.devFleet.manifest.highlight_3',
      'apps.devFleet.manifest.highlight_4',
      'apps.devFleet.manifest.highlight_5',
      'apps.devFleet.manifest.highlight_6',
      'apps.devFleet.manifest.highlight_7',
    ],
    useCases: ['apps.devFleet.manifest.use_case_1'],
    configuration: ['apps.devFleet.manifest.configuration_1'],
  },
  'file-explorer': {
    displayName: 'apps.fileExplorer.manifest.display_name',
    description: 'apps.fileExplorer.manifest.description',
    pageLabel: 'apps.fileExplorer.manifest.page_label',
    highlights: [
      'apps.fileExplorer.manifest.highlight_1',
      'apps.fileExplorer.manifest.highlight_2',
      'apps.fileExplorer.manifest.highlight_3',
      'apps.fileExplorer.manifest.highlight_4',
      'apps.fileExplorer.manifest.highlight_5',
      'apps.fileExplorer.manifest.highlight_6',
    ],
    useCases: ['apps.fileExplorer.manifest.use_case_1'],
    configuration: ['apps.fileExplorer.manifest.configuration_1'],
  },
  'issue-radar': {
    displayName: 'apps.issueRadar.manifest.display_name',
    description: 'apps.issueRadar.manifest.description',
    pageLabel: 'apps.issueRadar.manifest.page_label',
    highlights: [
      'apps.issueRadar.manifest.highlight_1',
      'apps.issueRadar.manifest.highlight_2',
      'apps.issueRadar.manifest.highlight_3',
      'apps.issueRadar.manifest.highlight_4',
      'apps.issueRadar.manifest.highlight_5',
      'apps.issueRadar.manifest.highlight_6',
      'apps.issueRadar.manifest.highlight_7',
      'apps.issueRadar.manifest.highlight_8',
      'apps.issueRadar.manifest.highlight_9',
    ],
    useCases: ['apps.issueRadar.manifest.use_case_1'],
    configuration: ['apps.issueRadar.manifest.configuration_1'],
  },
  'md-notebook': {
    displayName: 'apps.mdNotebook.manifest.display_name',
    description: 'apps.mdNotebook.manifest.description',
    pageLabel: 'apps.mdNotebook.manifest.page_label',
    highlights: [
      'apps.mdNotebook.manifest.highlight_1',
      'apps.mdNotebook.manifest.highlight_2',
      'apps.mdNotebook.manifest.highlight_3',
      'apps.mdNotebook.manifest.highlight_4',
      'apps.mdNotebook.manifest.highlight_5',
      'apps.mdNotebook.manifest.highlight_6',
      'apps.mdNotebook.manifest.highlight_7',
      'apps.mdNotebook.manifest.highlight_8',
    ],
    useCases: ['apps.mdNotebook.manifest.use_case_1'],
    configuration: ['apps.mdNotebook.manifest.configuration_1'],
  },
  'meetings': {
    displayName: 'apps.meetings.manifest.display_name',
    description: 'apps.meetings.manifest.description',
    pageLabel: 'apps.meetings.manifest.page_label',
    highlights: [
      'apps.meetings.manifest.highlight_1',
      'apps.meetings.manifest.highlight_2',
      'apps.meetings.manifest.highlight_3',
      'apps.meetings.manifest.highlight_4',
      'apps.meetings.manifest.highlight_5',
    ],
    useCases: ['apps.meetings.manifest.use_case_1'],
    configuration: ['apps.meetings.manifest.configuration_1'],
  },
  'mochi': {
    displayName: 'apps.mochi.manifest.display_name',
    description: 'apps.mochi.manifest.description',
    pageLabel: 'apps.mochi.manifest.page_label',
    highlights: [
      'apps.mochi.manifest.highlight_1',
      'apps.mochi.manifest.highlight_2',
      'apps.mochi.manifest.highlight_3',
      'apps.mochi.manifest.highlight_4',
      'apps.mochi.manifest.highlight_5',
      'apps.mochi.manifest.highlight_6',
    ],
    useCases: ['apps.mochi.manifest.use_case_1'],
    configuration: ['apps.mochi.manifest.configuration_1'],
  },
  'ops-mission-control': {
    displayName: 'apps.opsMissionControl.manifest.display_name',
    description: 'apps.opsMissionControl.manifest.description',
    pageLabel: 'apps.opsMissionControl.manifest.page_label',
    highlights: [
      'apps.opsMissionControl.manifest.highlight_1',
      'apps.opsMissionControl.manifest.highlight_2',
      'apps.opsMissionControl.manifest.highlight_3',
      'apps.opsMissionControl.manifest.highlight_4',
      'apps.opsMissionControl.manifest.highlight_5',
      'apps.opsMissionControl.manifest.highlight_6',
    ],
    useCases: ['apps.opsMissionControl.manifest.use_case_1'],
    configuration: ['apps.opsMissionControl.manifest.configuration_1'],
  },
  'papyrus': {
    displayName: 'apps.papyrus.manifest.display_name',
    description: 'apps.papyrus.manifest.description',
    pageLabel: 'apps.papyrus.manifest.page_label',
    highlights: [
      'apps.papyrus.manifest.highlight_1',
      'apps.papyrus.manifest.highlight_2',
      'apps.papyrus.manifest.highlight_3',
      'apps.papyrus.manifest.highlight_4',
      'apps.papyrus.manifest.highlight_5',
      'apps.papyrus.manifest.highlight_6',
      'apps.papyrus.manifest.highlight_7',
    ],
    useCases: ['apps.papyrus.manifest.use_case_1'],
    configuration: ['apps.papyrus.manifest.configuration_1'],
  },
  'personal-shopper': {
    displayName: 'apps.personalShopper.manifest.display_name',
    description: 'apps.personalShopper.manifest.description',
    pageLabel: 'apps.personalShopper.manifest.page_label',
    highlights: [
      'apps.personalShopper.manifest.highlight_1',
      'apps.personalShopper.manifest.highlight_2',
      'apps.personalShopper.manifest.highlight_3',
      'apps.personalShopper.manifest.highlight_4',
      'apps.personalShopper.manifest.highlight_5',
    ],
    useCases: ['apps.personalShopper.manifest.use_case_1'],
    configuration: ['apps.personalShopper.manifest.configuration_1'],
  },
  'pptx-maker': {
    displayName: 'apps.pptxMaker.manifest.display_name',
    description: 'apps.pptxMaker.manifest.description',
    pageLabel: 'apps.pptxMaker.manifest.page_label',
    highlights: [
      'apps.pptxMaker.manifest.highlight_1',
      'apps.pptxMaker.manifest.highlight_2',
      'apps.pptxMaker.manifest.highlight_3',
      'apps.pptxMaker.manifest.highlight_4',
      'apps.pptxMaker.manifest.highlight_5',
      'apps.pptxMaker.manifest.highlight_6',
    ],
    useCases: ['apps.pptxMaker.manifest.use_case_1'],
    configuration: ['apps.pptxMaker.manifest.configuration_1'],
  },
  'projects': {
    displayName: 'apps.projects.manifest.display_name',
    description: 'apps.projects.manifest.description',
    pageLabel: 'apps.projects.manifest.page_label',
    highlights: [
      'apps.projects.manifest.highlight_1',
      'apps.projects.manifest.highlight_2',
      'apps.projects.manifest.highlight_3',
      'apps.projects.manifest.highlight_4',
      'apps.projects.manifest.highlight_5',
    ],
    useCases: ['apps.projects.manifest.use_case_1'],
    configuration: ['apps.projects.manifest.configuration_1'],
  },
  // `spec-builder` ships no `highlights`, so its list is empty on both sides and
  // `appHighlights()` returns the manifest's own empty array. An entry is still
  // required: the sync gate derives keys from the app id, not from this table.
  'spec-builder': {
    displayName: 'apps.specBuilder.manifest.display_name',
    description: 'apps.specBuilder.manifest.description',
    pageLabel: 'apps.specBuilder.manifest.page_label',
    highlights: [],
    useCases: ['apps.specBuilder.manifest.use_case_1'],
    configuration: ['apps.specBuilder.manifest.configuration_1'],
  },
  'workflows': {
    displayName: 'apps.workflows.manifest.display_name',
    description: 'apps.workflows.manifest.description',
    pageLabel: 'apps.workflows.manifest.page_label',
    highlights: [
      'apps.workflows.manifest.highlight_1',
      'apps.workflows.manifest.highlight_2',
      'apps.workflows.manifest.highlight_3',
      'apps.workflows.manifest.highlight_4',
      'apps.workflows.manifest.highlight_5',
    ],
    useCases: ['apps.workflows.manifest.use_case_1'],
    configuration: ['apps.workflows.manifest.configuration_1'],
  },
}

/**
 * `hasOwnProperty`, not `in`: an app named `toString` would otherwise resolve to an
 * inherited `Object.prototype` member and hand a function to i18next. Same guard as
 * `categoryLabel()`; `effort.ts` documents the incident that made it a rule.
 *
 * Both provenance checks are required before the id is looked up. `_registry` rejects
 * an external store row even when install-state enrichment lends it `origin: builtin`;
 * `origin` rejects an installed third-party record, whose detail payload carries no
 * `_registry`. An id alone is never provenance: otherwise an app named `projects`
 * inherits trusted first-party copy next to controls that run its setup code.
 */
function keysFor(app: { name?: string, _registry?: string, origin?: string }): ManifestKeys | undefined {
  if (!app.name || app._registry || app.origin !== 'builtin') return undefined
  return Object.prototype.hasOwnProperty.call(APP_MANIFEST_KEY, app.name)
    ? APP_MANIFEST_KEY[app.name]
    : undefined
}

/** Localised app name, falling back to the manifest's own value then its id. */
export function appDisplayName(app: { name?: string; displayName?: string; _registry?: string; origin?: string }): string {
  const k = keysFor(app)
  return k ? i18nT(k.displayName) : (app.displayName || app.name || '')
}

/** Localised one-paragraph app description. */
export function appDescription(app: { name?: string; description?: string; _registry?: string; origin?: string }): string {
  const k = keysFor(app)
  return k ? i18nT(k.description) : (app.description || '')
}

/**
 * Localised nav-rail / sidebar label for an app's page.
 *
 * `label` is passed separately because the caller reads it off `ui.pages[i]` rather
 * than off the app record, and `App.tsx` resolves `page.label || displayName || name`.
 * Only installed apps contribute nav pages, so there is no `_registry` to weigh here.
 */
export function appPageLabel(name: string | undefined, label?: string, displayName?: string, origin?: string): string {
  const k = keysFor({ name, origin })
  // An overlay-only builtin declares no page-label key; fall through to the caller's
  // own strings exactly as a third-party app does.
  return k?.pageLabel ? i18nT(k.pageLabel) : (label || displayName || name || '')
}

/**
 * Localised feature bullets.
 *
 * The length guard is the point: if a manifest gains a seventh highlight and this
 * table is not updated, translating the six it knows about would SILENTLY DROP the
 * new one. Falling back to the manifest array instead renders all seven in English —
 * complete but untranslated, which the en-XA render gate then reports on `app-detail`.
 * Losing a bullet is a worse failure than showing it in the wrong language, and
 * `check-app-manifest-sync.mjs` fails the build for the same mismatch anyway.
 */
export function appHighlights(app: { name?: string; highlights?: string[]; _registry?: string; origin?: string }): string[] {
  const manifest = app.highlights || []
  const k = keysFor(app)
  if (!k || k.highlights.length !== manifest.length) return manifest
  return k.highlights.map(key => i18nT(key))
}

/**
 * Localised, operator-oriented situations where the app is a good fit.
 *
 * Installed third-party records do not carry `_registry`, so guidance has an
 * additional fail-closed provenance check. Without it, an installed app that
 * reuses a built-in id inherits first-party setup copy from the locale catalog.
 */
export function appUseCases(app: { name?: string; useCases?: unknown; _registry?: string; origin?: string }): string[] {
  const manifest = Array.isArray(app.useCases)
    && app.useCases.every((item): item is string => typeof item === 'string')
    ? app.useCases
    : []
  const k = keysFor(app)
  if (!k || k.useCases.length !== manifest.length) return manifest
  return k.useCases.map(key => i18nT(key))
}

/** Localised, concise setup/configuration instructions for an app. */
export function appConfiguration(app: { name?: string; configuration?: unknown; _registry?: string; origin?: string }): string[] {
  const manifest = Array.isArray(app.configuration)
    && app.configuration.every((item): item is string => typeof item === 'string')
    ? app.configuration
    : []
  const k = keysFor(app)
  if (!k || k.configuration.length !== manifest.length) return manifest
  return k.configuration.map(key => i18nT(key))
}
