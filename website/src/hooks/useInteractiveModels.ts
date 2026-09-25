import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { ModelInfo } from '../providers/types'

const EFFORT_SUFFIX = /^(.*)\[(low|medium|high|xhigh|max)\]$/

/** Codex advertises each model/effort pair as a model ID. Keep the base model
 *  visible while effort is selected through its own control. Window suffixes
 *  such as [1m] remain part of the model ID. */
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

/** A grouped model pick must not outrun migration of its old pair level. */
export async function switchGroupedModel(
  legacyEffort: string,
  persistEffort: (level: string) => Promise<void>,
  persistModel: () => Promise<void>,
): Promise<void> {
  if (legacyEffort) await persistEffort(legacyEffort)
  await persistModel()
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
  separateEffort = false,
): ModelInfo[] {
  const hidden = new Set(hiddenModels)
  const kept = new Set(activeModels.filter(Boolean))
  const visible = models.filter(model => model.name === 'auto' || kept.has(model.name) || !hidden.has(model.name))
  if (!separateEffort) return visible

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
    queryFn: () => api.dashboardConfig(),
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
    queryFn: () => api.dashboardConfig(),
  })
  return data?.model_picker_configured !== false
}
