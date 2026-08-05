import {
  AlertTriangle, AlertCircle, Wrench, Sparkle,
} from 'lucide-react'
import type { SevInfo, SeverityKey, Report, Screen, Blocked } from './types'
import { i18nT } from '../../i18n/t'

/**
 * ## Why the UI copy below sits behind GETTERS
 *
 * Every table in this module is evaluated ONCE, at import. A bare
 * `i18nT('…')` in a property position would therefore resolve against whatever
 * language happened to be active at boot and never re-resolve when the user
 * switches language — the frozen-at-import bug `lib/effort.ts` documents.
 *
 * `effort.ts` solves it by storing catalog KEYS and resolving in an exported
 * function. That shape is unavailable here: every consumer of these tables reads
 * the value POSITIONALLY (`list[i].label`, `KIND_WAIT[kind]`,
 * `sevOf(sev).label`), so moving the lookup into a new resolver would mean
 * rewriting four other modules. A getter puts the `i18nT()` call on the property
 * ACCESS instead, which is what already happens per render — same guarantee, no
 * change to any call site.
 *
 * `i18nT('<literal>')` inside the getter keeps `scripts/check-i18n-keys.mjs`
 * able to resolve the key statically, which is the property that makes a key
 * verifiable at all.
 *
 * One caveat, deliberate: `DesignCritiquePage` SPREADS a `BLOCKED` entry into
 * React state (`{ ...(BLOCKED[reason] || BLOCKED.other), detail }`). A spread
 * invokes getters, so that copy is resolved once and then held — an open blocked
 * screen keeps its boot-language text across a language switch until the run is
 * retried. See `blockedFor()` below for the one-line fix that closes it.
 */

// The core agent, not a bundled one. A builtin's declared `agents` are never
// registered (see the CRITIC note in prompts.ts), so naming `design-critic` here
// would fail every request; the persona travels with the prompt instead.
export const AGENT = 'kirocrew'
export const HKEY = 'dc-history-v1'
export const JOBKEY = 'dc-current-job'
// Every slot we create, until we've deleted it. Only ONE job record is kept, so
// without this list a slot abandoned at the scoping step is forgotten forever and
// lingers in the session list as a stray "design-critic" conversation.
export const SLOTSKEY = 'dc-open-slots-v1'
// Slots with a run currently in flight. Distinct from SLOTSKEY (every slot we
// ever opened, which is what the reaper sweeps): the reaper must spare EVERY
// live run, not just the one in JOBKEY, or starting a second critique orphans
// the first. Entries carry a timestamp so a tab closed mid-run cannot keep a
// slot un-reapable forever.
export const LIVEKEY = 'dc-live-runs-v1'
export const LIVE_TTL_MS = 30 * 60 * 1000

export const RAIL_W = '440px'
export const MAX_SCREENS = 20
// Backstop only — a job past this is stuck. Measured against Date.now(), never a
// tick count (see hooks/poll logic).
export const HARD_CAP_MS = 15 * 60 * 1000

/**
 * Severity display metadata, keyed by the LOWERCASE discriminant the critic
 * emits (`'major'`), which is a protocol value and stays as it is. Only `label`
 * — the NN/g severity name shown on the pill and the tally chip — is copy.
 */
export const SEV: Record<SeverityKey, SevInfo> = {
  catastrophe: { get label() { return i18nT('apps.designCritique.constants.sev_catastrophe') }, rank: 0, color: '#e5484d', icon: AlertTriangle },
  major:       { get label() { return i18nT('apps.designCritique.constants.sev_major') },       rank: 1, color: '#f5a623', icon: AlertCircle },
  minor:       { get label() { return i18nT('apps.designCritique.constants.sev_minor') },       rank: 2, color: '#e2c541', icon: Wrench },
  cosmetic:    { get label() { return i18nT('apps.designCritique.constants.sev_cosmetic') },    rank: 3, color: 'var(--muted, #8a8f98)', icon: Sparkle },
}
export const sevOf = (s: string | undefined): SevInfo => SEV[(s as SeverityKey)] || SEV.cosmetic

/**
 * The target kind, in ENGLISH. **Do not localise this table.**
 *
 * `prompts.ts` imports it and splices it into the discovery prompt ("I need to
 * know what screens are IN this <kind>"), which is English throughout. A
 * localised value here would put one non-English noun inside an English
 * instruction — worse than leaving it alone, because the model then has to guess
 * which language the reply should be in. The repo already treats prompt text as
 * non-copy: `eslint.i18n.config.js` ignores `src/apps/<app>/prompts.ts` outright, and
 * the strict config derives its exemptions from that same array.
 *
 * The right home for this map is therefore `prompts.ts` itself, next to its only
 * legitimate consumer — that move is left to whoever owns that file, since the
 * three UI call sites listed on `kindLabel()` have to move over in the same
 * change.
 */
export const KIND_LABEL: Record<string, string> = {
  figma: 'Figma file', repo: 'repo', local: 'local code', url: 'running app',
}

/**
 * The same four kinds as catalog keys, for the places the kind is SHOWN rather
 * than sent to the model. Split from `KIND_LABEL` rather than replacing it
 * because the two have genuinely different requirements: one must stay English,
 * the other must follow the UI language.
 */
export const KIND_LABEL_KEY: Record<string, string> = {
  figma: 'apps.designCritique.constants.kind_figma',
  repo: 'apps.designCritique.constants.kind_repo',
  local: 'apps.designCritique.constants.kind_local',
  url: 'apps.designCritique.constants.kind_url',
}

/**
 * Localised name for a target kind. Returns '' for an unknown kind so callers
 * keep their existing `|| fallback` behaviour.
 *
 * `hasOwnProperty`, not `in`: `kind` arrives from `detectKind()` and from a
 * persisted job record, so a stored `'toString'` would otherwise resolve to an
 * Object.prototype member and hand a function to i18next.
 *
 * NOT yet wired — the three UI call sites still read `KIND_LABEL` directly, and
 * two of them are string concatenations that this function alone cannot fix:
 *
 *   - `Composer.tsx:143`   `{KIND_LABEL[kind] || ''}` — a bare render; swap to
 *                          `kindLabel(kind)` and it is done.
 *   - `Composer.tsx:41`    `'Critique ' + (KIND_LABEL[kind] || 'this')`
 *   - `DesignCritiquePage.tsx:1008`
 *                          `'A ' + (KIND_LABEL[kind] || 'design') + ' — …'`
 *
 * The last two need a WHOLE-SENTENCE key with the kind interpolated
 * (`i18nT('…critique_kind', { kind: kindLabel(kind) })`). Dropping a localised
 * noun into those English fragments would produce a half-translated sentence and
 * hard-code English word order.
 */
export function kindLabel(kind: string | undefined): string {
  if (!kind) return ''
  return Object.prototype.hasOwnProperty.call(KIND_LABEL_KEY, kind)
    ? i18nT(KIND_LABEL_KEY[kind])
    : ''
}

// The stages a critique actually goes through, in SOP order. The last one is
// driven by a real signal (the critic has started replying); the rest advance on
// elapsed time, so they read as "what I'm doing now", not a verified percentage.
//
// `at` is a threshold in seconds, not copy. `label` is read inside
// `WaitingScreen`'s `list.map()` callback, so the getter fires per render.
export const STAGES: Array<{ at: number; label: string }> = [
  { at: 0,  get label() { return i18nT('apps.designCritique.constants.stage_getting_pixels') } },
  { at: 8,  get label() { return i18nT('apps.designCritique.constants.stage_reading_screens') } },
  { at: 22, get label() { return i18nT('apps.designCritique.constants.stage_checking_hierarchy') } },
  { at: 45, get label() { return i18nT('apps.designCritique.constants.stage_weighing_task') } },
  { at: 70, get label() { return i18nT('apps.designCritique.constants.stage_double_checking') } },
]
export const WRITING_STAGE = { get label() { return i18nT('apps.designCritique.constants.stage_writing_up') } }
export const SCAN_STAGES: Array<{ at: number; label: string }> = [
  { at: 0,  get label() { return i18nT('apps.designCritique.constants.scan_finding_screens') } },
  { at: 6,  get label() { return i18nT('apps.designCritique.constants.scan_which_renderable') } },
  { at: 14, get label() { return i18nT('apps.designCritique.constants.scan_grouping_flows') } },
]

// "I couldn't get in" is a different problem from "I got in and found nothing".
// Each cause has a different fix, so name it and pre-load the way forward.
//
// `fix` ('local' | 'retype' | 'shots' | 'retry') is the discriminant the error
// screen switches on, never rendered — it stays a bare literal. `auth.cmds` is a
// copy-paste block, so it is MIXED: the shell commands are left verbatim (a
// translated command does not run) while the prose in it is not.
export const BLOCKED: Record<string, Blocked> = {
  'no-access': {
    get say() { return i18nT('apps.designCritique.constants.blocked_no_access_say') },
    fix: 'local',
    get hint() { return i18nT('apps.designCritique.constants.blocked_no_access_hint') },
    auth: {
      get lead() { return i18nT('apps.designCritique.constants.blocked_no_access_auth_lead') },
      // Not a getter, and deliberately byte-identical to the base line: every
      // entry is a shell command or a shell comment inside a copy-paste block,
      // so nothing here is translatable and the line needs no lookup.
      cmds: ['gh auth login', '# or, to use the macOS keychain:', 'git config --global credential.helper osxkeychain'],
      get tail() { return i18nT('apps.designCritique.constants.blocked_no_access_auth_tail') },
    },
  },
  'not-found': {
    get say() { return i18nT('apps.designCritique.constants.blocked_not_found_say') },
    fix: 'retype',
    get hint() { return i18nT('apps.designCritique.constants.blocked_not_found_hint') },
  },
  'figma-app-missing': {
    get say() { return i18nT('apps.designCritique.constants.blocked_figma_app_missing_say') },
    fix: 'shots',
    get hint() { return i18nT('apps.designCritique.constants.blocked_figma_app_missing_hint') },
  },
  'figma-file-closed': {
    get say() { return i18nT('apps.designCritique.constants.blocked_figma_file_closed_say') },
    fix: 'retry',
    get hint() { return i18nT('apps.designCritique.constants.blocked_figma_file_closed_hint') },
  },
  'figma-no-permission': {
    get say() { return i18nT('apps.designCritique.constants.blocked_figma_no_permission_say') },
    fix: 'shots',
    get hint() { return i18nT('apps.designCritique.constants.blocked_figma_no_permission_hint') },
    auth: {
      get lead() { return i18nT('apps.designCritique.constants.blocked_figma_no_permission_auth_lead') },
      // Rendered in the same command block as the case above, but these are
      // numbered INSTRUCTIONS, not commands — all three are copy. The number
      // stays inside each string so the list cannot renumber itself per locale.
      get cmds() {
        return [
          i18nT('apps.designCritique.constants.blocked_figma_no_permission_auth_step_1'),
          i18nT('apps.designCritique.constants.blocked_figma_no_permission_auth_step_2'),
          i18nT('apps.designCritique.constants.blocked_figma_no_permission_auth_step_3'),
        ]
      },
      get tail() { return i18nT('apps.designCritique.constants.blocked_figma_no_permission_auth_tail') },
    },
  },
  other: {
    get say() { return i18nT('apps.designCritique.constants.blocked_other_say') },
    fix: 'shots',
    get hint() { return i18nT('apps.designCritique.constants.blocked_other_hint') },
  },
}

/**
 * The way-forward copy for a blocked reason, RESOLVED.
 *
 * `DesignCritiquePage` currently spreads the raw entry
 * (`{ ...(BLOCKED[reason] || BLOCKED.other), detail }`), and a spread invokes the
 * getters above — so the copy it stores in state is fixed at the moment the run
 * failed and will not follow a later language switch. Calling this instead is the
 * same one line and re-resolves on every render:
 *
 *     setBlocked({ ...blockedFor(info.blocked.reason), detail: … })
 *
 * (That still resolves at spread time; the fix is to call `blockedFor()` in the
 * error screen's render rather than to store the resolved copy in state. Both are
 * strictly better than the frozen literals this replaced.)
 */
export function blockedFor(reason: string | undefined): Blocked {
  const entry = reason && Object.prototype.hasOwnProperty.call(BLOCKED, reason)
    ? BLOCKED[reason]
    : BLOCKED.other
  return { ...entry, ...(entry.auth ? { auth: { ...entry.auth } } : {}) }
}

// Read as `KIND_WAIT[pendingKind]` in WaitingScreen's render body, so a getter
// per entry resolves per render and the `Record<string, string>` shape the caller
// indexes into is unchanged.
export const KIND_WAIT: Record<string, string> = {
  get figma() { return i18nT('apps.designCritique.constants.wait_figma') },
  get repo() { return i18nT('apps.designCritique.constants.wait_repo') },
  get local() { return i18nT('apps.designCritique.constants.wait_local') },
  get url() { return i18nT('apps.designCritique.constants.wait_url') },
}

// The built-in example is a real 4-screen checkout flow, captured from a real
// (small) site, so "See an example" demonstrates flow mode rather than one screen.
// Every box below is MEASURED from the rendered page, not estimated. Images live
// under the dashboard's public assets (copied by the build).
//
// ## Why nothing from here down is localised
//
// These two constants fill MODEL-OUTPUT slots. `showReport(SAMPLE_REPORT, …)` is
// the same entry point a finished run uses, and `FindingRow` renders `title`,
// `category`, `location`, `evidence`, `fix` and `rules` verbatim inside localised
// chrome ("Where", "What I saw", "Based on"). A real critique arrives in English —
// `prompts.ts` carries no language directive and pins the NN/g severity names — so
// translating the sample would make it the only localised report in the app and
// leave History rendering a Chinese example directly above English real ones.
//
// The content is also welded to the English PIXELS of the bundled screenshots:
// the evidence quotes button labels out of them ("Continue (dark) → Next step
// (blue) → Pay $130 (green)", 'Only Phone is annotated, as "Optional"'), and
// `SAMPLE_SCREENS[].label` captions frames whose own headings read Cart /
// Shipping / Payment / Confirmation. A translated caption over an untranslated
// screenshot breaks the evidence link the example exists to demonstrate.
//
// `FindingRow.tsx:63` also branches on `/accessib/i.test(f.category)`, an English
// regex over a category value — latent for these five categories, but it confirms
// the field is treated as an English token rather than copy.
//
// Localising the example is therefore a product decision (it depends on whether
// the critic prompt gains a language directive first), not a mechanical i18n fix.
// Reported to the shard parent rather than converted. See shard-02.json `notes`.
const SAMPLE = (name: string) => '/app-assets/design-critique/samples/' + name + '.png'
export const SAMPLE_SCREENS: Screen[] = [
  { step: 1, label: 'Cart', url: SAMPLE('1-cart') },
  { step: 2, label: 'Shipping', url: SAMPLE('2-shipping') },
  { step: 3, label: 'Payment', url: SAMPLE('3-payment') },
  { step: 4, label: 'Confirmation', url: SAMPLE('4-confirm') },
]
export const SAMPLE_REPORT: Report = {
  overallRead: 'The checkout gets the job done, but it loses momentum in the middle — the main button changes colour and label at every step, and nothing tells you how far along you are.',
  health: 'Promising, needs work',
  tally: { catastrophe: 0, major: 2, minor: 2, cosmetic: 1 },
  screens: SAMPLE_SCREENS.map(s => ({ step: s.step, label: s.label, path: '' })),
  findings: [
    { severity: 'major', scope: 'flow', steps: [1, 2, 3, 4], title: 'The primary button changes colour and label on every step', category: 'Consistency', location: 'Bottom action row, all four steps', evidence: 'Continue (dark) → Next step (blue) → Pay $130 (green). Three colours, three labels, and it moves from right-aligned to left-aligned on step 2.', fix: 'Consider one primary style for the whole flow and one verb pattern — "Continue to shipping", "Continue to payment", "Pay $130" — keeping its position fixed.', rules: ['Nielsen: consistency & standards', 'Gestalt: common fate', 'Fitts’s Law: moving target'], box: null },
    { severity: 'major', scope: 'flow', steps: [1, 2, 3, 4], title: 'No sense of progress — how many steps are left?', category: 'System status', location: 'All four steps', evidence: 'Nothing on any screen says step 2 of 4, so the length of the flow is unknowable until it ends.', fix: 'Consider a small step indicator in the header, or naming the next step in the button.', rules: ['Nielsen: visibility of system status'], box: null },
    { severity: 'minor', scope: 'flow', steps: [1, 2], title: 'No way back until step 3', category: 'User control', location: 'Steps 1 and 2', evidence: 'Payment is the first screen with a Back button; before that the only exit is the browser.', fix: 'You might add a consistent Back on every step after the first.', rules: ['Nielsen: user control & freedom'], box: null },
    { severity: 'minor', scope: 'screen', steps: [2], title: 'Required fields aren’t marked', category: 'Content', location: 'Address form labels', evidence: 'Only Phone is annotated, as "Optional" — so the other five read as ambiguous rather than required.', fix: 'Mark the optional one and leave the rest plain, or mark required fields explicitly. Pick one convention.', rules: ['Content Design: clear', 'Nielsen: error prevention'], box: { x: 0.182, y: 0.211, w: 0.636, h: 0.02 } },
    { severity: 'cosmetic', scope: 'screen', steps: [4], title: 'The confirmation is a dead end', category: 'Usability', location: 'Below the delivery estimate', evidence: 'The final screen has no onward action — no order detail, no continue shopping.', fix: 'You might offer one clear next step so the flow ends on an action rather than a full stop.', rules: ['Peak-end rule', 'Shneiderman: closure'], box: { x: 0.156, y: 0.074, w: 0.689, h: 0.514 } },
  ],
  keep: ['Single-column forms with generous field spacing — easy to move through.', 'The cart states the total plainly, with no surprise fees later in the flow.'],
  couldNotSee: ['Validation and error states (none were reachable in the captured pages).'],
}
