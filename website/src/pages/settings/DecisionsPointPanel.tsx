import { SettingsToggle, SettingsSelect } from '../../components/settings'
import {
  DECISIONS_LANE_LLM,
  DECISIONS_MODEL_ROUTE_PATH,
  DECISIONS_MODEL_ROUTE_TIERS,
  DECISIONS_NUDGE_WAKE_MODEL_PATH,
  DECISIONS_NUDGE_WAKE_POINT,
  DECISIONS_NUDGE_WAKE_PROVIDER_PATH,
  DECISIONS_NUDGE_WAKE_PROVIDERS,
  POINT_NEEDS_SCOPE,
  type DecisionPointRow,
} from './decisionsPreview'
import { i18nT } from '../../i18n/t'

/**
 * One decision point's own settings — the DETAIL half of the Decisions (Jev) card.
 *
 * Split into its own module so the CARD can be mounted synchronously while this is
 * loaded on demand. Both halves of that matter and they pull in opposite directions:
 *
 *  - the card must be synchronous, because settings search highlights a control by
 *    probing the DOM for it and gives up after 100 ms. A deep link to
 *    `developer.decisions-jev` that arrived while a chunk was still in flight found
 *    nothing and rang no control.
 *  - this panel need not be, because nothing it draws is in the settings registry:
 *    every control here takes its label from a map keyed by the SERVER's id (the
 *    scope, the tier), which is what lets a gateway ship another point with no edit
 *    on this side — and is exactly why the extractor cannot index them.
 *
 * So the registry-indexed controls stay eager and the rest rides a boundary. There is
 * no state of its own here: the panel is a pure function of the row and the view, and
 * the card remounts it per point (`key={shown}`) so one point's draft or open
 * disclosure cannot decide the next one's.
 */
export interface DecisionsPointPanelProps {
  /** The point this panel is for, as the gateway projected it. */
  row: DecisionPointRow
  /** Its plain-words name, from the card's label map. */
  name: string
  /** One line of what it decides. */
  what: string
  /** Label and description for the scope it needs, keyed by the keystone's spelling. */
  scopeLabel: string | undefined
  scopeDescription: string | undefined
  /** Whether THIS point's scope is recorded. Never another point's. */
  scopeGranted: boolean
  /** Whether a scope control may be offered at all: consent stands for the address. */
  scopeOffered: boolean
  scopeDisabled: boolean
  onScopeChange: (value: boolean) => void
  /** `model.route`'s tier map, the advertised models, and the labels for both. */
  modelRoute: Record<string, string>
  modelNames: string[]
  tierLabel: Record<string, string>
  inheritLabel: string
  tiersDisabled: boolean
  onTierChange: (tier: string, value: string) => void
  /** The judge point's provider and model, its option labels, and its writers. */
  judgeProvider: string
  judgeModel: string
  /**
   * INHERIT as the judge's picker must say it. Separate from `inheritLabel` above
   * because the two mean different models: a tier's empty value keeps the model the
   * SESSION would have used, while the judge's resolves the judge agent's own.
   */
  judgeInheritLabel: string
  judgeModelOptions: string[]
  providerLabel: Record<string, string>
  judgeDisabled: boolean
  onJudgeProviderChange: (value: string) => void
  onJudgeModelChange: (value: string) => void
  /** A sentence per `config.json` path this card points at instead of controlling. */
}

export function DecisionsPointPanel({
  row,
  name,
  what,
  scopeLabel,
  scopeDescription,
  scopeGranted,
  scopeOffered,
  scopeDisabled,
  onScopeChange,
  modelRoute,
  modelNames,
  tierLabel,
  inheritLabel,
  tiersDisabled,
  onTierChange,
  judgeProvider,
  judgeModel,
  judgeInheritLabel,
  judgeModelOptions,
  providerLabel,
  judgeDisabled,
  onJudgeProviderChange,
  onJudgeModelChange,
}: DecisionsPointPanelProps) {
  return (
    <>
      <div className="flex items-baseline justify-between gap-3 text-[13px]">
        {/* `title` as well as the text: the list track caps at 20rem and the longest
            point name is cut off there, so the full name has to be reachable on hover
            and to assistive tech from the heading too — a reader who cannot finish
            reading the name of the widest consent cannot decide about it. */}
        <span className="font-semibold text-text-strong" title={name}>
          {name}
        </span>
        {/* The identifier stays mono and untranslated — it is the string a reader greps
            the decision log for, and the muted prefix says so, since the token alone
            reads as a second, unexplained name. */}
        <span className="text-[11px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_point_logged_as')}{' '}
          <span className="font-mono" title={row.id}>
            {row.id}
          </span>
        </span>
      </div>
      <p className="text-[12px] text-text m-0">{what}</p>
      {/* Which control the overview's "needs your OK" chip meant. The chip is on the
          row and the switch is one level down, so without this line a reader is left to
          guess that opening the row was the way to give it — and that guess is the
          whole repair path for every point carrying a scope. */}
      {row.needsScope && scopeOffered && row.status === POINT_NEEDS_SCOPE && (
        <p className="text-[12px] text-text m-0">
          {i18nT('pages.developer.featurePreviewsTab.decisions_scope_is_the_ok')}
        </p>
      )}
      {/* The point's own egress SCOPE, where it has one. Drawn only while consent
          stands: with nothing being sent at all, a second egress control would describe
          a state that cannot happen, and the write would be refused anyway. */}
      {row.needsScope && scopeOffered && (
        <SettingsToggle
          label={scopeLabel ?? row.needsScope}
          description={scopeDescription}
          checked={scopeGranted}
          onChange={onScopeChange}
          disabled={scopeDisabled}
        />
      )}
      {/* `model.route` answers a TIER, and the tier-to-model map is what turns that
          answer into a model. Three pickers fed by the same advertised-model list every
          other picker reads, so an id that cannot run cannot be chosen — and INHERIT is
          the default, because a model id written into a build fails on the first prompt
          for every account not entitled to it. */}
      {row.id === 'model.route' &&
        DECISIONS_MODEL_ROUTE_TIERS.map(tier => {
          const server = modelRoute[tier] ?? ''
          // A pinned model the backend does not advertise must stay selectable, or a
          // reader could not switch back to it.
          const opts = ['', ...modelNames.filter(m => m !== 'auto')]
          if (server && !opts.includes(server)) opts.splice(1, 0, server)
          return (
            <SettingsSelect
              key={tier}
              label={tierLabel[tier] ?? tier}
              configKey={`${DECISIONS_MODEL_ROUTE_PATH}.${tier}`}
              value={server}
              options={opts}
              // One array for both props: SettingsSelect pairs a label to a value by
              // INDEX, so they must read the same list.
              optionLabels={opts.map(m => (m === '' ? inheritLabel : m))}
              onChange={v => onTierChange(tier, v)}
              disabled={tiersDisabled}
            />
          )
        })}
      {/* The judge point's own two settings. Drawn on the same terms as the tier
          pickers above and NOT gated on consent: the `llm` lane sends to the model
          provider this machine already uses, so an owner with no Jev key has to be
          able to reach these while the consent switch is off -- that is the case the
          lane exists for. `auto` is the default and resolves without either provider
          being named. The model list is the same advertised one every other picker
          reads, and INHERIT is its default for the same reason a tier's is. */}
      {row.id === DECISIONS_NUDGE_WAKE_POINT && (
        <>
          {/* The card's frame speaks for the Jev endpoint -- its heading says "while
              this is on" and its intro says nothing is sent while it is off -- and both
              are true of every other point. This lane is the exception, so the reader
              who has just seen the row name the small model is told here, on the point
              that does it, which lane answers and where the evidence goes. Drawn only
              when that lane is in fact the live one: on the Jev lane the frame above is
              already the whole story, and a second line would answer a question the
              reader does not have. */}
          {row.lane === DECISIONS_LANE_LLM && (
            <p className="text-[12px] text-muted m-0">
              {i18nT('pages.developer.featurePreviewsTab.decisions_judge_lane_note')}
            </p>
          )}
          <SettingsSelect
            label={i18nT('pages.developer.featurePreviewsTab.decisions_judge_provider')}
            description={i18nT(
              'pages.developer.featurePreviewsTab.decisions_judge_provider_desc',
            )}
            configKey={DECISIONS_NUDGE_WAKE_PROVIDER_PATH}
            value={judgeProvider}
            options={[...DECISIONS_NUDGE_WAKE_PROVIDERS]}
            optionLabels={DECISIONS_NUDGE_WAKE_PROVIDERS.map(p => providerLabel[p] ?? p)}
            onChange={onJudgeProviderChange}
            disabled={judgeDisabled}
          />
          <SettingsSelect
            label={i18nT('pages.developer.featurePreviewsTab.decisions_judge_model')}
            description={i18nT('pages.developer.featurePreviewsTab.decisions_judge_model_desc')}
            configKey={DECISIONS_NUDGE_WAKE_MODEL_PATH}
            value={judgeModel}
            options={judgeModelOptions}
            optionLabels={judgeModelOptions.map(m => (m === '' ? judgeInheritLabel : m))}
            onChange={onJudgeModelChange}
            disabled={judgeDisabled}
          />
        </>
      )}
      {/* The one setting this card does NOT offer a control for, on the one point that
          has one. A reader must not have to assume the card is the whole story, and the
          sentence names the `config.json` path they would grep for.

          A literal on this point rather than a per-row registry: one entry threaded
          through the payload, the reader and a props map to print one line is machinery
          the single case does not pay for. A second point needing one is when a map
          earns its place. */}
      {row.id === 'skills.select' && (
        <p className="text-[12px] text-muted m-0">
          {i18nT('pages.developer.featurePreviewsTab.decisions_pointer_skills_max_triggered')}
        </p>
      )}
    </>
  )
}
