/**
 * MarketListingRows — the apps of one added Kiro Space store, on the Discover page.
 *
 * Its own block, never mixed into the git registry list: every action here is
 * keyed by the store id and the listing ref, never by the app name, so a store
 * listing and a git row that share a name stay two cards that cannot trigger
 * each other's install. Install opens InstallFromSpaceDialog
 * (`POST /api/apps/market/install`); the row opens
 * `/apps/-/market/<store>/<namespace>/<name>`.
 */
import { useMemo, useState } from 'react'
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { BadgeCheck, Check, RefreshCw, Star } from 'lucide-react'
import ErrorNotice from '../ErrorNotice'
import Clickable from '../Clickable'
import { Btn, IconButton } from '../ui'
import AppIconTile from './AppIconTile'
import InstallFromSpaceDialog from './InstallFromSpaceDialog'
import { useMarketStatusText } from './MarketRegistryManager'
import {
  MarketAccountChanged, readMarketListings, registriesReadError, useHiddenStores, useMarketRegistries,
  useMarketRegistriesQuery, useMarketStatuses,
} from './marketAccount'
import { SignInProgress, useMarketRefresh, useStoreSignIn } from './marketSignIn'
import { fmtNumber } from '../../i18n/format'
import { cardDataKey } from '../../pages/apps/cardDataKey'
import {
  sourceRowKey, type MarketListingRow, type MarketListingsResponse, type MarketRegistryOffer,
  type MarketRegistryStatus,
} from './types'
import { categoryFor } from './categories'
import type { SourceRow } from './CategoryRail'

/** Most rows the gateway reads from one store (10 pages of 100). */
export const MARKET_LISTING_LIMIT = 1000

/**
 * A store's listing query. Not keyed by account: the page reloads when the
 * account it loaded under changes (`marketAccount.ts`).
 */
export const marketListingsKey = (registry: string) => ['market-listings', registry] as const
/** One listing's query, per store and ref. */
export const marketListingKey = (registry: string, ref: string) => ['market-listing', registry, ref] as const

/** The detail route of one store listing. `-/` keeps it off every app name. */
export function marketListingPath(row: Pick<MarketListingRow, 'registry' | 'ref'>): string {
  const [namespace, name] = row.ref.split('/')
  return `/apps/-/market/${encodeURIComponent(row.registry)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`
}

/** The read-error copy for a failed store read, by its code. */
export function readErrorKey(code: string): string {
  if (code === 'market_signed_out') return 'components.appstore.marketStore.err_signed_out'
  return code === 'market_api_refused'
    ? 'components.appstore.marketStore.err_read_refused'
    : 'components.appstore.marketStore.err_unreachable'
}

/** The rows of one listing answer; only a `signed_in` answer has any. */
export function listedRows(data: MarketListingsResponse | undefined): MarketListingRow[] {
  return data?.status.state === 'signed_in' ? data.rows : []
}

export function MarketRow({ row, label, onInstall }: {
  row: MarketListingRow
  label: string
  onInstall: (row: MarketListingRow) => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const rating = row.rating.status === 'ok' ? row.rating.value : null
  const reviews = row.ratingCount.status === 'ok' ? row.ratingCount.value : 0
  return (
    <Clickable
      aria-label={t('components.appstore.appListRow.view_details_for', { name: row.displayName })}
      className="flex items-center gap-3.5 px-3.5 py-3 border border-border rounded-xl bg-card mb-2 cursor-pointer hover:border-border-strong transition-colors focus-ring"
      onClick={() => navigate(marketListingPath(row))}
      data-testid={`market-row-${row.ref}`}
    >
      {/* Store icons need a signed-in fetch: initials until that exists. */}
      <AppIconTile name={row.name} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 text-[14px] font-semibold text-text-strong">
          <span className="truncate">{row.displayName || row.name}</span>
          {row.official && (
            <BadgeCheck size={14} className="text-accent shrink-0" aria-label={t('components.appstore.marketStore.official')} />
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[12px] text-muted min-w-0">
          <span className="truncate min-w-0">{t('components.appstore.marketStore.source_label', { label, publisher: row.publisher })}</span>
          {rating !== null && (
            <span className="inline-flex items-center gap-0.5 shrink-0" title={t('components.appstore.marketStore.rating_label')}>
              <Star size={12} className="shrink-0" aria-hidden /> {fmtNumber(rating, { maximumFractionDigits: 1 })}
            </span>
          )}
          {reviews > 0 && (
            <span className="shrink-0">{t('components.appstore.marketStore.reviews', { n: fmtNumber(reviews) })}</span>
          )}
        </div>
        <div className="text-[12.5px] text-muted truncate" title={row.summary}>{row.summary}</div>
        {/* In the text column, wrapping: in the non-shrinking action column it
            took the card's width and squeezed the name to an ellipsis. */}
        {row.nameTaken && !row.installed && <p className="text-[12px] text-muted mt-0.5">{t('components.appstore.marketStore.name_taken')}</p>}
      </div>
      <div
        className="flex flex-col items-end gap-1.5 shrink-0"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}
        role="presentation"
      >
        {row.installed ? (
          <span className="inline-flex items-center gap-1 text-[12px] text-muted"><Check size={12} /> {t('components.appstore.appListRow.installed')}</span>
        ) : row.nameTaken ? (
          <Btn disabled>{t('components.appstore.appListRow.install')}</Btn>
        ) : (
          <Btn primary className="rounded-full px-3.5 text-[12px] font-semibold" onClick={() => onInstall(row)}>{t('components.appstore.appListRow.install')}</Btn>
        )}
      </div>
    </Clickable>
  )
}

/**
 * A signed-out store's card: why there are no rows, and the same Sign in the
 * Sources popover offers, with the code and the approval link shown here.
 */
function SignInCard({ offer, status }: { offer: MarketRegistryOffer; status?: MarketRegistryStatus }) {
  const { t } = useTranslation()
  const statusText = useMarketStatusText()
  const refresh = useMarketRefresh()
  const signIn = useStoreSignIn(offer.id, useMarketStatuses()?.[offer.id]?.state, refresh)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const start = async () => {
    setBusy(true)
    setError('')
    try {
      await signIn.start()
    } catch (e) {
      setError((e as Error)?.message || t('components.appstore.marketStore.action_failed'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="border border-border rounded-xl bg-card px-3.5 py-3 mb-2" data-testid="market-signin-card">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-[14px] font-semibold text-text-strong">{t('components.appstore.marketStore.sign_in_card_title', { label: offer.label })}</div>
          <p className="text-[12.5px] text-muted">{statusText(status)}</p>
          <p className="text-[12.5px] text-muted">{t('components.appstore.marketStore.sign_in_card_body')}</p>
        </div>
        <Btn primary disabled={busy} onClick={() => { void start() }}>{t('components.appstore.marketStore.sign_in')}</Btn>
      </div>
      <SignInProgress signIn={signIn} className="mt-2" />
      {error && <div className="mt-2"><ErrorNotice message={error} variant="inline" onDismiss={() => setError('')} askAgent /></div>}
    </div>
  )
}

/**
 * Why a store's listing read failed, as its block says it: the read that threw
 * (unless it threw for an account change, which holds the store or reloads),
 * then the answer's own error, named by the status detail under `unknown`.
 */
function storeReadErrors(
  data: MarketListingsResponse | undefined,
  error: unknown,
  statusText: (status: MarketRegistryStatus | undefined) => string,
  t: TFunction,
): string[] {
  const out: string[] = []
  if (error && !(error instanceof MarketAccountChanged)) out.push((error as Error).message)
  if (data?.error) {
    out.push(data.status.state === 'unknown' && data.status.detail ? statusText(data.status) : t(readErrorKey(data.error.code)))
  }
  return out
}

/**
 * An added store's read failures while a git source hides its block, under the
 * store's name: the rail still counts that store, and without this a failed
 * read reads as "0 apps". A signed-out store is a state, not a failure: nothing.
 */
function MarketStoreReadErrors({ offer }: { offer: MarketRegistryOffer }) {
  const { t } = useTranslation()
  const statusText = useMarketStatusText()
  const registries = useMarketRegistries()
  const { data, error } = useQuery({
    queryKey: marketListingsKey(offer.id),
    queryFn: () => readMarketListings(offer.id),
    staleTime: 5 * 60_000,
    enabled: registries !== undefined,
  })
  const signedOut = data?.status.state === 'needs_sign_in' || data?.status.state === 'no_access'
  const messages = storeReadErrors(signedOut ? undefined : data, error, statusText, t)
  if (messages.length === 0) return null
  return (
    <section className="mb-6" data-testid={`market-store-errors-${offer.id}`}>
      <h3 className="text-[17px] font-semibold text-text-strong mb-3">{offer.label}</h3>
      {messages.map(message => <ErrorNotice key={message} message={message} askAgent />)}
    </section>
  )
}

export default function MarketListingRows({ offer, query = '', category = 'all', installedOnly = false }: {
  offer: MarketRegistryOffer
  query?: string
  category?: string
  installedOnly?: boolean
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const statusText = useMarketStatusText()
  const [installing, setInstalling] = useState<MarketListingRow | null>(null)
  const registries = useMarketRegistries()
  const statuses = useMarketStatuses()
  // A held store, or one whose shared status is `unknown`: no rows, no
  // Install, only the status line.
  const hidden = useHiddenStores().has(offer.id)
  const { data, error, refetch, isFetching } = useQuery({
    queryKey: marketListingsKey(offer.id),
    queryFn: () => readMarketListings(offer.id),
    staleTime: 5 * 60_000,
    enabled: registries !== undefined,
  })

  const listed = hidden ? [] : listedRows(data)
  const rows = listed.filter(r => matchesMarketQuery(r, query)
    && (category === 'all' || categoryFor(marketRowTags(r)) === category)
    && (!installedOnly || r.installed))
  const status = data?.status
  // Only these two need a sign-in. `unknown` (the store or its CLI cannot be
  // read right now) shows the read error, or the status line when the status
  // names why (the CLI is missing or too old), since a retry cannot help then.
  const signedOut = status?.state === 'needs_sign_in' || status?.state === 'no_access'
  const unknownState = status?.state === 'unknown'
  // A read that threw for an account change held the store (or reloads the
  // page): the held line says so, not this error's code.
  const readFailed = !!error && !(error instanceof MarketAccountChanged)

  return (
    <section className="mb-6" data-testid={`market-section-${offer.id}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-[17px] font-semibold text-text-strong">{offer.label}</h3>
        <IconButton
          aria-label={t('components.appstore.marketStore.refresh')}
          title={t('components.appstore.marketStore.refresh')}
          onClick={() => { void refetch() }}
          disabled={isFetching}
        >
          <RefreshCw size={14} />
        </IconButton>
      </div>
      {/* Errors sit outside the hidden gate: a store hidden for an `unknown`
          status whose read failed shows why, not "Checking sign-in…". */}
      {storeReadErrors(data, error, statusText, t).map(message => (
        <ErrorNotice key={message} message={message} askAgent />
      ))}
      {hidden && !readFailed && !data?.error && (
        <p className="text-[12.5px] text-muted" data-testid="market-store-held">{statusText(statuses?.[offer.id])}</p>
      )}
      {!hidden && <>
      {signedOut && <SignInCard offer={offer} status={status} />}
      {!signedOut && !unknownState && data && !data.error && listed.length === 0 && (
        <p className="text-[12.5px] text-muted">{t('components.appstore.marketStore.empty')}</p>
      )}
      {listed.length > 0 && rows.length === 0 && (
        <p className="text-[12.5px] text-muted">{t('pages.appsPage.no_matching_apps')}</p>
      )}
      {rows.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-3.5">
          {rows.map(row => (
            <MarketRow
              key={`space:${row.registry}:${row.ref}:${cardDataKey(row)}`}
              row={row}
              label={offer.label}
              onInstall={setInstalling}
            />
          ))}
        </div>
      )}
      {data?.truncated && (
        <p className="text-[12px] text-muted mt-1" data-testid="market-truncated">
          {t('components.appstore.marketStore.truncated', { limit: fmtNumber(MARKET_LISTING_LIMIT) })}
        </p>
      )}
      </>}
      {installing && !hidden && (
        <InstallFromSpaceDialog
          row={installing}
          onClose={() => setInstalling(null)}
          onInstalled={() => {
            setInstalling(null)
            void queryClient.invalidateQueries({ queryKey: ['market-listings'] })
            window.dispatchEvent(new Event('mc:apps-changed'))
          }}
        />
      )}
    </section>
  )
}

/** What the rail's category matcher reads for a store row: its category, then its tags. */
export function marketRowTags(row: Pick<MarketListingRow, 'category' | 'tags'>): string[] {
  return [row.category, ...row.tags]
}

/** The search box's match for a store row, on the same fields the block filters by. */
export function matchesMarketQuery(row: MarketListingRow, query: string): boolean {
  const needle = query.trim().toLowerCase()
  return !needle
    || row.displayName.toLowerCase().includes(needle)
    || row.name.includes(needle)
    || row.summary.toLowerCase().includes(needle)
}

/** The rail key of one Kiro Space store. A git registry name cannot hold `:`. */
export function marketSourceKey(id: string): string {
  return sourceRowKey({ name: `space:${id}`, builtin: false })
}

export function isMarketSourceKey(key: string | null): boolean {
  return !!key && key.startsWith(marketSourceKey(''))
}

/** The added stores this build offers, from the shared registries query. */
function useAddedStores(): { stores: MarketRegistryOffer[]; hidden: ReadonlySet<string> } {
  const data = useMarketRegistries()
  const hidden = useHiddenStores()
  return useMemo(() => {
    const added = new Set(data?.added ?? [])
    return { stores: (data?.offers ?? []).filter(o => added.has(o.id)), hidden }
  }, [data, hidden])
}

/** One rail source row per added store, counted from its listing read under the search box. */
export function useMarketSourceRows(query = ''): SourceRow[] {
  const { stores, hidden } = useAddedStores()
  // A string, so the rows below keep one identity until a count really changes.
  const counts = useQueries({
    queries: stores.map(offer => ({
      queryKey: marketListingsKey(offer.id),
      queryFn: () => readMarketListings(offer.id),
      staleTime: 5 * 60_000,
    })),
    combine: results => results
      .map(r => listedRows(r.data).filter(row => matchesMarketQuery(row, query)).length)
      .join(','),
  })
  return useMemo(() => {
    const each = counts.split(',')
    return stores.map((offer, i) => ({
      name: `space:${offer.id}`, label: offer.label,
      count: hidden.has(offer.id) ? 0 : Number(each[i]) || 0, builtin: false,
    }))
  }, [stores, counts, hidden])
}

/**
 * The store rows the rail counts, under the search box: every added store's
 * rows for All sources, the picked store's rows for a store source, and none
 * for a git source (git rows are counted by the page itself).
 */
export function useRailStoreRows(source: string | null, query = ''): MarketListingRow[] {
  const { stores, hidden } = useAddedStores()
  const counted = useMemo(() => (source === null
    ? stores
    : stores.filter(o => marketSourceKey(o.id) === source)).filter(o => !hidden.has(o.id)), [stores, source, hidden])
  const all = useQueries({
    queries: counted.map(offer => ({
      queryKey: marketListingsKey(offer.id),
      queryFn: () => readMarketListings(offer.id),
      staleTime: 5 * 60_000,
    })),
    combine: results => results.flatMap(r => listedRows(r.data)),
  })
  return useMemo(() => all.filter(r => matchesMarketQuery(r, query)), [all, query])
}

/**
 * Every added Kiro Space store's block, for the Discover page. Renders nothing
 * when the build offers no store or none is added. With a source picked in the
 * rail, only that store's block shows; a git source hides them all. The rail's
 * category and installed-only filters apply to the store rows too.
 */
export function MarketStoreSections({ query = '', source = null, category = 'all', installedOnly = false }: {
  query?: string
  source?: string | null
  category?: string
  installedOnly?: boolean
}) {
  const { stores } = useAddedStores()
  // A failed store-list read is said here too, not only in the Sources popover:
  // without it Discover looks the same as a build that offers no store.
  const readError = registriesReadError(useMarketRegistriesQuery().error)
  if (source !== null && !isMarketSourceKey(source)) {
    // A git source hides the store blocks, not their failures.
    return <>
      {readError && <div className="mb-3" data-testid="market-registries-error"><ErrorNotice message={readError} askAgent /></div>}
      {stores.map(offer => <MarketStoreReadErrors key={offer.id} offer={offer} />)}
    </>
  }
  const shown = stores.filter(o => source === null || marketSourceKey(o.id) === source)
  if (readError && shown.length === 0) {
    return <div className="mb-6" data-testid="market-registries-error"><ErrorNotice message={readError} askAgent /></div>
  }
  if (shown.length === 0) return null
  return <>{readError && <div className="mb-3" data-testid="market-registries-error"><ErrorNotice message={readError} askAgent /></div>}{shown.map(offer => (
    <MarketListingRows key={offer.id} offer={offer} query={query} category={category} installedOnly={installedOnly} />
  ))}</>
}
