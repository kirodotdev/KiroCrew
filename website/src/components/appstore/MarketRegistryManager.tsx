/**
 * MarketRegistryManager — the "Kiro Space" block of the Sources popover.
 *
 * Lists the Kiro Space stores this build offers (`GET /api/apps/market/registries`)
 * and lets the owner add or remove one, and sign in or out of an added one. The
 * person never types an address: only a store the build offers can be added.
 * With no offers (the open-source build has no store provider) the block renders
 * nothing at all; a failed read shows the error instead.
 */
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { MoreHorizontal, Store } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../ui/dropdown-menu'
import {
  marketPage, registriesReadError, useMarketRegistriesQuery, useMarketStatuses,
} from './marketAccount'
import { SignInProgress, useMarketRefresh, useStoreSignIn } from './marketSignIn'
import type { MarketRegistryOffer, MarketRegistryStatus } from './types'

export { SIGN_IN_POLL_MS, SIGN_IN_WAIT_MS } from './marketSignIn'

/** One line saying where a store's sign-in stands. */
export function useMarketStatusText() {
  const { t } = useTranslation()
  return (status: MarketRegistryStatus | undefined): string => {
    if (!status) return t('components.appstore.marketStore.state_checking')
    switch (status.state) {
      case 'signed_in':
        return t('components.appstore.marketStore.state_signed_in', { account: status.account })
      case 'needs_sign_in':
        return t('components.appstore.marketStore.state_needs_sign_in')
      case 'no_access':
        return t('components.appstore.marketStore.state_no_access')
      default:
        if (status.detail === 'cli_missing') return t('components.appstore.marketStore.state_cli_missing')
        if (status.detail === 'cli_outdated') return t('components.appstore.marketStore.state_cli_outdated')
        return t('components.appstore.marketStore.state_checking')
    }
  }
}

interface RowAction {
  key: string
  label: string
  primary?: boolean
  run: () => void
}

function StoreRow({ offer, added, offered, status, onChanged }: {
  offer: MarketRegistryOffer
  added: string[]
  /** The ids the build offers now: an added id outside it is not a store here. */
  offered: ReadonlySet<string>
  status?: MarketRegistryStatus
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const statusText = useMarketStatusText()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const isAdded = added.includes(offer.id)
  const otherAdded = added.some(id => id !== offer.id && offered.has(id))
  const state = status?.state
  const signIn = useStoreSignIn(offer.id, state, onChanged)

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true)
    setError('')
    try {
      await work()
      onChanged()
    } catch (e) {
      setError((e as Error)?.message || t('components.appstore.marketStore.action_failed'))
    } finally {
      setBusy(false)
    }
  }
  const add = () => run(() => api.setMarketRegistries([...added, offer.id]))
  const remove = () => run(async () => {
    signIn.clear()
    await api.setMarketRegistries(added.filter(id => id !== offer.id))
  })
  const startSignIn = () => run(signIn.start)
  // Signed out: nothing read for the old account may stay on this page.
  const signOut = () => run(async () => {
    signIn.clear()
    await api.marketSignOut(offer.id)
    marketPage.reload()
  })

  // `unknown` because the CLI is too old: signing in again cannot help.
  const outdated = state === 'unknown' && status?.detail === 'cli_outdated'
  const actions: RowAction[] = []
  // One store at a time: installs come from exactly one added store.
  if (!isAdded && !otherAdded) actions.push({ key: 'add', label: t('components.appstore.marketStore.add'), run: add })
  if (isAdded && !outdated && (state === 'needs_sign_in' || state === 'no_access')) {
    actions.push({ key: 'sign-in', label: t('components.appstore.marketStore.sign_in'), primary: true, run: startSignIn })
  }
  if (isAdded && state === 'unknown' && !outdated) {
    actions.push({ key: 'retry', label: t('components.appstore.marketStore.retry'), run: onChanged })
  }
  if (isAdded && (state === 'signed_in' || state === 'no_access')) {
    actions.push({ key: 'sign-out', label: t('components.appstore.marketStore.sign_out'), run: signOut })
  }
  if (isAdded) actions.push({ key: 'remove', label: t('components.appstore.marketStore.remove'), run: remove })
  // At most two controls: past two, the first stays a button and the rest go in a menu.
  const shown = actions.length > 2 ? actions.slice(0, 1) : actions
  const overflow = actions.length > 2 ? actions.slice(1) : []

  return (
    <div className="py-2 border-b border-border last:border-b-0" data-testid={`market-store-${offer.id}`}>
      <div className="flex items-center gap-2">
        <Store size={15} className="text-muted shrink-0" aria-hidden />
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-medium text-text-strong truncate">{offer.label}</div>
          {offer.web && <div className="text-[12px] text-muted truncate">{offer.web.replace(/^https:\/\//, '')}</div>}
          {isAdded && <div className="text-[12px] text-muted" role="status">{statusText(status)}</div>}
        </div>
        {shown.map(a => (
          <Btn key={a.key} primary={a.primary} disabled={busy} onClick={a.run}>{a.label}</Btn>
        ))}
        {overflow.length > 0 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Btn
                className="!px-1.5"
                disabled={busy}
                aria-label={t('components.appstore.marketStore.more_actions')}
                title={t('components.appstore.marketStore.more_actions')}
              >
                <MoreHorizontal size={14} />
              </Btn>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {overflow.map(a => (
                <DropdownMenuItem key={a.key} onSelect={a.run}>{a.label}</DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      <SignInProgress signIn={signIn} className="mt-2 ml-6" />
      {error && <div className="mt-2"><ErrorNotice message={error} variant="inline" onDismiss={() => setError('')} askAgent /></div>}
    </div>
  )
}

export default function MarketRegistryManager() {
  const { t } = useTranslation()
  const { data, error } = useMarketRegistriesQuery()
  const statuses = useMarketStatuses()
  const refresh = useMarketRefresh()
  const readError = registriesReadError(error)
  const offers = data?.offers ?? []
  const offeredIds = new Set(offers.map(o => o.id))
  if (!readError && offers.length === 0) return null
  return (
    <div className="border-t border-border mt-4 pt-4" data-testid="market-registry-manager">
      <div className="text-sm font-semibold tracking-tight text-text-strong mb-1">{t('components.appstore.marketStore.title')}</div>
      {/* A failed background read stays visible above the last answer's rows. */}
      {readError && <div className="mb-2"><ErrorNotice message={readError} variant="inline" askAgent /></div>}
      {offers.length > 0 && <p className="text-[12px] text-muted mb-2">{t('components.appstore.marketStore.intro')}</p>}
      {offers.map(offer => (
        <StoreRow
          key={offer.id}
          offer={offer}
          added={data?.added ?? []}
          offered={offeredIds}
          status={statuses?.[offer.id]}
          onChanged={refresh}
        />
      ))}
    </div>
  )
}
