import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { useState } from 'react'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsSelect } from '../../components/settings'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import { i18nT } from '../../i18n/t'

/** The config field this card writes. */
export const KIRO_CLI_ENGINE_CONFIG_KEY = 'agent.kiro_cli_engine'
/** Follow the default rule: Kiro CLI runs on KAS once kiro-cli offers v3. */
export const KIRO_CLI_ENGINE_AUTO = 'auto'
/** Keep Kiro CLI on kiro-cli's v2 engine while kiro-cli still offers it. */
export const KIRO_CLI_ENGINE_V2 = 'v2'
const ENGINES = [KIRO_CLI_ENGINE_AUTO, KIRO_CLI_ENGINE_V2] as const

const CONFIG_QUERY_KEY = ['kirocrewConfig']

/** `agent.kiro_cli_engine` as the picker shows it: `v2` only for that exact value. */
export function readKiroCliEngine(cfg: unknown): (typeof ENGINES)[number] {
  const agent = (cfg as { agent?: unknown } | undefined)?.agent
  const raw = (agent as { kiro_cli_engine?: unknown } | undefined)?.kiro_cli_engine
  return raw === KIRO_CLI_ENGINE_V2 ? KIRO_CLI_ENGINE_V2 : KIRO_CLI_ENGINE_AUTO
}

/**
 * Settings > Agent Harness, inside the Kiro CLI row: which kiro-cli engine a Kiro
 * CLI choice runs. Automatic follows the default rule; V2 keeps the choice on
 * kiro-cli's v2 engine until kiro-cli removes it, so the option is labelled as
 * temporary.
 *
 * An ordinary config PATCH. The setting is boot-only (the running process keeps
 * its engine), so after a save the card says a restart is pending, as the KAS
 * bundle card does.
 */
export function KiroCliEngineCard() {
  const qc = useQueryClient()
  const configQ = useQuery({ queryKey: CONFIG_QUERY_KEY, queryFn: () => api.kirocrewConfig() })
  const saved = readKiroCliEngine(configQ.data)
  const [restartPending, setRestartPending] = useState(false)
  const saveMut = useMutation({
    mutationFn: (value: string) => api.patchConfig(KIRO_CLI_ENGINE_CONFIG_KEY, value),
    onSuccess: () => setRestartPending(true),
    onSettled: () => qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
  })
  // The pick on screen while its write is in flight, so the picker follows the click.
  const shown = saveMut.isPending && saveMut.variables ? saveMut.variables : saved

  if (!configQ.data) return null

  return (
    <div className="space-y-2" data-testid="kiro-cli-engine-card">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <SettingsSelect
            configKey={KIRO_CLI_ENGINE_CONFIG_KEY}
            label={i18nT('pages.developer.kiroCliEngine.label')}
            description={i18nT('pages.developer.kiroCliEngine.description')}
            value={shown}
            options={[...ENGINES]}
            optionLabels={[
              i18nT('pages.developer.kiroCliEngine.option_auto'),
              i18nT('pages.developer.kiroCliEngine.option_v2'),
            ]}
            onChange={v => {
              if (v !== saved) saveMut.mutate(v)
            }}
            disabled={saveMut.isPending}
          />
        </div>
        <span className={restartPending ? 'invisible' : undefined} aria-hidden={restartPending || undefined}>
          <SchemaRestartBadge configKey={KIRO_CLI_ENGINE_CONFIG_KEY} />
        </span>
      </div>
      {restartPending && (
        <div
          className="flex items-center gap-2 text-[12px] text-warn"
          role="status"
          data-testid="kiro-cli-engine-restart-pending"
        >
          <RotateCcw className="lucide-inline" aria-hidden="true" />
          <span>{i18nT('pages.developer.kiroCliEngine.saved_restart_pending')}</span>
        </div>
      )}
      <ErrorNotice
        message={saveMut.isError ? i18nT('pages.developer.kiroCliEngine.save_failed') : undefined}
        variant="inline"
        askAgent
      />
    </div>
  )
}
