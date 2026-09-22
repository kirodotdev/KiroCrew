import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { resolveLegacyHighlightId } from '../../hooks/useSettingHighlight'
import { AlertTriangle, CircleDot } from 'lucide-react'

import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { Suspense, lazy } from 'react'
import { SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import {
  DECISIONS_API_KEY_SECRET,
  DECISIONS_BUCKET_PATH,
  DECISIONS_LANE_LLM,
  DECISIONS_MODEL_ROUTE_PATH,
  DECISIONS_NUDGE_WAKE_MODEL_PATH,
  DECISIONS_NUDGE_WAKE_POINT,
  DECISIONS_NUDGE_WAKE_PROVIDER_PATH,
  POINT_ACTIVE,
  POINT_NEEDS_SCOPE,
  readDecisions,
  readModelRoute,
  readNudgeWake,
  type DecisionPointRow,
} from './decisionsPreview'
import { fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/**
 * One point's own settings, on a lazy boundary.
 *
 * The CARD is mounted synchronously by `FeaturePreviewsSection`, because settings
 * search highlights a control by probing the DOM and gives up after 100 ms -- a deep
 * link that arrived while the card's chunk was in flight rang nothing. Only this
 * panel rides a boundary, and it can: nothing it draws is in the settings registry,
 * since every control in it takes its label from a map keyed by the SERVER's id.
 *
 * The fallback reserves height rather than collapsing to nothing, so opening a row
 * does not make the list jump while the chunk arrives, and it is `aria-hidden`
 * because an empty box is not a thing to announce -- the panel's own heading is.
 */
const DecisionsPointPanel = lazy(async () => ({
  default: (await import('./DecisionsPointPanel')).DecisionsPointPanel,
}))

/**
 * Decisions (Jev) — Settings › Developer › Feature Previews.
 *
 * ## Why it is a list and a detail
 *
 * This card used to be one column of prose: a switch, four paragraphs about what
 * leaves the machine, a second switch, an address, and one read-only row naming the
 * single thing Jev decided. Every point that shipped afterwards had to land in the
 * same column, so the card grew into a wall in which the reader's actual question —
 * "what does Jev decide for me, and is that one on" — was the hardest thing to find.
 *
 * So it takes the shape `AgentBackendTab` already uses for the same problem: the
 * POINTS are a list, one row each, and the panel beside it belongs to whichever row
 * is highlighted. Exactly one panel is ever rendered. A row carries the plain-words
 * name, one line of what it decides, and a status chip; everything a reader might
 * change about that point lives in its own panel, so the card is scannable at the
 * top level and complete one level down.
 *
 * Highlighting a row changes the panel and nothing else — no row is a switch. That
 * separation is what lets a point that is NOT running still get a row, which is the
 * state a reader most needs to be able to read about.
 *
 * ## Where the list comes from, and why not from here
 *
 * `GET /api/decisions/consent` projects one row per point the GATEWAY ships, from
 * the seam's own registry (`decisions/gate.py`). This file holds a LABEL per id and
 * no array of ids, exactly as `AgentBackendTab` holds a label per capability: a
 * build that ships another point lights up a row with no edit here, and a point this
 * build has no label for renders under its own identifier rather than vanishing. An
 * array written here would be a second registry, and the two would diverge silently
 * — the failure that makes a card claim a decision the gate will never make.
 *
 * `status` is the SERVER's verdict for the same reason `permits` is. It is the
 * EFFECTIVE answer, never a switch position: `off` covers every reason nothing is
 * sent, and `needs_scope` is its own value because the fix for it is a switch on the
 * point's own panel, not the main switch the reader already turned on.
 *
 * NO NUMBER rides a row, and that is a gap rather than a decision about layout: this
 * gateway keeps a decision LOG (`decisions/log.py`) but ships no reader for it, so
 * there is no agree rate and no turn count to print. Counting rows here would mean
 * adding a log aggregator, which is a mechanism this card does not get to invent.
 * The row is designed with the space for it.
 *
 * ## The global block, and why its switch is not inside the disclosure
 *
 * Above the list sits what the whole seam shares: the main consent switch, where
 * decisions are sent, the credential, the sampling share and the prior-conversation
 * budget. The last four are behind a disclosure that is CLOSED by default, because a
 * reader opening this card is asking about points and not about a char budget.
 *
 * The SWITCH is not, and neither is the sentence saying what leaves the machine.
 * Consent to send conversation text off the machine is the one fact on this card a
 * reader must meet without looking for it, and `AgentBackendTab` states the rule this
 * follows: a fact behind a closed disclosure is a fact they do not see. So the
 * disclosure holds the settings and never the consent.
 *
 * ## What each value is written to, which is three different places
 *
 * - **Consent and its scopes** — the keystone `decisions_consent.json`, through
 *   `/api/decisions/consent`. Not a config path: `config.json` is writable by an
 *   auto-approved agent shell, so a switch there could be flipped by a
 *   prompt-injected agent (see `decisionsPreview.ts`).
 * - **The credential** — the dashboard SECRETS VAULT, as the one entry
 *   `TYPESAFE_API_KEY`. `provider.api_key` honours `secret://TYPESAFE_API_KEY` and
 *   nothing else, so no key is ever written to `config.json` and none is ever read
 *   back: the field shows set or unset, never a value.
 * - **Everything else** — `config.json`, through the config PATCH the settings page
 *   already uses. Those values grant nothing on their own: they can only narrow what
 *   consent allows, or name a model the provider must already advertise.
 *
 * A per-point SCOPE switch omits `enabled` deliberately (`api.saveDecisionsScope`).
 * A scope is not a review of an address, so it must not restate consent to one; the
 * gateway preserves the recorded switch for an absent field and refuses the write
 * unless consent is already in force for the address config names.
 *
 * ## Governance, and the one state that hides rather than explains
 *
 * `capabilities.decisions` is a fleet ceiling resolved server-side and reported as
 * `decisions_enabled` on `GET /api/dashboard/config`. A fleet that pinned the seam
 * off gets NO CARD — not a disabled one — because a ceiling is not a state the user
 * can act on. A FAILED read is not a withdrawal and must not be silent: the card
 * fades and says the read failed, because nothing has been denied and the reader can
 * retry.
 */

/** DOM ids, so each control and panel can name what describes it. */
const GLOBAL_PANEL_ID = 'decisions-global-panel'
const POINT_PANEL_ID = 'decisions-point-panel'
const EGRESS_NOTE_ID = 'decisions-egress-note'
const BACKEND_NOTE_ID = 'decisions-backend-note'
const BUCKET_INPUT_ID = 'decisions-bucket-slider'
/**
 * The registry entries that live INSIDE the shared block's disclosure.
 *
 * Settings search highlights a control and then strips the `highlight` param, so a
 * target behind a closed <details> is a link that reports success and shows nothing,
 * with no way to retry. These two open it. Resolved through the same legacy-id rewrite
 * the highlight hook applies, so a bookmark saved against an older label still lands.
 */
const DISCLOSED_SETTING_IDS = new Set([
  'developer.jev-api-key',
  'developer.earlier-conversation-one-decision-may-carry-in-characters',
])

/** The one notice any failed write on this card renders into. */
const SAVE_ERROR_TESTID = 'decisions-save-error'

/** A point id is dotted; a DOM id may not be, so one spelling of the swap. */
const domSafe = (id: string) => id.replace(/[^a-zA-Z0-9]+/g, '-')
const pointRowId = (id: string) => `decisions-point-row-${domSafe(id)}`
const pointStatusId = (id: string) => `decisions-point-status-${domSafe(id)}`

/** Masked stand-in for a stored credential; the value itself is never sent back. */
const SECRET_PREVIEW = '••••••••'

export function DecisionsCard() {
  const qc = useQueryClient()
  // Whether settings search is pointing at a control inside the shared block. Read
  // here rather than in an effect: the disclosure has to be open on the FIRST paint,
  // because the highlight hook probes the DOM and gives up.
  const [searchParams] = useSearchParams()
  const highlightTarget = searchParams.get('highlight')
  const disclosureTargeted =
    highlightTarget !== null &&
    DISCLOSED_SETTING_IDS.has(resolveLegacyHighlightId(highlightTarget))
  // `capabilities.decisions`, resolved server-side and reported by the endpoint the
  // dashboard already fetches. FAIL CLOSED on `=== true`: an absent field is an
  // older gateway or a read that has not landed, and neither is permission to offer
  // an egress switch.
  const dashCfgQ = useQuery<{ decisions_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })
  const governancePermits = dashCfgQ.data?.decisions_enabled === true
  const configQ = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const consentQ = useQuery({
    queryKey: ['decisionsConsent'],
    queryFn: () => api.getDecisionsConsent(),
    // A 404 is the answer "this gateway predates the keystone", not a transient
    // failure worth retrying: the card renders it as the update notice.
    retry: false,
  })
  // Names only — the vault never exposes a value, which is what lets this card show
  // set/unset without ever holding the credential.
  const secretsQ = useQuery<{ names?: string[] }>({
    queryKey: ['secrets'],
    queryFn: () => api.secretsList(),
  })
  const view = readDecisions(consentQ.data, configQ.data)
  const modelsQ = useAvailableModelsQuery()

  /* ── Which row the panel belongs to ──────────────────────────────────────── */
  // Not seeded from the rows: they arrive after first render, so a seed would pin
  // the highlight to a guess. `null` resolves to the first row below, and the
  // derived value cannot outlive a row the gateway stopped shipping.
  const [highlighted, setHighlighted] = useState<string | null>(null)
  const rows = view.points
  const ids = rows.map(r => r.id)
  const shown = highlighted !== null && ids.includes(highlighted) ? highlighted : (ids[0] ?? '')
  const shownRow = rows.find(r => r.id === shown)

  /* ── Reads that feed the panels ──────────────────────────────────────────── */
  const modelRoute = readModelRoute(configQ.data)
  // The judge point's own two values, read on the same terms as the tier map above.
  const nudgeWake = readNudgeWake(configQ.data)
  // INHERIT first, then every advertised id. `auto` is dropped because the empty
  // option already means inherit and two words for it would read as two behaviours.
  // A pinned id the backend no longer advertises is kept so a reader can switch off it.
  const judgeModelOptions = ['', ...modelsQ.data.map(m => m.name).filter(m => m !== 'auto')]
  if (nudgeWake.llmModel && !judgeModelOptions.includes(nudgeWake.llmModel)) {
    judgeModelOptions.splice(1, 0, nudgeWake.llmModel)
  }
  // The reviewed CEILING, off the keystone the switch writes. `config.json` carries
  // what the seam asks for and an agent may raise it; this is the number the gate
  // clamps to, so it is the one a reader may act on and the one the box edits.
  const historyBudget = view.historyBudget

  /* ── Drafts: the fields that commit on blur or a button, not on a keystroke ── */
  const [budgetDraft, setBudgetDraft] = useState<string | null>(null)
  // The slider's own position while a drag is in flight. Without it the thumb reads
  // the SAVED share on every tick, so it snaps back to where it started until each
  // refetch lands and the drag stutters -- and every tick in between is a config
  // write. The draft holds the position and one write goes out on release.
  const [bucketDraft, setBucketDraft] = useState<number | null>(null)
  const [apiKeyDraft, setApiKeyDraft] = useState('')
  const [apiKeyEditing, setApiKeyEditing] = useState(false)
  // Whether the trash has been pressed and the reader has not yet confirmed. A
  // separate flag from the write itself: removing a vault entry cannot be undone,
  // so the question has to be asked BEFORE the DELETE goes out.
  const [apiKeyConfirmRemove, setApiKeyConfirmRemove] = useState(false)

  /* ── Writes ──────────────────────────────────────────────────────────────── */
  // The address on screen travels with the click, so consent binds to what the owner
  // reviewed; the gateway refuses (409) if config moved it meanwhile and the refetch
  // then shows the new address. No `tool_args`: the route preserves a recorded scope
  // for an absent field, so flipping this switch keeps whatever the owner chose about
  // tool arguments.
  const consentMut = useMutation({
    mutationFn: (value: boolean) => api.saveDecisionsConsent(value, view.configuredEndpoint),
    // RETURNED, not just started: react-query holds a mutation pending only while
    // `onSettled` has an unresolved promise outstanding, and without the return the
    // switch re-enabled while `checked` still read the pre-flip value.
    onSettled: () => qc.invalidateQueries({ queryKey: ['decisionsConsent'] }),
  })
  // A point's own egress scope, with `enabled` OMITTED: see the file comment.
  const scopeMut = useMutation({
    mutationFn: ({ scope, value }: { scope: string; value: boolean }) =>
      api.saveDecisionsScope(scope, value),
    onSettled: () => qc.invalidateQueries({ queryKey: ['decisionsConsent'] }),
  })
  const configMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) =>
      api.patchConfig(path, value),
    onSettled: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })
  // The prior-conversation ceiling, on the keystone beside the switch. `enabled` is
  // omitted for the reason a scope omits it: raising a ceiling is not a review of an
  // address, so the gateway preserves the recorded switch and endpoint and refuses
  // the write unless consent already stands for the address config names.
  const budgetMut = useMutation({
    mutationFn: (chars: number) => api.saveDecisionsHistoryBudget(chars),
    onSettled: () => qc.invalidateQueries({ queryKey: ['decisionsConsent'] }),
  })
  const secretMut = useMutation({
    mutationFn: (value: string) => api.secretsSave(DECISIONS_API_KEY_SECRET, value),
    onSuccess: () => {
      setApiKeyDraft('')
      setApiKeyEditing(false)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['secrets'] }),
  })
  const secretDelMut = useMutation({
    mutationFn: () => api.secretsDelete(DECISIONS_API_KEY_SECRET),
    onSettled: () => {
      setApiKeyConfirmRemove(false)
      return qc.invalidateQueries({ queryKey: ['secrets'] })
    },
  })

  // One write per gesture, and none at all when the drag ended where it started.
  const commitBucket = () => {
    const next = bucketDraft
    setBucketDraft(null)
    if (next === null || next === bucket) return
    configMut.mutate({ path: DECISIONS_BUCKET_PATH, value: next })
  }

  /* ── States the card has to tell apart ───────────────────────────────────── */
  // "Old gateway" and "could not read the settings" are different facts and must not
  // share a sentence: the first is fixed by updating, the second by retrying.
  const backendMissing = consentQ.isError && isNotFoundError(consentQ.error)
  // Every read this card draws from, including the two auxiliary ones. The secrets
  // index is the load-bearing case: `apiKeySet` is derived from it, so a transient
  // failure renders a key that EXISTS as unset and then offers Save -- which is an
  // invitation to overwrite a credential the reader has just been told is absent. A
  // read that did not succeed must never present itself as "no key set", so its
  // failure joins the notice rather than resolving to a default.
  const secretsFailed = secretsQ.isError
  // The reads the SWITCH ITSELF stands on: the keystone it writes, the address that
  // write is bound to, and the governance answer. A failure in one of these means the
  // card cannot say what is recorded, so it must not offer to write it.
  const consentReadFailed =
    configQ.isError || dashCfgQ.isError || (consentQ.isError && !backendMissing)
  // Every read the card draws from anywhere. The vault index and the advertised model
  // list are AUXILIARY: they decide what the shared block and the tier pickers may
  // offer, and neither is needed to turn the seam off.
  // A 404 on the consent route is a READ that did not answer too, so it joins this
  // rather than rendering a note of its own outside the notice: one predicate, one
  // notice. Its MESSAGE differs, because "could not read, reload" is not true of it.
  const readFailed =
    consentReadFailed || secretsFailed || modelsQ.isError || backendMissing
  const loading = configQ.isLoading || consentQ.isLoading
  const frozen = loading || readFailed || !view.supported
  // The main switch is held ONLY by the reads it needs. Holding it on an auxiliary
  // failure left a consented seam sending with no way for its owner to withdraw --
  // the vault being unreachable is not a reason to keep conversation leaving the
  // machine. Every dependent control still rides `frozen`.
  const switchFrozen = loading || consentReadFailed || !view.supported
  const describedBy = [backendMissing ? BACKEND_NOTE_ID : '', EGRESS_NOTE_ID]
    .filter(Boolean)
    .join(' ')

  /* ── Labels, one per SERVER id, never one per authored sentence ───────────── */
  // Literal `i18nT('…')` calls in a flat record: `check-i18n-keys.mjs` resolves a
  // literal and cannot follow a key built at render time, which is also why these
  // are maps rather than a lookup into a nested catalog object.
  const POINT_NAME: Record<string, string> = {
    'skills.select': i18nT('pages.developer.featurePreviewsTab.decisions_point_skills_select'),
    'tool.risk': i18nT('pages.developer.featurePreviewsTab.decisions_point_tool_risk'),
    'message.steer': i18nT('pages.developer.featurePreviewsTab.decisions_point_message_steer'),
    'model.route': i18nT('pages.developer.featurePreviewsTab.decisions_point_model_route'),
    'compaction.keep': i18nT(
      'pages.developer.featurePreviewsTab.decisions_point_compaction_keep',
    ),
    'memory.recall': i18nT('pages.developer.featurePreviewsTab.decisions_point_memory_recall'),
    'nudge.wake': i18nT('pages.developer.featurePreviewsTab.decisions_point_nudge_wake'),
  }
  const POINT_WHAT: Record<string, string> = {
    'skills.select': i18nT('pages.developer.featurePreviewsTab.decisions_what_skills_select'),
    'tool.risk': i18nT('pages.developer.featurePreviewsTab.decisions_what_tool_risk'),
    'message.steer': i18nT('pages.developer.featurePreviewsTab.decisions_what_message_steer'),
    'model.route': i18nT('pages.developer.featurePreviewsTab.decisions_what_model_route'),
    'compaction.keep': i18nT(
      'pages.developer.featurePreviewsTab.decisions_what_compaction_keep',
    ),
    'memory.recall': i18nT(
      'pages.developer.featurePreviewsTab.decisions_what_memory_recall',
    ),
    'nudge.wake': i18nT('pages.developer.featurePreviewsTab.decisions_what_nudge_wake'),
  }
  const STATUS_WORD: Record<string, string> = {
    [POINT_ACTIVE]: i18nT('pages.developer.featurePreviewsTab.decisions_status_active'),
    [POINT_NEEDS_SCOPE]: i18nT('pages.developer.featurePreviewsTab.decisions_status_needs_scope'),
  }
  const smallModelJudgeWord = i18nT(
    'pages.developer.featurePreviewsTab.decisions_status_judged_by_small_model',
  )
  // A label per SCOPE, so a point that needs a new one draws its switch with no
  // per-point branch here.
  const SCOPE_LABEL: Record<string, string> = {
    tool_args: i18nT('pages.developer.featurePreviewsTab.decisions_tool_args'),
    compaction: i18nT('pages.developer.featurePreviewsTab.decisions_compaction'),
    memory_text: i18nT('pages.developer.featurePreviewsTab.decisions_memory_text'),
  }
  const SCOPE_DESC: Record<string, string> = {
    tool_args: i18nT('pages.developer.featurePreviewsTab.decisions_tool_args_desc'),
    compaction: i18nT('pages.developer.featurePreviewsTab.decisions_compaction_desc'),
    memory_text: i18nT('pages.developer.featurePreviewsTab.decisions_memory_text_desc'),
  }
  const TIER_LABEL: Record<string, string> = {
    simple: i18nT('pages.developer.featurePreviewsTab.decisions_tier_simple'),
    medium: i18nT('pages.developer.featurePreviewsTab.decisions_tier_medium'),
    complex: i18nT('pages.developer.featurePreviewsTab.decisions_tier_complex'),
  }
  // One label per provider word the judge accepts, keyed as the config spells it.
  const PROVIDER_LABEL: Record<string, string> = {
    auto: i18nT('pages.developer.featurePreviewsTab.decisions_judge_provider_auto'),
    jev: i18nT('pages.developer.featurePreviewsTab.decisions_judge_provider_jev'),
    llm: i18nT('pages.developer.featurePreviewsTab.decisions_judge_provider_llm'),
  }
  const inheritLabel = i18nT('pages.developer.featurePreviewsTab.decisions_tier_inherit')
  // The judge's own word for INHERIT. The tier label above says the SESSION's model,
  // which is what a tier's empty value keeps; the judge's empty value resolves the
  // judge agent's own model instead, so reusing that label would name the wrong one.
  const judgeInheritLabel = i18nT(
    'pages.developer.featurePreviewsTab.decisions_judge_model_inherit',
  )
  const offWord = i18nT('pages.developer.featurePreviewsTab.decisions_status_off')

  // The recorded answer PER SCOPE, keyed as the keystone spells it. A switch reading
  // a fixed field would show one point's consent on another point's panel, and on this
  // card the switch is the only state display for it. An unknown scope reads false, the
  // fail-closed direction: a build whose gateway names a scope this dashboard does not
  // know must not draw it as granted.
  const SCOPE_GRANTED: Record<string, boolean> = {
    tool_args: view.toolArgs,
    compaction: view.compaction,
    memory_text: view.memoryText,
  }

  const nameOf = (id: string) => POINT_NAME[id] ?? id
  // An unknown status reads as OFF: the fail-closed direction, since the alternative
  // is claiming a point runs on a word this build cannot read. The judge row is the
  // one row whose ACTIVE has two meanings, and the generic word is a contradiction
  // under the list's own "while this is on" heading when the switch is off -- so it
  // names the lane instead, and only when that lane is the one that would run.
  //
  // WHICH lane comes off the row. This side cannot work it out: `auto` resolves
  // against the Jev lane being ARMED for this point, which needs that point's own
  // evidence scope, and this card holds no reader for a scope the build may not
  // register -- so deriving it from the switch would print the generic word over a
  // small-model judge for the default provider.
  const statusWord = (row: DecisionPointRow) => {
    if (
      row.id === DECISIONS_NUDGE_WAKE_POINT &&
      row.status === POINT_ACTIVE &&
      row.lane === DECISIONS_LANE_LLM
    ) {
      return smallModelJudgeWord
    }
    return STATUS_WORD[row.status] ?? offWord
  }

  /* ── Keyboard: the list is one tab stop and the arrows move within it ────── */
  const moveHighlight = (delta: number) => {
    if (!ids.length) return
    const at = Math.max(0, ids.indexOf(shown))
    const next = Math.min(ids.length - 1, Math.max(0, at + delta))
    setHighlighted(ids[next])
  }

  // Withdrawn by governance: no card at all, not a disabled one. A failed read is not
  // a withdrawal and falls through to `readFailed`, which fades the card and renders
  // the notice (AUTOSDE `errors-use-error-notice`). Still-loading keeps hiding it: an
  // unanswered ceiling is no basis for offering an egress switch.
  if (!governancePermits && !dashCfgQ.isError) return null

  const apiKeySet = (secretsQ.data?.names ?? []).includes(DECISIONS_API_KEY_SECRET)
  // Whether the vault index is KNOWN, as opposed to absent-by-default. Every
  // credential control is gated on it: without a successful read the card cannot tell
  // "no key" from "could not ask", and the two want opposite offers.
  const apiKeyKnown = secretsQ.isSuccess && !secretsFailed
  // A removal in flight is the other reason to hold the credential controls: the
  // DELETE and a POST would otherwise be outstanding together, and a server free to
  // reorder them can land the delete last and erase the key just saved.
  const apiKeyBusy = !apiKeyKnown || secretMut.isPending || secretDelMut.isPending
  const bucket = view.bucket ?? 100

  return (
    <SettingsCard>
      {/* ── GLOBAL: what the whole seam shares ──────────────────────────────── */}
      {/* The consent switch. Held while a read has not succeeded -- a keystone nobody
          could read is no basis for a write against the value it holds -- and while a
          SCOPE write is in flight, for any scope and any point's panel: both land on
          the same keystone, so a scope flip followed immediately by a revoke here is
          two concurrent writes, and the scope one landing second would put back a
          consent the owner just withdrew. The condition names the scope mutation
          rather than one scope, so a point that gains one is covered without touching
          it.

          The reasoning lives here and not among the props on purpose: the settings
          extractor walks an opening tag to find `label`, and gives up past a size
          ceiling -- a long comment BETWEEN the props drops this control out of
          settings search, which is the one entry `capabilities.decisions` governance
          is asserted against. `settingsCoverage.test.ts` fails when that happens. */}
      <SettingsToggle
        label={i18nT('pages.developer.featurePreviewsTab.decisions')}
        description={i18nT('pages.developer.featurePreviewsTab.decisions_desc')}
        checked={view.enabled}
        onChange={v => consentMut.mutate(v)}
        disabled={switchFrozen || consentMut.isPending || scopeMut.isPending}
        describedBy={describedBy}
      />
      {/* The egress fact carries body weight, not muted fine print: it is what a
          reader is actually consenting to, and it stays outside the disclosure so a
          closed one cannot hide it. It fades only on a gateway that cannot run this
          at all, where nothing can leave the machine. */}
      <p
        id={EGRESS_NOTE_ID}
        className={backendMissing ? 'text-[12px] text-muted opacity-40' : 'text-[12px] text-text'}
      >
        {i18nT('pages.developer.featurePreviewsTab.decisions_egress')}
      </p>
      {/* The sampling share, stated in BOTH switch states and OUTSIDE the disclosure.
        * The decisions module spec under docs/system-specs/modules pins it there: the
        * shipped default is 100, so "every session" is what a reader is agreeing to, and
        * they have to see it before they flip the switch. Behind a closed disclosure it
        * is a fact they do not see -- the same argument that keeps the egress note above
        * out. The SLIDER stays in the shared block; this is the fact, not the control. */}
      {view.supported && (
        <p className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_bucket_hint', {
            percent: fmtPercent(bucket / 100),
          })}
        </p>
      )}
      {/* WHERE the messages go, as a fact beside the switch: consent is given for an
          address, and the gate holds the config to that address afterwards. Mono and
          untranslated — it is a URL a reader may compare against their provider. */}
      {view.supported && view.configuredEndpoint && (
        <p className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_sent_to')}{' '}
          <span className="font-mono break-all">{view.configuredEndpoint}</span>
        </p>
      )}
      {/* And where that address is changed. The card offers no control for it: `PATCH
          /api/config/kirocrew` excludes `decisions.provider.*`, so a dashboard caller
          cannot choose where the state a decision point collects is sent, and a field
          here would be one whose every save is refused. Its own paragraph, so the
          address line stays the one short fact a reader compares against their
          provider. */}
      {view.supported && view.configuredEndpoint && (
        <p className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_endpoint_pointer')}
        </p>
      )}
      {/* The redirected-config state: consent stands for one address, config.json now
          names another, so nothing is sent. A WARNING, not a paragraph: it is the one
          state where the switch reads "on" and the truth is "off". */}
      {view.endpointMoved && (
        <p role="alert" className="text-[12px] text-warn m-0 flex items-start gap-1.5">
          <AlertTriangle size={13} className="flex-none mt-0.5" aria-hidden="true" />
          <span>{i18nT('pages.developer.featurePreviewsTab.decisions_endpoint_moved')}</span>
        </p>
      )}
      {/* A failed READ discards nothing: every control below is drawn from server
          state and there is no draft to lose, so the hand-off is on.

          A gateway with no consent route is the one read failure that gets its own
          words and NO hand-off: the copy already names the exact step (update, from
          Settings > Releases), an agent cannot shorten it, and "reload to try again"
          would send a reader round a loop that cannot succeed. It keeps the note id so
          the switch's aria-describedby resolves to whichever message is drawn. */}
      {readFailed && (
        <ErrorNotice
          variant="inline"
          className="mt-1"
          id={backendMissing ? BACKEND_NOTE_ID : undefined}
          askAgent={!backendMissing}
          message={i18nT(
            backendMissing
              ? 'pages.developer.featurePreviewsTab.decisions_backend_required'
              : 'pages.developer.featurePreviewsTab.decisions_config_unavailable',
          )}
        />
      )}
      {(consentMut.isError || scopeMut.isError || configMut.isError || budgetMut.isError || secretMut.isError || secretDelMut.isError) && (
        // No hand-off: a save failure is exactly when `apiKeyDraft` holds a
        // credential the vault has not taken yet, and asking the agent unmounts this
        // card, so the button would offer help at the cost of the typed key. The
        // notice states the failure and the reader retries with the draft intact.
        // A scope write names the switch it was about. "Could not save this setting"
        // over a card with several switches names none of them, and this notice renders
        // at the foot of the card rather than beside the control, so the label is the
        // only thing that says which write was refused.
        <ErrorNotice
          variant="inline"
          className="mt-1"
          testId={SAVE_ERROR_TESTID}
          message={
            scopeMut.isError && scopeMut.variables
              ? i18nT('pages.developer.featurePreviewsTab.decisions_scope_save_failed', {
                  switch: SCOPE_LABEL[scopeMut.variables.scope] ?? scopeMut.variables.scope,
                })
              : i18nT('pages.developer.featurePreviewsTab.decisions_save_failed')
          }
        />
      )}
      {view.supported && (
        <details
          className="mt-1 rounded-md border border-border bg-bg-accent px-2.5 py-1.5"
          open={disclosureTargeted || undefined}
        >
          <summary className="cursor-pointer text-[12px] font-medium text-text">
            {i18nT('pages.developer.featurePreviewsTab.decisions_global')}
          </summary>
          <div id={GLOBAL_PANEL_ID} className="mt-1.5 flex flex-col gap-1">
            {/* The credential, into the VAULT and never into config.json. Set or unset
                is all the card can show, because the vault does not hand a value back
                — which is the property that makes an agent-writable config safe to
                leave naming `secret://TYPESAFE_API_KEY`. */}
            {/* The rotate icon names the verb and not the noun, and beside a stored
                credential those read differently -- "replace the stored key" and
                "discard what I just typed" differ by whether work is lost -- so this
                one passes its own label.

                Removal ASKS FIRST. `cleared` stays false and the trash opens a
                confirmation instead of writing, which is the same shape the Secrets
                panel uses for this very vault: `cleared` drives the field's DEFERRED
                "will be removed on save" strip, and that strip offers Undo. A vault
                entry is not deferred and not recoverable -- the value is never handed
                back -- so reflecting the in-flight DELETE there rendered an Undo for
                something already done, in exactly the window a reader who reconsiders
                would reach for it. The question goes before the write; `editing` still
                closes while the write runs, so Save cannot appear under it. */}
            <SecretField
              label={i18nT('pages.developer.featurePreviewsTab.decisions_api_key_label')}
              description={i18nT('pages.developer.featurePreviewsTab.decisions_api_key_desc')}
              isSet={apiKeySet && apiKeyKnown}
              preview={SECRET_PREVIEW}
              replaceLabel={i18nT('pages.developer.featurePreviewsTab.decisions_api_key_replace')}
              value={apiKeyDraft}
              onChange={setApiKeyDraft}
              editing={apiKeyEditing && !secretDelMut.isPending && !apiKeyConfirmRemove}
              onEditingChange={setApiKeyEditing}
              cleared={false}
              onClearedChange={next => {
                if (next && !apiKeyBusy) setApiKeyConfirmRemove(true)
              }}
              permanentRemoval={{
                label: i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove'),
                text: i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove'),
                confirmation: apiKeyConfirmRemove ? (
                  <>
                    <span className="text-[13px] text-warn">
                      {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove_confirm')}
                    </span>
                    <Btn danger disabled={apiKeyBusy} onClick={() => secretDelMut.mutate()}>
                      {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_delete')}
                    </Btn>
                    <Btn
                      disabled={secretDelMut.isPending}
                      onClick={() => setApiKeyConfirmRemove(false)}
                    >
                      {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove_cancel')}
                    </Btn>
                  </>
                ) : undefined,
              }}
            />
            {apiKeyKnown && !secretDelMut.isPending && !apiKeyConfirmRemove && (!apiKeySet || apiKeyEditing) && (
              <button
                type="button"
                className="self-start rounded-md border border-border bg-bg-elevated px-2 py-1 text-[12px] text-text hover:border-border-strong disabled:opacity-40"
                // Held while EITHER write is outstanding, not just the save: a delete
                // and a post in flight together is the shape that loses the new key.
                disabled={!apiKeyDraft || apiKeyBusy}
                onClick={() => secretMut.mutate(apiKeyDraft)}
              >
                {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_save')}
              </button>
            )}
            {/* The sampling share. A slider because the value is a proportion a reader
                dials rather than a number they know, and it can only ever NARROW what
                consent already allows — which is why it is a config value at all.
                A range input cannot nest its label, so the two are paired by id. */}
            <div className="flex flex-col gap-1 py-1.5" data-setting-label={i18nT('pages.developer.featurePreviewsTab.decisions_bucket_label')}>
              <label htmlFor={BUCKET_INPUT_ID} className="text-[13px] font-semibold text-text">
                {i18nT('pages.developer.featurePreviewsTab.decisions_bucket_label')}
              </label>
              <input
                id={BUCKET_INPUT_ID}
                // The caption above is already this control's programmatic label
                // through `htmlFor`; the explicit name carries the SAME string, so
                // nothing announces twice, and it is what `control-has-associated-label`
                // reads — a range input cannot nest its own caption.
                aria-label={i18nT('pages.developer.featurePreviewsTab.decisions_bucket_label')}
                type="range"
                min={0}
                max={100}
                step={5}
                value={bucketDraft ?? bucket}
                disabled={frozen}
                // Dragging moves the thumb and nothing else; the write happens once,
                // when the reader lets go. `onChange` also fires for the keyboard's
                // arrow keys, which is why the commit is on release AND on blur --
                // a keyboard user never produces a pointer-up.
                onChange={e => setBucketDraft(Number(e.target.value))}
                onPointerUp={() => commitBucket()}
                onBlur={() => commitBucket()}
                onKeyUp={() => commitBucket()}
                className="w-full accent-[var(--accent)]"
              />
            </div>
            {/* How much PRIOR conversation one decision may carry. A number box and not
                a slider: the units are characters, so a reader who wants a budget has
                one in mind, and the shipped default of 0 — the message alone — is the
                value consent was recorded against.

                Writes the KEYSTONE ceiling, not the config value: what it bounds is
                how much of the conversation leaves the machine, so it is consent, and
                consent does not live in an agent-writable file. The pointer line
                below names the config path the seam asks with. */}
            <SettingsInput
              label={i18nT('pages.developer.featurePreviewsTab.decisions_history_label')}
              description={i18nT('pages.developer.featurePreviewsTab.decisions_history_desc')}
              type="number"
              min={0}
              value={budgetDraft ?? String(historyBudget)}
              onChange={setBudgetDraft}
              onBlur={() => {
                const raw = budgetDraft
                setBudgetDraft(null)
                if (raw === null) return
                // Digits only, and the WHOLE string. `parseInt` reads a prefix, so
                // "6,000" would save 6 -- a number the reader never typed, on the
                // value that decides how much conversation leaves the machine. An
                // input this cannot read is left alone and the saved ceiling stays on
                // screen, which is the fail-closed direction: the smaller number is
                // the one already consented to.
                const digits = raw.trim()
                if (!/^[0-9]+$/.test(digits)) return
                const next = Number.parseInt(digits, 10)
                if (!Number.isSafeInteger(next) || next === historyBudget) return
                budgetMut.mutate(next)
              }}
              disabled={frozen || !view.enabled}
            />
            {/* Held while consent is OFF, and it SAYS so. The keystone writer stores
                this ceiling as 0 whenever the switch is off, so a write would answer
                200 and the typed number would be gone on the next read -- a field that
                takes a value and discards it. The line is what makes the disabled
                state readable instead of a dead control. */}
            {view.supported && !view.enabled && (
              <p className="text-[12px] text-muted">
                {i18nT('pages.developer.featurePreviewsTab.decisions_history_off')}
              </p>
            )}
            <p className="text-[12px] text-muted">
              {i18nT('pages.developer.featurePreviewsTab.decisions_history_pointer')}
            </p>
          </div>
        </details>
      )}

      {/* ── OVERVIEW + DETAIL ───────────────────────────────────────────────── */}
      {view.supported && rows.length === 0 && (
        <p className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_points_unavailable')}
        </p>
      )}
      {view.supported && rows.length > 0 && (
        <div className="pt-1">
          <div className="text-[12px] text-muted mb-1">
            {i18nT('pages.developer.featurePreviewsTab.decisions_points')}
          </div>
          {/* List and detail. On a narrow viewport the grid collapses to one column
              and the list becomes a strip above the panel — the same elements and the
              same ARIA along the other axis, so there is no second implementation to
              keep honest. `min-w-0` on both tracks because a long config path in the
              panel would otherwise push the list off screen. */}
          {/* The list track takes what the longest point name needs before the panel
              takes the rest: a name that ellipsizes is a row a reader cannot scan, and
              the panel's own content wraps. */}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(13rem,20rem)_minmax(0,1fr)]">
            <div
              role="tablist"
              // Vertical is the wide layout's axis, and BOTH are handled below: the
              // orientation flips with a CSS breakpoint this component never reads, so
              // refusing one axis would break the keyboard in one of the two layouts.
              aria-orientation="vertical"
              aria-label={i18nT('pages.developer.featurePreviewsTab.decisions_points')}
              className="flex min-w-0 flex-row gap-0.5 overflow-x-auto rounded-lg border border-border bg-bg-accent p-[3px] md:flex-col md:overflow-x-visible"
            >
              {rows.map(row => (
                <button
                  key={row.id}
                  id={pointRowId(row.id)}
                  type="button"
                  role="tab"
                  // Which row you are LOOKING at. `aria-current` is which point is
                  // RUNNING. Two facts, two attributes — a row is never a switch.
                  aria-selected={row.id === shown}
                  aria-current={row.status === POINT_ACTIVE ? 'true' : undefined}
                  aria-controls={POINT_PANEL_ID}
                  aria-describedby={pointStatusId(row.id)}
                  // Roving tabindex: one stop for the whole list, arrows move within
                  // it. Every row being tabbable would put four stops between the
                  // panel's heading and its first control.
                  tabIndex={row.id === shown ? 0 : -1}
                  // `w-full` only from `md`, where the strip is a COLUMN. On narrow it
                  // is a horizontal scroller, and a full-width row there means one row
                  // on screen and three unreachable ones.
                  className={`flex max-w-[12rem] shrink-0 items-center gap-1.5 rounded-md border px-2 py-[5px] text-left text-[13px] cursor-pointer transition-colors md:max-w-none md:w-full ${
                    row.id === shown
                      ? 'border-border-strong bg-bg-elevated text-text-strong font-semibold shadow-sm'
                      : 'border-transparent bg-transparent text-muted font-medium hover:text-text-strong'
                  }`}
                  onClick={() => setHighlighted(row.id)}
                  onKeyDown={e => {
                    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
                      e.preventDefault()
                      moveHighlight(1)
                    } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
                      e.preventDefault()
                      moveHighlight(-1)
                    } else if (e.key === 'Home') {
                      e.preventDefault()
                      setHighlighted(ids[0] ?? shown)
                    } else if (e.key === 'End') {
                      e.preventDefault()
                      setHighlighted(ids[ids.length - 1] ?? shown)
                    } else if (e.key === 'Enter' || e.key === ' ') {
                      // Highlight, never select. The browser fires `click` for both on
                      // a <button> anyway; naming them is what makes "Enter does not
                      // change anything" a property of this file.
                      e.preventDefault()
                      setHighlighted(row.id)
                    }
                  }}
                >
                  {/* Leading, radio-style: which points are RUNNING. Its own reserved
                      column so the names keep one left edge. */}
                  <span className="flex w-3.5 shrink-0 items-center justify-center">
                    {row.status === POINT_ACTIVE && (
                      <span className="flex items-center" title={statusWord(row)}>
                        <CircleDot size={12} aria-hidden className="text-accent" />
                      </span>
                    )}
                  </span>
                  {/* `title` beside the truncation: the track caps at 20rem and the
                      longest point name does not fit, so the full name has to be
                      reachable on hover. A row whose name is cut off is a row a reader
                      cannot scan, and the widest consent this card governs is the one
                      with the longest name. */}
                  <span className="truncate" title={nameOf(row.id)}>
                    {nameOf(row.id)}
                  </span>
                  {/* Trailing: the status chip. The name yields before it does — the
                      state is what the row is scanned for. `aria-hidden` because the
                      row's description states it in full below. */}
                  <span
                    aria-hidden
                    className={`ml-auto shrink-0 rounded px-1 text-[10px] font-medium ${
                      row.status === POINT_ACTIVE
                        ? 'text-accent'
                        : row.status === POINT_NEEDS_SCOPE
                          ? 'text-warn'
                          : 'text-muted'
                    }`}
                  >
                    {statusWord(row)}
                  </span>
                </button>
              ))}
            </div>

            {/* Every row's state as a SENTENCE, for a reader who gets no chip at all.
                One per row rather than one for the shown row: a reader arrowing down
                the list is told each point's state as they reach it, which is the only
                way the list is scannable without sight. */}
            <div className="sr-only">
              {rows.map(row => (
                <span key={row.id} id={pointStatusId(row.id)}>
                  {`${POINT_WHAT[row.id] ?? row.id} ${statusWord(row)}`}
                </span>
              ))}
            </div>

            <div
              role="tabpanel"
              id={POINT_PANEL_ID}
              aria-labelledby={shown ? pointRowId(shown) : undefined}
              // Keyed on the shown row so React remounts the panel per point: a draft
              // or an open disclosure on one point must not decide the next one's.
              key={shown}
              className="min-w-0 flex flex-col gap-1"
            >
              {shownRow && (
                <Suspense fallback={<div className="h-24" aria-hidden />}>
                  <DecisionsPointPanel
                    row={shownRow}
                    name={nameOf(shownRow.id)}
                    what={POINT_WHAT[shownRow.id] ?? shownRow.id}
                    scopeLabel={shownRow.needsScope ? SCOPE_LABEL[shownRow.needsScope] : undefined}
                    scopeDescription={
                      shownRow.needsScope ? SCOPE_DESC[shownRow.needsScope] : undefined
                    }
                    scopeGranted={
                      shownRow.needsScope ? SCOPE_GRANTED[shownRow.needsScope] === true : false
                    }
                    // Consent has to stand FOR THE ADDRESS IN FORCE before a wider
                    // category can be offered; the gateway refuses the write otherwise.
                    scopeOffered={view.enabled && !view.endpointMoved}
                    scopeDisabled={frozen || consentMut.isPending || scopeMut.isPending}
                    onScopeChange={v =>
                      scopeMut.mutate({ scope: shownRow.needsScope as string, value: v })
                    }
                    modelRoute={modelRoute}
                    modelNames={modelsQ.data.map(m => m.name)}
                    tierLabel={TIER_LABEL}
                    inheritLabel={inheritLabel}
                    tiersDisabled={frozen}
                    onTierChange={(tier, value) =>
                      configMut.mutate({ path: `${DECISIONS_MODEL_ROUTE_PATH}.${tier}`, value })
                    }
                    judgeProvider={nudgeWake.provider}
                    judgeModel={nudgeWake.llmModel}
                    judgeInheritLabel={judgeInheritLabel}
                    // A pinned model the backend no longer advertises stays selectable,
                    // for the same reason a tier's does: otherwise a reader could not
                    // switch back off it. `auto` is dropped because INHERIT already is
                    // the empty option, and offering both would be two words for it.
                    judgeModelOptions={judgeModelOptions}
                    providerLabel={PROVIDER_LABEL}
                    // NOT gated on consent, unlike the scope switch above: the small-model
                    // lane needs none, and an owner with no Jev key must be able to pick
                    // it while the switch is off.
                    judgeDisabled={frozen}
                    onJudgeProviderChange={value =>
                      configMut.mutate({ path: DECISIONS_NUDGE_WAKE_PROVIDER_PATH, value })
                    }
                    onJudgeModelChange={value =>
                      configMut.mutate({ path: DECISIONS_NUDGE_WAKE_MODEL_PATH, value })
                    }
                  />
                </Suspense>
              )}
            </div>
          </div>
        </div>
      )}
    </SettingsCard>
  )
}
