/**
 * The model Mastermind's SUMMARIES run on (`agent.role_models.mastermind`), apart
 * from the model the chat itself answers on. One hook for both places that edit
 * it -- Settings > Mastermind and the Mastermind chat's composer -- built from the
 * same pieces as the Chat panel's per-role model rows: the config PATCH with an
 * optimistic display (`chatPanelConfigOpts`), the advertised model list
 * (`useAvailableModelsQuery`, the list the composer's own picker reads) and the
 * role rows' "Auto" label. Both read the shared `['kirocrewConfig']` cache, so a
 * pick in one shows in the other.
 *
 * Unpinned is `auto` and means "the provider picks", never "the chat's model":
 * the backend resolves the role without falling back to `agent.model`.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { MASTERMIND_QUERY_KEY } from '../../hooks/useMastermind'
import { i18nT } from '../../i18n/t'
import { useOptimisticConfigPaths } from './useOptimisticConfigPaths'
import { chatPanelConfigOpts } from './useChatPanelModels'
import type { KirocrewConfigShape } from './chatPanelOptions'

export const MASTERMIND_SUMMARY_MODEL_PATH = 'agent.role_models.mastermind'

export function useMastermindSummaryModel() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewConfigShape>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const overlay = useOptimisticConfigPaths(qc)
  const [error, setError] = useState('')
  const configOpts = chatPanelConfigOpts(overlay, (_path, msg) => setError(msg), () => setError(''))
  const base = configOpts(MASTERMIND_SUMMARY_MODEL_PATH, () => i18nT('pages.settings.mastermindPanel.summary_model_failed'))
  const mutation = useMutation({
    ...base,
    // GET /api/mastermind reports the resolved `summary_model`; re-read it too.
    onSuccess: (data, value, token) => {
      void qc.invalidateQueries({ queryKey: MASTERMIND_QUERY_KEY, exact: true })
      return base.onSuccess(data, value, token)
    },
  })

  // '' and absent mean unpinned, which the backend resolves as `auto`.
  const server = cfgQ.data?.agent?.role_models?.mastermind || 'auto'
  const shown = overlay.shown(MASTERMIND_SUMMARY_MODEL_PATH, server)
  const options = useAvailableModelsQuery().data.map(m => m.name)
  if (!options.includes('auto')) options.unshift('auto')
  // A pinned model the backend no longer advertises stays selectable, both the
  // persisted and the shown id (the same rule as the Chat panel's role rows).
  for (const kept of [server, shown]) {
    if (!options.includes(kept)) options.splice(1, 0, kept)
  }
  const labelOf = (m: string) => (m === 'auto' ? i18nT('pages.settings.chatPanel.role_model_auto') : m)

  return {
    /** The config has been read: the control can be used. */
    ready: cfgQ.isSuccess,
    shown,
    options,
    /** Index-paired with `options`, as `SimpleSelect` reads them. */
    labels: options.map(labelOf),
    labelOf,
    pending: mutation.isPending,
    set: (model: string) => mutation.mutate(model),
    error,
    dismissError: () => setError(''),
  }
}
