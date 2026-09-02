import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useAvailableModels } from '../../hooks/useAvailableModels'
import SimpleSelect from '../../components/SimpleSelect'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'

// ── Constants matching backend _EDITABLE_CONFIG bounds ──
const CHUNK_BUDGET_MIN = 0
const CHUNK_BUDGET_MAX = 10000
const CHUNK_BUDGET_DEFAULT = 150

const MAX_SOURCES_MIN = 0
const MAX_SOURCES_MAX = 1000
const MAX_SOURCES_DEFAULT = 50

const EMBED_RATE_MIN = 0
const EMBED_RATE_MAX = 10000
const EMBED_RATE_DEFAULT = 120

const POOL_SIZE_MIN = 1
const POOL_SIZE_MAX = 10
const POOL_SIZE_DEFAULT = 3

/**
 * Knowledge Library settings tab — ingestion cost & performance controls.
 *
 * Fields: per-source chunk limit, max sources, embedding rate limit,
 * extraction model, extraction pool size. Reads/writes via the same
 * PATCH /api/config/kirocrew endpoint as the Settings page.
 */
export function SettingsTab() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')

  // ── Server config ──
  const cfgQ = useQuery<{
    knowledge?: {
      auto_add_documents?: boolean
      auto_register_project_docs?: boolean
      auto_ingest_artifacts?: boolean
      auto_ingest_chunk_budget?: number
      max_sources?: number
      embed_rate_limit?: number
      extraction_model?: string
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
      setLocalChunkBudget(String(cfg?.auto_ingest_chunk_budget ?? CHUNK_BUDGET_DEFAULT))
      setLocalMaxSources(String(cfg?.max_sources ?? MAX_SOURCES_DEFAULT))
      setLocalEmbedRate(String(cfg?.embed_rate_limit ?? EMBED_RATE_DEFAULT))
      setLocalPoolSize(String(cfg?.extraction_pool_size ?? POOL_SIZE_DEFAULT))
    },
  })
  const disabled = !cfgQ.isSuccess || patchMut.isPending

  // ── Local state for number inputs (commit on blur) ──
  const [localChunkBudget, setLocalChunkBudget] = useState('')
  const [localMaxSources, setLocalMaxSources] = useState('')
  const [localEmbedRate, setLocalEmbedRate] = useState('')
  const [localPoolSize, setLocalPoolSize] = useState('')

  const initRef = useRef(false)
  useEffect(() => {
    if (cfgQ.data && !initRef.current) {
      initRef.current = true
      setLocalChunkBudget(String(cfg?.auto_ingest_chunk_budget ?? CHUNK_BUDGET_DEFAULT))
      setLocalMaxSources(String(cfg?.max_sources ?? MAX_SOURCES_DEFAULT))
      setLocalEmbedRate(String(cfg?.embed_rate_limit ?? EMBED_RATE_DEFAULT))
      setLocalPoolSize(String(cfg?.extraction_pool_size ?? POOL_SIZE_DEFAULT))
    }
  }, [cfgQ.data, cfg])

  // ── Model dropdown ──
  const availableModels = useAvailableModels()
  const modelOptions = availableModels.map(m => m.name)
  const currentModel = cfg?.extraction_model || 'auto'
  if (!modelOptions.includes(currentModel)) modelOptions.unshift(currentModel)

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
        label={i18nT('pages.knowledge.settings.auto_project_label')}
        description={i18nT('pages.knowledge.settings.auto_project_desc')}
      >
        <Toggle checked={cfg?.auto_register_project_docs ?? false} onChange={v => patchMut.mutate({ path: 'knowledge.auto_register_project_docs', value: v })} disabled={disabled} />
      </SettingRow>

      <SettingRow
        label={i18nT('pages.knowledge.settings.auto_artifacts_label')}
        description={i18nT('pages.knowledge.settings.auto_artifacts_desc')}
      >
        <Toggle checked={cfg?.auto_ingest_artifacts ?? false} onChange={v => patchMut.mutate({ path: 'knowledge.auto_ingest_artifacts', value: v })} disabled={disabled} />
      </SettingRow>

      {/* Per-source chunk limit */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.chunk_limit_label')}
        description={i18nT('pages.knowledge.settings.chunk_limit_desc')}
      >
        <NumberInput
          value={localChunkBudget}
          onChange={setLocalChunkBudget}
          onBlur={() => commitNumber(
            localChunkBudget, 'knowledge.auto_ingest_chunk_budget',
            CHUNK_BUDGET_MIN, CHUNK_BUDGET_MAX,
            cfg?.auto_ingest_chunk_budget ?? CHUNK_BUDGET_DEFAULT,
            setLocalChunkBudget,
          )}
          min={CHUNK_BUDGET_MIN}
          max={CHUNK_BUDGET_MAX}
          step={50}
          disabled={disabled}
        />
      </SettingRow>

      {/* Max sources */}
      <SettingRow
        label={i18nT('pages.knowledge.settings.max_sources_label')}
        description={i18nT('pages.knowledge.settings.max_sources_desc')}
      >
        <NumberInput
          value={localMaxSources}
          onChange={setLocalMaxSources}
          onBlur={() => commitNumber(
            localMaxSources, 'knowledge.max_sources',
            MAX_SOURCES_MIN, MAX_SOURCES_MAX,
            cfg?.max_sources ?? MAX_SOURCES_DEFAULT,
            setLocalMaxSources,
          )}
          min={MAX_SOURCES_MIN}
          max={MAX_SOURCES_MAX}
          step={10}
          disabled={disabled}
        />
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

function SettingRow({ label, description, children }: {
  label: string
  description: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-start justify-between py-3 border-b border-border last:border-b-0 gap-4">
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-text">{label}</div>
        <div className="text-[11px] text-muted mt-0.5 leading-relaxed">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
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
  return (
    <input
      type="number"
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
  return (
    <button
      role="switch"
      aria-checked={checked}
      onClick={() => !disabled && onChange(!checked)}
      disabled={disabled}
      className={`relative w-[36px] h-[20px] rounded-full transition-colors cursor-pointer border-none ${checked ? 'bg-accent' : 'bg-border'} ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      <span className={`absolute top-[2px] w-[16px] h-[16px] rounded-full bg-card transition-transform ${checked ? 'left-[18px]' : 'left-[2px]'}`} />
    </button>
  )
}
