import { useState, useEffect, useRef, useId, createContext, useContext } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useAvailableModels } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel } from '../../lib/effort'
import SimpleSelect from '../../components/SimpleSelect'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'

// ── Constants matching backend _EDITABLE_CONFIG bounds ──
const EMBED_RATE_MIN = 0
const EMBED_RATE_MAX = 10000
const EMBED_RATE_DEFAULT = 120

// Mirrors EXTRACTION_POOL_SIZE_MIN/MAX + the default on the backend
// (config/loader.py clamp and handlers/core.py _EDITABLE_CONFIG bounds).
const POOL_SIZE_MIN = 1
const POOL_SIZE_MAX = 10
const POOL_SIZE_DEFAULT = 3

/**
 * Knowledge Library settings tab — ingestion cost & performance controls.
 *
 * Fields: embedding rate limit, extraction model, extraction effort, URL
 * fetch effort, extraction pool size. Effort here is NOT gated on a model
 * pick: the pools gate per-worker against whatever model the backend serves,
 * and an unsupported model simply keeps the provider default. '' on an effort
 * selects inherits the Background Effort role policy (then each pool's
 * default) — resolved server-side by llm_pool._get_workload_effort.
 * Reads/writes via the same PATCH /api/config/kirocrew endpoint as the
 * Settings page.
 */
export function SettingsTab() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')

  // ── Server config ──
  const cfgQ = useQuery<{
    knowledge?: {
      auto_add_documents?: boolean
      auto_ingest_artifacts?: boolean
      embed_rate_limit?: number
      extraction_model?: string
      extraction_effort?: string
      fetch_effort?: string
      extraction_pool_size?: number
    }
  }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const cfg = cfgQ.data?.knowledge

  // ── Mutation ──
  const patchMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: number | string | boolean }) =>
      api.patchConfig(path, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
    onError: () => {
      setSaveError(i18nT('pages.knowledge.settings.save_failed'))
      // Revert all local inputs to last-known server values
      setLocalEmbedRate(String(cfg?.embed_rate_limit ?? EMBED_RATE_DEFAULT))
      setLocalPoolSize(String(cfg?.extraction_pool_size ?? POOL_SIZE_DEFAULT))
    },
  })
  const disabled = !cfgQ.isSuccess || patchMut.isPending

  // ── Local state for number inputs (commit on blur) ──
  const [localEmbedRate, setLocalEmbedRate] = useState('')
  const [localPoolSize, setLocalPoolSize] = useState('')

  const initRef = useRef(false)
  useEffect(() => {
    if (cfgQ.data && !initRef.current) {
      initRef.current = true
      setLocalEmbedRate(String(cfg?.embed_rate_limit ?? EMBED_RATE_DEFAULT))
      setLocalPoolSize(String(cfg?.extraction_pool_size ?? POOL_SIZE_DEFAULT))
    }
  }, [cfgQ.data, cfg])

  // ── Commit helpers ──
  function commitNumber(
    raw: string,
    path: string,
    min: number,
    max: number,
    fallback: number,
    setLocal: (v: string) => void,
  ) {
    const trimmed = raw.trim()
    if (trimmed === '') {
      setLocal(String(fallback))
      return
    }
    const n = Number(trimmed)
    if (!Number.isInteger(n) || n < min || n > max) {
      setLocal(String(fallback))
      return
    }
    patchMut.mutate({ path, value: n })
  }

  // ── Model dropdown ──
  const availableModels = useAvailableModels()
  const modelOptions = availableModels.map(m => m.name)
  const currentModel = cfg?.extraction_model || 'auto'
  if (!modelOptions.includes(currentModel)) modelOptions.unshift(currentModel)

  // ── Effort dropdowns ──
  // Same workload-effort vocabulary as the Settings ▸ Chat role-effort rows;
  // '' is the inherit option labelled 'Default' (effortLabel), matching
  // the row hints. Index-paired labels: SimpleSelect matches by position.
  // Strings, not the EFFORT_LEVELS literal union: cfg values come from a JSON
  // payload and may hold anything.
  const effortOptions: string[] = [...EFFORT_LEVELS]
  const effortChoiceLabels = (): string[] => effortOptions.map(effortLabel)

  return (
    <div className="max-w-xl space-y-1 animate-rise">
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-4" />
      {cfgQ.isError && (
        <div className="mb-4 text-[13px] text-danger flex items-center gap-2">
          {i18nT('pages.knowledge.settings.load_failed')}
          <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => cfgQ.refetch()}>
            {i18nT('pages.knowledge.settings.retry')}
          </button>
        </div>
      )}

      <h3 className="text-[15px] font-semibold text-text mb-3">
        {i18nT('pages.knowledge.settings.title')}
      </h3>
      <p className="text-[12px] text-muted mb-4">
        {i18nT('pages.knowledge.settings.description')}
      </p>

      {/* Auto-ingest toggles */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.auto_add_label')}
        description={i18nT('pages.knowledge.settings.auto_add_desc')}
      >
        <Toggle checked={cfg?.auto_add_documents ?? false} onChange={v => patchMut.mutate({ path: 'knowledge.auto_add_documents', value: v })} disabled={disabled} />
      </SettingRow>

      <SettingRow
        label={i18nT('pages.knowledge.settings.auto_artifacts_label')}
        description={i18nT('pages.knowledge.settings.auto_artifacts_desc')}
      >
        <Toggle checked={cfg?.auto_ingest_artifacts ?? false} onChange={v => patchMut.mutate({ path: 'knowledge.auto_ingest_artifacts', value: v })} disabled={disabled} />
      </SettingRow>

      {/* Embedding rate limit */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.embed_rate_label')}
        description={i18nT('pages.knowledge.settings.embed_rate_desc')}
      >
        <div className="flex items-center gap-1.5">
          <NumberInput
            value={localEmbedRate}
            onChange={setLocalEmbedRate}
            onBlur={() => commitNumber(
              localEmbedRate, 'knowledge.embed_rate_limit',
              EMBED_RATE_MIN, EMBED_RATE_MAX,
              cfg?.embed_rate_limit ?? EMBED_RATE_DEFAULT,
              setLocalEmbedRate,
            )}
            min={EMBED_RATE_MIN}
            max={EMBED_RATE_MAX}
            step={10}
            disabled={disabled}
          />
          <span className="text-[11px] text-muted">/min</span>
        </div>
      </SettingRow>

      {/* Extraction model */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.model_label')}
        description={i18nT('pages.knowledge.settings.model_desc')}
      >
        <SimpleSelect
          options={modelOptions}
          optionLabels={modelOptions.map(m => m === 'auto' ? i18nT('pages.knowledge.settings.model_auto') : m)}
          value={currentModel}
          onChange={v => patchMut.mutate({
            path: 'knowledge.extraction_model',
            value: v === 'auto' ? '' : v,
          })}
          aria-label={i18nT('pages.knowledge.settings.model_label')}
          disabled={disabled}
        />
      </SettingRow>

      {/* Extraction effort */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.extraction_effort_label')}
        description={i18nT('pages.knowledge.settings.effort_hint')}
      >
        <SimpleSelect
          options={effortOptions}
          optionLabels={effortChoiceLabels()}
          value={cfg?.extraction_effort ?? ''}
          onChange={v => patchMut.mutate({ path: 'knowledge.extraction_effort', value: v })}
          aria-label={i18nT('pages.knowledge.settings.extraction_effort_label')}
          triggerFallback={effortChoiceLabels()[effortOptions.indexOf(cfg?.extraction_effort ?? '')]}
          disabled={disabled}
        />
      </SettingRow>

      {/* URL fetch effort */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.fetch_effort_label')}
        description={i18nT('pages.knowledge.settings.fetch_effort_hint')}
      >
        <SimpleSelect
          options={effortOptions}
          optionLabels={effortChoiceLabels()}
          value={cfg?.fetch_effort ?? ''}
          onChange={v => patchMut.mutate({ path: 'knowledge.fetch_effort', value: v })}
          aria-label={i18nT('pages.knowledge.settings.fetch_effort_label')}
          triggerFallback={effortChoiceLabels()[effortOptions.indexOf(cfg?.fetch_effort ?? '')]}
          disabled={disabled}
        />
      </SettingRow>

      {/* Extraction pool size */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.pool_size_label')}
        description={i18nT('pages.knowledge.settings.pool_size_desc')}
      >
        <div className="flex items-center gap-2">
          <NumberInput
            value={localPoolSize}
            onChange={setLocalPoolSize}
            onBlur={() => commitNumber(
              localPoolSize, 'knowledge.extraction_pool_size',
              POOL_SIZE_MIN, POOL_SIZE_MAX,
              cfg?.extraction_pool_size ?? POOL_SIZE_DEFAULT,
              setLocalPoolSize,
            )}
            min={POOL_SIZE_MIN}
            max={POOL_SIZE_MAX}
            step={1}
            disabled={disabled}
          />
          <span className="text-[10px] text-warn bg-warn-subtle px-1.5 py-0.5 rounded">
            {i18nT('pages.knowledge.settings.requires_restart')}
          </span>
        </div>
      </SettingRow>
    </div>
  )
}

// ── Sub-components ──

/**
 * Id of the enclosing row's visible label text, so the control on the other
 * side of the row can name itself with `aria-labelledby` instead of repeating
 * the label as an `aria-label` — one string, and it cannot drift from what the
 * user reads.
 *
 * Carried by context rather than by prop because several rows wrap their
 * control in a layout `<div>` (the unit suffix, the restart badge), so the row
 * cannot hand the id to the control directly.
 */
const RowLabelIdContext = createContext<string | undefined>(undefined)

function SettingRow({ label, description, children }: {
  label: string
  description: string
  children: React.ReactNode
}) {
  const labelId = useId()
  return (
    <div className="flex items-start justify-between py-3 border-b border-border last:border-b-0 gap-4">
      <div className="flex-1 min-w-0">
        <div id={labelId} className="text-[13px] font-medium text-text">{label}</div>
        <div className="text-[11px] text-muted mt-0.5 leading-relaxed">{description}</div>
      </div>
      <div className="shrink-0">
        <RowLabelIdContext.Provider value={labelId}>{children}</RowLabelIdContext.Provider>
      </div>
    </div>
  )
}

function NumberInput({ value, onChange, onBlur, min, max, step, disabled }: {
  value: string
  onChange: (v: string) => void
  onBlur: () => void
  min: number
  max: number
  step: number
  disabled: boolean
}) {
  const labelId = useContext(RowLabelIdContext)
  return (
    <input
      type="number"
      aria-labelledby={labelId}
      className="w-[80px] px-2 py-1 text-[12px] text-right border border-border rounded-md bg-bg text-text"
      value={value}
      onChange={e => onChange(e.target.value)}
      onBlur={onBlur}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
    />
  )
}

function Toggle({ checked, onChange, disabled }: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled: boolean
}) {
  const labelId = useContext(RowLabelIdContext)
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-labelledby={labelId}
      onClick={() => !disabled && onChange(!checked)}
      disabled={disabled}
      className={`relative w-[36px] h-[20px] rounded-full transition-colors cursor-pointer border-none ${checked ? 'bg-accent' : 'bg-border'} ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      <span className={`absolute top-[2px] w-[16px] h-[16px] rounded-full bg-card transition-transform ${checked ? 'left-[18px]' : 'left-[2px]'}`} />
    </button>
  )
}
