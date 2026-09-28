import { useQuery } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { Database } from 'lucide-react'
import { api } from '../../api/client'
import { retryPolicy } from '../../api/queryClient'
import { parseErrorCode } from '../../utils/errorReport'
import { Card, CardTitle, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import SimpleSelect from '../../components/SimpleSelect'
import CrewAvatar from '../../components/CrewAvatar'
import { fmtNumber, fmtDateTimeNumeric } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { MemoryStoreSummary } from '../../types'

/**
 * The Memory tab's scope header: which memory store the page is reading, what is
 * in it, and the member that owns it.
 *
 * Also the owner of the store-scope vocabulary the sibling cards share — the
 * query keys, the retry policy, and {@link MemoryScopeNotice}. One module holds
 * them so a refusal reads identically on every card that can hit it.
 */

/** React Query key for the store listing. Its own resource, with no store
 *  parameter: `GET /api/memory/stores` enumerates every silo and is owner-gated
 *  unconditionally. */
export const MEMORY_STORES_KEY = ['memory-stores'] as const

/** Every `/api/memory/*` query-key PREFIX this tab owns.
 *
 *  The page's global refresh invalidates the family through these rather than
 *  naming each store, because a prefix match also reaches the stores a user
 *  looked at earlier in the session and whose cached rows would otherwise
 *  survive the refresh. */
export const MEMORY_QUERY_PREFIXES: readonly string[][] = [
  ['memory-stores'],
  ['memory-doc'],
  ['memory-retired'],
  ['memory-backups'],
  ['memory-carve'],
  ['member-memory'],
  ['memory-records'],
]

/** Statuses whose refusal is settled: the owner gate, an undeclared store name,
 *  and a lineage that has no facets. A retry cannot change any of those answers,
 *  it only delays the sentence the card is about to render. Everything else keeps
 *  the client's default ladder, which is what still buys the 429 tunnel retries. */
const SETTLED_REFUSAL_STATUSES: ReadonlySet<number> = new Set([400, 403, 404, 409])

const PRIVATE_MEMORY_VERSION = 'V2'

/** Retry policy for every store-scoped memory query. */
export const memoryQueryRetry = (failureCount: number, error: unknown): boolean => {
  const status = (error as { status?: unknown } | null)?.status
  if (typeof status === 'number' && SETTLED_REFUSAL_STATUSES.has(status)) return false
  return retryPolicy(failureCount, error)
}

/** The machine-readable `code` a memory route put in its refusal body, if any.
 *  Read structurally rather than through `instanceof ApiError`, because the class
 *  identity does not survive a module mock that omits the export — and a helper
 *  that throws while rendering an error is worse than one that misses a code. */
export function memoryErrorCode(error: unknown): string | undefined {
  const body = (error as { body?: unknown } | null)?.body
  return parseErrorCode(typeof body === 'string' ? body : undefined)
}

/** The server's own sentence for a refusal. Backend strings have no catalog path,
 *  so this is rendered verbatim and is the fallback for a code this UI does not
 *  recognise — a blank card is the one outcome that must not happen. */
function serverMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '')
}

/**
 * A readable line for a store-scoped read that was refused.
 *
 * Renders nothing when there is no error. The recognised codes get a sentence
 * that says what to DO; anything else falls through to the server's message, so
 * an unrecognised refusal still explains itself.
 */
export function MemoryScopeNotice({ error }: { error: unknown }) {
  if (!error) return null
  const code = memoryErrorCode(error)
  const title = code === 'owner_only' ? i18nT('pages.overview.memoryStoreCard.only_the_signed_in_dashboard_owner_can_address_a')
    : code === 'unknown_memory_store' ? i18nT('pages.overview.memoryStoreCard.this_install_does_not_declare_a_memory_store_by')
    : code === 'store_unavailable' ? i18nT('pages.overview.memoryStoreCard.this_store_is_declared_but_its_database_could_no') : undefined
  // No hand-off: these notices share a page with editable memory documents.
  return <ErrorNotice title={title} message={serverMessage(error)} />
}

/** The store listing, shared by every caller through React Query's key dedupe so
 *  the tab issues one request however many cards ask for the list. */
export function useMemoryStores(refreshOnMount = false) {
  return useQuery({
    queryKey: MEMORY_STORES_KEY,
    queryFn: () => api.memoryStores(),
    retry: memoryQueryRetry,
    // The page's scope header refreshes member avatars when returning from the
    // editor. Nested provenance/source consumers keep sharing the settled list.
    refetchOnMount: refreshOnMount ? 'always' : true,
  })
}

/** Stable empty listing, so a pending query does not hand consumers a fresh array
 *  identity on every render and re-run their effects. */
export const NO_MEMORY_STORES: readonly MemoryStoreSummary[] = []

/** The member identity is authoritative; directory names never seed a face. */
export function MemoryStoreAvatar({ summary, size = 24 }: { summary?: MemoryStoreSummary; size?: number }) {
  return summary?.owner_member && !summary.is_default
    ? <CrewAvatar seed={summary.owner_member} avatar={summary.owner_avatar} size={size} />
    : <span className="inline-flex shrink-0 items-center justify-center rounded-md bg-bg-elevated text-muted" style={{ width: size, height: size }} aria-hidden="true"><Database className="lucide-inline" /></span>
}

/** A count the listing could not read comes back as `null`, which means "not
 *  known" and must never render as a zero — that would report an unreadable silo
 *  as an empty one. */
function CountValue({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) {
    return <span className="text-muted">{i18nT('pages.overview.memoryStoreCard.unknown')}</span>
  }
  return <>{fmtNumber(value)}</>
}

export default function MemoryStoreCard({
  store,
  onStoreChange,
  compact = false,
}: {
  /** The store every card on the page is reading. `''` means no store has been
   *  named, so the requests carry no `store=` and the gateway serves the global
   *  store. */
  store: string
  onStoreChange: (store: string) => void
  compact?: boolean
}) {
  const stores = useMemoryStores(true)
  const rows = stores.data?.stores ?? NO_MEMORY_STORES
  // The store the caller ALREADY reads with no `store=` on the wire, named by the
  // gateway rather than inferred from a session header. The content routes
  // always serve the global store when the parameter is absent.
  const active = stores.data?.active ?? ''
  // What the picker DISPLAYS is `store` when one is named and the active store
  // otherwise. The two are deliberately not the same value; see `pick` below.
  const shown = store || active
  const selected = rows.find(s => s.name === shown)
  const storeLabel = (summary: MemoryStoreSummary) => summary.is_default
    ? i18nT('pages.kiroCrewAgentsPage.global_memory_v1')
    : summary.owner_member
      ? i18nT('memoryV2.member_store_label', { member: summary.owner_member, version: PRIVATE_MEMORY_VERSION })
      : summary.name

  /** Translate a picked store into what goes ON THE WIRE.
   *
   * Picking the store the caller already reads sends NOTHING, and that is the
   * whole point of keeping the two apart. Sending `store=<active>` would be
   * semantically identical and behaviourally worse in two ways: the parameter's
   * presence takes the owner gate, so an install with no configured owner would
   * meet a refusal on a page that worked before; and it changes the value the
   * document cards are keyed on, which REMOUNTS them and silently discards a draft
   * the user had already typed — the save that follows then writes the stale
   * server copy back and reports success.
   *
   * Naming a different store still sends it and takes the owner gate.
   */
  const pick = (name: string) => onStoreChange(name === active ? '' : name)

  // The named-memory page owns its identity skeleton/error. Keep this query
  // mounted for refresh-on-return, but do not render a raw directory fallback
  // (or a duplicate catalog error) before that owner can be displayed.
  if (compact && (!selected || (selected.memory_version === 2 && !selected.owner_member))) return null

  return (
    <Card className={compact ? '!px-3 !py-2' : undefined}>
      {!compact && <CardTitle>
        <Database className="lucide-inline" aria-hidden="true" /> {i18nT('pages.overview.memoryStoreCard.memory_store')}
      </CardTitle>}
      <div className="flex gap-2 items-center flex-wrap">
        <SimpleSelect
          aria-label={i18nT('pages.overview.memoryStoreCard.memory_store')}
          title={selected ? storeLabel(selected) : undefined}
          style={{ flex: '0 1 320px', minWidth: 0, maxWidth: '100%' }}
          options={rows.map(s => s.name)}
          optionLabels={rows.map(storeLabel)}
          optionIcons={rows.map(s => <MemoryStoreAvatar key={s.name} summary={s} />)}
          value={shown}
          onChange={pick}
          disabled={rows.length === 0}
        />
      </div>
      <MemoryScopeNotice error={stores.error} />
      {!!stores.error && compact && selected && <Btn className="mt-2 min-h-11" disabled={stores.isFetching} onClick={() => void stores.refetch()}>{i18nT('memoryV2.retry_identity')}</Btn>}
      {/* The scope this picker actually reaches. Said here rather than left to the
          reader, because the cards below it are split: some take the store name on
          the wire and some have no parameter for it at all. */}
      {!compact && <p className="mt-2 text-[12px] leading-relaxed text-muted">
        {i18nT('memoryV2.picker_explanation')}
      </p>}
      {/* V1 counters reuse the diagnostic tiles' labels. V2 count sentences
          keep their `<v/>` slots so each language controls the word order. */}
      {!compact && selected && (
        <div className="flex gap-4 items-center flex-wrap text-[13px] mt-2">
          <span className="text-muted">
            <Trans
              i18nKey="memoryV2.store_format"
              components={{ v: <span className="text-text">{selected.lineage}</span> }}
            />
          </span>
          <span className="text-muted">
            {selected.memory_version === 2 ? <Trans
              i18nKey="memoryV2.store_facts_rules"
              components={{ v: <span className="text-text"><CountValue value={selected.semantic_count} /></span> }}
            /> : <>{i18nT('pages.overview.vectorMemoryCard.semantic')} <span className="text-text"><CountValue value={selected.semantic_count} /></span></>}
          </span>
          <span className="text-muted">
            {selected.memory_version === 2 ? <Trans
              i18nKey="memoryV2.store_experiences"
              components={{ v: <span className="text-text"><CountValue value={selected.episodic_count} /></span> }}
            /> : <>{i18nT('pages.overview.vectorMemoryCard.episodic')} <span className="text-text"><CountValue value={selected.episodic_count} /></span></>}
          </span>
          <span className="text-muted">
            <Trans
              i18nKey="pages.overview.memoryStoreCard.backups"
              components={{ v: <span className="text-text"><CountValue value={selected.backup_count} /></span> }}
            />
          </span>
          {selected.newest_backup && (
            <span className="text-muted">
              <Trans
                i18nKey="pages.overview.memoryStoreCard.newest_backup"
                components={{ v: <span className="text-text">{fmtDateTimeNumeric(selected.newest_backup)}</span> }}
              />
            </span>
          )}
        </div>
      )}
      {selected && !selected.exists && (
        <p className="text-[12px] leading-relaxed text-warn mt-1">
          {i18nT('pages.overview.memoryStoreCard.this_store_has_no_database_file_that_can_be_read')}
        </p>
      )}
    </Card>
  )
}
