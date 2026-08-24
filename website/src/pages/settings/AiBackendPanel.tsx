import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CircleCheck, Info } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection, SettingsSelect } from '../../components/settings'
import { RestartGatewayButton } from './AboutPanel'

import { i18nT } from '../../i18n/t'
type AcpBackend = '' | 'codex' | 'kas'
type KiroCrewConfig = { agent?: { acp_backend?: string } }
type ConfigPatchResult = KiroCrewConfig & { restart_required?: boolean }

const BACKEND_VALUES: AcpBackend[] = ['', 'codex', 'kas']
const BACKEND_LABEL_KEY: Record<AcpBackend, string> = {
  '': 'pages.settings.aiBackendPanel.kiro',
  codex: 'pages.settings.aiBackendPanel.codex',
  kas: 'pages.settings.aiBackendPanel.kiro_agent_server',
}

function backendDescription(backend: AcpBackend) {
  if (backend === 'codex') {
    return (
      <span className="flex flex-col items-start gap-1">
        <span>{i18nT('pages.settings.aiBackendPanel.codex_description')}</span>
        <code className="font-mono">codex login</code>
      </span>
    )
  }
  if (backend === 'kas') {
    return <span>{i18nT('pages.settings.aiBackendPanel.kas_description')}</span>
  }
  return <span>{i18nT('pages.settings.aiBackendPanel.kiro_description')}</span>
}

function normalizeBackend(value: string | undefined): AcpBackend {
  return BACKEND_VALUES.includes(value as AcpBackend) ? value as AcpBackend : ''
}

export function AiBackendPanel() {
  const queryClient = useQueryClient()
  const [saveError, setSaveError] = useState('')
  const [restartRequired, setRestartRequired] = useState(false)
  const [restarting, setRestarting] = useState(false)

  const configQuery = useQuery<KiroCrewConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const backend = normalizeBackend(configQuery.data?.agent?.acp_backend)

  const backendMutation = useMutation({
    mutationFn: (value: AcpBackend) =>
      api.patchConfig('agent.acp_backend', value) as Promise<ConfigPatchResult>,
    onMutate: async (value) => {
      setSaveError('')
      await queryClient.cancelQueries({ queryKey: ['kirocrewConfig'] })
      const previous = queryClient.getQueryData<KiroCrewConfig>(['kirocrewConfig'])
      queryClient.setQueryData<KiroCrewConfig>(['kirocrewConfig'], (current) => ({
        ...(current ?? {}),
        agent: { ...(current?.agent ?? {}), acp_backend: value },
      }))
      return { previous }
    },
    onSuccess: (result) => {
      setRestartRequired(result.restart_required !== false)
      queryClient.removeQueries({ queryKey: ['available-models'] })
    },
    onError: (_error, _value, context) => {
      if (context?.previous) queryClient.setQueryData(['kirocrewConfig'], context.previous)
      setSaveError(i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config'))
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })

  const restartMutation = useMutation({
    mutationFn: () => api.restartGateway(),
    onSuccess: () => setRestarting(true),
    onError: (error: unknown) => {
      if (error instanceof ApiError) {
        setSaveError(error.message || i18nT('pages.settings.aboutPanel.restart_failed'))
      } else {
        // The process replacement can reset the connection before fetch sees the
        // response. That is the expected success path for a gateway restart.
        setRestarting(true)
      }
    },
  })

  return (
    <>
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-4 animate-rise" />
      <ErrorNotice
        message={configQuery.isError ? i18nT('pages.settings.chatPanel.failed_to_load_config') : ''}
        className="mb-4 animate-rise"
      />
      <SettingsSection title={i18nT('pages.settings.aiBackendPanel.ai_backend')}>
        <SettingsCard>
          <SettingsSelect
            label={i18nT('pages.settings.aiBackendPanel.acp_backend')}
            description={i18nT('pages.settings.aiBackendPanel.choose_which_agent_runtime_handles_new_chats')}
            value={backend}
            options={BACKEND_VALUES}
            optionLabels={BACKEND_VALUES.map(value => i18nT(BACKEND_LABEL_KEY[value]))}
            onChange={value => backendMutation.mutate(value as AcpBackend)}
            disabled={!configQuery.isSuccess || backendMutation.isPending || restarting}
            configKey="agent.acp_backend"
          />
          <div className="mt-2 flex items-start gap-2 rounded-md border border-border bg-bg-accent px-3 py-2.5">
            <Info size={15} className="lucide-inline mt-0.5 shrink-0 text-muted" aria-hidden />
            <div className="text-[12px] text-muted">{backendDescription(backend)}</div>
          </div>
          {configQuery.isSuccess && (
            <div className="mt-2 flex items-center gap-2 text-[12px] text-ok" aria-live="polite">
              <CircleCheck size={14} className="lucide-inline shrink-0" aria-hidden />
              <span>{i18nT('pages.settings.aiBackendPanel.selected_backend_saved')}</span>
            </div>
          )}
          {restartRequired && (
            <div className="mt-3 rounded-md border border-warn/30 bg-warn-subtle p-3" role="status">
              <div className="text-[13px] font-semibold text-text-strong">
                {i18nT('pages.settings.aiBackendPanel.restart_required')}
              </div>
              <div className="mt-1 text-[12px] text-muted">
                {i18nT('pages.settings.aiBackendPanel.restart_new_chats_description')}
              </div>
              <div className="mt-3">
                <RestartGatewayButton
                  pending={restartMutation.isPending}
                  restarting={restarting}
                  onConfirm={() => restartMutation.mutate()}
                  testId="ai-backend-restart"
                />
              </div>
            </div>
          )}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
