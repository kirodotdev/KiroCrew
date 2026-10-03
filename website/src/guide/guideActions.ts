/**
 * The registered-action guide's action and anchor registry — product-owned.
 *
 * An agent may only NAME an action here (`settings.show`, `crewmate.create`,
 * `mcp.open_add`) and hand it parameters; everything the guide does with the
 * page — where it navigates, which control the arrow points at, what counts as
 * a step being done — is decided by this file. An agent never supplies a
 * selector, script or coordinate, so nothing it sends can make the arrow point
 * at a control the product did not register, or make a click happen.
 *
 * Step kinds:
 * - `ack`: the target is shown; the human presses Next in the guide pill.
 * - `reach`: done once the UI shows a LATER registered anchor (the human moved
 *   the form forward themselves).
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

/** Stable `data-guide-anchor` values. A control opts in by carrying one. */
export const GUIDE_ANCHORS = {
  crewmateGoalNext: 'crewmate.goal-next',
  crewmateNameNext: 'crewmate.name-next',
  crewmateCreate: 'crewmate.create',
  mcpServersTab: 'mcp.servers-tab',
  mcpAddCustom: 'mcp.add-custom',
  mcpCustomForm: 'mcp.custom-form',
} as const

export type GuideAnchorId = typeof GUIDE_ANCHORS[keyof typeof GUIDE_ANCHORS]

export type GuideTarget =
  | { kind: 'anchor'; anchor: GuideAnchorId }
  | { kind: 'setting'; entry: SettingEntry }

export type GuideCompletion =
  | { kind: 'ack' }
  | { kind: 'reach'; anchors: readonly GuideAnchorId[] }
  | { kind: 'committed' }

export interface GuideStepPlan {
  target: GuideTarget
  complete: GuideCompletion
  /** Catalog key of the pill's instruction for this step. */
  textKey: string
}

export type GuideEnterPlan = { kind: 'navigate'; to: (here: { pathname: string; search: string }) => string }

export interface ResolvedGuideAction {
  id: string
  titleKey: string
  titleVars: Record<string, string>
  steps: readonly GuideStepPlan[]
  enter: GuideEnterPlan
}

export type GuideActionRefusal = 'unknown_action' | 'invalid_params' | 'unknown_setting' | 'sensitive_setting'

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
      steps: [
        {
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.crewmateGoalNext },
          complete: { kind: 'reach', anchors: [GUIDE_ANCHORS.crewmateNameNext, GUIDE_ANCHORS.crewmateCreate] },
          textKey: 'components.guideLayer.step_crewmate_goal',
        },
        {
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.crewmateNameNext },
          complete: { kind: 'reach', anchors: [GUIDE_ANCHORS.crewmateCreate] },
          textKey: 'components.guideLayer.step_crewmate_name',
        },
        {
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.crewmateCreate },
          complete: { kind: 'committed' },
          textKey: 'components.guideLayer.step_crewmate_create',
        },
      ],
      enter: {
        kind: 'navigate',
        // Hands the draft to the Crewmates page's own `?create=1` hand-off,
        // which gives it to the guided flow as a PROPOSAL: a draft the user
        // already edited is kept (MeetCrewmatesFlow `onDraftKept`). On that
        // page already, the rest of the address (the open member) is kept so
        // "back to chat" still knows where the user came from.
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
          complete: { kind: 'reach', anchors: [GUIDE_ANCHORS.mcpAddCustom, GUIDE_ANCHORS.mcpCustomForm] },
          textKey: 'components.guideLayer.step_mcp_open_tab',
        },
        {
          target: { kind: 'anchor', anchor: GUIDE_ANCHORS.mcpAddCustom },
          complete: { kind: 'reach', anchors: [GUIDE_ANCHORS.mcpCustomForm] },
          textKey: 'components.guideLayer.step_mcp_add_custom',
        },
      ],
      enter: { kind: 'navigate', to: () => '/capabilities?tab=mcp' },
    },
  }
}

const RESOLVERS: Record<string, (params: Record<string, unknown>) => GuideActionResolution> = {
  'settings.show': resolveSettingsShow,
  'crewmate.create': resolveCrewmateCreate,
  'mcp.open_add': resolveMcpOpenAdd,
}

/** Resolve an agent-proposed action against this registry, or say why not. */
export function resolveGuideAction(action: GuideAction | undefined): GuideActionResolution {
  if (!action || typeof action.id !== 'string') return { ok: false, reason: 'unknown_action' }
  const resolve = Object.prototype.hasOwnProperty.call(RESOLVERS, action.id) ? RESOLVERS[action.id] : undefined
  if (!resolve) return { ok: false, reason: 'unknown_action' }
  const params = action.params && typeof action.params === 'object' && !Array.isArray(action.params) ? action.params : {}
  return resolve(params)
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
