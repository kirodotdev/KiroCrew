/**
 * Settings > Developer > Feature Previews > **Session control**: the On/Off row
 * for `agent.session_control`, the one switch that withdraws the session tools
 * (`session_create`, `session_send`, `session_stop`, ...) from every agent at once.
 *
 * Same write path as `CrewmatesSection`: PATCH the key through the owner-only
 * config route, then refetch the shared `['kirocrewConfig']` query. The backend
 * reads the key on every session-control call, so a change needs no restart.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { SettingsCard, SettingsSection, SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'

type KirocrewConfigShape = { agent?: { session_control?: boolean } }

export function SessionControlSection() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const [saveError, setSaveError] = useState('')
  const mut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig('agent.session_control', v),
    onSuccess: () => {
      setSaveError('')
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: () => setSaveError(i18nT('pages.settings.sessionControlSection.failed_to_save')),
  })

  return (
    <SettingsSection title={i18nT('pages.settings.sessionControlSection.title')}>
      {/* askAgent: a toggle holds no draft, so the hand-off loses nothing. */}
      {cfgQ.isError && (
        <ErrorNotice
          message={i18nT('pages.settings.sessionControlSection.failed_to_load')}
          askAgent
          className="mb-2"
          testId="session-control-config-error"
          footer={
            <Btn onClick={() => { void cfgQ.refetch() }} disabled={cfgQ.isFetching} data-testid="session-control-config-retry">
              {i18nT('pages.settings.sessionControlSection.retry')}
            </Btn>
          }
        />
      )}
      {saveError && (
        <ErrorNotice
          message={saveError}
          onDismiss={() => setSaveError('')}
          askAgent
          className="mb-2"
          testId="session-control-save-error"
        />
      )}
      {/* No switch until the value has been read: this one is on by default, so
          a placeholder in either position could show the wrong state. */}
      {cfgQ.data && (
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.sessionControlSection.session_control_tools')}
            description={i18nT('pages.settings.sessionControlSection.session_control_tools_desc')}
            hint={i18nT('pages.settings.sessionControlSection.session_control_tools_hint')}
            checked={cfgQ.data.agent?.session_control !== false}
            onChange={(v) => mut.mutate(v)}
            disabled={!cfgQ.isSuccess || mut.isPending}
            configKey="agent.session_control"
          />
        </SettingsCard>
      )}
    </SettingsSection>
  )
}
