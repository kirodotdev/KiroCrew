import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { modelWithoutEffort } from './useInteractiveModels'

/**
 * What the composer chip needs to tell the Settings default from a pin, for
 * both chat hosts. `settingsDefault` is Settings → Chat → Default Model from
 * the shared `['kirocrewConfig']` entry, which every Settings write updates:
 * `''` when none is set (unset or `auto`), effort-stripped when the host shows
 * models without their effort. It is `null` while a read it depends on is
 * unknown -- loading, failed, or a remote session, whose default lives on the
 * peer -- so no caller claims a default it cannot see.
 *
 * `agentName` is the agent a slot with no model of its own resolves through
 * (`''` for a slot that holds a model). `agentPinned` says that agent pins its
 * own model: a pin equal to the Settings default does not move when the
 * default does, so it is no default. `failed` is set when a read errored with
 * no answer on hand, for the host's notice.
 */
export function useSettingsDefaultModel(
  agentName: string,
  remote: boolean,
  stripEffort: boolean,
): { settingsDefault: string | null; agentPinned: boolean; failed: boolean } {
  const configQ = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: !remote,
  })
  const resolvedQ = useQuery<{ pinned?: boolean }>({
    queryKey: ['agent-resolved-model', agentName],
    queryFn: () => api.agentResolvedModel(agentName),
    enabled: !!agentName && !remote,
  })
  const failed = !remote && (
    (configQ.isError && !configQ.data) || (!!agentName && resolvedQ.isError && !resolvedQ.data)
  )
  const agentPinned = !!agentName && !!resolvedQ.data?.pinned
  if (remote || !configQ.data || (agentName && !resolvedQ.data)) {
    return { settingsDefault: null, agentPinned, failed }
  }
  const model = (configQ.data as { agent?: { model?: unknown } }).agent?.model
  const settingsDefault = typeof model === 'string' && model !== 'auto'
    ? (stripEffort ? modelWithoutEffort(model) : model)
    : ''
  return { settingsDefault, agentPinned, failed }
}
