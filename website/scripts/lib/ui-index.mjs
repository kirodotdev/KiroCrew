/**
 * The find_ui location index: pure building blocks for `gen-ui-index.mjs`.
 *
 * Nothing here walks the filesystem or exits the process; the entry script
 * gathers inputs and these functions turn them into one index plus a list of
 * errors. The fixture tests in `src/uiLocations/uiIndex.test.ts` drive the same
 * functions on synthetic sources.
 *
 * Three kinds of input become locations, and none of them is guessed:
 *
 *  - pages and tabs, from the registries the dashboard itself renders
 *    (`surfaceData.ts`, `pagesData.ts`, the Settings and Customize tab lists);
 *  - settings, projected from the committed settings extraction (the same
 *    entries settings search and `settings.show` use), joined by stable id;
 *  - registered controls: a `uiLocation('<id>')` marker at the real render site
 *    plus one descriptor in its area file, `src/uiLocations/areas/<area>.ts`
 *    (merged by {@link mergeLocationAreas}). The marker proves
 *    where the control is drawn and which catalog key its visible label comes
 *    from; the descriptor supplies what syntax cannot (parents, prerequisites).
 *
 * A registered control whose label, marker or parent cannot be proven is an
 * ERROR, never a best guess: a wrong path is worse than no path.
 *
 * This module assembles the index and its artifacts (`buildUiIndex`, the auto
 * tier's locations and `buildAutoArtifact`, the digests, the stamp manifest,
 * the serializers) and is the one import path: it re-exports every name its
 * owners serve callers (not the helpers `ui-index-source.mjs` shares with its
 * siblings), and no owner imports it. `ui-index-source.mjs` reads the
 * dashboard source, `ui-index-auto.mjs` maps files to pages and decides each
 * auto site's id and guide policy, and `ui-index-guide.mjs` compiles the
 * `ui.show` guide plans.
 */

import { createHash } from 'node:crypto'

import { DESCRIPTION_PREFIX, LITERAL_PREFIX, matchRoute } from './ui-index-source.mjs'
import {
  AUTO_MARKER_ATTR,
  autoParentId,
  autoSitePolicy,
  GUIDABLE_POLICIES,
  SHARED_PARENT,
  SHELL_SURFACE,
} from './ui-index-auto.mjs'
import {
  autoGuidePlan,
  compileGuideNeeds,
  compileRevealScopes,
  GUIDE_CAUTION_IDS,
  GUIDE_DENY_IDS,
  guidePlanFor,
} from './ui-index-guide.mjs'
// Every name the owners serve callers, re-exported: import them from here.
export {
  collectKeyMap,
  collectNavItemPaths,
  collectObjectList,
  collectRouteTable,
  DESCRIPTION_PREFIX,
  isBoundTranslateCall,
  MARKER_ATTR,
  MARKER_HELPER,
  matchRoute,
  parseSource,
  resolveSiteLabel,
  scanCandidates,
  scanMarkerSource,
  SEGMENT_HOSTS,
} from './ui-index-source.mjs'
export {
  AUDITED_PRIMITIVES,
  AUTO_MARKER_ATTR,
  AUTO_SITE_ID_RE,
  autoParentId,
  autoSiteIds,
  autoSitePolicy,
  buildPageMap,
  candidateKind,
  CAUTION_LABEL_RE,
  classifyUses,
  collectRouteElementSpans,
  collectTabPanels,
  GUIDABLE_POLICIES,
  GUIDE_POLICIES,
  makeRouteLabel,
  scanImports,
  SENSITIVE_LABEL_RE,
  SHARED_PARENT,
  SHELL_SURFACE,
  TRUST_ROOT_PARENTS,
} from './ui-index-auto.mjs'
export {
  autoGuidePlan,
  compileGuideNeeds,
  compileRevealScopes,
  GUIDE_CAUTION_IDS,
  GUIDE_DENY_IDS,
  GUIDE_MAX_STEPS,
  GUIDE_PLAN_VERSION,
  guidePlanFor,
  OPENER_FACT_PREDICATES,
  previewGateId,
} from './ui-index-guide.mjs'

export const SCHEMA_VERSION = 1

/** Prefixes owned by generated locations; a registered id may not use them. */
const RESERVED_PREFIXES = ['page.', 'tab.', 'settings.', 'setting:']
const LOCATION_ID_RE = /^[a-z0-9]+(?:[.-][a-z0-9]+)+$/
/** What the auto tier covers, said in the full index's scope and the auto artifact's. */
export const AUTO_SCOPE = 'auto-indexed controls (label and page proven from source, exact place and prerequisites unknown)'
/**
 * Name of the build-time auto-tier artifact. `npm run build` writes it into the
 * vite output (website/dist), so it ships wherever the dashboard bundle does
 * (src/kiro_crew/static/dist); it is never committed.
 */
export const AUTO_ARTIFACT_NAME = 'ui-index.auto.json'

const REQUIREMENT_KINDS = new Set(['shown_by', 'viewport', 'preview_flag', 'condition'])
const ENTRY_KINDS = new Set(['rail', 'tab', 'sidebar', 'menu', 'toolbar', 'header', 'content', 'direct-link'])
const KINDS = new Set(['page', 'tab', 'setting', 'button', 'toggle', 'disclosure', 'menu-item', 'link', 'field', 'list'])

// ---------------------------------------------------------------- settings freshness

/**
 * Compare the live settings extraction (run in memory by the entry script)
 * with the two committed outputs this index is built from. Any byte difference
 * means the index would be generated from stale Settings controls.
 */
export function checkSettingsExtraction({ liveRegistrySource, liveAgentJson, committedRegistrySource, committedAgentJson }) {
  const errors = []
  if (liveRegistrySource !== committedRegistrySource) {
    errors.push('settingsRegistry.gen.ts does not match the live settings extraction; run npm run gen:settings')
  }
  if (liveAgentJson !== committedAgentJson) {
    errors.push('settings-registry.generated.json does not match the live settings extraction; run npm run gen:settings')
  }
  return errors
}

// ---------------------------------------------------------------- auto tier: build digest, stamp manifest

/**
 * The auto tier's own build digest: over the committed `build_digest` it
 * hangs off and every auto plan. The gateway records it on an auto `ui.show`
 * guide and the bundle carries the same value (injected at build from the
 * stamp manifest), so a tab built from another tree refuses (`build_mismatch`).
 */
export function autoBuildDigest(baseBuildDigest, locations) {
  const plans = {}
  for (const l of locations) if (l.guide_plan) plans[l.id] = l.guide_plan
  return `sha256:${createHash('sha256').update(JSON.stringify({ base: baseBuildDigest, plans })).digest('hex')}`
}

/**
 * What the build-time marker step stamps: per source file (website-relative),
 * the sha256 of the exact text the generator scanned and, per pointable site,
 * the offset right after its tag name, the end of its opening tag, its tag and
 * its site id. The Vite transform (scripts/lib/ui-auto-stamp.mjs) inserts
 * `data-ui-auto="<site>"` at each offset and refuses a file whose text no
 * longer hashes the same: it never re-derives an id or re-parses a file.
 */
export function autoStampManifest({ stamps, textOf, buildDigest }) {
  const files = {}
  for (const s of [...stamps].sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : a.tagEnd - b.tagEnd))) {
    const f = (files[s.rel] ??= { sha256: createHash('sha256').update(textOf(s.rel)).digest('hex'), stamps: [] })
    f.stamps.push({ offset: s.tagEnd, open_end: s.openEnd, tag: s.tag, site: s.siteId })
  }
  return { schema_version: 1, attr: AUTO_MARKER_ATTR, build_digest: buildDigest, files }
}

// ---------------------------------------------------------------- index assembly

function settingsRouteParts(route) {
  const path = route.split('?')[0]
  const segs = path.split('/').filter(Boolean)
  return { tab: segs[1], sub: segs[2] }
}

/**
 * Merge the per-area descriptor tables (`src/uiLocations/areas/<area>.ts`, as
 * aggregated by `UI_LOCATION_AREAS` in `descriptors.ts`) into one table.
 *
 * `areas` maps area name -> { id: descriptor }; `areaFiles` lists the stems of
 * the files actually in `areas/`. Refuses an id two areas declare (the
 * aggregator's object spread would silently keep the last), an aggregated area
 * with no file of that name, and a file the aggregator does not list (its
 * locations would never be indexed).
 */
export function mergeLocationAreas(areas, areaFiles) {
  const errors = []
  const descriptors = {}
  const owner = new Map()
  for (const [area, table] of Object.entries(areas ?? {})) {
    if (!areaFiles.includes(area)) errors.push(`UI_LOCATION_AREAS names area '${area}', but there is no src/uiLocations/areas/${area}.ts`)
    for (const [id, d] of Object.entries(table ?? {})) {
      if (owner.has(id)) {
        errors.push(`ui location '${id}' is declared in two areas ('${owner.get(id)}' and '${area}'); one id, one area`)
        continue
      }
      owner.set(id, area)
      descriptors[id] = d
    }
  }
  for (const f of areaFiles) {
    if (!Object.hasOwn(areas ?? {}, f)) errors.push(`src/uiLocations/areas/${f}.ts is not listed in UI_LOCATION_AREAS (descriptors.ts), so none of its locations would be indexed`)
  }
  return { descriptors, errors }
}

/**
 * Assemble the index. `inputs` is plain data; every error is collected and
 * returned rather than thrown so one run names every problem.
 */
export function buildUiIndex(inputs) {
  const errors = []
  const locations = new Map()
  const labelKeysUsed = new Set()
  const english = inputs.catalogs.en

  const keyExists = (key) => key.startsWith(LITERAL_PREFIX) || english[key] !== undefined
  const addLocation = (loc, origin) => {
    if (locations.has(loc.id)) {
      errors.push(`duplicate location id '${loc.id}' (${origin})`)
      return
    }
    if (!keyExists(loc.label_key)) errors.push(`location '${loc.id}': label key '${loc.label_key}' is not in the English catalog`)
    if (loc.label_key.startsWith(DESCRIPTION_PREFIX) !== (loc.label_kind === 'description')) {
      errors.push(`location '${loc.id}': a '${DESCRIPTION_PREFIX}' key is a find_ui description, only for label.from 'description'`)
    }
    const text = english[loc.label_key]
    if (typeof text === 'string' && /\{\{(?!productName\}\})/.test(text)) {
      errors.push(`location '${loc.id}': label '${loc.label_key}' interpolates a value and cannot be quoted as rendered text`)
    }
    locations.set(loc.id, loc)
  }
  const sourceKey = (s) => (s.key !== undefined ? s.key : `${LITERAL_PREFIX}${s.literal}`)

  // Pages ------------------------------------------------------------------
  const previewRequires = (flag, origin) => {
    if (!flag) return []
    if (!inputs.previewEnablers[flag]) {
      errors.push(`${origin}: preview flag '${flag}' has no enabling setting in PREVIEW_FLAG_ENABLERS`)
      return []
    }
    return [{ kind: 'preview_flag', flag, location: `setting:${inputs.previewEnablers[flag]}` }]
  }
  const navPaths = new Set(inputs.navPaths ?? [])
  const pinnableTabs = new Set()
  const surfaceIds = new Set()
  for (const s of inputs.surfaces) {
    if (s.pinnable) {
      const m = /^\/capabilities\?tab=([a-z-]+)$/.exec(s.route)
      if (!m) errors.push(`pinnable surface '${s.navId}' is not a Customize tab route`)
      else pinnableTabs.add(m[1])
      continue
    }
    if (s.navId === SHELL_SURFACE) errors.push(`surface '${s.navId}' collides with the reserved shell surface id`)
    surfaceIds.add(s.navId)
    const requires = [...previewRequires(s.previewFlag, `surface '${s.navId}'`)]
    if (s.appOnly) requires.push({ kind: 'condition', id: 'app_enabled' })
    addLocation({
      id: `page.${s.navId}`,
      kind: 'page',
      label_key: s.labelKey,
      placements: [{
        surface_id: s.navId,
        route: s.route,
        parent_ids: [],
        // An app-only page is a rail row while its app is on (the condition
        // above); a page hidden from the registry's rows is one only when the
        // shell draws its NavItem by hand (`navPaths`).
        entry_kind: s.appOnly || !s.hiddenFromNav || navPaths.has(s.route) ? 'rail' : 'direct-link',
        requires,
      }],
    }, 'surface')
  }
  const routesTaken = new Set(inputs.surfaces.map((s) => s.route))
  const routeTable = inputs.routes ?? []
  const legacy = inputs.legacyPages ?? {}
  const legacyAliases = []
  for (const p of inputs.extraPages) {
    if (routesTaken.has(p.route)) continue
    const key = inputs.extraTitleKeys[p.key]
    if (!key) {
      errors.push(`extra page '${p.key}' has no title key`)
      continue
    }
    const landed = matchRoute(routeTable, p.route)
    if (landed?.redirect) {
      // A route that only redirects is never an answer: its title searches the
      // location the redirect lands on, which keeps its own route and gates.
      if (!legacy[p.key]) {
        errors.push(`extra page '${p.key}' route '${p.route}' redirects to '${landed.redirect}'; name its canonical location in LEGACY_PAGE_CANONICAL`)
      } else legacyAliases.push({ page: p, key, canonical: legacy[p.key], target: landed.redirect })
      continue
    }
    if (legacy[p.key]) errors.push(`LEGACY_PAGE_CANONICAL names '${p.key}', whose route '${p.route}' does not redirect; remove the mapping`)
    surfaceIds.add(p.key)
    addLocation({
      id: `page.${p.key}`,
      kind: 'page',
      label_key: key,
      placements: [{
        surface_id: p.key, route: p.route, parent_ids: [], entry_kind: navPaths.has(p.route) ? 'rail' : 'direct-link',
        requires: previewRequires(p.previewFlag, `extra page '${p.key}'`),
      }],
    }, 'extra page')
  }
  for (const k of Object.keys(legacy)) {
    if (!inputs.extraPages.some((p) => p.key === k)) errors.push(`LEGACY_PAGE_CANONICAL names unknown extra page '${k}'`)
  }

  // Tabs -------------------------------------------------------------------
  for (const t of inputs.capabilityTabs) {
    addLocation({
      id: `tab.capabilities.${t.id}`,
      kind: 'tab',
      label_key: sourceKey(t),
      ...(pinnableTabs.has(t.id) ? { pinnable: true } : {}),
      placements: [{
        surface_id: 'capabilities', route: `/capabilities?tab=${t.id}`, parent_ids: ['page.capabilities'],
        entry_kind: 'tab', requires: [],
      }],
    }, 'Customize tab')
  }
  // Tabs of other SidePanelLayout pages (Developer). Always the explicit
  // `?tab=` route: the bare page route restores whichever tab was open last.
  for (const [surface, tabs] of Object.entries(inputs.pageTabs ?? {})) {
    const page = locations.get(`page.${surface}`)
    if (!page) {
      errors.push(`page tabs for unknown page '${surface}'`)
      continue
    }
    for (const t of tabs) {
      addLocation({
        id: `tab.${surface}.${t.id}`,
        kind: 'tab',
        label_key: sourceKey(t),
        placements: page.placements.map((p) => ({
          surface_id: p.surface_id, route: `${p.route}?tab=${t.id}`, parent_ids: [page.id],
          entry_kind: 'tab', requires: [...p.requires],
        })),
      }, `${surface} tab`)
    }
  }
  for (const id of pinnableTabs) {
    if (!inputs.capabilityTabs.some((t) => t.id === id)) errors.push(`pinnable surface names Customize tab '${id}', which the page does not render`)
  }
  for (const t of inputs.settingsTabs) {
    addLocation({
      id: `settings.tab.${t.id}`,
      kind: 'tab',
      label_key: sourceKey(t),
      placements: [{
        surface_id: 'settings', route: `/settings/${t.id}`, parent_ids: ['page.settings'], entry_kind: 'tab',
        requires: previewRequires(inputs.settingsTabPreview[t.id], `Settings tab '${t.id}'`),
      }],
    }, 'Settings tab')
  }
  for (const [tab, subs] of Object.entries(inputs.settingsSubs)) {
    if (!locations.has(`settings.tab.${tab}`)) errors.push(`Settings sub-pages for unknown tab '${tab}'`)
    for (const s of subs) {
      addLocation({
        id: `settings.sub.${tab}.${s.id}`,
        kind: 'tab',
        label_key: sourceKey(s),
        placements: [{
          surface_id: 'settings', route: `/settings/${tab}/${s.id}`,
          parent_ids: ['page.settings', `settings.tab.${tab}`], entry_kind: 'tab', requires: [],
        }],
      }, 'Settings sub-page')
    }
  }

  // Settings -----------------------------------------------------------------
  const agentById = new Map(inputs.agentSettings.map((a) => [a.id, a]))
  const settingIds = new Set()
  for (const e of inputs.settingsEntries) {
    const agent = agentById.get(e.id)
    if (!agent) {
      errors.push(`setting '${e.id}' is in settingsRegistry.gen.ts but not the agent registry; run npm run gen:settings`)
      continue
    }
    if (!e.labelKey) {
      errors.push(`setting '${e.id}' has no labelKey; its label cannot be localized`)
      continue
    }
    settingIds.add(e.id)
    const { tab, sub } = settingsRouteParts(agent.route)
    const tabId = `settings.tab.${tab}`
    const subId = sub ? `settings.sub.${tab}.${sub}` : null
    if (!locations.has(tabId)) errors.push(`setting '${e.id}': route tab '${tab}' is not a Settings tab`)
    if (subId && !locations.has(subId)) errors.push(`setting '${e.id}': route sub-page '${tab}/${sub}' is not a known Settings sub-page`)
    addLocation({
      id: `setting:${e.id}`,
      kind: 'setting',
      label_key: e.labelKey,
      setting_id: e.id,
      placements: [{
        surface_id: 'settings', route: agent.route,
        parent_ids: ['page.settings', tabId, ...(subId ? [subId] : [])], entry_kind: 'tab', requires: [],
      }],
    }, 'setting')
  }
  for (const a of inputs.agentSettings) {
    if (!settingIds.has(a.id) && !inputs.settingsEntries.some((e) => e.id === a.id)) {
      errors.push(`setting '${a.id}' is in the agent registry but not settingsRegistry.gen.ts; run npm run gen:settings`)
    }
  }
  for (const [flag, sid] of Object.entries(inputs.previewEnablers)) {
    if (!settingIds.has(sid)) errors.push(`PREVIEW_FLAG_ENABLERS['${flag}'] names unknown setting '${sid}'`)
  }

  // Legacy page titles become aliases of where their redirect lands --------------
  const pathOf = (url) => url.split('?')[0].split('#')[0].replace(/\/+$/, '') || '/'
  for (const a of legacyAliases) {
    const loc = locations.get(a.canonical)
    const where = `LEGACY_PAGE_CANONICAL['${a.page.key}']`
    if (!loc) {
      errors.push(`${where} names unknown location '${a.canonical}'`)
      continue
    }
    const route = loc.placements[0]?.route ?? ''
    const [, targetQuery] = a.target.split('?')
    const [, routeQuery] = route.split('?')
    if (pathOf(route) !== pathOf(a.target) || (targetQuery && targetQuery !== routeQuery)) {
      errors.push(`${where}: '${a.page.route}' redirects to '${a.target}', but '${a.canonical}' is at '${route}'`)
      continue
    }
    loc.alias_keys = [...new Set([...(loc.alias_keys ?? []), a.key])]
  }

  // Every route a location hands out must be one the router renders ------------
  if (!Array.isArray(inputs.routes) || inputs.routes.length === 0) errors.push('no route table: pass the routes read from App.tsx')
  const appRoutes = new Set(inputs.surfaces.filter((s) => s.appOnly).map((s) => pathOf(s.route)))
  const checkRoute = (url, where) => {
    if (typeof url !== 'string' || !url.startsWith('/')) {
      errors.push(`${where}: route '${url ?? ''}' is empty or not an absolute path`)
      return
    }
    const hit = matchRoute(routeTable, url)
    if (hit?.redirect) errors.push(`${where}: route '${url}' only redirects (to '${hit.redirect}'); name the location it lands on`)
    // Built-in apps are served by the `/:builtinApp/*` arm, keyed by their surface route.
    else if (!hit && !appRoutes.has(`/${pathOf(url).split('/').filter(Boolean)[0] ?? ''}`)) {
      errors.push(`${where}: route '${url}' is not in the dashboard route table (App.tsx)`)
    }
  }

  // Registered controls --------------------------------------------------------
  const sitesById = new Map()
  for (const site of inputs.markerSites) {
    if (!inputs.descriptors[site.id]) {
      errors.push(`${site.rel}:${site.line}: unknown ui location '${site.id}' — add it to its area file in src/uiLocations/areas/`)
      continue
    }
    if (sitesById.has(site.id)) {
      const first = sitesById.get(site.id)
      errors.push(`ui location '${site.id}' is marked twice (${first.rel}:${first.line}, ${site.rel}:${site.line}); one id names one render site`)
      continue
    }
    sitesById.set(site.id, site)
  }
  const registered = []
  for (const [id, d] of Object.entries(inputs.descriptors)) {
    if (RESERVED_PREFIXES.some((p) => id.startsWith(p)) || !LOCATION_ID_RE.test(id)) {
      errors.push(`ui location id '${id}' must be lowercase dotted/hyphenated and not start with ${RESERVED_PREFIXES.join(', ')}`)
      continue
    }
    if (!KINDS.has(d.kind)) errors.push(`ui location '${id}': unknown kind '${d.kind}'`)
    const site = sitesById.get(id)
    if (!site) {
      errors.push(`ui location '${id}' is described but no render site carries {...uiLocation('${id}')}`)
      continue
    }
    const label = site.resolved
    if (label.error) {
      errors.push(label.error)
      continue
    }
    if (!Array.isArray(d.placements) || d.placements.length === 0) {
      errors.push(`ui location '${id}' has no placements`)
      continue
    }
    for (const k of d.aliasKeys ?? []) {
      if (!keyExists(k)) errors.push(`ui location '${id}': alias key '${k}' is not in the English catalog`)
      if (k.startsWith(DESCRIPTION_PREFIX)) errors.push(`ui location '${id}': alias key '${k}' is a find_ui description, not on-screen text`)
    }
    if (d.cautionKey !== undefined) {
      if (typeof d.cautionKey !== 'string' || !keyExists(d.cautionKey)) errors.push(`ui location '${id}': caution key '${d.cautionKey}' is not in the English catalog`)
      else if (!(inputs.guideCaution ?? GUIDE_CAUTION_IDS).includes(id)) errors.push(`ui location '${id}': cautionKey is only for a location in GUIDE_CAUTION_IDS`)
    }
    if (label.description) {
      // Authored for find_ui alone, so every shipped locale must carry it.
      const key = label.source.key
      const absent = inputs.locales.filter((l) => typeof inputs.catalogs[l]?.[key] !== 'string' || !inputs.catalogs[l][key].trim())
      if (absent.length) errors.push(`ui location '${id}': description '${key}' is missing in ${absent.join(', ')}`)
    }
    registered.push({ id, d, labelKey: sourceKey(label.source), site, excluded: label.excluded, description: !!label.description })
  }
  for (const r of registered) {
    addLocation({
      id: r.id,
      kind: r.d.kind,
      label_key: r.labelKey,
      // A description says what the control is; its on-screen label is runtime data.
      ...(r.description ? { label_kind: 'description' } : {}),
      ...(r.d.aliasKeys?.length ? { alias_keys: [...r.d.aliasKeys] } : {}),
      // The page's own words for what a destructive control removes and keeps.
      ...(typeof r.d.cautionKey === 'string' ? { caution_key: r.d.cautionKey } : {}),
      // Resolved below, once every registered id exists to be a parent.
      placements: r.d.placements.map((p) => ({ ...p })),
      source: { file: r.site.rel, line: r.site.line },
    }, 'descriptor')
  }

  // Placements for registered controls, resolved whole and in dependency
  // order: a parent's placements are final before a child picks one, whatever
  // order the descriptors were declared in. The child inherits the chosen
  // parent placement's path, route and requirements.
  const registeredIds = new Set(registered.map((r) => r.id))
  // A description location has no label to quote, so it can never be a step in
  // someone else's path: nothing hangs under it and nothing is shown_by it.
  const describedIds = new Set(registered.filter((r) => r.description).map((r) => r.id))
  for (const r of registered) {
    r.d.placements.forEach((p, i) => {
      const where = `ui location '${r.id}' placement ${i}`
      if (describedIds.has(p.parent)) errors.push(`${where}: parent '${p.parent}' has only a description, no label to put in a path`)
      for (const q of p.requires ?? []) {
        if (q.kind === 'shown_by' && describedIds.has(q.location)) errors.push(`${where}: shown_by '${q.location}' has only a description, no label to name the step`)
      }
    })
  }
  const finalPlacements = new Map()
  const viewportOf = (reqs) => reqs.find((q) => q.kind === 'viewport')?.value
  const sameShape = (a, b) => JSON.stringify([a.route, a.parent_ids, a.requires]) === JSON.stringify([b.route, b.parent_ids, b.requires])
  const ownRequires = (r, p, where) => {
    const requires = []
    for (const req of p.requires ?? []) {
      if (!REQUIREMENT_KINDS.has(req.kind)) {
        errors.push(`${where}: unknown requirement kind '${req.kind}'`)
        continue
      }
      if (req.kind === 'shown_by') {
        if (!locations.has(req.location)) errors.push(`${where}: shown_by names unknown location '${req.location}'`)
        if (req.location === r.id) errors.push(`${where}: a location cannot be shown by itself`)
        if (req.when !== undefined && !Object.hasOwn(inputs.revealStates ?? {}, req.when)) {
          errors.push(`${where}: unknown reveal state '${req.when}' (add it to src/uiLocations/conditions.ts)`)
        }
        requires.push({ kind: 'shown_by', location: req.location, ...(req.when !== undefined ? { when: req.when } : {}) })
      } else if (req.kind === 'viewport') {
        if (req.value !== 'desktop' && req.value !== 'mobile') errors.push(`${where}: viewport must be desktop or mobile`)
        requires.push({ kind: 'viewport', value: req.value })
      } else if (req.kind === 'condition') {
        if (!Object.hasOwn(inputs.conditions ?? {}, req.id)) errors.push(`${where}: unknown condition '${req.id}' (add it to src/uiLocations/conditions.ts)`)
        requires.push({ kind: 'condition', id: req.id })
      } else {
        requires.push(...previewRequires(req.flag, where))
      }
    }
    return requires
  }
  // The parent placement a child hangs under: same surface, no conflicting
  // viewport, and one shape (or the one named by parentPlacement).
  const pickParent = (p, own, parentPlacements, where) => {
    const v = viewportOf(own)
    const compatible = parentPlacements
      .map((q, qi) => ({ q, qi }))
      .filter(({ q }) => q.surface_id === p.surface && (v === undefined || !q.requires.some((x) => x.kind === 'viewport' && x.value !== v)))
    if (p.parentPlacement !== undefined) {
      const hit = compatible.find(({ qi }) => qi === p.parentPlacement)?.q ?? null
      if (!hit) errors.push(`${where}: parentPlacement ${p.parentPlacement} is not a placement of '${p.parent}' on surface '${p.surface}' compatible with this one`)
      return hit
    }
    if (compatible.length === 0) {
      errors.push(`${where}: parent '${p.parent}' has no placement on surface '${p.surface}' compatible with this one`)
      return null
    }
    if (compatible.some(({ q }) => !sameShape(q, compatible[0].q))) {
      errors.push(`${where}: parent '${p.parent}' has ${compatible.length} different compatible placements; add a viewport requirement or name parentPlacement`)
      return null
    }
    return compatible[0].q
  }
  // The parent's requirements, then the child's own, each once.
  const mergeRequires = (inherited, own) => {
    const requires = []
    const seen = new Set()
    for (const q of [...inherited, ...own]) {
      const k = JSON.stringify(q)
      if (!seen.has(k)) {
        seen.add(k)
        requires.push(q)
      }
    }
    return requires
  }
  const resolvePlacements = (id, stack) => {
    if (!registeredIds.has(id)) return locations.get(id)?.placements ?? null
    if (finalPlacements.has(id)) return finalPlacements.get(id)
    if (stack.includes(id)) {
      errors.push(`ui location parent cycle: ${[...stack, id].join(' -> ')}`)
      return null
    }
    const r = registered.find((x) => x.id === id)
    const out = []
    r.d.placements.forEach((p, i) => {
      const where = `ui location '${id}' placement ${i}`
      if (p.surface !== SHELL_SURFACE && !surfaceIds.has(p.surface)) errors.push(`${where}: unknown surface '${p.surface}'`)
      if (!ENTRY_KINDS.has(p.entry)) errors.push(`${where}: unknown entry kind '${p.entry}'`)
      const own = ownRequires(r, p, where)
      if (p.surface === SHELL_SURFACE) {
        // Shell chrome is on every page: no route. It may hang under another
        // shell control (a row in the phone menu, under the menu button), whose
        // placement it inherits like a page child does; never under a page.
        if (p.route !== undefined) {
          errors.push(`${where}: a '${SHELL_SURFACE}' placement takes no route (it is drawn on every page)`)
          return
        }
        if (p.parent === undefined) {
          if (p.parentPlacement !== undefined) errors.push(`${where}: parentPlacement needs a parent`)
          else out.push({ surface_id: SHELL_SURFACE, route: '', parent_ids: [], entry_kind: p.entry, requires: own })
          return
        }
        const parentPlacements = registeredIds.has(p.parent) ? resolvePlacements(p.parent, [...stack, id]) : null
        if (!parentPlacements || parentPlacements.length === 0 || parentPlacements.some((q) => q.surface_id !== SHELL_SURFACE)) {
          errors.push(`${where}: a '${SHELL_SURFACE}' placement can only hang under another registered shell location, not '${p.parent}'`)
          return
        }
        const chosen = pickParent(p, own, parentPlacements, where)
        if (!chosen) return
        out.push({
          surface_id: SHELL_SURFACE, route: '', parent_ids: [...chosen.parent_ids, p.parent], entry_kind: p.entry,
          requires: mergeRequires(chosen.requires, own),
        })
        return
      }
      if (!locations.has(p.parent)) {
        errors.push(`${where}: unknown parent '${p.parent}'`)
        return
      }
      const parentPlacements = resolvePlacements(p.parent, [...stack, id])
      if (!parentPlacements) return
      const chosen = pickParent(p, own, parentPlacements, where)
      if (!chosen) return
      const route = p.route ?? chosen.route
      checkRoute(route, where)
      out.push({
        surface_id: p.surface,
        route: route ?? '',
        parent_ids: [...chosen.parent_ids, p.parent],
        entry_kind: p.entry,
        requires: mergeRequires(chosen.requires, own),
      })
    })
    finalPlacements.set(id, out)
    return out
  }
  for (const r of registered) {
    const loc = locations.get(r.id)
    if (!loc) continue
    loc.placements = resolvePlacements(r.id, []) ?? []
    if (loc.placements.length === 0) errors.push(`ui location '${r.id}' has no placement that resolves`)
    if (r.d.guide) {
      const g = r.d.guide
      const params = g.params ?? {}
      const verdict = inputs.resolveGuide ? inputs.resolveGuide(g.action, params) : { ok: false, reason: 'no guide catalog was given' }
      if (!verdict.ok) errors.push(`ui location '${r.id}': guide binding ${g.action} is refused by the guide catalog (${verdict.reason})`)
      else loc.guide_ref = { action_id: g.action, ...(g.params ? { params: g.params } : {}) }
    }
  }
  // The generic `ui.show` plan, once every placement is final (see guidePlanFor),
  // over the compiled reveal graph (see compileRevealScopes).
  const { scopes: revealScopes, scopeForState, scopeForParent } = compileRevealScopes({ registered, registeredIds, finalPlacements, inputs, errors })
  const predicates = new Set(inputs.runtimePredicates ?? [])
  for (const id of predicates) {
    if (!Object.hasOwn(inputs.conditions ?? {}, id)) errors.push(`runtime predicate '${id}' is not a condition in src/uiLocations/conditions.ts`)
  }
  const { selections, gates } = compileGuideNeeds({ inputs, registeredIds, predicates, errors })
  const guideCtx = { locations, registeredIds, describedIds, scopeForState, scopeForParent, predicates, selections, gates, deny: new Set(inputs.guideDeny ?? GUIDE_DENY_IDS), caution: new Set(inputs.guideCaution ?? GUIDE_CAUTION_IDS) }
  for (const r of registered) {
    const loc = locations.get(r.id)
    if (!loc || loc.placements.length === 0) continue
    const verdict = guidePlanFor(r, loc, guideCtx)
    if (verdict.plan) loc.guide_plan = verdict.plan
    // A curated location is pointable (with a caution when it is destructive)
    // unless it is denied or opts out; a plan may still be missing for a
    // prerequisite the guide cannot walk.
    loc.guide_policy = verdict.reason === 'denied' ? 'deny'
      : verdict.reason === 'opted_out' ? 'search-only'
        : guideCtx.caution.has(r.id) ? 'caution' : 'point'
  }
  // A shell placement is searchable by its own surface id, like a page's.
  if ([...finalPlacements.values()].some((ps) => ps.some((p) => p.surface_id === SHELL_SURFACE))) surfaceIds.add(SHELL_SURFACE)
  // Generated placements were built before the route table check existed.
  for (const loc of locations.values()) {
    if (registeredIds.has(loc.id)) continue
    loc.placements.forEach((p, i) => {
      checkRoute(p.route, `location '${loc.id}' placement ${i}`)
      for (const q of p.requires) {
        if (q.kind === 'condition' && !Object.hasOwn(inputs.conditions ?? {}, q.id)) {
          errors.push(`location '${loc.id}': unknown condition '${q.id}' (add it to src/uiLocations/conditions.ts)`)
        }
      }
    })
  }

  // Search terms ---------------------------------------------------------------
  // Registered locations carry them in their descriptor, generated ones in the
  // SEARCH_TERMS overlay keyed by stable id; never both, and never a guess.
  const termSources = []
  for (const r of registered) if (r.d.terms !== undefined) termSources.push([r.id, r.d.terms, 'descriptor'])
  for (const [id, terms] of Object.entries(inputs.searchTerms ?? {})) {
    if (!locations.has(id)) {
      errors.push(`SEARCH_TERMS names unknown location '${id}'`)
      continue
    }
    if (inputs.descriptors[id]) {
      errors.push(`SEARCH_TERMS names registered location '${id}'; put its terms in its descriptor`)
      continue
    }
    termSources.push([id, terms, 'SEARCH_TERMS'])
  }
  for (const [id, terms, origin] of termSources) {
    const loc = locations.get(id)
    if (!loc) continue
    const checked = validateTerms(id, terms, origin, loc, inputs, errors)
    if (checked) loc.terms = checked
  }

  // Auto tier ------------------------------------------------------------------
  // Unregistered controls whose one static label key and one page are proven
  // from source (see buildPageMap). Never a guess: no search terms, no
  // prerequisites of their own (`conditions_unknown`), and a label key that a
  // generated or registered location already shows is left to that location.
  const auto = buildAutoLocations(inputs, locations, errors, addLocation)

  // Labels -------------------------------------------------------------------
  for (const loc of locations.values()) {
    labelKeysUsed.add(loc.label_key)
    for (const k of loc.alias_keys ?? []) labelKeysUsed.add(k)
    if (loc.caution_key) labelKeysUsed.add(loc.caution_key)
  }
  const labels = {}
  const missing = {}
  for (const locale of inputs.locales) {
    const cat = inputs.catalogs[locale] ?? {}
    const out = {}
    for (const key of [...labelKeysUsed].sort()) {
      if (key.startsWith(LITERAL_PREFIX)) {
        if (locale === 'en') out[key] = key.slice(LITERAL_PREFIX.length)
        continue
      }
      const v = cat[key]
      if (typeof v === 'string' && v.trim()) out[key] = v.replaceAll('{{productName}}', inputs.productName)
      else if (locale !== 'en') (missing[locale] ??= []).push(key)
    }
    labels[locale] = out
  }

  const sorted = [...locations.values()].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  const counts = {}
  // The auto tier is only counted when it was asked for: the committed index is
  // built without it (it is a build-time artifact, see buildAutoArtifact), so
  // its coverage block never moves when an unregistered control is added.
  const withAuto = Array.isArray(inputs.autoCandidates)
  const tiers = withAuto ? { generated: 0, curated: 0, auto: 0 } : { generated: 0, curated: 0 }
  for (const l of sorted) {
    if (!l.tier) l.tier = registeredIds.has(l.id) ? 'curated' : 'generated'
    tiers[l.tier]++
    if (l.tier !== 'auto') counts[l.kind] = (counts[l.kind] ?? 0) + 1
  }
  const index = {
    $comment: 'AUTO-GENERATED by website/scripts/gen-ui-index.mjs from the dashboard source — DO NOT EDIT. '
      + 'Regenerate with `npm run gen:ui`; read by the find_ui tool (kiro_crew/mcp_guide.py).',
    schema_version: SCHEMA_VERSION,
    input_digest: inputs.inputDigest,
    // Filled below from what the browser bundle and the gateway must agree on.
    build_digest: '',
    coverage: {
      scope: `pages, page tabs, Settings tabs and sub-pages, every Settings control, registered controls${withAuto ? `, and ${AUTO_SCOPE}` : ''}; `
        + 'not every dashboard control',
      counts,
      tiers,
      registered_controls: registered.length,
      ...(withAuto ? { auto_controls: tiers.auto } : {}),
    },
    locales: [...inputs.locales],
    surfaces: [...surfaceIds].sort(),
    // What each prerequisite id means, from src/uiLocations/conditions.ts.
    conditions: { ...(inputs.conditions ?? {}) },
    reveal_states: { ...(inputs.revealStates ?? {}) },
    reveal_scopes: Object.fromEntries(Object.entries(revealScopes).sort(([a], [b]) => (a < b ? -1 : 1))),
    // Conditions a guide step may carry as a live predicate (a browser evaluator exists).
    runtime_predicates: [...predicates].sort(),
    // Selection scopes (a `select` step points at the picker until the page
    // reports the fact) and gates (a `gate` step pauses on a blocker naming
    // the setting that turns it on). Both are also live predicates.
    guide_selections: Object.fromEntries([...selections].sort(([a], [b]) => (a < b ? -1 : 1))),
    guide_gates: Object.fromEntries([...gates].sort(([a], [b]) => (a < b ? -1 : 1))),
    locations: sorted.map(({ source, ...rest }) => rest),
    labels,
  }
  index.build_digest = guideBuildDigest(index)
  return { index, errors, missing, registered, auto }
}

/**
 * The ONE digest the browser bundle (`guidePlans.gen.ts` `GUIDE_BUILD_DIGEST`)
 * and the packaged index (`build_digest`) both carry: over exactly what a live
 * guide relies on the two halves agreeing about -- the curated location ids a
 * tab may be asked to observe, the reveal scopes, and every `ui.show` plan. A
 * gateway stores it on each `ui.show` record it accepts, and a tab whose
 * bundle carries another refuses the guide (`build_mismatch`) instead of
 * pointing at what may no longer be there.
 */
export function guideBuildDigest(index) {
  const plans = {}
  for (const l of index.locations) if (l.guide_plan) plans[l.id] = l.guide_plan
  const payload = JSON.stringify({
    schema: index.schema_version,
    observable: guideObservableIds(index),
    scopes: index.reveal_scopes ?? {},
    selections: index.guide_selections ?? {},
    gates: index.guide_gates ?? {},
    plans,
  })
  return `sha256:${createHash('sha256').update(payload).digest('hex')}`
}

/** Curated location ids, sorted: the only ids a live observation may name. */
export function guideObservableIds(index) {
  return index.locations.filter((l) => l.tier === 'curated').map((l) => l.id).sort()
}

/**
 * Auto locations from `inputs.autoCandidates` (`{ page, tab, key, kind, rel }`,
 * already mapped to one page by the caller). One location per (page or tab,
 * label key); `skipped` counts why a candidate was left out.
 */
/** Stands in for the shell among a shared control's parents (it has no location). */
const SHELL_PLACEMENT_PARENT = Object.freeze({ tier: 'shell', placements: [] })

function buildAutoLocations(inputs, locations, errors, addLocation) {
  const shown = new Set()
  for (const loc of locations.values()) {
    shown.add(loc.label_key)
    for (const k of loc.alias_keys ?? []) shown.add(k)
  }
  const skipped = { shown_by_curated: 0, untranslated: 0, interpolated: 0, not_in_catalog: 0 }
  let covered = 0
  // One location per (page or tab, English label): seven channel panels that
  // each say "Setup guide" are one answer, not seven identical paths. Each
  // location keeps every render site it groups, with that site's policy.
  const groups = new Map()
  const english = inputs.catalogs.en
  const deny = new Set(inputs.guideDeny ?? GUIDE_DENY_IDS)
  const caution = new Set(inputs.guideCaution ?? GUIDE_CAUTION_IDS)
  for (const c of inputs.autoCandidates ?? []) {
    const shell = c.page === SHELL_SURFACE
    const parentId = autoParentId(c)
    // A shared control hangs under each page that draws it, in that order.
    // (the shell, when it draws it too, last: on every page, no route).
    const parentIds = shell ? [] : parentId === SHARED_PARENT ? c.pages : [parentId]
    const parents = parentIds.map((id) => (id === SHELL_SURFACE ? SHELL_PLACEMENT_PARENT : locations.get(id)))
    const unknown = parentIds.find((id, i) => !parents[i] || parents[i].tier === 'auto')
    if (unknown !== undefined || (parentId === SHARED_PARENT && parentIds.length < 2)) {
      errors.push(`auto candidate at ${c.rel}: maps to unknown location '${unknown ?? parentId}'`)
      continue
    }
    const placementsUnder = () => parents.flatMap((parent, i) => (parent === SHELL_PLACEMENT_PARENT
      ? [{ surface_id: SHELL_SURFACE, route: '', parent_ids: [], entry_kind: 'content', requires: [] }]
      : parent.placements.map((p) => ({
        surface_id: p.surface_id, route: p.route, parent_ids: [...p.parent_ids, parentIds[i]],
        entry_kind: 'content', requires: p.requires.map((q) => ({ ...q })),
      }))))
    if (typeof english[c.key] !== 'string' || c.key.startsWith(DESCRIPTION_PREFIX)) { skipped.not_in_catalog++; continue }
    if (shown.has(c.key)) { skipped.shown_by_curated++; continue }
    if (/\{\{(?!productName\}\})/.test(english[c.key])) { skipped.interpolated++; continue }
    if (inputs.locales.some((l) => typeof inputs.catalogs[l]?.[c.key] !== 'string' || !inputs.catalogs[l][c.key].trim())) {
      skipped.untranslated++
      continue
    }
    covered++
    const siteId = typeof c.siteId === 'string' ? c.siteId : null
    const site = siteId
      ? { siteId, c, ...autoSitePolicy(c, { siteId, deny, caution, english: english[c.key] }) }
      : { siteId: null, c, policy: 'search-only', reason: 'no_site_id' }
    const seenKey = `${parentId}\0${english[c.key]}`
    const group = groups.get(seenKey)
    if (group) {
      group.sites.push(site)
      // Two shared files with one label: the location lists every page either draws on.
      if (parentId === SHARED_PARENT) {
        const loc = locations.get(group.id)
        for (const p of placementsUnder()) {
          if (!loc.placements.some((q) => JSON.stringify(q) === JSON.stringify(p))) loc.placements.push(p)
        }
      }
      continue
    }
    const id = `auto:${parentId}:${c.key}`
    if (locations.has(id)) continue
    addLocation({
      id,
      kind: KINDS.has(c.kind) ? c.kind : 'button',
      tier: 'auto',
      conditions_unknown: true,
      label_key: c.key,
      // The parent's own proven gates (a preview flag, an enabled app) still
      // apply; the control's own are unknown, which conditions_unknown says.
      // A control only the app shell draws (the rail, the top bar) is on every
      // page: no route and no parent, like a registered shell control.
      placements: shell
        ? [{ surface_id: SHELL_SURFACE, route: '', parent_ids: [], entry_kind: 'content', requires: [] }]
        : placementsUnder(),
    }, 'auto')
    groups.set(seenKey, { id, sites: [site] })
  }
  // Each location's policy. Deny wins (a deny-listed location, or any site of
  // it denied); a guidable verdict (`point`, or `caution` for a destructive
  // site or a caution-listed location) takes exactly ONE site, itself
  // guidable, on placements with no prerequisite (a single-step plan cannot
  // walk a gate): one placement, or one per page for a shared control, whose
  // page decides at guide time which instance is the target (the only one
  // visible there). Everything else is search-only. Only a guidable location
  // gets a plan.
  const policy = { point: 0, caution: 0, 'search-only': 0, deny: 0 }
  const reasons = {}
  const stamps = []
  const destructive = []
  for (const { id, sites } of groups.values()) {
    const loc = locations.get(id)
    for (const s of sites) if (s.reason === 'sensitive_lint') destructive.push({ id, site: s.siteId, rel: s.c.rel, line: s.c.line, label: english[loc.label_key] })
    const shared = id.startsWith(`auto:${SHARED_PARENT}:`)
    let verdict
    let reason
    if (deny.has(id) || sites.some((s) => s.policy === 'deny')) {
      verdict = 'deny'
      reason = deny.has(id) ? 'denied' : sites.find((s) => s.policy === 'deny').reason
    } else if (sites.length !== 1) {
      verdict = 'search-only'
      reason = 'several_sites'
    } else if (!GUIDABLE_POLICIES.includes(sites[0].policy)) {
      verdict = 'search-only'
      reason = sites[0].reason
    } else if ((loc.placements.length !== 1 && !shared) || loc.placements.some((p) => p.requires.length !== 0)) {
      verdict = 'search-only'
      reason = 'placement_prerequisite'
    } else if (sites[0].policy === 'caution' || caution.has(id)) {
      verdict = 'caution'
      reason = sites[0].policy === 'caution' ? sites[0].reason : 'caution_listed'
    } else {
      verdict = 'point'
      reason = shared ? 'shared_unique_at_runtime' : 'audited_primitive'
    }
    loc.guide_policy = verdict
    policy[verdict]++
    reasons[reason] = (reasons[reason] ?? 0) + 1
    if (GUIDABLE_POLICIES.includes(verdict)) {
      loc.guide_plan = autoGuidePlan(loc, sites[0].siteId, { caution: verdict === 'caution' })
      const c = sites[0].c
      stamps.push({ rel: c.rel, tagEnd: c.tagEnd, openEnd: c.openEnd, tag: c.tag, siteId: sites[0].siteId })
    }
  }
  return {
    covered, skipped, entries: [...locations.values()].filter((l) => l.tier === 'auto').length,
    policy, reasons, stamps, destructive,
  }
}

const MAX_TERMS_PER_LOCALE = 16
const MAX_TERM_CHARS = 60

/** Approximates the search's normalization (NFKC, casefold, punctuation as space). */
function foldTerm(text) {
  return text.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{M}\p{N}]+/gu, ' ').trim()
}

/**
 * One location's search terms, validated and in index locale order, or null.
 * Every problem is an error: an unknown locale, a non-list, a blank, overlong
 * or repeated term, and a term that only repeats a label the index already
 * searches in that locale.
 */
function validateTerms(id, terms, origin, loc, inputs, errors) {
  const where = `${origin} '${id}'`
  if (!terms || typeof terms !== 'object' || Array.isArray(terms)) {
    errors.push(`${where}: terms must be an object of locale -> list`)
    return null
  }
  const out = {}
  for (const locale of Object.keys(terms)) {
    if (!inputs.locales.includes(locale)) errors.push(`${where}: unknown locale '${locale}' (shipped: ${inputs.locales.join(', ')})`)
  }
  for (const locale of inputs.locales) {
    const list = terms[locale]
    if (list === undefined) continue
    if (!Array.isArray(list) || list.length === 0) {
      errors.push(`${where}: terms.${locale} must be a nonempty list`)
      continue
    }
    if (list.length > MAX_TERMS_PER_LOCALE) errors.push(`${where}: terms.${locale} has ${list.length} terms; at most ${MAX_TERMS_PER_LOCALE}`)
    const cat = inputs.catalogs[locale] ?? {}
    const labelForms = new Set(
      [loc.label_key, ...(loc.alias_keys ?? [])]
        .map((k) => (k.startsWith(LITERAL_PREFIX) ? k.slice(LITERAL_PREFIX.length) : cat[k]))
        .filter((v) => typeof v === 'string')
        .map((v) => foldTerm(v.replaceAll('{{productName}}', inputs.productName))),
    )
    const seen = new Set()
    const kept = []
    for (const term of list) {
      if (typeof term !== 'string' || !foldTerm(term)) {
        errors.push(`${where}: terms.${locale} has a blank or non-string term`)
        continue
      }
      if (term.trim().length > MAX_TERM_CHARS) errors.push(`${where}: term '${term}' is longer than ${MAX_TERM_CHARS} characters`)
      const folded = foldTerm(term)
      if (seen.has(folded)) errors.push(`${where}: term '${term}' is listed twice for ${locale}`)
      else if (labelForms.has(folded)) errors.push(`${where}: term '${term}' only repeats the ${locale} label, which is searched already`)
      seen.add(folded)
      kept.push(term.trim())
    }
    out[locale] = kept
  }
  return Object.keys(out).length ? out : null
}

/**
 * The build-time auto tier, split out of an index built WITH auto candidates:
 * only its `tier: auto` locations and the labels they use. It names the
 * committed index it hangs off (`base_input_digest`): every auto placement's
 * path ends at a page or tab of that index, so find_ui refuses the artifact
 * (auto tier unavailable, committed index untouched) when it was built against
 * a different one. `coverage` is extra build-time reporting (core control
 * coverage), carried along so the shipped file says what it was built from.
 */
export function buildAutoArtifact(full, { baseInputDigest, baseBuildDigest = '', inputDigest, coverage = {} }) {
  const locations = full.locations.filter((l) => l.tier === 'auto')
  const keys = new Set(locations.map((l) => l.label_key))
  const labels = {}
  for (const [locale, table] of Object.entries(full.labels)) {
    labels[locale] = Object.fromEntries(Object.entries(table).filter(([k]) => keys.has(k)))
  }
  const policies = { point: 0, caution: 0, 'search-only': 0, deny: 0 }
  for (const l of locations) policies[l.guide_policy ?? 'search-only']++
  return {
    $comment: 'AUTO-GENERATED at build time by website/scripts/gen-ui-index.mjs --auto-out — DO NOT EDIT, never committed. '
      + 'The find_ui auto tier; read beside docs/ui-index.generated.json by kiro_crew/ui_index.py.',
    schema_version: SCHEMA_VERSION,
    artifact: 'auto',
    base_input_digest: baseInputDigest,
    input_digest: inputDigest,
    // The committed index's guide digest this tier's plans hang off, and the
    // tier's own (see autoBuildDigest): an auto `ui.show` guide carries the latter.
    base_build_digest: baseBuildDigest,
    build_digest: autoBuildDigest(baseBuildDigest, locations),
    coverage: {
      scope: AUTO_SCOPE,
      auto_controls: locations.length,
      auto_point: policies.point,
      auto_caution: policies.caution,
      auto_search_only: policies['search-only'],
      auto_denied: policies.deny,
      ...coverage,
    },
    locales: [...full.locales],
    locations,
    labels,
  }
}

/**
 * Stable bytes for the committed file and the `--check` comparison: one
 * location per line and one label per line, so a change reads as a small diff
 * without paying indentation on every nested field.
 */
/**
 * The browser's copy of every `ui.show` plan, as a TypeScript module
 * (`src/uiLocations/guidePlans.gen.ts`): the same plans the committed index
 * carries, keyed by location id, so the page and the gateway read one source.
 */
/**
 * Conditions that hold only while there is something for a control to act on
 * (a notification to clear or mark read). A control drawn only under one of
 * them is absent, not hidden, when there is nothing: the guide says so instead
 * of asking the person to open a menu it is not in.
 */
export const ONLY_WITH_ITEMS_CONDITIONS = Object.freeze(['has_notifications', 'has_unread_notifications'])

/** Registered locations every placement of which needs an ONLY_WITH_ITEMS_CONDITIONS condition. */
function guideOnlyWithItems(index) {
  const only = new Set(ONLY_WITH_ITEMS_CONDITIONS)
  return index.locations
    .filter((l) => l.tier !== 'auto' && l.placements?.length
      && l.placements.every((p) => (p.requires ?? []).some((q) => q.kind === 'condition' && only.has(q.id))))
    .map((l) => l.id)
    .sort()
}

function guideCautionLocations(index) {
  const ids = new Set()
  for (const l of index.locations) {
    if (l.guide_policy === 'caution') ids.add(l.id)
    for (const p of l.guide_plan?.placements ?? []) for (const s of p.steps) if (s.caution && s.location) ids.add(s.location)
  }
  return [...ids].sort()
}

export function serializeGuidePlans(index) {
  const plans = {}
  for (const l of index.locations) if (l.guide_plan) plans[l.id] = l.guide_plan
  return '// AUTO-GENERATED by website/scripts/gen-ui-index.mjs from the dashboard source — DO NOT EDIT.\n'
    + '// Regenerate with `npm run gen:ui`; the same plans as `guide_plan` in src/kiro_crew/docs/ui-index.generated.json.\n'
    + "import type { UiGuidePlan } from './types'\n\n"
    + '/** The `build_digest` of the packaged index this bundle was built with (see `guideBuildDigest`). */\n'
    + `export const GUIDE_BUILD_DIGEST = ${JSON.stringify(index.build_digest)}\n\n`
    + '/** Curated location ids: the only ids a live observation may name. */\n'
    + `export const GUIDE_OBSERVABLE_IDS: readonly string[] = ${JSON.stringify(guideObservableIds(index), null, 2)}\n\n`
    + '/** Every compiled reveal scope: id -> what kind of container it is and the control that opens it. */\n'
    + `export const GUIDE_REVEAL_SCOPES = ${JSON.stringify(index.reveal_scopes ?? {}, null, 2)} as const\n\n`
    + '/** A reveal scope id of this build: the only ids a `<GuideRevealScope>` owner may report. */\n'
    + 'export type GuideRevealScopeId = keyof typeof GUIDE_REVEAL_SCOPES\n\n'
    + '/** Locations whose guide policy is `caution` (a destructive control), and the sites their plans point at. */\n'
    + `export const GUIDE_CAUTION_LOCATIONS: readonly string[] = ${JSON.stringify(guideCautionLocations(index), null, 2)}\n\n`
    + '/** Locations drawn only while there is something for them to act on (`ONLY_WITH_ITEMS_CONDITIONS`): missing means nothing to act on yet. */\n'
    + `export const GUIDE_ONLY_WITH_ITEMS: readonly string[] = ${JSON.stringify(guideOnlyWithItems(index), null, 2)}\n\n`
    + `export const GUIDE_PLANS: Readonly<Record<string, UiGuidePlan>> = ${JSON.stringify(plans, null, 2)}\n`
}

export function serializeIndex(index) {
  const parts = []
  for (const [key, value] of Object.entries(index)) {
    const k = JSON.stringify(key)
    if (key === 'locations') {
      parts.push(`${k}: [\n${value.map((v) => JSON.stringify(v)).join(',\n')}\n]`)
    } else if (key === 'labels') {
      const locales = Object.entries(value).map(([loc, map]) => {
        const rows = Object.entries(map).map(([lk, lv]) => `${JSON.stringify(lk)}: ${JSON.stringify(lv)}`)
        return `${JSON.stringify(loc)}: {\n${rows.join(',\n')}\n}`
      })
      parts.push(`${k}: {\n${locales.join(',\n')}\n}`)
    } else {
      parts.push(`${k}: ${JSON.stringify(value)}`)
    }
  }
  return `{\n${parts.join(',\n')}\n}\n`
}
