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
import { GUIDE_BUILD_DIGEST, GUIDE_PLANS } from '../uiLocations/guidePlans.gen'
import type { UiGuidePlan, UiGuidePlanPlacement } from '../uiLocations/types'
import { isAutoLocationId, isAutoSiteId, UI_AUTO_BUILD_DIGEST } from '../uiLocations/autoBuild'
import { soleShown, uiLocationCopies } from './liveRegistry'
import { GUIDE_GATE_TEXT_KEYS, GUIDE_SELECTION_TEXT_KEYS, isGate, isSelectionScope } from './guidePredicates'

/** Stable `data-guide-anchor` values. A control opts in by carrying one. */
export const GUIDE_ANCHORS = {
  crewmateCreate: 'crewmate.create',
  mcpServersTab: 'mcp.servers-tab',
  mcpAddCustom: 'mcp.add-custom',
  mcpCustomForm: 'mcp.custom-form',
} as const

export type GuideAnchorId = typeof GUIDE_ANCHORS[keyof typeof GUIDE_ANCHORS]

export type GuideTarget =
  | { kind: 'anchor'; anchor: GuideAnchorId }
  | { kind: 'setting'; entry: SettingEntry }
  /** A registered UI location: exactly ONE element carrying its `data-ui-location`. */
  | { kind: 'location'; id: string }
  /** A gate step points at nothing: it only waits for its gate. */
  | { kind: 'none' }

export type GuideCompletion =
  | { kind: 'ack' }
  /** `scope`: a reveal scope (`<GuideRevealScope>`) whose owner reporting it
   *  open completes the step too, before any later target is measured. */
  | { kind: 'reach'; targets: readonly GuideTarget[]; scope?: string }
  | { kind: 'committed' }
  /** Done only when the page reports the selection fact (`UI_SELECTION_SCOPES`);
   *  never because a later control showed up. An empty picker is a blocker. */
  | { kind: 'select'; selection: string; entity: string }
  /** Done once the gate is on; off, the guide pauses on a blocker naming
   *  `settingId` and goes on by itself when it turns on. Never flipped here. */
  | { kind: 'gate'; gate: string; settingId?: string }

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
  /** The guide was accepted against another build's index than this bundle's. */
  | 'build_mismatch'

export type GuideActionResolution =
  | { ok: true; action: ResolvedGuideAction }
  | { ok: false; reason: GuideActionRefusal }

/** Settings the guide never points at, even though the registry lists them: a
 *  credential field or a control that widens what the agent is allowed to do.
 *  A guide proposed by the agent must not walk the human to its own ceiling. */
const SENSITIVE_TABS: ReadonlySet<string> = new Set(['security', 'secrets', 'instances', 'computer-use'])
const SENSITIVE_IDS: ReadonlySet<string> = new Set([
  'developer.remote-crew-sessions',
  'skills.require-approval-before-generated-skills-go-live',
])
const CREDENTIAL_RE = /token|secret|password|api-key|credential|client-id/i

export function isSensitiveSetting(entry: SettingEntry): boolean {
  if (SENSITIVE_TABS.has(entry.tab) || SENSITIVE_IDS.has(entry.id)) return true
  // Only an input can hold a credential; a toggle named "show context tokens"
  // holds none.
  return entry.type === 'input' && (CREDENTIAL_RE.test(entry.id) || CREDENTIAL_RE.test(entry.label))
}

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
          complete: { kind: 'reach', targets: [anchor(GUIDE_ANCHORS.mcpCustomForm)] },
          textKey: 'components.guideLayer.step_mcp_add_custom',
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
  const want = plan ? pickGuidePlacement(plan, viewport)?.id ?? null : null
  return want && want !== action.placement ? want : null
}

/** The plan's placement for *viewport*: the one made for it, else one for any viewport. */
export function pickGuidePlacement(plan: UiGuidePlan, viewport: 'desktop' | 'mobile'): UiGuidePlanPlacement | null {
  return plan.placements.find(p => p.viewport === viewport) ?? plan.placements.find(p => p.viewport === undefined) ?? null
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
  if (!Array.isArray(plan.placements) || plan.placements.length !== 1) return undefined
  const [p] = plan.placements
  if (!Array.isArray(p.steps) || p.steps.length !== 1) return undefined
  const [st] = p.steps
  if (st.kind !== undefined || typeof st.location !== 'string' || !isAutoSiteId(st.location)) return undefined
  if (p.route !== null && !(typeof p.route === 'string' && p.route.startsWith('/') && !p.route.startsWith('//'))) return undefined
  return plan
}

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

/**
 * Point at one indexed UI location (`chat.older-sessions`), walking through
 * the controls that reveal it. Everything comes from the GENERATED plan
 * (`guidePlans.gen.ts`, built with the find_ui index): the page, the steps and
 * the labels. Each reveal or menu step is done as soon as a later step's
 * control is on screen (so an already-open sidebar or menu is skipped); the
 * last step is shown and acknowledged. The guide never clicks anything.
 */
function resolveUiShow(params: Record<string, unknown>, action: GuideAction): GuideActionResolution {
  if (Object.keys(params).some(k => k !== 'location_id')) return { ok: false, reason: 'invalid_params' }
  const id = str(params.location_id, 120)
  if (!id) return { ok: false, reason: 'invalid_params' }
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
  const targets = placement.steps.map((st): GuideTarget => (st.kind === 'gate' || !st.location ? { kind: 'none' } : { kind: 'location', id: st.location }))
  const last = placement.steps.length - 1
  if (placement.steps[last].kind !== undefined) return { ok: false, reason: 'unknown_location' }
  const route = placement.route
  const steps: GuideStepPlan[] = []
  for (const [i, st] of placement.steps.entries()) {
    const shared = { stepId: st.id, ...(st.requires?.length ? { requires: st.requires } : {}) }
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
      const text = st.entity && Object.hasOwn(GUIDE_SELECTION_TEXT_KEYS, st.entity)
        ? GUIDE_SELECTION_TEXT_KEYS[st.entity as keyof typeof GUIDE_SELECTION_TEXT_KEYS]
        : null
      if (!st.selection || !isSelectionScope(st.selection) || !text) return { ok: false, reason: 'build_mismatch' }
      steps.push({
        ...shared,
        target: targets[i],
        complete: { kind: 'select', selection: st.selection, entity: st.entity! },
        textKey: text.choose,
        textVars: { label: labelOf(st.label_key) },
      })
      continue
    }
    steps.push({
      ...shared,
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

const RESOLVERS: Record<string, (params: Record<string, unknown>, action: GuideAction) => GuideActionResolution> = {
  'settings.show': resolveSettingsShow,
  'crewmate.create': resolveCrewmateCreate,
  'mcp.open_add': resolveMcpOpenAdd,
  'ui.show': resolveUiShow,
}

/** Resolve an agent-proposed action against this registry, or say why not. */
export function resolveGuideAction(action: GuideAction | undefined): GuideActionResolution {
  if (!action || typeof action.id !== 'string') return { ok: false, reason: 'unknown_action' }
  const resolve = Object.prototype.hasOwnProperty.call(RESOLVERS, action.id) ? RESOLVERS[action.id] : undefined
  if (!resolve) return { ok: false, reason: 'unknown_action' }
  const params = action.params && typeof action.params === 'object' && !Array.isArray(action.params) ? action.params : {}
  return resolve(params, action)
}

/** Resolve every action of a guide; the first refusal refuses the whole guide,
 *  so Start is never offered for a guide the page could not finish. */
export function resolveGuideActions(actions: readonly GuideAction[]): { ok: true; actions: ResolvedGuideAction[] } | { ok: false; reason: GuideActionRefusal } {
  if (actions.length === 0) return { ok: false, reason: 'unknown_action' }
  const out: ResolvedGuideAction[] = []
  for (const a of actions) {
    const r = resolveGuideAction(a)
    if (!r.ok) return r
    out.push(r.action)
  }
  return { ok: true, actions: out }
}

/** The element carrying a registered anchor, or null. Exact match only. */
export function findGuideAnchor(anchor: GuideAnchorId): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-guide-anchor="${CSS.escape(anchor)}"]`)
}

/**
 * The ONE element carrying `data-ui-location="<id>"`, or null. The match is
 * on the visible elements only, and must be exact and unique: two visible
 * copies (a list rendering the control per row) mean the guide cannot tell
 * which one the person means, so it points at neither and the step counts as
 * missing. A hidden copy (the other layout's, kept mounted) is not a rival.
 */
export function findUiLocation(id: string, isVisible: (el: HTMLElement) => boolean): HTMLElement | null {
  return soleShown(uiLocationCopies(id), isVisible).element
}
