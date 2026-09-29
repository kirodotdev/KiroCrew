import { useQuery } from '@tanstack/react-query'

import { fetchDashboardConfig } from '../api/dashboardConfigQuery'
import type { ModelInfo } from '../providers/types'

const EFFORT_SUFFIX = /^(.*)\[(low|medium|high|xhigh|max)\]$/

/** Codex advertises each model/effort pair as a model ID. Keep the base model
 *  visible while effort is selected through the slider embedded in the model
 *  picker. Window suffixes such as [1m] remain part of the model ID. */
export function modelWithoutEffort(name: string): string {
  return EFFORT_SUFFIX.exec(name)?.[1] || name
}

export function modelEffortSuffix(name: string): string {
  return EFFORT_SUFFIX.exec(name)?.[2] || ''
}

/** Migrate an old Codex pair pin only when no separate slot effort exists. */
export function legacyCodexEffort(model: string, slotEffort: string, pairIds: boolean): string {
  return pairIds && !slotEffort ? modelEffortSuffix(model) : ''
}

/** The effort a model pick must write BEFORE the model, or null for none.
 *  The store lags the user: an effort picked inside the slider's debounce is
 *  only STAGED, and a pick on the model list in that window must not migrate
 *  the old pair level over the user's newer choice. So a staged effort (''
 *  included -- it clears the override) is carried onto the wire by the model
 *  pick itself. An effort already IN FLIGHT is not carried: it is on the wire
 *  once, and a repeat would take its own confirm budget queued behind the
 *  original, timing the model pick out for nothing -- the model pick instead
 *  WAITS for that write's verdict (see `switchGroupedModel`). An in-flight
 *  write is declared intent too: the store still reads '' until it lands, and
 *  migrating the old pair level over it would queue `max` behind the user's
 *  `high` and silently revert the pick. Only with no declared intent at all
 *  -- nothing staged, nothing on the wire -- does a legacy pair level migrate.
 *  `staged` is `stagedSlotSwitchTarget` for the slot's `reasoning_effort`;
 *  `inFlight` is whether `inFlightSlotSwitchOutcome` for it is non-null. */
export function effortToCarry(
  model: string,
  storedEffort: string,
  staged: string | null,
  inFlight: boolean,
  pairIds: boolean,
): string | null {
  if (staged !== null) return staged
  if (inFlight) return null
  return legacyCodexEffort(model, storedEffort, pairIds) || null
}

/** A grouped model pick must not outrun the effort it depends on -- a staged
 *  pick it carries, an old pair level's migration, or an effort write already
 *  in flight on the slot: the effort reaches the wire first, and a refused
 *  effort aborts the pick.
 *
 *  Both switches are REGISTERED at once. `persistModel` is called
 *  synchronously so the model pick takes its `performSlotSwitch` ticket the
 *  moment the user clicks -- a second pick made while the first effort write
 *  is still in flight therefore gets the HIGHER ticket, and its request waits
 *  behind the first in the model chain. Registering the model only after the
 *  effort settled (the earlier form) let the newer pick take the lower ticket
 *  and the older pick's model become what the backend runs. The ordering
 *  between the two fields is kept inside the model request itself: the
 *  caller awaits `afterEffort` before sending the model POST, so the wire
 *  still sees effort-then-model and an effort failure still aborts the pick.
 *
 *  `effortVerdict` reads `inFlightSlotSwitchOutcome('reasoning_effort', slot)`
 *  and is called AFTER the carried write registered, so it returns that
 *  write's verdict (chained behind any effort already on the wire) or, with
 *  nothing to carry, the verdict of the write already in flight. The model
 *  waits on THAT, never on `persistEffort`'s own promise: the latter is the
 *  CALLER's budget and rejects at `SWITCH_CONFIRM_TIMEOUT_MS` while the wire
 *  call keeps its place, so waiting on it turned a stalled-then-recovering
 *  effort into a model pick that was never sent (its effort landed, the toast
 *  said the pick "may still apply", and it never did). Nor is an in-flight
 *  level re-sent: the repeat would join the chain behind the original and
 *  burn its own budget waiting, failing the model pick while the effort it
 *  waited for lands fine.
 *
 *  The verdict is awaited RAW, on purpose. It is bounded exactly the way a
 *  same-field stall is (slotSwitch.ts, "THE ADJUDICATION MODEL"): the CALLER's
 *  wait ends at `SWITCH_CONFIRM_TIMEOUT_MS` inside `performSlotSwitch` (the
 *  unconfirmed toast fires), while the wire call keeps its place. Racing the
 *  verdict here and then sending would put the model ahead of an effort that
 *  may still land (the ordering this function exists to keep); racing it and
 *  then dropping would discard a pick the module's own rule says must still
 *  apply when it is what the backend was left running. An effort POST that
 *  never settles is a wedged connection, and a model POST on it would sit in
 *  the same place -- so the outcome is the one a stalled model pick already
 *  gets: unconfirmed at the budget, applied when the connection recovers. */
export async function switchGroupedModel(
  effort: string | null,
  persistEffort: (level: string) => Promise<void>,
  persistModel: (afterEffort: Promise<void>) => Promise<void>,
  effortVerdict: () => Promise<void> | null = () => null,
): Promise<void> {
  const carried = effort !== null ? persistEffort(effort) : null
  const afterEffort = effortVerdict() ?? carried ?? Promise.resolve()
  const model = persistModel(afterEffort)
  // Promise.all subscribes to both, so the second rejection of a pair that
  // failed together is handled, not left dangling as unhandled. The carried
  // write's own (caller-budget) promise is what the user is told about; the
  // model send keeps waiting on the wire verdict past it.
  await Promise.all([carried ?? afterEffort, model])
}

export function shouldSeparateModelEffort(pairIds: boolean | undefined, models: readonly ModelInfo[]): boolean {
  return pairIds === true && models.some(model => !!modelEffortSuffix(model.name))
}

export function normalizeHiddenModels(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  const result: string[] = []
  for (const raw of value) {
    if (typeof raw !== 'string') continue
    const model = raw.trim()
    if (!model || model === 'auto' || seen.has(model)) continue
    seen.add(model)
    result.push(model)
  }
  return result
}

export function filterInteractiveModels(
  models: ModelInfo[],
  hiddenModels: readonly string[],
  activeModels: readonly string[] = [],
  groupEffortPairs = false,
): ModelInfo[] {
  const hidden = new Set(hiddenModels)
  const kept = new Set(activeModels.filter(Boolean))
  const visible = models.filter(model => model.name === 'auto' || kept.has(model.name) || !hidden.has(model.name))
  if (!groupEffortPairs) return visible

  const seen = new Set<string>()
  const baseModels = new Map(visible.filter(model => modelWithoutEffort(model.name) === model.name).map(model => [model.name, model]))
  const pairDescriptions = new Map<string, string | null>()
  for (const model of models) {
    const name = modelWithoutEffort(model.name)
    if (name === model.name) continue
    const description = model.description?.trim() || ''
    if (!pairDescriptions.has(name)) pairDescriptions.set(name, description)
    else if (pairDescriptions.get(name) !== description) pairDescriptions.set(name, null)
  }
  return visible.flatMap(model => {
    const name = modelWithoutEffort(model.name)
    if (seen.has(name)) return []
    seen.add(name)
    // Prefer metadata from an explicitly advertised base model. Without one,
    // retain a description only if every advertised effort variant agrees;
    // a price remains level-specific and cannot describe the grouped row.
    const base = baseModels.get(name)
    return [{ ...(base ?? model), name, ...(!base && name !== model.name ? { description: pairDescriptions.get(name) || '', rateMultiplier: undefined } : {}) }]
  })
}

export function useModelPickerHiddenModelsQuery() {
  const query = useQuery({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
  })
  return {
    ...query,
    data: normalizeHiddenModels(query.data?.model_picker_hidden_models),
  }
}

export function useModelPickerHiddenModels(): string[] {
  return useModelPickerHiddenModelsQuery().data
}

/** Keep the first-use prompt hidden until configuration is known. Opening
 * Settings is not acknowledgement; only the server records a successful save. */
export function useModelPickerConfigured(): boolean {
  const { data } = useQuery({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
  })
  return data?.model_picker_configured !== false
}
