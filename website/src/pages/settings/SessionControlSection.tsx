/**
 * Settings > Developer > **Session control**: the On/Off row for
 * `agent.session_control`, the operator's one switch that withdraws the
 * kirocrew-dashboard session tools (`session_create`, `session_send`,
 * `session_stop`, `session_close`, ...) from every agent at once.
 *
 * On by default (#8375): the grant that decides WHO can use the tools is the
 * agent spec that mounts the server, so this switch is the global withdrawal,
 * not the grant. Turning it back on is still a trust change, which is why the
 * description says what "on" allows rather than just naming the key.
 *
 * Same write path as the Crewmates row: PATCH `agent.session_control` through
 * the owner-only config route, then refetch the shared `['kirocrewConfig']`
 * query. The backend reads the key on every session-control call
 * (`session_control_enabled()`), so there is no restart and no "new sessions
 * only" caveat: the next call from any session sees the new value.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { SettingsCard, SettingsField, SettingsSection, SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'

const SESSION_CONTROL_CONFIG_KEY = 'agent.session_control'

type KirocrewConfigShape = { agent?: { session_control?: boolean } }

/** The gateway refuses (409) a write that `config.local.json` would shadow. "Try
 * again" cannot fix that, so the row names the file instead. Duck-typed on
 * `status`/`body` so a mocked `api/client` rejection works the same. */
function overlayOwned(e: unknown): boolean {
  const err = e as { status?: number; body?: string } | null
  if (err?.status !== 409 || typeof err.body !== 'string') return false
  try {
    return (JSON.parse(err.body) as { code?: string }).code === 'session_control_overlay_owned'
  } catch {
    return false
  }
}

export function SessionControlSection() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const [saveError, setSaveError] = useState('')
  const enabled = cfgQ.data?.agent?.session_control === true
  const mut = useMutation({
    mutationFn: (v: boolean) => api.patchConfig(SESSION_CONTROL_CONFIG_KEY, v),
    onSuccess: (_res, v) => {
      setSaveError('')
      // Write the saved value into the cache first, so the switch does not show
      // the pre-write value between the save and the refetch that confirms it.
      qc.setQueryData<KirocrewConfigShape>(['kirocrewConfig'], (old) => ({
        ...old,
        agent: { ...old?.agent, session_control: v },
      }))
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: (e) => setSaveError(i18nT(
      overlayOwned(e)
        ? 'pages.settings.sessionControlSection.overlay_owned'
        : 'pages.settings.sessionControlSection.failed_to_save',
    )),
  })

  return (
    <SettingsSection title={i18nT('pages.settings.sessionControlSection.title')}>
      {/* A failed config read draws no switch; say so, with the query's own
          retry, rather than leaving the row without a state or a way forward. */}
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
      <SettingsCard>
        {/* No switch until the server's value is known. A disabled Off while the
            read is in flight (or after it failed) would show a definite state for a
            default-On trust setting that nobody has read yet. Once a value has been
            read, a failed refetch keeps it: the query holds `data` in `error`
            status, so the switch stays, disabled until a read succeeds again. */}
        {cfgQ.data !== undefined ? (
          <SettingsToggle
            label={i18nT('pages.settings.sessionControlSection.session_control_tools')}
            description={i18nT('pages.settings.sessionControlSection.session_control_tools_desc')}
            hint={i18nT('pages.settings.sessionControlSection.session_control_tools_hint')}
            checked={enabled}
            onChange={(v) => mut.mutate(v)}
            disabled={!cfgQ.isSuccess || mut.isPending}
            configKey={SESSION_CONTROL_CONFIG_KEY}
          />
        ) : (
          <SettingsField
            label={i18nT('pages.settings.sessionControlSection.session_control_tools')}
            description={i18nT('pages.settings.sessionControlSection.session_control_tools_desc')}
            hint={i18nT('pages.settings.sessionControlSection.session_control_tools_hint')}
            configKey={SESSION_CONTROL_CONFIG_KEY}
          >
            {cfgQ.isPending && (
              <span role="status" className="text-[13px] text-muted" data-testid="session-control-loading">
                {i18nT('pages.settings.sessionControlSection.loading')}
              </span>
            )}
          </SettingsField>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}
