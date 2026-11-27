/**
 * InstallFromSpaceDialog — install one Kiro Space listing, after the person says yes.
 *
 * Opening the dialog runs `POST /api/apps/market/install {ref}`: the gateway
 * resolves the version (refusing when the account signed in now is not the
 * one the page loaded under), downloads it, checks its SHA-256 and unpacks it, and
 * installs nothing. The dialog shows what came back (publisher, version, size,
 * SHA-256 prefix, the permissions the publisher declared, the trust label). Only
 * "Install" calls `/install/confirm` with `confirmed: true`; closing discards the
 * staging. The app is installed switched off.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import Modal from '../Modal'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { fmtBytes } from '../../i18n/format'
import { pageAccount, useMarketRegistries } from './marketAccount'
import type { MarketInstallConfirm, MarketListingRow } from './types'

/** The copy key for an install refusal code (design table 4.1), or `''`. */
export function installErrorKey(code: string): string {
  switch (code) {
    case 'market_registry_unavailable':
    case 'market_registry_changed':
      return 'components.appstore.marketStore.err_registry_unavailable'
    case 'market_signed_out':
      return 'components.appstore.marketStore.err_signed_out'
    case 'market_account_changed':
      return 'components.appstore.marketStore.err_account_changed'
    case 'market_api_refused':
      return 'components.appstore.marketStore.err_refused'
    case 'market_api_unreachable':
    case 'market_unavailable':
      return 'components.appstore.marketStore.err_unreachable'
    default:
      return ''
  }
}

const PERMISSION_ROWS = [
  ['commands', 'components.appstore.marketStore.perm_commands'],
  ['network', 'components.appstore.marketStore.perm_network'],
  ['files', 'components.appstore.marketStore.perm_files'],
  ['background', 'components.appstore.marketStore.perm_background'],
  ['mcpServers', 'components.appstore.marketStore.perm_mcp'],
  ['agents', 'components.appstore.marketStore.perm_agents'],
] as const

export default function InstallFromSpaceDialog({ row, onClose, onInstalled }: {
  row: MarketListingRow
  onClose: () => void
  /** After a confirmed install, with the installed app's name. */
  onInstalled: (name: string) => void
}) {
  const { t } = useTranslation()
  // The store's own label, as on the card the user clicked.
  const storeLabel = useMarketRegistries()?.offers.find(o => o.id === row.registry)?.label || row.registry
  const [confirm, setConfirm] = useState<MarketInstallConfirm | null>(null)
  const [error, setError] = useState('')
  const [installing, setInstalling] = useState(false)
  const stagingId = useRef('')
  const done = useRef(false)

  const describe = (e: unknown): string => {
    const key = installErrorKey(apiErrorCode(e))
    return key ? t(key) : (e as Error)?.message || t('components.appstore.marketStore.action_failed')
  }

  useEffect(() => {
    let cancelled = false
    // The account and store the page loaded under: prepare refuses when
    // another account is signed in now, or installs come from another store.
    api.prepareMarketInstall(row.ref, pageAccount(row.registry), row.registry).then(
      res => {
        if (cancelled) {
          void api.confirmMarketInstall(res.stagingId, false).catch(() => undefined)
          return
        }
        stagingId.current = res.stagingId
        setConfirm(res.confirm)
      },
      e => { if (!cancelled) setError(describe(e)) },
    )
    return () => {
      cancelled = true
      // Closing without "Install" discards what prepare verified.
      if (stagingId.current && !done.current) {
        void api.confirmMarketInstall(stagingId.current, false).catch(() => undefined)
      }
    }
  }, [row.ref]) // eslint-disable-line react-hooks/exhaustive-deps

  const install = async () => {
    if (!stagingId.current || installing) return
    setInstalling(true)
    setError('')
    try {
      const res = await api.confirmMarketInstall(stagingId.current, true)
      done.current = true
      onInstalled(res.name || row.name)
    } catch (e) {
      done.current = true // the id is single use, whatever the answer
      setError(describe(e))
    } finally {
      setInstalling(false)
    }
  }

  const perms = confirm?.permissions
  return (
    <Modal
      open
      onClose={onClose}
      title={t('components.appstore.marketStore.dialog_title', { name: row.displayName || row.name, label: storeLabel })}
      maxWidth={520}
      footer={(
        <div className="flex justify-end gap-2">
          <Btn onClick={onClose}>{t('components.appstore.marketStore.cancel')}</Btn>
          <Btn primary disabled={!confirm || installing || done.current} onClick={install}>
            {installing ? t('components.appstore.marketStore.installing') : t('components.appstore.marketStore.confirm_install')}
          </Btn>
        </div>
      )}
    >
      {!confirm && !error && (
        <div className="flex items-center gap-2 text-[13px] text-muted" role="status" aria-live="polite">
          <Loader2 size={14} className="animate-spin" aria-hidden /> {t('components.appstore.marketStore.preparing')}
        </div>
      )}
      {confirm && (
        <dl className="grid grid-cols-1 sm:grid-cols-[max-content_minmax(0,1fr)] sm:gap-x-4 gap-y-1.5 text-[13px] [overflow-wrap:anywhere]" data-testid="market-install-confirm">
          <dt className="text-muted">{t('components.appstore.marketStore.publisher_label')}</dt>
          <dd>{confirm.app.publisher}</dd>
          <dt className="text-muted">{t('components.appstore.marketStore.version_label')}</dt>
          <dd>{confirm.app.appVersion || String(confirm.app.version)}</dd>
          <dt className="text-muted">{t('components.appstore.marketStore.size_label')}</dt>
          <dd>{fmtBytes(confirm.size)}</dd>
          <dt className="text-muted">{t('components.appstore.marketStore.sha_label')}</dt>
          <dd className="font-mono">{confirm.app.sha256Prefix}</dd>
          <dt className="text-muted">{t('components.appstore.marketStore.trust_label')}</dt>
          <dd>{confirm.trustLabel}</dd>
          {confirm.components.length > 0 && (
            <>
              <dt className="text-muted">{t('components.appstore.marketStore.components_label')}</dt>
              <dd>{confirm.components.map(c => `${c.kind}: ${c.name}`).join(', ')}</dd>
            </>
          )}
          <dt className="text-muted sm:col-span-2 mt-2">{t('components.appstore.marketStore.permissions_label')}</dt>
          {/* Written here, not the server's English notice: what the list is,
              and what still holds the app back once it is installed. */}
          <dd className="sm:col-span-2" data-testid="market-permissions-notice">{t('components.appstore.marketStore.permissions_notice')}</dd>
          {perms && PERMISSION_ROWS.map(([field, key]) => (
            <div key={field} className="contents">
              <dt className="text-muted">{t(key)}</dt>
              <dd className="min-w-0">{perms[field].length ? perms[field].join(', ') : t('components.appstore.marketStore.none')}</dd>
            </div>
          ))}
        </dl>
      )}
      {error && <div className="mt-3"><ErrorNotice message={error} askAgent /></div>}
    </Modal>
  )
}
