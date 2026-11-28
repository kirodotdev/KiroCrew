import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { Check, ChevronDown, KeyRound, Loader2, UserPlus, X, type LucideIcon } from 'lucide-react'

import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { i18nT } from '../../../i18n/t'
import { browserLiveStore } from '../../../hooks/useBrowserLive'
import { askUi, type HeldSheet } from './askUi'
import type { QuickAsk, QuickAskVaultLogin } from './types'

/**
 * The sign-in sheet's Secrets vault part.
 *
 * The gateway sends the NAMES of the vault logins for the ask's site (same
 * base domain; see `browser_harness/vault_fill.py`). A click on one asks the gateway to fill it: the username and
 * password go from the vault to the page in the gateway and never reach this
 * page. After a typed Fill (no match, or "Other account" beside one) the sheet
 * stays open: once the gateway says the sign-in landed it asks "Save this
 * login?", and the gateway, which holds the login meanwhile, saves it on Save.
 */

/** The ask's vault part, or null when the gateway sent none. */
export function useVaultPart(ask: QuickAsk): { items: QuickAskVaultLogin[]; more?: number } | null {
  return ask.vault ?? null
}

/** How many login buttons show before "More". */
export const LOGINS_SHOWN = 3

const rowCls = 'h-11 w-full justify-start gap-2 px-3 text-[15px]'

/** A row that opens more of the card in place: a chevron, or *icon* when it adds an entry. */
export function ExpandRow({ label, open, onToggle, busy, testId, icon: Icon }: { label: string; open: boolean; onToggle: () => void; busy: boolean; testId: string; icon?: LucideIcon }) {
  return (
    <Btn type="button" className={rowCls} aria-expanded={open} disabled={busy} onClick={onToggle} data-testid={testId}>
      {Icon
        ? <Icon size={16} aria-hidden="true" className="shrink-0 text-muted" />
        : <ChevronDown size={16} aria-hidden="true" className={`shrink-0 text-muted ${open ? "rotate-180" : ""}`} />}
      <span className="min-w-0 truncate">{label}</span>
    </Btn>
  )
}

/** The "Other account" row: a separate icon, so it does not read as "More". */
export const OTHER_ACCOUNT_ICON = UserPlus

/**
 * What a login's button reads: its username, or its entry name when it has
 * none; a username two offered logins share also shows each entry name, muted.
 */
export function loginLabels(items: QuickAskVaultLogin[]): { main: string; muted: string }[] {
  const seen = new Map<string, number>()
  for (const it of items) if (it.username) seen.set(it.username, (seen.get(it.username) ?? 0) + 1)
  return items.map(it => {
    if (!it.username) return { main: it.name, muted: '' }
    return { main: it.username, muted: (seen.get(it.username) ?? 0) > 1 ? it.name : '' }
  })
}

/**
 * One button per matching login, one per row: the first {@link LOGINS_SHOWN},
 * then a "More" row that shows the rest in place. Logins past the gateway's
 * own cap are counted, never silently dropped.
 */
export function VaultLogins({ items, more = 0, busy, onPick, other }: {
  items: QuickAskVaultLogin[]
  more?: number
  busy: boolean
  onPick: (name: string) => void
  /** The "Other account" row: it shares the "More" row, so neither falls below a short card's fold. */
  other?: ReactNode
}) {
  const [all, setAll] = useState(false)
  if (items.length === 0) return null
  const labels = loginLabels(items)
  const shown = all ? items : items.slice(0, LOGINS_SHOWN)
  return (
    <div className="flex flex-col gap-2" data-testid="quick-ask-vault-logins">
      {shown.map((it, i) => (
        <Btn key={it.name} type="button" className={rowCls} disabled={busy} onClick={() => onPick(it.name)} data-testid="quick-ask-vault-login">
          <KeyRound size={16} aria-hidden="true" className="shrink-0 text-accent" />
          <span className="min-w-0 truncate">
            {labels[i].main}
            {labels[i].muted && <span className="ml-2 text-muted">{labels[i].muted}</span>}
          </span>
        </Btn>
      ))}
      {!all && items.length > LOGINS_SHOWN ? (
        <div className="flex min-w-0 gap-2">
          <ExpandRow label={i18nT('pages.chat.quickAsk.more_logins')} open={false} onToggle={() => setAll(true)} busy={busy} testId="quick-ask-vault-more-row" />
          {other}
        </div>
      ) : other}
      {all && more > 0 && <p className="text-[12px] text-muted" data-testid="quick-ask-vault-more">{i18nT('pages.chat.quickAsk.vault_more', { count: more })}</p>}
    </div>
  )
}

/** How long "Saved to Secrets store" shows before the sheet closes. */
export const SAVED_SHOW_MS = 2500
/** The question closes this long before the gateway's hold ends, so a Save sent at the last moment still lands. */
export const OFFER_EXPIRY_MARGIN_MS = 5000

/** The held sheet's heading row: the same icon tile, title and close button as the ask's own. */
function HeldHead({ icon: Icon, title, titleId, spin, onClose }: { icon: LucideIcon; title: string; titleId: string; spin?: boolean; onClose: () => void }) {
  return (
    <div className="flex items-center gap-3">
      <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent-subtle text-accent">
        <Icon size={20} aria-hidden="true" className={spin ? 'animate-spin' : undefined} />
      </div>
      <h2 id={titleId} className="min-w-0 flex-1 text-[16px] font-semibold leading-6 text-text-strong">{title}</h2>
      <Btn type="button" className="!px-1.5 border-transparent shrink-0" onClick={onClose} aria-label={i18nT('pages.chat.quickAsk.not_now')} title={i18nT('pages.chat.quickAsk.not_now')} data-testid="quick-ask-held-close">
        <X size={16} aria-hidden="true" />
      </Btn>
    </div>
  )
}

/**
 * The sign-in sheet after a typed Fill, still open: "Signing in…" while the
 * gateway watches the page, "Save this login?" with the site and username once
 * the sign-in landed, then "Saved to Secrets store" for a moment before it closes.
 * Not now (or Esc) closes it and the gateway drops the login.
 */
export function HeldSaveBody({ sessionKey, held, titleId }: { sessionKey: string; held: HeldSheet; titleId: string }) {
  const [busy, setBusy] = useState(false)
  const saveRef = useRef<HTMLButtonElement>(null)
  const askId = held.ask.id
  const { phase, offer } = held
  useEffect(() => {
    if (phase === 'offer') saveRef.current?.focus()
    if (phase !== 'saved') return
    const t = setTimeout(() => askUi.releaseSheet(sessionKey, askId), SAVED_SHOW_MS)
    return () => clearTimeout(t)
  }, [sessionKey, askId, phase])
  // The question closes when the gateway lets go of the login, so a Save is never
  // offered for a login that is already gone.
  const expiresInMs = phase === 'offer' ? offer?.expiresInMs : undefined
  useEffect(() => {
    if (!expiresInMs) return
    const t = setTimeout(() => askUi.releaseSheet(sessionKey, askId), Math.max(0, expiresInMs - OFFER_EXPIRY_MARGIN_MS))
    return () => clearTimeout(t)
  }, [sessionKey, askId, expiresInMs])
  const close = () => foldHeldSheet(sessionKey, held)
  if (phase === 'signing') {
    return <div role="status" data-testid="quick-ask-held-signing"><HeldHead icon={Loader2} spin title={i18nT('pages.chat.quickAsk.signing_in')} titleId={titleId} onClose={close} /></div>
  }
  if (phase === 'saved') {
    return <div role="status" data-testid="quick-ask-held-saved"><HeldHead icon={Check} title={i18nT('pages.chat.quickAsk.save_offer_saved')} titleId={titleId} onClose={close} /></div>
  }
  if (!offer) return null
  const notNow = () => {
    askUi.releaseSheet(sessionKey, askId)
    void browserLiveStore.answerSaveOffer(sessionKey, offer.id, 'drop')
  }
  const save = async () => {
    setBusy(true)
    const ok = await browserLiveStore.answerSaveOffer(sessionKey, offer.id, 'save')
    setBusy(false)
    if (ok) askUi.markHeldSaved(sessionKey, askId)
    else { askUi.releaseSheet(sessionKey, askId); askUi.markUnsaved(sessionKey) }
  }
  return (
    <div data-testid="quick-ask-held-offer">
      <HeldHead icon={KeyRound} title={i18nT('pages.chat.quickAsk.save_offer_title')} titleId={titleId} onClose={notNow} />
      <div className="mt-4 flex h-11 min-w-0 items-center gap-3 rounded-md border border-border bg-bg-hover px-3 text-[15px]" data-testid="quick-ask-held-login">
        <span className="min-w-0 truncate font-semibold text-text-strong">{offer.site}</span>
        {offer.username && <span className="ml-auto min-w-0 truncate text-text">{offer.username}</span>}
      </div>
      <div className="mt-4 flex flex-col-reverse gap-2 py-2 sm:flex-row sm:justify-end">
        <Btn type="button" className="h-11 justify-center px-4 text-[15px]" disabled={busy} onClick={notNow} data-testid="quick-ask-held-not-now">
          {i18nT('pages.chat.quickAsk.not_now')}
        </Btn>
        <Btn ref={saveRef} primary type="button" className="h-11 justify-center px-4 text-[15px]" disabled={busy} onClick={() => { void save() }} data-primary="" data-testid="quick-ask-held-save">
          {i18nT('pages.chat.quickAsk.save_offer_save')}
        </Btn>
      </div>
    </div>
  )
}

/** Esc, or the phone's backdrop, on a held sheet: Not now on the question; else just close. */
export function foldHeldSheet(sessionKey: string, held: HeldSheet): void {
  // The ask was answered by its Fill: closing the held sheet must not bring the form back.
  askUi.markAnswered(held.ask.id)
  askUi.releaseSheet(sessionKey, held.ask.id)
  if (held.phase === 'offer' && held.offer) void browserLiveStore.answerSaveOffer(sessionKey, held.offer.id, 'drop')
}

/**
 * After the sheet is gone: the sign-in landed but the asked-for save did not.
 * Drawn by the session's current card in every view (live or finished) and by
 * the big view in both modes, so it stays until dismissed, not until the turn ends.
 */
export function VaultUnsavedNotice({ sessionKey }: { sessionKey: string }) {
  useSyncExternalStore(askUi.subscribe, askUi.snapshot, askUi.snapshot)
  if (!askUi.isUnsaved(sessionKey)) return null
  return (
    <div className="border-t border-border px-3 py-2">
      <ErrorNotice
        variant="inline"
        message={i18nT('pages.chat.quickAsk.error_vault_not_saved')}
        askAgent
        onDismiss={() => askUi.dismissUnsaved(sessionKey)}
        testId="quick-ask-vault-unsaved"
      />
    </div>
  )
}
