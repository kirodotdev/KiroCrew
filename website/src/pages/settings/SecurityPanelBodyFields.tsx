/**
 * Settings -> Security -> Message body fields: the MCP tool fields the tool_input
 * gate scans as a document instead of a command line (`security.mcp_body_fields`).
 *
 * Three lists, all from the owner-gated `/api/security/mcp-body-fields`:
 * - fields the gate refused for size, each with "Allow this field from now on";
 * - fields the owner allowed, each with Remove;
 * - the built-in defaults, read-only.
 *
 * There is no text input: the server adds only a field the gate itself refused,
 * so the owner never types a field name and nothing on a page can nominate one.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { retryPolicy } from '../../api/queryClient'
import { Btn, PanelSectionHeader } from '../../components/ui'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { api, type McpBodyFieldRow, type McpBodyFieldsData } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtDateTime } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import { isOwnerGateRefusal } from './SecurityPanelRedaction'

const QUERY_KEY = ['mcp-body-fields']

function rowKey(row: McpBodyFieldRow): string {
  return JSON.stringify([row.server, row.tool, row.field])
}

/** One row: the tool identity and the field, verbatim (stored text, no translator). */
function FieldName({ row }: { row: McpBodyFieldRow }) {
  return (
    <div className="min-w-0">
      <div className="truncate font-mono text-[13px] text-text">@{row.server}/{row.tool}</div>
      <div className="text-[11px] text-muted">
        {i18nT('pages.settings.securityPanel.bodyFields.field_name', { field: row.field })}
      </div>
    </div>
  )
}

function BodyFieldsCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError, error } = useQuery<McpBodyFieldsData>({
    queryKey: QUERY_KEY,
    queryFn: api.mcpBodyFields,
    // A non-owner's refusal is a fact about the viewer; a retry writes another
    // audited refusal.
    retry: (count, err) => !isOwnerGateRefusal(err) && retryPolicy(count, err),
  })
  const allow = useMutation({
    mutationFn: (row: McpBodyFieldRow) => api.allowMcpBodyField(row),
    onSuccess: next => qc.setQueryData(QUERY_KEY, next),
    onSettled: () => void qc.invalidateQueries({ queryKey: QUERY_KEY }),
  })
  const remove = useMutation({
    mutationFn: (row: McpBodyFieldRow) => api.removeMcpBodyField(row),
    onSuccess: next => qc.setQueryData(QUERY_KEY, next),
    onSettled: () => void qc.invalidateQueries({ queryKey: QUERY_KEY }),
  })
  const view = isError ? undefined : data
  const busy = allow.isPending || remove.isPending

  return (
    <SettingsCard>
      <p className="m-0 mb-3 text-[12px] leading-relaxed text-muted">
        {i18nT('pages.settings.securityPanel.bodyFields.desc')}
      </p>
      {isError && (
        <ErrorNotice
          variant="inline"
          askAgent
          message={i18nT(isOwnerGateRefusal(error)
            ? 'pages.settings.securityPanel.bodyFields.owner_only'
            : 'pages.settings.securityPanel.bodyFields.load_failed')}
          testId="body-fields-read-failed"
        />
      )}
      <ErrorNotice
        variant="inline"
        askAgent
        message={allow.isError || remove.isError ? i18nT('pages.settings.securityPanel.bodyFields.write_failed') : null}
        onDismiss={() => { allow.reset(); remove.reset() }}
      />
      {!isLoading && view && (
        <div className="flex flex-col gap-4">
          <div>
            <PanelSectionHeader className="mb-1" label={i18nT('pages.settings.securityPanel.bodyFields.refused_title')} count={view.refused.length} />
            {view.refused.length === 0 ? (
              <div className="text-[12px] text-muted" data-testid="body-fields-refused-empty">
                {i18nT('pages.settings.securityPanel.bodyFields.refused_empty')}
              </div>
            ) : (
              <div className="divide-y divide-border" data-testid="body-fields-refused">
                {view.refused.map(row => (
                  <div key={rowKey(row)} className="flex items-center justify-between gap-3 py-2">
                    <div className="min-w-0">
                      <FieldName row={row} />
                      <div className="text-[11px] text-muted">
                        {i18nT('pages.settings.securityPanel.bodyFields.refused_at', { time: fmtDateTime(row.refused_at) })}
                      </div>
                    </div>
                    <Btn
                      primary
                      disabled={busy}
                      onClick={() => allow.mutate({ server: row.server, tool: row.tool, field: row.field })}
                      data-testid="body-fields-allow"
                    >
                      {i18nT('pages.settings.securityPanel.bodyFields.allow')}
                    </Btn>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>
            <PanelSectionHeader className="mb-1" label={i18nT('pages.settings.securityPanel.bodyFields.owner_title')} count={view.owner.length} />
            {view.owner.length === 0 ? (
              <div className="text-[12px] text-muted" data-testid="body-fields-owner-empty">
                {i18nT('pages.settings.securityPanel.bodyFields.owner_empty')}
              </div>
            ) : (
              <div className="divide-y divide-border" data-testid="body-fields-owner">
                {view.owner.map(row => (
                  <div key={rowKey(row)} className="flex items-center justify-between gap-3 py-2">
                    <FieldName row={row} />
                    <Btn
                      danger
                      disabled={busy}
                      onClick={() => remove.mutate({ server: row.server, tool: row.tool, field: row.field })}
                      data-testid="body-fields-remove"
                    >
                      {i18nT('pages.settings.securityPanel.bodyFields.remove')}
                    </Btn>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>
            <PanelSectionHeader className="mb-1" label={i18nT('pages.settings.securityPanel.bodyFields.defaults_title')} count={view.defaults.length} />
            <div className="divide-y divide-border" data-testid="body-fields-defaults">
              {view.defaults.map(row => (
                <div key={rowKey(row)} className="py-2">
                  <FieldName row={row} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </SettingsCard>
  )
}

/** Settings -> Security -> Message body fields. */
export function BodyFieldsSection() {
  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.bodyFields.section_title')}>
      <BodyFieldsCard />
    </SettingsSection>
  )
}
