/**
 * Settings > Display > View: Task Runner's switch (`taskrunner.enabled`).
 *
 * Off hides Task Runner's sidebar row and Search Everywhere result, and the
 * gateway refuses every new run. Default on; only a literal `false` is off.
 *
 * The switch shows the gateway's setting alone. Library does not list Task
 * Runner, so the sidebar unpin this browser may hold for its row
 * (`mc-app-nav-hidden`, under the row's nav id) is cleared when it is turned on.
 *
 * Shown only when the gateway's config carries the key: an older gateway refuses
 * the write, and a toggle that can only fail is worse than none.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { toggleAppNavHidden, useAppNavHidden } from '../../lib/appNavHidden'
import { BUILTIN_SURFACE_NAV } from '../../surfaces/surfaceData'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

/** The config path this row writes; the rail's Task Runner row reads the same one. */
export const TASK_RUNNER_SWITCH = 'taskrunner.enabled'

type TaskRunnerCfg = { taskrunner?: { enabled?: boolean } }

export function TaskRunnerSetting() {
  const qc = useQueryClient()
  const cfgQ = useQuery<TaskRunnerCfg>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const overlay = useOptimisticConfigPaths(qc)
  const [error, setError] = useState<string | null>(null)
  const mut = useMutation(overlay.mutationOpts<boolean>({
    queryKey: ['kirocrewConfig'],
    mutationFn: (value: boolean) => api.patchConfig(TASK_RUNNER_SWITCH, value),
    path: () => TASK_RUNNER_SWITCH,
    displayValue: v => v,
    applyToCache: (cached, value) => setConfigPathValue(cached as TaskRunnerCfg, TASK_RUNNER_SWITCH, value),
    onFailure: () => setError(i18nT('pages.settings.displayPanel.task_runner_save_failed')),
    onSupersede: () => setError(null),
  }))
  const unpinned = useAppNavHidden().has(BUILTIN_SURFACE_NAV.projects.navId)
  if (typeof cfgQ.data?.taskrunner?.enabled !== 'boolean') return null
  const on = overlay.shown(TASK_RUNNER_SWITCH, cfgQ.data.taskrunner.enabled !== false)
  const change = (value: boolean) => {
    if (value && unpinned) toggleAppNavHidden(BUILTIN_SURFACE_NAV.projects.navId)
    if (value !== on) mut.mutate(value)
  }
  return (
    <>
      <SettingsToggle
        configKey="taskrunner.enabled"
        label={i18nT('pages.settings.displayPanel.task_runner')}
        description={i18nT('pages.settings.displayPanel.task_runner_hint')}
        checked={on}
        onChange={change}
        disabled={mut.isPending}
      />
      {/* No hand-off: this row renders inside DisplayPanel, whose `shellDraft`
          and `installValue` are unsaved local drafts that navigation would
          discard. */}
      <ErrorNotice message={error} variant="inline" />
    </>
  )
}
