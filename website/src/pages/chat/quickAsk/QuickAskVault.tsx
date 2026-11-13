import { useState, useSyncExternalStore, type ReactNode } from 'react'
import { ChevronDown, KeyRound, UserPlus, type LucideIcon } from 'lucide-react'

import { Btn, Checkbox } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import InfoTip from '../../../components/InfoTip'
import { i18nT } from '../../../i18n/t'
import { usePreviewFlag } from '../../../hooks/usePreviewFlag'
import { PREVIEW_SECRETS_VAULT } from '../../../utils/previewFlags'
import { askUi } from './askUi'
import type { QuickAsk, QuickAskVaultLogin } from './types'

/**
 * The sign-in sheet's Secrets vault part (preview `PREVIEW_SECRETS_VAULT`).
 *
 * The gateway sends the NAMES of the vault logins for the ask's site (same
 * base domain; see `browser_harness/vault_fill.py`). A click on one asks the gateway to fill it: the username and
 * password go from the vault to the page in the gateway and never reach this
 * page. Typing (no match, or "Other account" beside one) offers "Save to Secrets store"
 * (off by default); the gateway saves what was typed only after the fill lands.
 */

/** The ask's vault part, or null: the preview is off, or the gateway sent none. */
export function useVaultPart(ask: QuickAsk): { items: QuickAskVaultLogin[]; more?: number } | null {
  const on = usePreviewFlag(PREVIEW_SECRETS_VAULT)
  return on && ask.vault ? ask.vault : null
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

/** "Save to Secrets store": off until the person ticks it. */
export function SaveToVaultBox({ on, setOn }: { on: boolean; setOn: (v: boolean) => void }) {
  return (
    <div className="flex items-center gap-1">
      <label className="flex w-fit cursor-pointer items-center gap-2 text-[12px] text-text">
        <Checkbox checked={on} onChange={e => setOn(e.target.checked)} data-testid="quick-ask-save-to-vault" />
        {i18nT('pages.chat.quickAsk.save_to_vault')}
      </label>
      <InfoTip text={i18nT('pages.chat.quickAsk.save_to_vault_tip')} />
    </div>
  )
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
