/**
 * The **Crewmate 2.0** switch (`dashboard.crewmate_v2`), drawn INSIDE the Crew
 * Members preview card in Settings > Developer > Feature Previews and only while
 * that preview is on: it changes how the crewmates on that page work, so it is
 * a second-level switch under the one that shows the page.
 *
 * Unlike the per-device preview flags around it, this one is server config: it
 * moves crewmates between their template and their conductor/worker shape, so it
 * is PATCHed to config and read back through the shared `['kirocrewConfig']`
 * query, like the Crewmates section's reply-threads switch. The gateway reads it
 * once at startup (`kiro_crew/crewmate_v2.py`), so a change says "restart to
 * apply" until the page is reloaded. On by default.
 *
 * Dynamic capabilities is not here: it is a per-crewmate preview on each
 * crewmate's own Profile card (`pages/members/CrewProfileSettings.tsx`).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import { i18nT } from '../../i18n/t'

/** The config path the toggle writes; read once by the gateway at startup. */
export const CREWMATE_V2_CONFIG_KEY = 'dashboard.crewmate_v2'

type KirocrewConfigShape = { dashboard?: { crewmate_v2?: boolean } }

export function CrewmateV2Toggle() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const [changed, setChanged] = useState(false)
  const [saveError, setSaveError] = useState('')
  const on = cfgQ.data?.dashboard?.crewmate_v2 === true
  const mut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig(CREWMATE_V2_CONFIG_KEY, v),
    onSuccess: () => {
      setSaveError('')
      setChanged(true)
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: () => setSaveError(i18nT('pages.developer.featurePreviewsTab.crewmate_v2_failed_to_save')),
  })
  return (
    <div data-testid="crewmate-v2-toggle" className="mt-2 border-l-2 border-border pl-3">
      <SettingsToggle
        label={i18nT('pages.developer.featurePreviewsTab.crewmate_v2')}
        hint={i18nT('pages.developer.featurePreviewsTab.crewmate_v2_desc')}
        checked={on}
        onChange={v => mut.mutate(v)}
        disabled={!cfgQ.isSuccess || mut.isPending}
        configKey={CREWMATE_V2_CONFIG_KEY}
      />
      <div className="pt-1">
        <SchemaRestartBadge configKey={CREWMATE_V2_CONFIG_KEY} />
      </div>
      {changed && (
        <p data-testid="crewmate-v2-restart" role="status" className="text-[12px] text-muted pt-1">
          {i18nT('pages.developer.featurePreviewsTab.crewmate_v2_restart')}
        </p>
      )}
      {saveError && (
        <ErrorNotice
          message={saveError}
          onDismiss={() => setSaveError('')}
          className="mt-2"
          testId="crewmate-v2-save-error"
        />
      )}
    </div>
  )
}
