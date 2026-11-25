import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { SettingsCard, SettingsSection, SettingsToggle } from '../../components/settings'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import ErrorNotice from '../../components/ErrorNotice'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'

/** The gateway config value the MCP sign-ins switch writes. */
const KAS_PERSIST_MCP_SIGN_IN = 'agent.kas_persist_mcp_sign_in'

/**
 * Whether Kiro Agent (KAS) keeps remote MCP sign-ins across sessions (#26891),
 * at the foot of Settings → Connections. The gateway's own config value, PATCHed
 * like any other; a KAS process reads it when it starts, so the schema's restart
 * pill sits beside the switch. Hidden until the config read answers; a failed
 * read says so.
 */
export function McpSignInsSection() {
  const qc = useQueryClient()
  const cfgQ = useQuery<{ agent?: { kas_persist_mcp_sign_in?: boolean } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const saveMut = useMutation({
    mutationFn: (value: boolean) => api.patchConfig(KAS_PERSIST_MCP_SIGN_IN, value),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })
  const stored = cfgQ.data?.agent?.kas_persist_mcp_sign_in
  if (!cfgQ.isError && typeof stored !== 'boolean') return null

  return (
    <SettingsSection title={i18nT('pages.settings.connectionsPanel.sign_ins_title')}>
      {/* No hand-off: a Connections card above may hold a typed OAuth client ID
          or secret that is not saved yet, and the hand-off's navigation drops it. */}
      {cfgQ.isError && (
        <ErrorNotice message={i18nT('pages.settings.connectionsPanel.remember_sign_ins_load_failed')} />
      )}
      {typeof stored === 'boolean' && (
        <SettingsCard>
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-0 flex-1">
              <SettingsToggle
                settingId="connections.remember-mcp-sign-ins-across-sessions"
                configKey="agent.kas_persist_mcp_sign_in"
                label={i18nT('pages.settings.connectionsPanel.remember_sign_ins')}
                hint={i18nT('pages.settings.connectionsPanel.remember_sign_ins_hint')}
                checked={stored}
                onChange={value => saveMut.mutate(value)}
                disabled={saveMut.isPending}
              />
            </div>
            <SchemaRestartBadge configKey={KAS_PERSIST_MCP_SIGN_IN} />
          </div>
          {/* No hand-off: the same unsaved OAuth app drafts as the notice above. */}
          {saveMut.isError && (
            <ErrorNotice
              message={i18nT('pages.settings.connectionsPanel.remember_sign_ins_save_failed')}
              variant="inline"
              onDismiss={() => saveMut.reset()}
            />
          )}
        </SettingsCard>
      )}
    </SettingsSection>
  )
}
