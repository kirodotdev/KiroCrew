/**
 * Settings → Chat → Model → Apps: one model picker per enabled app, writing
 * `apps.<name>.model`.
 *
 * The empty value INHERITS, so an app nobody picked for runs exactly as it would
 * without this control. A pick applies to the sessions the app owns and to its
 * background agents when nothing more specific chose a model: a model picked for
 * the session, the app's own pick (a Research campaign, the Spec Builder default)
 * and a model pinned on the agent or crew all still win.
 *
 * The rows write through the same config PATCH as the role-model rows, which
 * validates the id against what the account can run, and render the same
 * optimistic overlay (`useOptimisticConfigPaths`) so a pick shows at once.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsCard, SettingsSelect } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { appDisplayName } from '../../components/appstore/appManifest'
import type { InstalledApp } from '../../components/appstore/types'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

type AppsTable = Record<string, { model?: unknown } | undefined>
type ConfigWithApps = { apps?: AppsTable; agent?: { model?: unknown } }

/** The config path an app's model row writes. */
export function appModelPath(name: string): string {
  return `apps.${name}.model`
}

/** The stored pick for *name*, with both inherit spellings read as `''`. */
export function storedAppModel(apps: AppsTable | undefined, name: string): string {
  const raw = apps?.[name]?.model
  if (typeof raw !== 'string') return ''
  const model = raw.trim()
  return model === 'auto' ? '' : model
}

/**
 * The row's options: Inherit first, then every advertised model except `auto`,
 * then any kept value the list no longer carries. `auto` is left out because a
 * per-app pin of it would only shadow the chain it means to defer to; a retained
 * pin the account stopped advertising stays selectable so the row never claims
 * Inherit while the setting still names a model.
 */
export function appModelOptions(advertised: string[], kept: string[]): string[] {
  const opts = ['', ...advertised.filter(m => m && m !== 'auto')]
  for (const value of kept) {
    if (value && !opts.includes(value)) opts.push(value)
  }
  return opts
}

/** Enabled apps, in display-name order. */
export function appsForModelRows(apps: InstalledApp[]): InstalledApp[] {
  return apps
    .filter(app => app.enabled && app.name)
    .sort((a, b) => compareText(appDisplayName(a), appDisplayName(b)))
}

type Overlay = ReturnType<typeof useOptimisticConfigPaths>

function AppModelRow({
  app,
  server,
  models,
  inheritLabel,
  overlay,
  disabled,
  onFailure,
  onSupersede,
}: {
  app: InstalledApp
  server: string
  models: string[]
  inheritLabel: string
  overlay: Overlay
  disabled: boolean
  onFailure: (path: string, message: string) => void
  onSupersede: (path: string) => void
}) {
  const path = appModelPath(app.name)
  const name = appDisplayName(app)
  const mut = useMutation(
    overlay.mutationOpts<string>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (value: string) => api.patchConfig(path, value),
      path: () => path,
      displayValue: value => value,
      applyToCache: (cached, value) => setConfigPathValue(cached as ConfigWithApps, path, value),
      onFailure: () => onFailure(path, i18nT('pages.settings.appModels.failed_to_save', { app: name })),
      onSupersede,
    }),
  )
  const shown = overlay.shown(path, server)
  const options = appModelOptions(models, [server, shown])
  return (
    <SettingsSelect
      label={name}
      hint={i18nT('pages.settings.appModels.row_hint')}
      value={shown}
      options={options}
      optionLabels={options.map(m => (m === '' ? inheritLabel : m))}
      onChange={value => mut.mutate(value)}
      disabled={disabled}
    />
  )
}

/**
 * The Inherit option's label, naming what an app with no pick falls back to: the
 * Default Model row above (`agent.model`), or auto when that row is unset.
 */
export function inheritOptionLabel(globalModel: unknown): string {
  const model = typeof globalModel === 'string' ? globalModel.trim() : ''
  return model && model !== 'auto'
    ? i18nT('pages.settings.appModels.inherit_named', { model })
    : i18nT('pages.settings.appModels.inherit_auto')
}

export function AppModelsSection({ index }: { index?: number }) {
  const qc = useQueryClient()
  const overlay = useOptimisticConfigPaths(qc)
  const [failure, setFailure] = useState<{ path: string; message: string } | null>(null)
  const configQ = useQuery<ConfigWithApps>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const appsQ = useQuery({ queryKey: ['apps'], queryFn: () => api.listApps() })
  const modelsQ = useAvailableModelsQuery()
  const models = modelsQ.data.map(m => m.name)
  const rows = appsForModelRows(appsQ.data ?? [])
  const configured = configQ.data?.apps
  const inheritLabel = inheritOptionLabel(configQ.data?.agent?.model)

  return (
    <div className="mt-8" data-testid="app-models-section">
      <h4 className="text-base font-semibold text-text-strong">{i18nT('pages.settings.appModels.title')}</h4>
      <div className="text-[12px] text-muted mb-1">{i18nT('pages.settings.appModels.description')}</div>
      {/* No hand-off: this group mounts inside ChatPanel, whose `localRoleOther` /
          `localBudget` / `localKeepChars` drafts stay mounted while the Model page
          is open, so the agent-chat navigation would discard them. A refused save
          is retried by picking again, and the row already shows its stored value. */}
      <ErrorNotice
        variant="inline"
        className="mb-2"
        message={failure?.message ?? (appsQ.isError ? i18nT('pages.settings.appModels.load_failed') : '')}
        testId="app-models-error"
      />
      <SettingsCard index={index}>
        {rows.length === 0 && !appsQ.isPending && !appsQ.isError && (
          <div className="text-[13px] text-muted py-2">{i18nT('pages.settings.appModels.none_enabled')}</div>
        )}
        {rows.map(app => (
          <AppModelRow
            key={app.name}
            app={app}
            server={storedAppModel(configured, app.name)}
            models={models}
            inheritLabel={inheritLabel}
            overlay={overlay}
            disabled={!configQ.isSuccess}
            onFailure={(path, message) => setFailure({ path, message })}
            onSupersede={path => setFailure(prev => (prev?.path === path ? null : prev))}
          />
        ))}
      </SettingsCard>
    </div>
  )
}
