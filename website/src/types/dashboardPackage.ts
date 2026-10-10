/**
 * The browser's view of a `kind="dashboard"` artifact package.
 *
 * WRITTEN FROM `dashboardPackage.schema.json` beside this file, which is
 * `kiro_crew.artifact_store.dashboard_package.package_json_schema()`'s own
 * output -- a document the generator builds out of the live catalogs
 * (`data_type_catalog`, `view_block_catalog`, `fold_names`) rather than a
 * hand-kept copy beside them. So the chain is one direction with no second
 * spelling anywhere on it:
 *
 *     python catalogs  ->  package_json_schema()  ->  the .json here  ->  this file
 *
 * and `dashboardPackageTypes.test.ts` pins BOTH joins: every union below
 * against the schema's enum, and the schema's enums against the literal names
 * in the python source. A type added to a catalog with no edit here reddens it,
 * which is the only reason these declarations can be trusted at all -- a TS
 * union is erased at runtime and would otherwise drift silently forever.
 *
 * The package is LAYOUT ONLY. No value ever lives in it: values stay in the
 * crew log and reach the page through the fold / bus / controller path, which
 * is why nothing here has a `value` field and why a dashboard artifact versions
 * only when `model` / `view` / `theme` change.
 */

/** Admissible `model.types[*].type`. The schema's enum, which IS `data_type_catalog()`. */
export const DASHBOARD_FIELD_TYPES = ['bool', 'enum', 'number', 'text', 'timestamp'] as const
export type DashboardFieldType = (typeof DASHBOARD_FIELD_TYPES)[number]

/** Admissible `view.blocks[*].type`. The schema's enum, which IS `view_block_catalog()`. */
export const DASHBOARD_BLOCK_TYPES = [
  'bars',
  'gauge',
  'list',
  'note',
  'orbit',
  'pills',
  'stat',
  'stat_band',
  'table',
  'timeline',
] as const
export type DashboardBlockType = (typeof DASHBOARD_BLOCK_TYPES)[number]

/** Admissible `source.fold`. The schema's enum, which IS `fold_names()` -- the
 *  crew-log folds, so a field bound to one of these is read from the log and no
 *  agent can write it. */
export const DASHBOARD_FOLD_NAMES = [
  'agentic',
  'approvals',
  'class',
  'ledger',
  'mistakes',
  'panel',
  'radar',
  'status',
  'subagents',
  'timeline',
  'tools',
  'usage',
  'work',
  'workstreams',
] as const
export type DashboardFoldName = (typeof DASHBOARD_FOLD_NAMES)[number]

/**
 * Where a field's value comes from, and the one discriminator on this whole
 * type that a reader's trust depends on.
 *
 * A `fold` source is a number the gateway read out of the crew log; an
 * `agentic` one is a value the crewmate asserted about itself. The schema makes
 * `source` required for exactly this reason: without it a consumer could only
 * guess, and the sole available guess (`agentic`) would mark every field on
 * every page as the crewmate's own claim.
 */
export type DashboardFieldSource =
  | { fold: DashboardFoldName; path: string }
  | { agentic: true }

/**
 * One declared field of the Model.
 *
 * The index signature is not laziness: the schema deliberately leaves a field
 * object OPEN (no `additionalProperties: false`), because the per-type keys are
 * the catalog's -- `unit` and `precision` on a number, `choices` on an enum,
 * `max_len` on text -- and the catalog is python's. Declaring them here would
 * be a third spelling of a table the schema does not carry and nothing in the
 * browser reads: the blocks are composed server-side, so those keys never reach
 * this code. Open here means open there, which is the property the test pins.
 */
export interface DashboardModelField {
  type: DashboardFieldType
  source: DashboardFieldSource
  label?: string
  description?: string
  [catalogKey: string]: unknown
}

/** `model`: field name -> field shape. The page may render these and nothing else. */
export interface DashboardModel {
  types: Record<string, DashboardModelField>
}

/**
 * One placed block of the View.
 *
 * `fields` is doing two jobs at once, which is worth saying because it is the
 * whole of the push protocol's addressing: it is what the block RENDERS, and it
 * is the block's SUBSCRIPTION set on the crew-log bus. A fold advancing is
 * pushed to the blocks whose `fields` name a field that fold sources, and to no
 * others.
 *
 * Open for the same reason a field is: `span` and its siblings are the block
 * catalog's keys, read by the server-side renderer and never here.
 */
export interface DashboardViewBlock {
  id: string
  type: DashboardBlockType
  fields: string[]
  title?: string
  [catalogKey: string]: unknown
}

/** `view`: the blocks, in the order the page lays them out. */
export interface DashboardView {
  blocks: DashboardViewBlock[]
}

/** `theme`: CSS custom properties, and optionally the package's own stylesheet.
 *  Style is not hard-coded in the product, so these travel with the layout. */
export interface DashboardTheme {
  tokens: Record<string, string>
  css?: string
}

/** The content of a `kind="dashboard"` artifact. */
export interface DashboardPackage {
  kind: 'dashboard'
  /** `crewmate:<slug>` or `session:<slot key>`. Outside the layout fingerprint:
   *  a rebind is not a layout change and creates no version. */
  bound_to: string
  model: DashboardModel
  view: DashboardView
  theme: DashboardTheme
}

/** `bound_to`'s grammar, as the schema spells it. */
export const DASHBOARD_BOUND_TO_PATTERN = '^(?:crewmate|session):[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
/** A model field NAME's grammar. */
export const DASHBOARD_FIELD_NAME_PATTERN = '^[a-z][a-z0-9_]{0,63}$'
/** A view block ID's grammar. */
export const DASHBOARD_BLOCK_ID_PATTERN = '^[a-z][a-z0-9_-]{0,63}$'
/** A theme token NAME's grammar: a CSS custom property, so it opens with `--`. */
export const DASHBOARD_THEME_TOKEN_PATTERN = '^--[a-z][a-z0-9-]{0,63}$'

/** The schema's own bounds, so a reader of a package can tell a refusal from a bug. */
export const DASHBOARD_MAX_MODEL_FIELDS = 64
export const DASHBOARD_MAX_VIEW_BLOCKS = 48
export const DASHBOARD_MAX_THEME_TOKENS = 64

/**
 * WHY THERE IS NO READER FUNCTION HERE.
 *
 * Nothing in the browser parses package CONTENT, so a `isDashboardPackage`
 * guard would ship dead. The dashboard read hands the page a DESCRIPTOR
 * (`DashboardPackageRef`: the artifact's slug, version, layout fingerprint and
 * binding) plus the document the server composed from the layout and the block
 * values it resolved -- so `model`, `view` and `theme` never cross to this side.
 *
 * These declarations are still the contract and still load-bearing, for two
 * reasons that do not need a function:
 *
 * 1. `Artifact['kind']` now includes `'dashboard'`, so the Artifacts library
 *    lists, versions and reverts these records -- and this is the type of what
 *    is inside one.
 * 2. The constants are what `dashboardPackageTypes.test.ts` pins against the
 *    schema and the python catalogs. That pin is the thing a future reader of
 *    package content inherits: by the time somebody needs to parse one, the
 *    vocabulary is already known to be current rather than a year-old copy.
 */
