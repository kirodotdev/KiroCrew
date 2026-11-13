import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { MemoryProviderCapabilities, MemoryProviderRow } from '../../api/client/memory'
import type { MemoryProviderChangeTarget } from '../../app-sdk/memoryProviderChange'
import ErrorNotice from '../../components/ErrorNotice'
import { MEMORY_PROVIDER_QUERY_KEY, MemoryProviderSheet, providerName } from '../../components/MemoryProviderSheet'
import { Badge, Btn, Card, CardTitle } from '../../components/ui'
import { fmtNumber } from '../../i18n/format'

function CapabilityChips({ caps }: { caps: MemoryProviderCapabilities }) {
  const { t } = useTranslation()
  return (
    <span className="flex flex-wrap gap-1">
      {caps.supports_vectors && <Badge variant="muted">{t('pages.overview.memoryProvider.chip_semantic')}</Badge>}
      {caps.supports_lessons && <Badge variant="muted">{t('pages.overview.memoryProvider.chip_lessons')}</Badge>}
      {caps.read_only && <Badge variant="muted">{t('pages.overview.memoryProvider.chip_read_only')}</Badge>}
      {caps.egress === 'remote' && <Badge variant="warn">{t('pages.overview.memoryProvider.chip_remote')}</Badge>}
    </span>
  )
}

function ProviderRow({
  row,
  mode,
  activeId,
  onPick,
}: {
  row: MemoryProviderRow
  mode: string
  activeId: string
  onPick: (target: MemoryProviderChangeTarget) => void
}) {
  const { t } = useTranslation()
  const caps = row.capabilities
  const active = row.id === activeId && mode !== 'builtin'
  const p95 = row.latency?.ops?.context?.p95_ms
  const canReplace = !!caps && !row.blocked_reason && (row.replace_gaps ?? []).length === 0
  const canAugment = !!caps && !row.blocked_reason && caps.supports_search
  return (
    <li className="flex flex-col gap-1 py-2 border-t border-[var(--border)]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-text-strong">{row.display_name}</span>
        {active && (
          <Badge variant="ok">
            {mode === 'replace' ? t('pages.overview.memoryProvider.mode_replace') : t('pages.overview.memoryProvider.mode_augment')}
          </Badge>
        )}
        {caps && <CapabilityChips caps={caps} />}
        {p95 !== undefined && (
          <span className="text-muted text-[11px]">
            {t('pages.overview.memoryProvider.p95', { ms: fmtNumber(p95, { maximumFractionDigits: 0 }) })}
          </span>
        )}
      </div>
      {row.description && <p className="text-muted text-[12px]">{row.description}</p>}
      {/* A failed describe is a provider call that failed; any other reason is eligibility. */}
      {row.blocked_reason && row.describe_failed && (
        <ErrorNotice
          variant="inline"
          askAgent
          message={t('pages.overview.memoryProvider.blocked', { reason: row.blocked_reason })}
        />
      )}
      {row.blocked_reason && !row.describe_failed && (
        <p className="text-[12px] text-warn-text">{t('pages.overview.memoryProvider.blocked', { reason: row.blocked_reason })}</p>
      )}
      <div className="flex flex-wrap gap-2">
        <Btn disabled={!canReplace || (active && mode === 'replace')} onClick={() => onPick({ mode: 'replace', id: row.id })}>
          {t('pages.overview.memoryProvider.use_replace')}
        </Btn>
        {/* The active source stops in place of being added again; augment while a
            provider replaces Crew memory would quietly end the replacement. */}
        {active && mode === 'augment' ? (
          <Btn onClick={() => onPick({ mode: 'builtin' })}>{t('pages.overview.memoryProvider.stop_augment')}</Btn>
        ) : (
          <Btn disabled={!canAugment || mode === 'replace'} onClick={() => onPick({ mode: 'augment', id: row.id })}>
            {t('pages.overview.memoryProvider.use_augment')}
          </Btn>
        )}
      </div>
      {canReplace && !active && (
        <p className="text-muted text-[12px]">{t('pages.overview.memoryProvider.replace_keeps')}</p>
      )}
    </li>
  )
}

/**
 * Settings > Memory > "Memory provider": which memory Crew uses. Lists Crew's own
 * memory and every provider an installed app declares; names no app of its own.
 * Every switch goes through the confirmation sheet.
 */
export default function MemoryProviderPicker() {
  const { t } = useTranslation()
  const [target, setTarget] = useState<MemoryProviderChangeTarget | null>(null)
  const overview = useQuery({ queryKey: MEMORY_PROVIDER_QUERY_KEY, queryFn: () => api.memoryProvider(true) })
  const data = overview.data
  const canReset =
    data?.mode === 'replace' || (data?.mode === 'augment' && !data.providers.some(p => p.id === data.id))
  return (
    <Card data-testid="memory-provider-picker">
      <CardTitle>{t('pages.overview.memoryProvider.title')}</CardTitle>
      {overview.isError && (
        <ErrorNotice variant="inline" askAgent message={t('pages.overview.memoryProvider.load_failed')} />
      )}
      {data && (
        <div className="flex flex-col gap-2 text-sm">
          <p>
            {data.mode === 'builtin'
              ? t('pages.overview.memoryProvider.current_builtin')
              : data.mode === 'replace'
                ? t('pages.overview.memoryProvider.current_replace', { name: providerName(data, data.id) })
                : t('pages.overview.memoryProvider.current_augment', { name: providerName(data, data.id) })}
          </p>
          {data.active.unavailable && (
            <ErrorNotice
              variant="inline"
              askAgent
              message={t('pages.overview.memoryProvider.unavailable', { reason: data.active.unavailable })}
            />
          )}
          {data.mode === 'replace' && (
            <p className="text-muted text-[12px]">{t('pages.overview.memoryProvider.backup_note')}</p>
          )}
          <ul className="flex flex-col">
            <li className="flex flex-wrap items-center gap-2 py-2">
              <span className="font-medium text-text-strong">{t('pages.overview.memoryProvider.builtin_name')}</span>
              {data.mode !== 'replace' && <Badge variant="ok">{t('pages.overview.memoryProvider.in_use')}</Badge>}
              {/* Crew memory is in use unless a provider replaces it; a read-only source
                  stops from its own row, or here once its app is gone. */}
              <Btn disabled={!canReset} onClick={() => setTarget({ mode: 'builtin' })}>
                {t('pages.overview.memoryProvider.use_builtin')}
              </Btn>
            </li>
            {data.providers.map(row => (
              <ProviderRow key={row.id} row={row} mode={data.mode} activeId={data.id} onPick={setTarget} />
            ))}
          </ul>
          {data.providers.length === 0 && (
            <p className="text-muted text-[12px]">{t('pages.overview.memoryProvider.none_installed')}</p>
          )}
          {data.shared_crew && (
            <p className="text-muted text-[12px]">{t('pages.overview.memoryProvider.shared_crew_note')}</p>
          )}
          {!data.policy_allows && (
            <p className="text-muted text-[12px]">{t('pages.overview.memoryProvider.policy_note')}</p>
          )}
        </div>
      )}
      {target && <MemoryProviderSheet target={target} onDone={() => setTarget(null)} />}
    </Card>
  )
}
