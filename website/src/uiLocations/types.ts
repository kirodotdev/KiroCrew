/**
 * The shape of a registered UI location descriptor (see `./descriptors.ts` and
 * the per-area files in `./areas/`). Types only: the area files import them
 * without importing each other or the aggregator.
 */
import type { UiConditionId, UiRevealState } from './conditions'

/** What must hold before a person can see the location. Facts, not proof that they hold. */
export type UiRequirement =
  /**
   * Another registered location opens or reveals this one first. With `when`,
   * only while that state holds; otherwise this location is already visible.
   * The revealing control's own requirements travel with the step.
   */
  | { kind: 'shown_by'; location: string; when?: UiRevealState }
  | { kind: 'viewport'; value: 'desktop' | 'mobile' }
  /** A preview flag from `utils/previewFlags.ts`; its enabler is `PREVIEW_FLAG_ENABLERS`. */
  | { kind: 'preview_flag'; flag: string }
  /** A named runtime condition from the shared table in `./conditions.ts`. */
  | { kind: 'condition'; id: UiConditionId }

/** Shared fields of every placement. */
interface UiPlacementBase {
  /**
   * How the person reaches it from the parent (or, on the shell, where it
   * sits). `content` is the page body itself, such as an empty state.
   */
  entry: 'rail' | 'tab' | 'sidebar' | 'menu' | 'toolbar' | 'header' | 'content' | 'direct-link'
  requires?: readonly UiRequirement[]
}

/** A placement inside one page: under a parent location, at that page's route. */
export interface UiPagePlacement extends UiPlacementBase {
  /** The built-in surface (rail destination) the control is drawn on. */
  surface: string
  /** The location directly above it in the path: a page, tab, setting or registered id. */
  parent: string
  /**
   * Canonical route; defaults to the parent's. Must be a route the router
   * renders, not a redirect: the generator checks it against App.tsx.
   */
  route?: string
  /**
   * Which of the parent's placements this one hangs under, by index, when more
   * than one is compatible (same surface, no conflicting viewport). The chosen
   * parent placement's requirements are inherited.
   */
  parentPlacement?: number
}

/**
 * A placement in the dashboard shell (the top bar, the phone menu), drawn over
 * every page. It has no route of its own: the index marks it `on_every_page`.
 * It may hang under another registered shell location (a row of the phone menu
 * under the menu button), inheriting that placement's path and requirements;
 * never under a page. Only for chrome the app shell renders outside any page; a
 * control a page draws is a {@link UiPagePlacement} even when several pages draw one.
 */
export interface UiShellPlacement extends UiPlacementBase {
  surface: 'shell'
  /** Another shell location this one is reached through, if any. */
  parent?: string
  route?: never
  parentPlacement?: number
}

export type UiPlacement = UiPagePlacement | UiShellPlacement

/**
 * Curated search words per shipped locale code ('en', 'zh-CN', …): what a
 * newcomer types, not the label (which is searched already). Only add a locale
 * whose wording differs from English.
 */
export type UiSearchTerms = Readonly<Record<string, readonly string[]>>

export interface UiLocationDescriptor {
  /** `list`: a picker a guide's selection step points at (`UI_SELECTION_SCOPES`). */
  kind: 'button' | 'toggle' | 'disclosure' | 'menu-item' | 'link' | 'field' | 'tab' | 'list'
  /**
   * Where the label comes from at the render site. `text` (the default) is the
   * element's visible text minus nested controls; `attr` reads one attribute
   * (`aria-label`, `title`, or a forwarded `label` prop). `key` picks one key
   * when the site can render several (a conditional, or icon + text).
   *
   * `description` is for a control whose label is runtime data (the model chip
   * shows the session's model name): the site's text, or `attr`, must really be
   * dynamic, and `key` names a static catalog key under
   * `uiLocations.description.` saying what the control IS. find_ui returns it
   * as `description`, never as the exact on-screen label. Every shipped locale
   * must carry the key.
   */
  label?:
    | { from: 'text'; key?: string }
    | { from: 'attr'; attr: string; key?: string }
    | { from: 'description'; key: `uiLocations.description.${string}`; attr?: string }
  /** Extra catalog keys a person may search by (the accessible name, a synonym). */
  aliasKeys?: readonly string[]
  /**
   * For one element whose label flips with a state (Switch to board view /
   * Switch to list view): which label is on screen in which state. Every entry
   * names the label key or one of `aliasKeys`, and the label key must be one of
   * them. find_ui returns them as `label_by_state`, so the agent quotes the label
   * the person actually sees, never the other state's. `when` is a condition or
   * a reveal state from `./conditions.ts`.
   */
  stateLabels?: readonly { key: string; when: UiConditionId | UiRevealState }[]
  /** Newcomer words for this control; see {@link UiSearchTerms}. */
  terms?: UiSearchTerms
  /** Alternatives, never one concatenated path: each is a separate way to reach it. */
  placements: readonly UiPlacement[]
  /**
   * An approved guide action. Opt-in only; a marker never implies guidance.
   * `false` keeps the location out of the generic `ui.show` guide too, which
   * otherwise covers every eligible curated location (see `guidePlanFor` in
   * scripts/lib/ui-index.mjs).
   */
  guide?: false | { action: string; params?: Record<string, string> }
  /**
   * For a destructive control (`guide_policy: caution`): the catalog key of the
   * page's own text saying what pressing it removes and keeps. The guide panel
   * shows it in place of the generic caution line, and find_ui returns it as
   * `caution_text`, so neither the panel nor the agent describes a consequence
   * the product does not state.
   */
  cautionKey?: string
}

/**
 * One step of a `ui.show` plan: a registered location the guide points at.
 * `when` names the reveal state under which the step is needed; whenever a
 * LATER step's control is already on screen, the step is skipped.
 */
export interface UiGuidePlanStep {
  /**
   * Unique within the plan: `<placement id>:<location>`, `<placement
   * id>:select:<selection>` or `<placement id>:gate:<gate>`. Progress reports
   * name it; the part after the placement id is the step's key, shared by
   * every placement that walks the same step (a re-plan compares keys).
   */
  id: string
  /**
   * `select`: point at the picker `location` until the page reports
   * `selection` (a `UI_SELECTION_SCOPES` id) holds. `gate`: pass once `gate`
   * (a `UI_GATES` id or `preview_flag:<flag>`) is on, else pause on a blocker
   * naming `setting_id`. Absent: point at `location`.
   */
  kind?: 'select' | 'gate'
  /** The control pointed at; a gate step points at nothing and has none. */
  location?: string
  label_key?: string
  selection?: string
  entity?: string
  gate?: string
  /** The settings registry id that turns the gate on, when there is one. */
  setting_id?: string
  when?: string
  /**
   * The compiled reveal scope this step opens (`GUIDE_REVEAL_SCOPES`): the
   * step is done once that scope reports open. Every step but the last has one.
   */
  scope?: string
  /**
   * Runtime predicates (`UI_RUNTIME_PREDICATES`) the step's control needs to
   * be drawn: unmet, the guide shows a blocker instead of pointing.
   */
  requires?: readonly string[]
  /**
   * The last step's control is destructive (`guide_policy: caution`): the
   * panel says what it removes is permanent, and a press on the control alone
   * never finishes the step (its own confirm, or Done, does).
   */
  caution?: true
  /** With `caution`: the location's `cautionKey`, the page's own words for what it removes. */
  caution_key?: string
}

/** The steps for one placement: the page to open (`null`: shell chrome, the page the person is on) and the controls in order. */
export interface UiGuidePlanPlacement {
  /**
   * The placement's id: its viewport, `any`, or for an auto control several
   * pages share, `pa`, `pb`, ... one per page (picked by the page the person
   * is on). The gateway records the one a tab claims with.
   */
  id: string
  route: string | null
  viewport?: 'desktop' | 'mobile'
  steps: readonly UiGuidePlanStep[]
}

/** A generated `ui.show` plan (`guidePlans.gen.ts`), version 2: placements may differ in length. */
export interface UiGuidePlan {
  version: 2
  label_key: string
  placements: readonly UiGuidePlanPlacement[]
}

/** One area file's table: location id -> descriptor. */
export type UiLocationArea = Readonly<Record<string, UiLocationDescriptor>>
