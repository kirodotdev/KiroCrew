import { useCallback, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { KeyRound } from 'lucide-react'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { readAgentEnvMap } from './agentEnvMap'
import { CONFIG_QUERY_KEY, SECRETS_QUERY_KEY, VaultEnvField } from './VaultEnvField'

/** The settings-search anchor and test id of the section. */
export const DEEPSEEK_KEY_ANCHOR = 'deepseek-key'

/** The config path the field writes its reference to. */
export const DEEPSEEK_ENV_KEY = 'agent.deepseek_env'

/** The variable the harness's default provider route reads; the only one the
 *  gateway lets the dashboard map under `agent.deepseek_env`. */
export const DEEPSEEK_KEY_ENV = 'DEEPSEEK_API_KEY'

/** The one vault entry the field saves to, so saving again replaces the key. */
export const DEEPSEEK_KEY_VAULT_NAME = 'DEEPSEEK_HARNESS_API_KEY'

const readDeepSeekEnv = (cfg: unknown) => readAgentEnvMap(cfg, 'deepseek_env')

/**
 * Settings > Agent Harness > DeepSeek Harness: the provider key its default
 * provider route needs. The key goes to the Secrets vault under one fixed name and
 * only its `secret://` reference to `agent.deepseek_env`, so the field shows saved
 * or not saved, never the key. Any other entry of that map (another provider
 * route's key, set in the config file) is left alone: each write names only
 * `DEEPSEEK_API_KEY`.
 */
export function DeepSeekKeyCard() {
  const configQ = useQuery({ queryKey: CONFIG_QUERY_KEY, queryFn: () => api.kirocrewConfig() })
  const secretsQ = useQuery<{ names?: string[] }>({
    queryKey: SECRETS_QUERY_KEY,
    queryFn: () => api.secretsList(),
  })
  // A Save's vault write and its reference never interleave with a Remove.
  const tail = useRef<Promise<unknown>>(Promise.resolve())
  const serial = useCallback(<T,>(write: () => Promise<T>): Promise<T> => {
    const run = tail.current.then(write, write)
    tail.current = run.catch(() => undefined)
    return run
  }, [])
  const title = i18nT('pages.developer.deepseekKey.title')

  return (
    <section
      aria-label={title}
      className="border-t border-border pt-3"
      data-setting-key={DEEPSEEK_KEY_ANCHOR}
      data-testid="deepseek-key-card"
    >
      <h3 className="mb-1 flex items-center gap-1.5 text-[13px] font-semibold text-text-strong">
        <KeyRound className="lucide-inline" aria-hidden="true" />
        {title}
      </h3>
      <p className="mb-1 text-[12px] leading-relaxed text-muted">
        {i18nT('pages.developer.deepseekKey.intro')}
      </p>
      {/* No hand-off: the field below may hold an unsaved key. */}
      {(configQ.isError || secretsQ.isError) && (
        <ErrorNotice message={i18nT('pages.developer.deepseekKey.read_failed')} variant="inline" />
      )}
      <VaultEnvField
        target={{
          configKey: DEEPSEEK_ENV_KEY,
          env: DEEPSEEK_KEY_ENV,
          vaultName: DEEPSEEK_KEY_VAULT_NAME,
          label: DEEPSEEK_KEY_ENV,
          description: i18nT('pages.developer.deepseekKey.desc_api_key'),
          missingText: i18nT('pages.developer.deepseekKey.value_missing'),
          readEnv: readDeepSeekEnv,
          testIdPrefix: 'deepseek',
        }}
        env={readDeepSeekEnv(configQ.data)}
        vaultNames={secretsQ.data?.names ?? []}
        known={configQ.isSuccess && secretsQ.isSuccess}
        serial={serial}
      />
    </section>
  )
}
