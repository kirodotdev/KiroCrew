import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { MemoryMove, MemoryProviderRow } from '../../api/client/memory'
import type { MemoryProviderChangeTarget } from '../../app-sdk/memoryProviderChange'
import type { MemoryMoveTarget } from '../../app-sdk/memoryMove'
import { MemoryMoveSheet } from '../../components/MemoryMoveSheet'
import ErrorNotice from '../../components/ErrorNotice'
import { MEMORY_PROVIDER_QUERY_KEY, MemoryProviderSheet, providerName } from '../../components/MemoryProviderSheet'
import { Card, CardTitle, PanelSectionHeader } from '../../components/ui'
import { ProviderMark, QuietBtn } from '../../components/memorySheetParts'
import { i18nT } from '../../i18n/t'
import { memoryKindLabel } from '../../components/MemoryMoveSheet'

/** One mode gap the gateway names (`op:delete`, `kind:finding`, `writes.verbatim`, ...), in words. */
function gapLabel(gap: string): string {
  switch (gap) {
    case 'op:get':
      return i18nT('pages.overview.memoryProvider.gap.op_get')
    case 'op:list':
      return i18nT('pages.overview.memoryProvider.gap.op_list')
    case 'op:put':
      return i18nT('pages.overview.memoryProvider.gap.op_put')
    case 'op:delete':
      return i18nT('pages.overview.memoryProvider.gap.op_delete')
    case 'op:search':
      return i18nT('pages.overview.memoryProvider.gap.op_search')
  }
  if (gap.startsWith('op:') && gap !== 'op:context_or_search')
    return i18nT('pages.overview.memoryProvider.gap.op', { op: gap.slice(3) })
  if (gap.startsWith('kind:'))
    return i18nT('pages.overview.memoryProvider.gap.kind', { kind: memoryKindLabel(gap.slice(5)) })
  switch (gap) {
    case 'read_only':
      return i18nT('pages.overview.memoryProvider.gap.read_only')
    case 'writes.verbatim':
      return i18nT('pages.overview.memoryProvider.gap.verbatim')
    case 'writes.idempotent':
      return i18nT('pages.overview.memoryProvider.gap.idempotent')
    case 'op:context_or_search':
      return i18nT('pages.overview.memoryProvider.gap.context_or_search')
    default:
      return gap
  }
}

/** A green dot while the memory in use answers, a red one when it does not. */
function HealthDot({ ok }: { ok: boolean }) {
  const { t } = useTranslation()
  const label = ok ? t('pages.overview.memoryProvider.health_ok') : t('pages.overview.memoryProvider.health_down')
  return (
    <span className="inline-flex items-center" title={label}>
      <span className={`h-2 w-2 rounded-full ${ok ? 'bg-ok' : 'bg-danger'}`} aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </span>
  )
}

/** One row of "Other providers": its mark, its name, one line, and its links. */
function OtherRow({ name, line, warn = false, extra, links }: {
  name: string
  line: string
  warn?: boolean
  extra?: ReactNode
  links: ReactNode
}) {
  return (
    <li className="grid grid-cols-[28px_minmax(0,1fr)] items-start gap-x-3 gap-y-1 px-3 py-3 sm:grid-cols-[28px_minmax(0,1fr)_auto]">
      <ProviderMark name={name} size={28} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="font-medium text-text-strong">{name}</span>
        {line && <span className={`text-[12px] ${warn ? 'text-warn-text' : 'text-muted'}`}>{line}</span>}
        {extra}
      </div>
      <div className="col-start-2 flex flex-wrap items-start gap-x-3 sm:col-start-3 sm:row-start-1 sm:flex-col sm:items-end">{links}</div>
    </li>
  )
}

function ProviderRow({
  row,
  mode,
  activeId,
  onPick,
  onMove,
}: {
  row: MemoryProviderRow
  mode: string
  activeId: string
  onPick: (target: MemoryProviderChangeTarget) => void
  onMove: (move: MemoryMoveTarget) => void
}) {
  const { t } = useTranslation()
  const caps = row.capabilities
  const reading = row.id === activeId && mode === 'augment'
  const canReplace = !!caps && !row.blocked_reason && (row.replace_gaps ?? []).length === 0
  const canAugment = !!caps && !row.blocked_reason && (row.mode_gaps?.augment ?? []).length === 0
  const canTakeMove = !!caps && !row.blocked_reason && !caps.read_only && caps.ops.includes('put')
  const gaps = row.mode_gaps?.replace ?? row.replace_gaps ?? []
  const remote = caps?.egress === 'remote'
  // One line: why it cannot be used, else where it keeps memory. A failed connect has
  // no line of its own: its notice below says it once.
  const line = row.blocked_reason
    ? row.connect_failed ? '' : t('pages.overview.memoryProvider.blocked', { reason: row.blocked_reason })
    : reading
      ? t('pages.overview.memoryProvider.mode_augment')
      : remote
        ? t('pages.overview.memoryProvider.line_remote')
        : t('pages.overview.memoryProvider.line_local')
  return (
    <OtherRow
      name={row.display_name}
      line={line}
      warn={!!row.blocked_reason || remote}
      extra={
        <>
          {/* A failed connect is a provider call that failed; any other reason is eligibility. */}
          {row.blocked_reason && row.connect_failed && (
            <ErrorNotice
              variant="inline"
              askAgent
              actionPlacement="below-on-phone"
              message={t('pages.overview.memoryProvider.blocked', { reason: row.blocked_reason })}
            />
          )}
          {!!caps && !canReplace && !row.blocked_reason && gaps.length > 0 && (
            <span className="text-[12px] text-muted">
              {caps.read_only
                ? t('pages.overview.memoryProvider.read_only_reason')
                : t('pages.overview.memoryProvider.replace_gaps', { gaps: gaps.map(gapLabel).join(', ') })}
            </span>
          )}
        </>
      }
      links={
        <>
          {canReplace ? (
            <QuietBtn className="px-0" onClick={() => onPick({ mode: 'replace', id: row.id })}>
              {t('pages.overview.memoryProvider.switch_link')}
            </QuietBtn>
          ) : (
            // A read-only source never replaces; it is added next to Crew memory, and
            // not while a provider replaces it, which the add would quietly end.
            canAugment &&
            !reading &&
            mode !== 'replace' && (
              <QuietBtn className="px-0" onClick={() => onPick({ mode: 'augment', id: row.id })}>
                {t('pages.overview.memoryProvider.add_source_link')}
              </QuietBtn>
            )
          )}
          {canTakeMove && (
            <QuietBtn className="px-0" onClick={() => onMove({ from: 'builtin', to: row.id })}>
              {t('pages.overview.memoryProvider.move_here')}
            </QuietBtn>
          )}
        </>
      }
    />
  )
}

/** A move that stopped before it finished: the owner resumes or removes it from here. */
const UNFINISHED_STATES = new Set<MemoryMove['state']>(['paused', 'failed', 'rollback_failed'])

function moveStateLabel(state: MemoryMove['state']): string {
  if (state === 'paused') return i18nT('pages.overview.memoryProvider.move_paused')
  if (state === 'rollback_failed') return i18nT('pages.overview.memoryProvider.move_rollback_failed')
  return i18nT('pages.overview.memoryProvider.move_failed')
}

/**
 * Settings > Overview > Memory > "Memory provider": which memory Crew uses. A card for
 * the memory in use, then every other choice: Crew's own memory and each provider an
 * installed app declares; names no app of its own. Every switch goes through the
 * confirmation sheet.
 */
export default function MemoryProviderPicker() {
  const { t } = useTranslation()
  const [target, setTarget] = useState<MemoryProviderChangeTarget | null>(null)
  const [move, setMove] = useState<{ target: MemoryMoveTarget; runId?: string } | null>(null)
  const overview = useQuery({ queryKey: [...MEMORY_PROVIDER_QUERY_KEY, 'probe'], queryFn: () => api.memoryProvider(true) })
  const moves = useQuery({ queryKey: [...MEMORY_PROVIDER_QUERY_KEY, 'moves'], queryFn: () => api.memoryMoves() })
  const unfinished = (moves.data?.migrations ?? []).filter(job => !job.dry_run && UNFINISHED_STATES.has(job.state))
  const data = overview.data
  const replaced = data?.mode === 'replace'
  const currentName = data && replaced ? providerName(data, data.id) : t('pages.overview.memoryProvider.builtin_name')
  const others = data ? data.providers.filter(p => !(replaced && p.id === data.id)) : []
  return (
    <Card data-testid="memory-provider-picker">
      <CardTitle>{t('pages.overview.memoryProvider.title')}</CardTitle>
      {overview.isError && (
        <ErrorNotice variant="inline" askAgent message={t('pages.overview.memoryProvider.load_failed')} />
      )}
      {data && (
        <div className="flex flex-col gap-4 text-sm">
          <div className="flex items-start gap-3 rounded-lg border border-border bg-bg p-3" data-testid="memory-provider-current">
            <ProviderMark name={currentName} />
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex items-center gap-2">
                <span className="font-semibold text-text-strong">{currentName}</span>
                <HealthDot ok={!data.active.unavailable && (data.active.health?.ok ?? true)} />
              </span>
              <span className="text-[12px] text-muted">
                {data.mode === 'builtin'
                  ? t('pages.overview.memoryProvider.picker_mode_builtin')
                  : replaced
                    ? t('pages.overview.memoryProvider.mode_replace')
                    : t('pages.overview.memoryProvider.picker_mode_augment', { name: providerName(data, data.id) })}
              </span>
              {replaced && <span className="text-[12px] text-muted">{t('pages.overview.memoryProvider.backup_note')}</span>}
            </div>
            {/* Back to Crew memory alone: ends a replacement, or stops reading a source. */}
            {data.mode !== 'builtin' && (
              <QuietBtn className="shrink-0 px-0" onClick={() => setTarget({ mode: 'builtin' })}>
                {replaced ? t('pages.overview.memoryProvider.switch_back') : t('pages.overview.memoryProvider.confirm_stop')}
              </QuietBtn>
            )}
          </div>
          {(data.active.unavailable || data.active.health?.ok === false) && (
            <ErrorNotice
              variant="inline"
              askAgent
              actionPlacement="below-on-phone"
              message={data.active.unavailable
                ? t('pages.overview.memoryProvider.unavailable', { reason: data.active.unavailable })
                : t('pages.overview.memoryProvider.blocked', {
                  reason: data.active.health?.detail || t('pages.overview.memoryProvider.health_down'),
                })}
            />
          )}
          <section className="flex flex-col gap-2">
            <PanelSectionHeader label={t('pages.overview.memoryProvider.other_title')} />
            {replaced || others.length > 0 ? (
              <ul className="flex flex-col divide-y divide-border rounded-lg border border-border bg-bg">
                {replaced && (
                  <OtherRow
                    name={t('pages.overview.memoryProvider.builtin_name')}
                    line={t('pages.overview.memoryProvider.line_local')}
                    links={
                      <QuietBtn className="px-0" onClick={() => setTarget({ mode: 'builtin' })}>
                        {t('pages.overview.memoryProvider.switch_link')}
                      </QuietBtn>
                    }
                  />
                )}
                {others.map(row => (
                  <ProviderRow
                    key={row.id}
                    row={row}
                    mode={data.mode}
                    activeId={data.id}
                    onPick={setTarget}
                    onMove={next => setMove({ target: next })}
                  />
                ))}
              </ul>
            ) : (
              <p className="text-muted text-[12px]">{t('pages.overview.memoryProvider.none_installed')}</p>
            )}
          </section>
          {/* No unsent choice sits on this card, so the agent can pick up the failure. */}
          {moves.isError && (
            <ErrorNotice
              variant="inline"
              askAgent
              actionPlacement="below-on-phone"
              message={t('pages.overview.memoryProvider.moves_load_failed')}
            />
          )}
          {unfinished.length > 0 && (
            <section className="flex flex-col gap-2" data-testid="memory-move-unfinished">
              <PanelSectionHeader label={t('pages.overview.memoryProvider.unfinished_title')} count={unfinished.length} />
              <ul className="flex flex-col divide-y divide-border rounded-lg border border-border bg-bg">
                {unfinished.map(job => (
                  <li key={job.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                    <span className="text-[13px]">
                      {t('pages.overview.memoryProvider.unfinished_row', {
                        to: providerName(data, job.to === 'builtin' ? undefined : job.to),
                        state: moveStateLabel(job.state),
                      })}
                    </span>
                    <QuietBtn className="px-0" onClick={() => setMove({ target: { from: job.from, to: job.to }, runId: job.id })}>
                      {t('pages.overview.memoryProvider.unfinished_open')}
                    </QuietBtn>
                  </li>
                ))}
              </ul>
            </section>
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
      {move && (
        <MemoryMoveSheet
          target={move.target}
          runId={move.runId}
          onDone={() => {
            setMove(null)
            void moves.refetch()
          }}
        />
      )}
    </Card>
  )
}
