import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Layers, X } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Badge, EmptyState } from '../../components/ui'
import Clickable from '../../components/Clickable'
import SimpleSelect from '../../components/SimpleSelect'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { MemoryScopeNotice, memoryQueryRetry, useMemoryStores } from './MemoryStoreCard'

/**
 * How a store's memory divides up: which crews, surfaces, scopes and sessions
 * filled it, and how much each contributed.
 *
 * A count first, rows second. "Which surfaces wrote this crew's memory" is the
 * question an operator actually opens this card with, and a bare page of rows
 * answers it only by being read end to end.
 */

/** The row TYPE column. Not a facet — nothing stamps it, it is what a row IS —
 *  but it is a legal count axis and belongs on the same control, because "how
 *  much of this store is directives" is asked in the same breath. Mirrors
 *  `memory_schema._KIND_COLUMN`. */
const KIND_AXIS = 'kind'

/** Keep backend grouping identifiers stable; localize their display labels. */
const FACET_AXES = ['scope', 'surface', 'crew', 'session_key', 'derived_from'] as const
const AXIS_LABELS: Record<string, string> = {
  scope: 'memoryV2.group_scope', surface: 'memoryV2.group_surface', crew: 'memoryV2.group_crew',
  session_key: 'memoryV2.group_session', derived_from: 'memoryV2.group_source', kind: 'memoryV2.group_kind',
}
const KIND_LABELS: Record<string, string> = { fact: 'memoryV2.fact', directive: 'memoryV2.directive', episode: 'memoryV2.episode' }

/** Every axis a count may be grouped by. Mirrors `memory_schema.GROUPABLE_COLUMNS`. */
const COUNT_AXES: readonly string[] = [...FACET_AXES, KIND_AXIS]

/** Rows one carve page asks for. Matches the route's own default page size. */
const CARVE_PAGE = 50

/** Longest remembered text rendered inline, so one long episode cannot push the
 *  facet columns off the card. */
const TEXT_PREVIEW_CHARS = 160

/** The narrowing a clicked count applies: one axis pinned to one value.
 *  `axis` is `kind` or one of {@link FACET_AXES}; the two travel to the gateway on
 *  different parameters, which is why the axis is carried rather than inferred. */
interface CarveFilter {
  axis: string
  value: string
}

/** One count row's label. An empty stored value is a real answer — the rows no
 *  writer attributed on that axis — and has to read as that rather than as a
 *  blank cell. */
function AxisValue({ value }: { value: string }) {
  if (value === '') return <span className="text-muted">{i18nT('pages.overview.memoryCarveCard.not_recorded')}</span>
  return <>{value}</>
}

export default function MemoryCarveCard({ store }: { store: string }) {
  const [countBy, setCountBy] = useState<string>(FACET_AXES[1])
  const [filter, setFilter] = useState<CarveFilter | null>(null)
  const stores = useMemoryStores()
  const summary = stores.data?.stores.find(item => item.name === store)
  // Global V1 has no facets. Other stores report the capability in their listing;
  // skip an inapplicable request rather than turning its rejection into status.
  const unsupported = !store || store === 'default' || summary?.facets_supported === false

  const counts = useQuery({
    queryKey: ['memory-carve', store, 'counts', countBy],
    queryFn: () => api.memoryCarve({ store: store || undefined, countBy }),
    retry: memoryQueryRetry,
    enabled: !unsupported && !stores.isPending,
  })

  const entries = useQuery({
    queryKey: ['memory-carve', store, 'entries', filter?.axis ?? '', filter?.value ?? ''],
    queryFn: () => api.memoryCarve({
      store: store || undefined,
      ...(filter && filter.axis === KIND_AXIS ? { kind: filter.value } : {}),
      ...(filter && filter.axis !== KIND_AXIS ? { facets: { [filter.axis]: filter.value } } : {}),
      limit: CARVE_PAGE,
    }),
    retry: memoryQueryRetry,
    enabled: filter !== null && !unsupported,
  })

  const countRows = Object.entries(counts.data?.counts ?? {})
  const entryRows = entries.data?.entries ?? []

  return (
    <Card>
      <CardTitle>{i18nT('memoryV2.analysis_title')}</CardTitle>
      {unsupported ? (
        <p className="text-[12px] leading-relaxed text-muted">{i18nT('memoryV2.analysis_unsupported')}</p>
      ) : (
        <>
          <p className="text-[12px] leading-relaxed text-muted mb-2">
            {i18nT('memoryV2.analysis_hint')}
          </p>
          <div className="flex gap-2 items-center flex-wrap mb-3">
            {/* A caption, not a `<label htmlFor>`: the select's trigger is a
                button, which `<label>` does not name in every screen reader, so
                the accessible name comes from `aria-label` instead. */}
            <span className="text-[13px] text-muted">{i18nT('pages.overview.memoryCarveCard.count_by')}</span>
            <SimpleSelect
              aria-label={i18nT('pages.overview.memoryCarveCard.count_by')}
              style={{ flex: '0 0 180px' }}
              options={[...COUNT_AXES]}
              optionLabels={COUNT_AXES.map(axis => i18nT(AXIS_LABELS[axis]))}
              value={countBy}
              onChange={axis => { setCountBy(axis); setFilter(null) }}
            />
            {filter && (
              <>
                <Badge variant="ok">
                  {i18nT(AXIS_LABELS[filter.axis])} = {filter.axis === KIND_AXIS && KIND_LABELS[filter.value] ? i18nT(KIND_LABELS[filter.value]) : <AxisValue value={filter.value} />}
                </Badge>
                <Btn onClick={() => setFilter(null)}>
                  <X className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryCarveCard.clear_filter')}
                </Btn>
              </>
            )}
          </div>
          <MemoryScopeNotice error={counts.error} />
          {!counts.error && !counts.isPending && countRows.length === 0 && (
            <EmptyState
              icon={<Layers className="lucide-inline" />}
              title={i18nT('memoryV2.analysis_empty')}
              subtitle={i18nT('memoryV2.analysis_hint')}
            />
          )}
          {countRows.length > 0 && (
            <div className="flex flex-col gap-1">
              {countRows.map(([value, count]) => (
                <Clickable
                  key={value}
                  onClick={() => setFilter({ axis: countBy, value })}
                  className="flex justify-between items-center gap-3 px-2.5 py-1.5 rounded-md text-sm hover:bg-bg-hover transition-colors focus-ring"
                >
                  {countBy === KIND_AXIS && KIND_LABELS[value] ? i18nT(KIND_LABELS[value]) : <AxisValue value={value} />}
                  <span className="text-muted">{fmtNumber(count)}</span>
                </Clickable>
              ))}
            </div>
          )}
          {filter && (
            <div className="mt-3">
              <MemoryScopeNotice error={entries.error} />
              {entries.isPending && <p role="status" className="text-[13px] text-muted">{i18nT('pages.overview.memoryTab.loading')}</p>}
              {!entries.error && !entries.isPending && (
                entryRows.length === 0 ? (
                  <EmptyState
                    icon={<Layers className="lucide-inline" />}
                    title={i18nT('memoryV2.no_matches')}
                    subtitle={i18nT('memoryV2.no_matches_hint')}
                  />
                ) : <>
                  <div className="grid gap-2 sm:hidden" data-testid="memory-carve-mobile-results">
                    {entryRows.map(row => (
                      <article key={row.id} className="min-w-0 rounded-lg border border-border bg-bg-elevated p-3 text-sm">
                        <div className="mb-2 flex min-w-0 items-start gap-2">
                          <Badge variant="muted">{KIND_LABELS[row.kind] ? i18nT(KIND_LABELS[row.kind]) : row.kind}</Badge>
                        </div>
                        <p className="min-w-0 break-words">{(row.text ?? row.key ?? '').slice(0, TEXT_PREVIEW_CHARS)}</p>
                        <dl className="mt-2 grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-2 text-[12px]">
                          <dt className="text-muted">{i18nT('memoryV2.group_crew')}</dt>
                          <dd className="min-w-0 break-words text-right"><AxisValue value={row.crew ?? ''} /></dd>
                          <dt className="text-muted">{i18nT('memoryV2.group_surface')}</dt>
                          <dd className="min-w-0 break-words text-right"><AxisValue value={row.surface ?? ''} /></dd>
                        </dl>
                      </article>
                    ))}
                  </div>
                  <div className="hidden overflow-x-auto sm:block">
                    <table className="w-full border-collapse table-striped">
                      <thead>
                        <tr>
                          <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('memoryV2.group_kind')}</th>
                          <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryCarveCard.memory')}</th>
                          <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('memoryV2.group_crew')}</th>
                          <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('memoryV2.group_surface')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {entryRows.map(row => (
                          <tr key={row.id} className="hover:bg-bg-hover transition-colors">
                            <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="muted">{KIND_LABELS[row.kind] ? i18nT(KIND_LABELS[row.kind]) : row.kind}</Badge></td>
                            <td className="px-2.5 py-2 border-b border-border text-sm">
                              {(row.text ?? row.key ?? '').slice(0, TEXT_PREVIEW_CHARS)}
                            </td>
                            <td className="px-2.5 py-2 border-b border-border text-sm"><AxisValue value={row.crew ?? ''} /></td>
                            <td className="px-2.5 py-2 border-b border-border text-sm"><AxisValue value={row.surface ?? ''} /></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          )}
        </>
      )}
    </Card>
  )
}
