/**
 * Settings > Developer > Feature previews > ACP runtime sharing: one switch per
 * harness whose chats may share an agent process, shown only while that preview
 * is on (`FeaturePreviewsSection.tsx` mounts it under the preview's own switch).
 *
 * Server-side: each switch PATCHes `agent.chat_runtime_sharing_harnesses.<name>`
 * and reads back through the shared `['kirocrewConfig']` query. The gateway reads
 * the same key at each chat start (`acp.chat_runtime_sharing.
 * chat_runtime_sharing_harness_enabled`), so a switch off keeps that harness at
 * one process per chat even with the preview on.
 *
 * `CHAT_SHARING_HARNESSES` mirrors the backend's `CHAT_RUNTIME_SHARING_HARNESSES`
 * (`agent_sdk/backends_sessions.py`): a harness that may share is one row there
 * and one row here. The default here only fills the gap before the config read
 * answers; the gateway's own config read always carries every row.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'

/** One row per harness that may share: its backend name, default, label and line. */
export const CHAT_SHARING_HARNESSES: ReadonlyArray<{
  name: string
  defaultOn: boolean
  label: () => string
  description: () => string
}> = [
  {
    name: 'kas',
    defaultOn: true,
    label: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_kas'),
    description: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_kas_desc'),
  },
  {
    name: 'codex',
    defaultOn: false,
    label: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_codex'),
    description: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_codex_desc'),
  },
  {
    name: 'claude',
    defaultOn: false,
    label: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_claude'),
    description: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_claude_desc'),
  },
  {
    name: 'goose',
    defaultOn: false,
    label: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_goose'),
    description: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_goose_desc'),
  },
  {
    name: 'deepseek',
    defaultOn: false,
    label: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_deepseek'),
    description: () => i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_deepseek_desc'),
  },
]

export const CHAT_SHARING_HARNESSES_CONFIG_KEY = 'agent.chat_runtime_sharing_harnesses'

type KirocrewConfigShape = { agent?: { chat_runtime_sharing_harnesses?: Record<string, unknown> } }

export function ChatRuntimeSharingHarnessRows() {
  const qc = useQueryClient()
  const cfgQ = useQuery<KirocrewConfigShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const [saveFailed, setSaveFailed] = useState(false)
  const stored = cfgQ.data?.agent?.chat_runtime_sharing_harnesses
  const mut = useMutation({
    mutationFn: ({ name, on }: { name: string; on: boolean }) =>
      api.patchConfig(`${CHAT_SHARING_HARNESSES_CONFIG_KEY}.${name}`, on),
    onSuccess: () => {
      setSaveFailed(false)
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    onError: () => setSaveFailed(true),
  })

  return (
    <div className="pl-4" data-testid="chat-sharing-harnesses">
      {CHAT_SHARING_HARNESSES.map(h => {
        const value = stored?.[h.name]
        // Until the config read answers, the switch shows off rather than its
        // default: a failed read must not look like a stored "on".
        const checked = typeof value === 'boolean' ? value : cfgQ.isSuccess && h.defaultOn
        return (
          <SettingsToggle
            key={h.name}
            label={h.label()}
            description={h.description()}
            checked={checked}
            onChange={on => mut.mutate({ name: h.name, on })}
            disabled={!cfgQ.isSuccess || mut.isPending}
          />
        )
      })}
      {/* A failed config read leaves the switches disabled; say so, with the
          query's own retry. askAgent: a toggle holds no draft, so the hand-off
          loses nothing. */}
      {cfgQ.isError && (
        <ErrorNotice
          message={i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_load_failed')}
          askAgent
          className="mt-2"
          testId="chat-sharing-harness-load-error"
          footer={
            <Btn onClick={() => { void cfgQ.refetch() }} disabled={cfgQ.isFetching} data-testid="chat-sharing-harness-retry">
              {i18nT('pages.settings.crewmatesSection.retry')}
            </Btn>
          }
        />
      )}
      {saveFailed && (
        <ErrorNotice
          message={i18nT('pages.developer.featurePreviewsTab.chat_runtime_sharing_harness_save_failed')}
          onDismiss={() => setSaveFailed(false)}
          askAgent
          className="mt-2"
          testId="chat-sharing-harness-save-error"
        />
      )}
    </div>
  )
}
