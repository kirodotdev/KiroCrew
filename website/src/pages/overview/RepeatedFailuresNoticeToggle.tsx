import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/** The config field this switch writes (`MemoryConfig.repeated_failures_notice`). */
export const REPEATED_FAILURES_NOTICE_KEY = 'memory.repeated_failures_notice'

/**
 * The opt-in weekly bell note about repeated tool failures, inside the Repeated
 * failures card it links to. Off unless the stored value is exactly `true`, the
 * same reading the gateway makes, so the switch never shows on while the note
 * is off. Saved through the ordinary config PATCH, like the other Settings rows.
 */
export default function RepeatedFailuresNoticeToggle() {
  const qc = useQueryClient()
  const cfgQ = useQuery<{ memory?: { repeated_failures_notice?: unknown } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const save = useMutation({
    mutationFn: (value: boolean) => api.patchConfig(REPEATED_FAILURES_NOTICE_KEY, value),
    onSettled: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })
  const checked = cfgQ.data?.memory?.repeated_failures_notice === true
  return (
    <div className="mb-2">
      <SettingsToggle
        configKey={REPEATED_FAILURES_NOTICE_KEY}
        label={i18nT('pages.overview.memoryFailuresCard.notice_label')}
        description={i18nT('pages.overview.memoryFailuresCard.notice_description')}
        checked={checked}
        onChange={value => save.mutate(value)}
        disabled={!cfgQ.isSuccess}
        busy={save.isPending}
      />
      {/* No hand-off: a draft lesson in the card around this switch may be unsaved. */}
      <ErrorNotice
        title={i18nT('pages.overview.memoryFailuresCard.notice_load_failed')}
        message={cfgQ.isError ? (cfgQ.error instanceof Error ? cfgQ.error.message : String(cfgQ.error)) : null}
        askAgent={false}
        footer={<Btn type="button" onClick={() => void cfgQ.refetch()}>{i18nT('pages.developer.agentBackendTab.retry')}</Btn>}
      />
      <ErrorNotice
        title={i18nT('pages.overview.memoryFailuresCard.notice_save_failed')}
        message={save.error ? (save.error instanceof Error ? save.error.message : String(save.error)) : null}
        onDismiss={() => save.reset()}
        askAgent={false}
      />
    </div>
  )
}
