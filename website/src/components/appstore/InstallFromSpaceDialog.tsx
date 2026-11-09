/**
 * InstallFromSpaceDialog — install an app from Kiro Space by pasting its
 * reference, in two steps over the gateway's two market install routes.
 *
 * Step 1 takes `<namespace>/<name>` (optional `@<version>`), checked with
 * `parseMarketRef` before any request. The gateway then downloads, verifies and
 * stages the app and answers with the confirm data. Step 2 shows that data and
 * asks. Install posts `confirmed: true`; every other way out (Cancel, Escape,
 * the backdrop) posts `confirmed: false` so the staged copy is discarded at once
 * instead of waiting out its lifetime. The installed app arrives disabled, so
 * the usual trust prompt on enable is unchanged.
 */
import { useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react'
import { Loader2, Package } from 'lucide-react'

import { api } from '../../api/client'
import { fmtBytes } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { parseMarketRef } from '../../lib/marketRef'
import { Btn, Input } from '../ui'
import AppStoreDialog from './AppStoreDialog'
import { errorCode } from './TrustAppModal'

/** The part of the route's `confirm` object this dialog shows. */
interface MarketConfirm {
  app: { ref: string; version: number; appVersion: string; publisher: string; ownerGroup: string }
  components: { kind: string; name: string }[]
  dependencies: unknown[]
  permissions: {
    notice: string
    commands: string[]
    network: string[]
    files: string[]
    background: string[]
    mcpServers: string[]
    agents: string[]
  }
  size: number
  trustLabel: string
}

/** One plain sentence per route error code; any other refusal gets the generic line. */
function errorSentence(e: unknown): string {
  switch (errorCode(e)) {
    case 'invalid_json': return i18nT('components.appstore.installFromSpace.error_invalid_json')
    case 'market_resolve_invalid': return i18nT('components.appstore.installFromSpace.error_market_resolve_invalid')
    case 'market_staging_id_invalid': return i18nT('components.appstore.installFromSpace.error_market_staging_id_invalid')
    case 'market_confirm_invalid': return i18nT('components.appstore.installFromSpace.error_market_confirm_invalid')
    case 'market_staging_unknown': return i18nT('components.appstore.installFromSpace.error_market_staging_unknown')
    case 'market_unavailable': return i18nT('components.appstore.installFromSpace.error_market_unavailable')
    case 'market_client_unavailable': return i18nT('components.appstore.installFromSpace.error_market_client_unavailable')
    case 'market_install_too_many_pending': return i18nT('components.appstore.installFromSpace.error_market_install_too_many_pending')
    default: return i18nT('components.appstore.installFromSpace.error_generic')
  }
}

export interface InstallFromSpaceDialogProps {
  onClose: () => void
  /** Called with the installed app's name once the gateway has installed it. */
  onInstalled: (name: string) => void
  /** The trigger the dialog was opened from; focus goes back there on close. */
  returnFocusTo: RefObject<HTMLElement | null>
}

function PermissionGroup({ label, items }: { label: string; items: string[] }) {
  return (
    <div>
      <div className="text-[12px] font-medium text-text">{label}</div>
      {items.length === 0 ? (
        <div className="text-[12px] text-muted">{i18nT('components.appstore.installFromSpace.none')}</div>
      ) : (
        <ul className="text-[12px] text-muted list-disc ps-4 break-words">
          {items.map((item, i) => <li key={`${i}:${item}`}>{item}</li>)}
        </ul>
      )}
    </div>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-x-2 text-[13px]">
      <span className="text-muted">{label}</span>
      <span className="text-text break-words min-w-0">{children}</span>
    </div>
  )
}

export default function InstallFromSpaceDialog({ onClose, onInstalled, returnFocusTo }: InstallFromSpaceDialogProps) {
  const [value, setValue] = useState('')
  const [error, setError] = useState('')
  const [preparing, setPreparing] = useState(false)
  const [installing, setInstalling] = useState(false)
  const [staged, setStaged] = useState<{ id: string; confirm: MarketConfirm } | null>(null)
  // Read after an await, where state would be stale: set once the person has left.
  const leftRef = useRef(false)

  const discard = (id: string) => { api.marketInstallConfirm(id, false).catch(() => {}) }

  const leave = () => {
    if (installing) return
    leftRef.current = true
    if (staged) discard(staged.id)
    onClose()
  }

  const review = async (e: FormEvent) => {
    e.preventDefault()
    const parsed = parseMarketRef(value)
    if (!parsed) { setError(i18nT('components.appstore.installFromSpace.ref_invalid')); return }
    setError('')
    setPreparing(true)
    try {
      const res = await api.marketInstallPrepare(parsed.ref, parsed.version) as { stagingId: string; confirm: MarketConfirm }
      // Left while the gateway was still staging: nobody will answer, so discard it now.
      if (leftRef.current) { discard(res.stagingId); return }
      setStaged({ id: res.stagingId, confirm: res.confirm })
    } catch (err) {
      setError(errorSentence(err))
    } finally {
      setPreparing(false)
    }
  }

  const install = async () => {
    if (!staged) return
    setError('')
    setInstalling(true)
    try {
      const res = await api.marketInstallConfirm(staged.id, true) as { name: string }
      leftRef.current = true
      onInstalled(res.name)
      onClose()
    } catch (err) {
      // The id is single use: whatever failed, the staged copy is gone, so start over.
      setStaged(null)
      setError(errorSentence(err))
    } finally {
      setInstalling(false)
    }
  }

  const c = staged?.confirm
  const p = c?.permissions
  return (
    <AppStoreDialog
      className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-xs animate-rise p-safe"
      onClose={leave}
      returnFocusTo={returnFocusTo}
      aria-label={i18nT('components.appstore.installFromSpace.title')}
    >
      {/* Presentation-only: keeps a click inside the card from reaching the backdrop's close. */}
      <div
        role="presentation"
        className="bg-card border border-border rounded-xl p-5 max-w-md w-full mx-4 max-h-[85vh] overflow-y-auto shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-xl bg-bg-elevated flex items-center justify-center shrink-0">
            <Package size={20} className="text-muted" />
          </div>
          <div className="font-medium text-text">
            {c ? i18nT('components.appstore.installFromSpace.review_title') : i18nT('components.appstore.installFromSpace.title')}
          </div>
        </div>

        {error && <div role="alert" className="mb-3 text-[13px] text-danger-text">{error}</div>}

        {!c && (
          <form onSubmit={review} className="space-y-3">
            <label className="block text-[13px] text-text" htmlFor="install-from-space-ref">
              {i18nT('components.appstore.installFromSpace.ref_label')}
            </label>
            <Input
              id="install-from-space-ref"
              value={value}
              onChange={e => setValue(e.target.value)}
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              className="w-full"
            />
            <div className="text-[12px] text-muted">{i18nT('components.appstore.installFromSpace.ref_hint')}</div>
            <div className="flex justify-end gap-2">
              <Btn type="button" onClick={leave}>{i18nT('components.appstore.installFromSpace.cancel')}</Btn>
              <Btn type="submit" primary disabled={preparing || !value.trim()}>
                {preparing && <Loader2 size={14} className="animate-spin" />}
                {preparing ? i18nT('components.appstore.installFromSpace.checking') : i18nT('components.appstore.installFromSpace.review')}
              </Btn>
            </div>
          </form>
        )}

        {c && p && (
          <div className="space-y-3">
            <div className="space-y-1">
              <Row label={i18nT('components.appstore.installFromSpace.name')}>{c.app.ref}</Row>
              <Row label={i18nT('components.appstore.installFromSpace.publisher')}>{c.app.publisher}</Row>
              <Row label={i18nT('components.appstore.installFromSpace.version')}>{c.app.appVersion} ({c.app.version})</Row>
              <Row label={i18nT('components.appstore.installFromSpace.size')}>{fmtBytes(c.size)}</Row>
              <Row label={i18nT('components.appstore.installFromSpace.trust')}>{c.trustLabel}</Row>
            </div>
            <div>
              <div className="text-[13px] font-medium text-text">{i18nT('components.appstore.installFromSpace.components')}</div>
              {c.components.length === 0 ? (
                <div className="text-[12px] text-muted">{i18nT('components.appstore.installFromSpace.none')}</div>
              ) : (
                <ul className="text-[12px] text-muted list-disc ps-4 break-words">
                  {c.components.map((x, i) => <li key={`${i}:${x.kind}:${x.name}`}>{x.kind} {x.name}</li>)}
                </ul>
              )}
            </div>
            <PermissionGroup
              label={i18nT('components.appstore.installFromSpace.dependencies')}
              items={c.dependencies.map(d => (typeof d === 'string' ? d : String((d as { name?: unknown })?.name ?? '')))}
            />
            <div className="space-y-2">
              <div className="text-[13px] font-medium text-text">{i18nT('components.appstore.installFromSpace.permissions')}</div>
              <div className="text-[12px] text-muted">{p.notice}</div>
              <PermissionGroup label={i18nT('components.appstore.installFromSpace.perm_commands')} items={p.commands} />
              <PermissionGroup label={i18nT('components.appstore.installFromSpace.perm_network')} items={p.network} />
              <PermissionGroup label={i18nT('components.appstore.installFromSpace.perm_files')} items={p.files} />
              <PermissionGroup label={i18nT('components.appstore.installFromSpace.perm_background')} items={p.background} />
              <PermissionGroup label={i18nT('components.appstore.installFromSpace.perm_mcpServers')} items={p.mcpServers} />
              <PermissionGroup label={i18nT('components.appstore.installFromSpace.perm_agents')} items={p.agents} />
            </div>
            <div className="flex justify-end gap-2">
              <Btn type="button" onClick={leave} disabled={installing}>{i18nT('components.appstore.installFromSpace.cancel')}</Btn>
              <Btn type="button" primary onClick={install} disabled={installing}>
                {installing && <Loader2 size={14} className="animate-spin" />}
                {installing ? i18nT('components.appstore.installFromSpace.installing') : i18nT('components.appstore.installFromSpace.install')}
              </Btn>
            </div>
          </div>
        )}
      </div>
    </AppStoreDialog>
  )
}
