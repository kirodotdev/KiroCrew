/**
 * The registered-action guide's action and anchor registry — product-owned.
 *
 * An agent may only NAME an action here (`settings.show`, `crewmate.create`,
 * `mcp.open_add`, `ui.show`) and hand it parameters; everything the guide does with the
 * page — where it navigates, which control the arrow points at, what counts as
 * a step being done — is decided by this file. An agent never supplies a
 * selector, script or coordinate, so nothing it sends can make the arrow point
 * at a control the product did not register, or make a click happen.
 *
 * Step kinds:
 * - `ack`: the target is shown; the human presses Next in the guide pill
 *   (Done on the guide's last step, which only ends the guide).
 * - `reach`: done once the UI shows a LATER registered target (the human moved
 *   the form forward, or opened the menu, themselves; one already open is
 *   skipped at once).
 * - `committed`: the step is a real save. The page's own Save button does the
 *   write, carrying the guide headers; the GATEWAY decides completion from what
 *   was actually saved. The browser never reports a mutation as done.
 */
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import type { SettingEntry } from '../components/commandPalette/settingsTypes'
import { resolveLegacyHighlightId } from '../hooks/useSettingHighlight'
import { settingsRoute } from '../components/commandPalette/settingsRoute'
import { i18nT } from '../i18n/t'
import type { GuideAction } from '../api/guide'
import { GUIDE_BUILD_DIGEST, GUIDE_CAUTION_LOCATIONS, GUIDE_PLANS } from '../uiLocations/guidePlans.gen'
import type { UiGuidePlan, UiGuidePlanPlacement } from '../uiLocations/types'
import { isAutoLocationId, isAutoSiteId, UI_AUTO_BUILD_DIGEST } from '../uiLocations/autoBuild'
import { firstShown, soleShown, uiLocationCopies } from './liveRegistry'
import { registeredCopies, registeredId, registeredWithin } from '../uiLocations/targetRegistry'
import { GUIDE_GATE_TEXT_KEYS, GUIDE_SELECTION_TEXT_KEYS, isGate, isSelectionScope } from './guidePredicates'
import { findKey, isFindRole, normalizeName, type FindQuery } from './findByName'
import { isSensitiveSetting, isTrustRootLocationId, isTrustRootPath } from './findTargetPolicy'

/** Stable `data-guide-anchor` values. A control opts in by carrying one. */
export const GUIDE_ANCHORS = {
  crewmateCreate: 'crewmate.create',
  mcpServersTab: 'mcp.servers-tab',
  mcpAddCustom: 'mcp.add-custom',
  mcpCustomForm: 'mcp.custom-form',
  mcpCustomJson: 'mcp.custom-json',
} as const

export type GuideAnchorId = typeof GUIDE_ANCHORS[keyof typeof GUIDE_ANCHORS]

export type GuideTarget =
  | { kind: 'anchor'; anchor: GuideAnchorId }
  | { kind: 'setting'; entry: SettingEntry }
  /**
   * A registered UI location: exactly ONE element carrying its
   * `data-ui-location`. `repeated`: an auto site a list draws once per row,
   * pointed at by its first visible copy (`firstShown`).
   */
  /**
   * `pickFrom`: the location is a picker (a select step's list); the step
   * outlines the one item in it the person can mean, never the whole list
   * when it holds one item, or the one named `pickName`, which also binds
   * the step: it completes only once that entity is the one open. See
   * `pickCandidate`.
   */
  | { kind: 'location'; id: string; repeated?: true; pickFrom?: true; pickName?: string; pickControl?: string }
  /** A gate step points at nothing: it only waits for its gate. */
  | { kind: 'none' }
  /** `ui.find`: the one visible control whose accessible name is the label. */
  | { kind: 'find'; query: FindQuery; key: string }
  /**
   * `ui.find`: what to open first (`findState`): the registered control the
   * index places it under (`opener`, a menu's trigger), else the container
   * the probe found it in.
   */
  | { kind: 'find-container'; key: string; opener?: string }

export type GuideCompletion =
  /** `at`: the page the step says the person is on (a page guide's "you're
   *  here"). Away from it the step is missing, so Done cannot end a guide
   *  whose page the person has left; see `atPage`. */
  | { kind: 'ack'; at?: string }
  /** `scope`: a reveal scope (`<GuideRevealScope>`) whose owner reporting it
   *  open completes the step too, before any later target is measured. */
  | { kind: 'reach'; targets: readonly GuideTarget[]; scope?: string }
  | { kind: 'committed' }
  /** Done when the page reports the selection fact (`UI_SELECTION_SCOPES`).
   *  A later control showing up counts only when the picker holds exactly
   *  one item (`then`, the next step's target): there is nothing else the
   *  person could have meant. An empty picker is a blocker. */
  | { kind: 'select'; selection: string; entity: string; then?: GuideTarget }
  /** Done once the gate is on; off, the guide pauses on a blocker naming
   *  `settingId` and goes on by itself when it turns on. Never flipped here. */
  | { kind: 'gate'; gate: string; settingId?: string }
  /** Done once the person is on exactly the page `route` opens (a `ui.find`
   *  that names the page its route opens: there is no control named like it
   *  there). See `atPage`: a sub-route or another tab is a different page. */
  | { kind: 'arrive'; route: string }

export interface GuideStepPlan {
  target: GuideTarget
  complete: GuideCompletion
  /** `ui.show` (plan version 2): the step's id, the only way a progress report names it. */
  stepId?: string
  /**
   * Runtime predicates (`guidePredicates.ts`) the target needs to be drawn.
   * Unmet while the target is absent, the guide shows a blocker instead.
   */
  requires?: readonly string[]
  /** Catalog key of the pill's instruction for this step. */
  textKey: string
  /** Interpolation for `textKey` (a location's label). */
  textVars?: Record<string, string>
  /**
   * The step points at a destructive control: the panel shows the caution
   * line, and pressing the control never finishes the step by itself.
   */
  caution?: boolean
  /**
   * With `caution`: the catalog key of the page's own text for what the
   * control removes and keeps, shown in place of the generic caution line.
   */
  cautionKey?: string
  /**
   * A step after a select step depends on that selection: it is pointed at
   * only while the selection still holds (an entity open, and when a pick
   * named one, that entity), and only at a control drawn for that entity
   * (`stepBoundHolds`, `resolveStepTarget`). The selection changing or
   * clearing leaves the step missing, so the guide goes back to the choice.
   */
  bound?: GuideSelectionBound
}

/** The selection a step after a select step depends on (`GuideStepPlan.bound`). */
export interface GuideSelectionBound {
  selection: string
  /** The entity a `pick` named; without one, whichever entity is open. */
  pickName?: string
}

export type GuideEnterPlan = { kind: 'navigate'; to: (here: { pathname: string; search: string }) => string }

export interface ResolvedGuideAction {
  id: string
  titleKey: string
  titleVars: Record<string, string>
  steps: readonly GuideStepPlan[]
  enter: GuideEnterPlan
}

export type GuideActionRefusal =
  | 'unknown_action' | 'invalid_params' | 'unknown_setting' | 'sensitive_setting'
  | 'unknown_location' | 'location_not_on_this_screen'
  /** `ui.find` of a page of the agent's own ceiling. */
  | 'sensitive_page'
  /** The guide was accepted against another build's index than this bundle's. */
  | 'build_mismatch'

export type GuideActionResolution =
  | { ok: true; action: ResolvedGuideAction }
  | { ok: false; reason: GuideActionRefusal }

export { isSensitiveSetting }

const anchor = (id: GuideAnchorId): GuideTarget => ({ kind: 'anchor', anchor: id })

const str = (v: unknown, max: number): string | null | undefined => {
  if (v === undefined || v === null) return undefined
  if (typeof v !== 'string' || v.length > max) return null
  return v
}

function resolveSettingsShow(params: Record<string, unknown>): GuideActionResolution {
  const raw = str(params.setting_id, 200)
  if (!raw) return { ok: false, reason: 'invalid_params' }
  const id = resolveLegacyHighlightId(raw)
  const entry = SETTINGS_REGISTRY.find(e => e.id === id)
  if (!entry) return { ok: false, reason: 'unknown_setting' }
  if (isSensitiveSetting(entry)) return { ok: false, reason: 'sensitive_setting' }
  return {
    ok: true,
    action: {
      id: 'settings.show',
      titleKey: 'components.guideLayer.title_settings_show',
      titleVars: { label: entry.labelKey ? i18nT(entry.labelKey) : entry.label },
      steps: [{ target: { kind: 'setting', entry }, complete: { kind: 'ack' }, textKey: 'components.guideLayer.step_settings_show' }],
      enter: { kind: 'navigate', to: () => settingsRoute(entry) },
    },
  }
}

function resolveCrewmateCreate(params: Record<string, unknown>): GuideActionResolution {
  const name = str(params.name, 64)
  const goal = str(params.goal, 2000)
  if (name === null || goal === null) return { ok: false, reason: 'invalid_params' }
  return {
    ok: true,
    action: {
      id: 'crewmate.create',
      titleKey: 'components.guideLayer.title_crewmate_create',
      titleVars: {},
      // One step: the New crewmate card opens with the name and goal filled
      // in, and the guide points at its Create button.
      steps: [
        {
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.crewmateCreate },
          complete: { kind: 'committed' },
          textKey: 'components.guideLayer.step_crewmate_create',
        },
      ],
      enter: {
        kind: 'navigate',
        // Hands the draft to the Crewmates page's own `?create=1` hand-off,
        // which opens the New crewmate card with it filled in; a card the
        // user already edited is replaced only once they agree to leave it.
        // On that page already, the rest of the address (the open member) is
        // kept so "back to chat" still knows where the user came from.
        to: (here) => {
          const next = new URLSearchParams(here.pathname === '/members' ? here.search : '')
          next.set('create', '1')
          if (name) next.set('name', name); else next.delete('name')
          if (goal) next.set('goal', goal); else next.delete('goal')
          return `/members?${next.toString()}`
        },
      },
    },
  }
}

/**
 * Walk the human to the product's OWN add-an-MCP-server form and stop there.
 * Navigation only: no server name, spec or credential is carried, nothing is
 * pre-filled, and the guide finishing means the native form is open -- never
 * that a server was added. Saving is the human's own act on that form.
 */
function resolveMcpOpenAdd(params: Record<string, unknown>): GuideActionResolution {
  if (Object.keys(params).length > 0) return { ok: false, reason: 'invalid_params' }
  return {
    ok: true,
    action: {
      id: 'mcp.open_add',
      titleKey: 'components.guideLayer.title_mcp_open_add',
      titleVars: {},
      steps: [
        {
          // Already on the MCP Servers view: the Add Custom button is visible
          // and this step is reached at once.
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.mcpServersTab },
          complete: { kind: 'reach', targets: [anchor(GUIDE_ANCHORS.mcpAddCustom), anchor(GUIDE_ANCHORS.mcpCustomForm)] },
          textKey: 'components.guideLayer.step_mcp_open_tab',
        },
        {
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.mcpAddCustom },
          complete: { kind: 'reach', targets: [anchor(GUIDE_ANCHORS.mcpCustomJson)] },
          textKey: 'components.guideLayer.step_mcp_add_custom',
        },
        // Into the form that just opened: the JSON box is where the command,
        // its args and its env variables go, so the guide ends there, inside
        // the dialog, never on the button that opened it.
        {
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.mcpCustomJson },
          complete: { kind: 'ack' },
          textKey: 'components.guideLayer.step_mcp_custom_json',
        },
      ],
      enter: { kind: 'navigate', to: () => '/capabilities?tab=mcp' },
    },
  }
}

/** `useIsMobile`'s width query (`MOBILE_BREAKPOINT` 768), read directly: the
 *  index's `viewport: mobile` placements are exactly what that hook renders. */
const PHONE_QUERY = '(max-width: 767px)'

export function currentGuideViewport(): 'desktop' | 'mobile' {
  const mm = typeof window !== 'undefined' ? window.matchMedia : undefined
  return typeof mm === 'function' && mm.call(window, PHONE_QUERY).matches ? 'mobile' : 'desktop'
}

/** Subscribe to the viewport class changing (for `useSyncExternalStore`). */
export function subscribeGuideViewport(onChange: () => void): () => void {
  const mm = typeof window !== 'undefined' ? window.matchMedia : undefined
  if (typeof mm !== 'function') return () => undefined
  const list = mm.call(window, PHONE_QUERY)
  if (typeof list?.addEventListener !== 'function') return () => undefined
  list.addEventListener('change', onChange)
  return () => list.removeEventListener('change', onChange)
}

/**
 * The placement the current viewport would walk for a CLAIMED `ui.show`
 * action, when it differs from the one claimed; null when nothing changed,
 * the action is not a claimed `ui.show`, or this bundle has no plan for it.
 */
export function replanPlacementFor(action: GuideAction | undefined, viewport: 'desktop' | 'mobile'): string | null {
  if (action?.id !== 'ui.show' || typeof action.placement !== 'string') return null
  const lid = action.params?.location_id
  const plan = typeof lid === 'string' ? planFor(lid, action) : undefined
  // A shared control's placements are pages, not viewports: the one claimed
  // is walked to the end, wherever the person goes.
  if (!plan || isPerPagePlan(plan)) return null
  const want = pickGuidePlacement(plan, viewport)?.id ?? null
  return want && want !== action.placement ? want : null
}

/** Whether *plan* lists one placement per page (an auto control several pages share). */
function isPerPagePlan(plan: UiGuidePlan): boolean {
  return plan.placements.length > 1 && plan.placements.every(p => p.viewport === undefined)
}

/**
 * The plan's placement for *viewport*: the one made for it, else one for any
 * viewport. A per-page plan (an auto control several pages share) takes the
 * placement of the page the person is on (*here*) when it is one of them,
 * else the first: the guide stays where they are, or opens the first page
 * that draws the control.
 */
export function pickGuidePlacement(
  plan: UiGuidePlan,
  viewport: 'desktop' | 'mobile',
  here: { pathname: string; search: string } = currentLocation(),
): UiGuidePlanPlacement | null {
  if (isPerPagePlan(plan)) {
    return plan.placements.find(p => p.route !== null && alreadyAt(p.route, here))
      ?? plan.placements.find(p => p.route === null)
      ?? plan.placements[0]
  }
  return plan.placements.find(p => p.viewport === viewport) ?? plan.placements.find(p => p.viewport === undefined) ?? null
}

function currentLocation(): { pathname: string; search: string } {
  return typeof window !== 'undefined' ? { pathname: window.location.pathname, search: window.location.search } : { pathname: '/', search: '' }
}

/**
 * The `ui.show` plan for location *id*: a curated one from this bundle's own
 * generated plans; an AUTO one only from the guide record (`auto_plan`, which
 * the gateway read from the build-time auto tier) and only when the record's
 * digest is this bundle's own auto digest -- the same build that stamped the
 * `data-ui-auto` markers. Its one step must name a site id of that shape.
 */
function planFor(id: string, action?: GuideAction): UiGuidePlan | undefined {
  if (!isAutoLocationId(id)) return Object.prototype.hasOwnProperty.call(GUIDE_PLANS, id) ? GUIDE_PLANS[id] : undefined
  const plan = action?.auto_plan
  if (!UI_AUTO_BUILD_DIGEST || action?.build_digest !== UI_AUTO_BUILD_DIGEST || !plan || plan.version !== 2) return undefined
  if (!Array.isArray(plan.placements) || plan.placements.length === 0 || plan.placements.length > AUTO_MAX_PLACEMENTS) return undefined
  // Several placements only for a shared control (one per page), all at one site.
  if (plan.placements.length > 1 && !id.startsWith(`auto:${AUTO_SHARED_PARENT}:`)) return undefined
  const sites = new Set<string>()
  const ids = new Set<string>()
  for (const p of plan.placements) {
    if (!Array.isArray(p.steps) || p.steps.length !== 1 || p.viewport !== undefined || ids.has(p.id)) return undefined
    ids.add(p.id)
    const [st] = p.steps
    if (st.kind !== undefined || typeof st.location !== 'string' || !isAutoSiteId(st.location)) return undefined
    if (p.route !== null && !(typeof p.route === 'string' && p.route.startsWith('/') && !p.route.startsWith('//'))) return undefined
    sites.add(st.location)
  }
  return sites.size === 1 ? plan : undefined
}

/** The parent of an auto control several pages share (`SHARED_PARENT` in the generator). */
const AUTO_SHARED_PARENT = 'shared'
/** Most placements a shared auto plan may list (`_AUTO_MAX_PLACEMENTS` in guide_catalog.py). */
const AUTO_MAX_PLACEMENTS = 32

/**
 * The placement this tab claims each action with: for a `ui.show` action, the
 * id of its plan's placement for the current viewport; `null` for every other
 * action (and for one this bundle cannot plan, which the gateway then refuses).
 * The gateway records the chosen placement's step ids at claim.
 */
export function guideClaimPlacements(actions: readonly GuideAction[]): (string | null)[] {
  const viewport = currentGuideViewport()
  return actions.map((a) => {
    if (a?.id !== 'ui.show') return null
    const lid = a.params?.location_id
    const plan = typeof lid === 'string' ? planFor(lid, a) : undefined
    return plan ? pickGuidePlacement(plan, viewport)?.id ?? null : null
  })
}

const labelOf = (key: string) => (key.startsWith('literal:') ? key.slice('literal:'.length) : i18nT(key))

/** Whether *here* is already the page *route* names: the same page (or a
 *  sub-route of it) with every query parameter the route sets. */
export function alreadyAt(route: string, here: { pathname: string; search: string }): boolean {
  const want = new URL(route, 'http://guide.invalid')
  const base = want.pathname.replace(/\/+$/, '')
  if (here.pathname !== want.pathname && here.pathname !== base && !here.pathname.startsWith(`${base}/`)) return false
  const have = new URLSearchParams(here.search)
  for (const [k, v] of want.searchParams) if (have.get(k) !== v) return false
  return true
}

/** Query parameters that choose which page a path shows (`/capabilities?tab=mcp`). */
const PAGE_IDENTITY_PARAMS = ['tab'] as const

/** Whether *here* is exactly the page *route* opens: the same path (trailing
 *  slashes aside, no sub-route) and the same page-identity query parameters
 *  the route sets. A page guide's walk and its completion both use this, so
 *  standing on `/apps/library` never completes a guide to `/apps`. */
export function atPage(route: string, here: { pathname: string; search: string }): boolean {
  const want = new URL(route, 'http://guide.invalid')
  if (here.pathname.replace(/\/+$/, '') !== want.pathname.replace(/\/+$/, '')) return false
  const have = new URLSearchParams(here.search)
  return PAGE_IDENTITY_PARAMS.every(k => !want.searchParams.has(k) || have.get(k) === want.searchParams.get(k))
}

/**
 * Pickers whose rows carry their entity's name as `data-guide-pick`, so a
 * `ui.show` `pick` can be bound to the entity the person named. Mirrors
 * `UI_SHOW_PICKABLE_PICKERS` in the gateway's guide_catalog.py.
 */
export const GUIDE_PICKABLE_PICKERS: readonly string[] = ['agents.crew-list', 'members.roster-list', 'schedule.job-list', 'apps.library.app-list', 'artifacts.list']

/**
 * Point at one indexed UI location (`chat.older-sessions`), walking through
 * the controls that reveal it. Everything comes from the GENERATED plan
 * (`guidePlans.gen.ts`, built with the find_ui index): the page, the steps and
 * the labels. Each reveal or menu step is done as soon as a later step's
 * control is on screen (so an already-open sidebar or menu is skipped); the
 * last step is shown and acknowledged. The guide never clicks anything.
 */
function resolveUiShow(params: Record<string, unknown>, action: GuideAction): GuideActionResolution {
  if (Object.keys(params).some(k => k !== 'location_id' && k !== 'pick')) return { ok: false, reason: 'invalid_params' }
  const id = str(params.location_id, 120)
  if (!id) return { ok: false, reason: 'invalid_params' }
  const pick = str(params.pick, 80)
  if (pick === null) return { ok: false, reason: 'invalid_params' }
  // The gateway stored the digest of the index it accepted this guide against.
  // Another build's plan may name places this bundle draws elsewhere or not at
  // all, so it is refused here, before any of it is read.
  // An auto location's plan carries the auto tier's own digest instead (see planFor).
  if (isAutoLocationId(id) ? !UI_AUTO_BUILD_DIGEST || action.build_digest !== UI_AUTO_BUILD_DIGEST : action.build_digest !== GUIDE_BUILD_DIGEST) {
    return { ok: false, reason: 'build_mismatch' }
  }
  const plan = planFor(id, action)
  if (!plan || plan.version !== 2) return { ok: false, reason: 'unknown_location' }
  // Once claimed, the gateway holds the placement this tab chose and its step
  // ids; the guide walks exactly those, whatever the viewport does since (a
  // resize mid-guide shows a missing target, never another branch's steps).
  // Before the claim, the current viewport's placement is what Start claims.
  const placement = action.placement !== undefined
    ? plan.placements.find(p => p.id === action.placement) ?? null
    : pickGuidePlacement(plan, currentGuideViewport())
  if (!placement) return { ok: false, reason: 'location_not_on_this_screen' }
  if (placement.steps.length === 0) return { ok: false, reason: 'unknown_location' }
  // The recorded step ids are the gateway's; any difference is another build.
  if (action.step_ids !== undefined
    && (action.step_ids.length !== placement.steps.length || action.step_ids.some((sid, i) => sid !== placement.steps[i].id))) {
    return { ok: false, reason: 'build_mismatch' }
  }
  // An auto site drawn once per list row is any one of its copies, except on
  // a destructive step, where the person must not be shown the wrong row's.
  const targets = placement.steps.map((st): GuideTarget => (st.kind === 'gate' || !st.location
    ? { kind: 'none' }
    : { kind: 'location', id: st.location, ...(isAutoSiteId(st.location) && st.kind === undefined && st.caution !== true ? { repeated: true as const } : {}) }))
  const last = placement.steps.length - 1
  if (placement.steps[last].kind !== undefined) return { ok: false, reason: 'unknown_location' }
  const route = placement.route
  const steps: GuideStepPlan[] = []
  // Every step after the select step is about the entity chosen there.
  let bound: GuideSelectionBound | undefined
  for (const [i, st] of placement.steps.entries()) {
    const shared = { stepId: st.id, ...(st.requires?.length ? { requires: st.requires } : {}), ...(bound ? { bound } : {}) }
    if (st.kind === 'gate') {
      const gate = st.gate ?? ''
      const entry = st.setting_id ? SETTINGS_REGISTRY.find(e => e.id === st.setting_id) : undefined
      if (!isGate(gate) || (st.setting_id && !entry)) return { ok: false, reason: 'build_mismatch' }
      steps.push({
        ...shared,
        target: targets[i],
        complete: { kind: 'gate', gate, ...(st.setting_id ? { settingId: st.setting_id } : {}) },
        ...(entry
          ? { textKey: 'components.guideLayer.gate_setting', textVars: { label: entry.labelKey ? i18nT(entry.labelKey) : entry.label } }
          : { textKey: Object.hasOwn(GUIDE_GATE_TEXT_KEYS, gate) ? GUIDE_GATE_TEXT_KEYS[gate as keyof typeof GUIDE_GATE_TEXT_KEYS] : 'components.guideLayer.predicate_unknown' }),
      })
      continue
    }
    if (!st.location || !st.label_key) return { ok: false, reason: 'unknown_location' }
    if (st.kind === 'select') {
      // A pick binds the step to a named entity: only a list whose rows carry
      // their names can do that, so any other one refuses it.
      if (pick && !GUIDE_PICKABLE_PICKERS.includes(st.location)) return { ok: false, reason: 'invalid_params' }
      const text = st.entity && Object.hasOwn(GUIDE_SELECTION_TEXT_KEYS, st.entity)
        ? GUIDE_SELECTION_TEXT_KEYS[st.entity as keyof typeof GUIDE_SELECTION_TEXT_KEYS]
        : null
      if (!st.selection || !isSelectionScope(st.selection) || !text) return { ok: false, reason: 'build_mismatch' }
      const then = targets.slice(i + 1).find(t => t.kind !== 'none')
      const picker = targets[i]
      steps.push({
        ...shared,
        target: picker.kind === 'location' ? { ...picker, pickFrom: true, pickControl: st.selection, ...(pick ? { pickName: pick } : {}) } : picker,
        complete: { kind: 'select', selection: st.selection, entity: st.entity!, ...(then ? { then } : {}) },
        textKey: text.choose,
        textVars: { label: labelOf(st.label_key) },
      })
      bound = { selection: st.selection, ...(pick ? { pickName: pick } : {}) }
      continue
    }
    steps.push({
      ...shared,
      ...(i === last && st.caution === true ? { caution: true, ...(st.caution_key ? { cautionKey: st.caution_key } : {}) } : {}),
      target: targets[i],
      complete: i === last
        ? { kind: 'ack' }
        : { kind: 'reach', targets: targets.slice(i + 1).filter(t => t.kind !== 'none'), ...(st.scope ? { scope: st.scope } : {}) },
      // A reveal step names where it leads, never the revealer: an icon-only
      // toggle's accessible label ("Toggle sessions") means nothing to someone
      // looking at an icon. The last step says what to do next (press it, or
      // Done), because "Here is X" left a new user waiting for something.
      ...(i === last
        ? { textKey: 'components.guideLayer.step_ui_show_here', textVars: { label: labelOf(st.label_key) } }
        : { textKey: 'components.guideLayer.step_ui_show_open', textVars: { target: labelOf(plan.label_key) } }),
    })
  }
  return {
    ok: true,
    action: {
      id: 'ui.show',
      titleKey: 'components.guideLayer.title_ui_show',
      titleVars: { label: labelOf(plan.label_key) },
      steps,
      // Shell chrome (`route: null`) is on every page: the guide stays where the
      // person is. Already on the route's page (a session open under /chat, the
      // tab's query already set), the address is kept, so Start never closes
      // what the person has open.
      enter: { kind: 'navigate', to: (here) => (route === null || alreadyAt(route, here) ? `${here.pathname}${here.search}` : route) },
    },
  }
}

/**
 * Point at a control by its accessible name on one page (`findByName.ts`).
 * Two steps, as the gateway's catalog has them: `open` points at the
 * container the probe found the control in, and is passed at once when the
 * control is already visible; `show` points at the control itself, its panel
 * naming it as the page does. The page, the label and the role come from the
 * gateway-checked params; a trust-root page is refused here too. A
 * `location_id` (from a `find_ref`) names the registered control itself, so
 * it is found whatever its label reads in the state the page is in. An
 * `opener` (also from a `find_ref`) is the registered control the index
 * places it under, a menu's trigger, which the gateway derives from the
 * index: while the control is not showing and that trigger is, the `open`
 * step points at the trigger, and passes once the person opened it and the
 * control shows. Nothing is opened for them. A trigger that removes something
 * or belongs to the agent's own ceiling is refused, here by its id and in the
 * page by the real element (`isSafeOpenerTarget`).
 */
const FIND_LOCATION_RE = /^[a-z][a-z0-9_.-]{0,159}$/
function resolveUiFind(params: Record<string, unknown>, _action: GuideAction, guideId: string, actionIndex: number): GuideActionResolution {
  if (Object.keys(params).some(k => !['route', 'label', 'role', 'container', 'caution', 'location_id', 'opener', 'page'].includes(k))) return { ok: false, reason: 'invalid_params' }
  if (params.page !== undefined && params.page !== true) return { ok: false, reason: 'invalid_params' }
  const label = str(params.label, 80)
  const location = str(params.location_id, 160)
  const opener = str(params.opener, 160)
  const route = str(params.route, 200)
  const container = str(params.container, 80)
  if (!label || route === null || container === null || location === null || opener === null) return { ok: false, reason: 'invalid_params' }
  if (location !== undefined && !FIND_LOCATION_RE.test(location)) return { ok: false, reason: 'invalid_params' }
  // The gateway derives the opener from the index; one without its control's
  // id, one that removes something or one of the agent's own ceiling is
  // refused here too.
  if (opener !== undefined && (!FIND_LOCATION_RE.test(opener) || opener === location || location === undefined)) return { ok: false, reason: 'invalid_params' }
  if (opener !== undefined && (GUIDE_CAUTION_LOCATIONS.includes(opener) || isTrustRootLocationId(opener))) return { ok: false, reason: 'unknown_location' }
  if (params.role !== undefined && !isFindRole(params.role)) return { ok: false, reason: 'invalid_params' }
  if (params.caution !== undefined && typeof params.caution !== 'boolean') return { ok: false, reason: 'invalid_params' }
  if (route !== undefined && (!route.startsWith('/') || route.startsWith('//'))) return { ok: false, reason: 'invalid_params' }
  if (route !== undefined && isTrustRootPath(new URL(route, 'http://guide.invalid').pathname)) return { ok: false, reason: 'sensitive_page' }
  // The gateway marks a find that names the page its route opens (`page`).
  // The guide never takes the person there itself: away from the page, the
  // first step points at the link that opens it (the menu entry named like
  // the page, never a link in the chat) and is done when they arrive; on the
  // page, the second step says so and waits for Done, so the guide never
  // ends with nothing shown.
  if (params.page === true) {
    if (route === undefined || location !== undefined) return { ok: false, reason: 'invalid_params' }
    const query: FindQuery = { label, role: 'link' }
    return {
      ok: true,
      action: {
        id: 'ui.find',
        titleKey: 'components.guideLayer.title_ui_find',
        titleVars: { label },
        steps: [
          { target: { kind: 'find', query, key: findKey(guideId, actionIndex, query) }, complete: { kind: 'arrive', route }, textKey: 'components.guideLayer.step_page_open', textVars: { target: label } },
          { target: { kind: 'none' }, complete: { kind: 'ack', at: route }, textKey: 'components.guideLayer.step_page_here', textVars: { label } },
        ],
        enter: { kind: 'navigate', to: (here) => `${here.pathname}${here.search}` },
      },
    }
  }
  const query: FindQuery = { label, ...(isFindRole(params.role) ? { role: params.role } : {}), ...(container ? { container } : {}), ...(location ? { location } : {}) }
  const key = findKey(guideId, actionIndex, query)
  const target: GuideTarget = { kind: 'find', query, key }
  return {
    ok: true,
    action: {
      id: 'ui.find',
      titleKey: 'components.guideLayer.title_ui_find',
      titleVars: { label },
      steps: [
        {
          target: { kind: 'find-container', key, ...(opener ? { opener } : {}) },
          complete: { kind: 'reach', targets: [target] },
          textKey: 'components.guideLayer.step_ui_find_open',
          textVars: { target: label },
        },
        {
          ...(params.caution === true ? { caution: true } : {}),
          target,
          complete: { kind: 'ack' },
          textKey: 'components.guideLayer.step_ui_show_here',
          textVars: { label },
        },
      ],
      enter: { kind: 'navigate', to: (here) => (route === undefined || alreadyAt(route, here) ? `${here.pathname}${here.search}` : route) },
    },
  }
}

const RESOLVERS: Record<string, (params: Record<string, unknown>, action: GuideAction, guideId: string, actionIndex: number) => GuideActionResolution> = {
  'settings.show': resolveSettingsShow,
  'crewmate.create': resolveCrewmateCreate,
  'mcp.open_add': resolveMcpOpenAdd,
  'ui.show': resolveUiShow,
  'ui.find': resolveUiFind,
}

/** Resolve an agent-proposed action against this registry, or say why not.
 *  *guideId* and *actionIndex* name a `ui.find` search (its `findState`). */
export function resolveGuideAction(action: GuideAction | undefined, guideId = '', actionIndex = 0): GuideActionResolution {
  if (!action || typeof action.id !== 'string') return { ok: false, reason: 'unknown_action' }
  const resolve = Object.prototype.hasOwnProperty.call(RESOLVERS, action.id) ? RESOLVERS[action.id] : undefined
  if (!resolve) return { ok: false, reason: 'unknown_action' }
  const params = action.params && typeof action.params === 'object' && !Array.isArray(action.params) ? action.params : {}
  return resolve(params, action, guideId, actionIndex)
}

/** Resolve every action of a guide; the first refusal refuses the whole guide,
 *  so Start is never offered for a guide the page could not finish. */
export function resolveGuideActions(actions: readonly GuideAction[], guideId = ''): { ok: true; actions: ResolvedGuideAction[] } | { ok: false; reason: GuideActionRefusal } {
  if (actions.length === 0) return { ok: false, reason: 'unknown_action' }
  const out: ResolvedGuideAction[] = []
  for (const [i, a] of actions.entries()) {
    const r = resolveGuideAction(a, guideId, i)
    if (!r.ok) return r
    out.push(r.action)
  }
  return { ok: true, actions: out }
}

/** The element carrying a registered anchor, or null. Exact match only. */
export function findGuideAnchor(anchor: GuideAnchorId): HTMLElement | null {
  return registeredCopies('anchor', anchor)[0] ?? null
}

/** A picker's items: each choosable row or card React registered with `guidePick(name)` (an add card is never one). */
export function pickItems(picker: Element): HTMLElement[] {
  return registeredWithin('pick', picker)
}

/** The pick name *el* was registered with, if it is a picker item. */
export function pickNameOf(el: Element | null | undefined): string | undefined {
  return registeredId(el, 'pick')
}

/** The item's control registered for *selection* (`guidePickControl`), if visible. */
function pickControlOf(item: HTMLElement, selection: string, isVisible: (el: HTMLElement) => boolean): HTMLElement | undefined {
  return registeredWithin('pickControl', item)
    .find(el => (registeredId(el, 'pickControl') ?? '').split(/\s+/).includes(selection) && isVisible(el))
}

/**
 * Whether picker item *el* is the one *want* names: by the name it shows, or
 * by the other name it was registered with (`guidePickAlias`, a built-in
 * app's manifest name beside its translated one).
 */
export function itemIsNamed(el: Element, want: string): boolean {
  return isPickedName(pickNameOf(el), want) || isPickedName(registeredId(el, 'pickAlias'), want)
}

/** Whether *name* (an entity's own name, as its row carries it) is the pick *want*. */
export function isPickedName(name: string | undefined, want: string): boolean {
  const w = normalizeName(want)
  return !!w && normalizeName(name ?? '') === w
}

/**
 * What a select step outlines in *picker*: one item, never the whole list.
 * With a pick *name*: the one item whose `data-guide-pick` (or its alias) is
 * exactly that name (case and spacing folded), or null when none or several are, whatever
 * else the list holds; text inside a row (a job's prompt, a description)
 * never matches. Without one: the first item, the place to choose from (the
 * panel says to choose one; an add card is never an item). Null means there
 * is no item to choose. When the item carries a control for *selection*
 * (`data-guide-pick-control`), that control is outlined, not the item.
 */
export function pickCandidate(picker: HTMLElement, isVisible: (el: HTMLElement) => boolean, name?: string, selection?: string): HTMLElement | null {
  const items = pickItems(picker).filter(isVisible)
  let item: HTMLElement | null
  if (name !== undefined) {
    const named = items.filter(el => itemIsNamed(el, name))
    item = named.length === 1 ? named[0] : null
  } else {
    item = items[0] ?? null
  }
  if (!item || !selection) return item
  return pickControlOf(item, selection, isVisible) ?? item
}

/**
 * The other items a select step with no pick name leaves to choose from: each
 * is outlined beside the first (`pickCandidate`), so the outline says "one of
 * these" and never suggests the first is the one meant. Empty with a name,
 * or with a single item.
 */
export function pickAlternatives(picker: HTMLElement, isVisible: (el: HTMLElement) => boolean, selection?: string): HTMLElement[] {
  const items = pickItems(picker).filter(isVisible)
  return items.slice(1).map(item => (selection ? pickControlOf(item, selection, isVisible) : undefined) ?? item)
}

/**
 * The menu trigger a `ui.find` opens first (its `opener`): the one visible
 * copy, or, when every list row draws its own (a session row's ⋯ menu), the
 * copy on the row the person has open now (`aria-current`). Several copies
 * and no current row point at none: the guide cannot tell which row is meant.
 */
export function findOpenerLocation(id: string, isVisible: (el: HTMLElement) => boolean): HTMLElement | null {
  const shown = Array.from(uiLocationCopies(id)).filter(isVisible)
  if (shown.length === 1) return shown[0]
  const current = shown.filter(el => !!el.closest('[aria-current="true"], [aria-current="page"], [aria-selected="true"]'))
  return current.length === 1 ? current[0] : null
}

/**
 * The ONE element registered as location *id*, or null. The match is
 * on the visible elements only, and must be exact and unique: two visible
 * copies (a list rendering the control per row) mean the guide cannot tell
 * which one the person means, so it points at neither and the step counts as
 * missing. A hidden copy (the other layout's, kept mounted) is not a rival.
 */
export function findUiLocation(id: string, isVisible: (el: HTMLElement) => boolean, repeated = false): HTMLElement | null {
  return repeated ? firstShown(uiLocationCopies(id), isVisible) : soleShown(uiLocationCopies(id), isVisible).element
}
