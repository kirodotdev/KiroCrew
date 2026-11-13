import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react'
import { EyeOff, Lock, Unlock, X } from 'lucide-react'

import { Btn } from '../../../components/ui'
import Clickable from '../../../components/Clickable'
import ErrorNotice from '../../../components/ErrorNotice'
import { OverlayPortal, useInsideDialog } from '../../../components/overlayRoot'
import { i18nT } from '../../../i18n/t'
import { useDocumentImeLatch } from '../../../hooks/useImeGuard'
import { useScrollEdgesY } from '../../../hooks/useScrollEdges'
import { sanitizeLlmOutput } from '../../../utils/sanitize'
import { browserLiveStore } from '../../../hooks/useBrowserLive'
import { askUi } from './askUi'
import {
  answerErrorText, askIcon, askTitle, isSignInTakeover, labelWord, OriginLine, originHost, otpDigits, QuickAskTargetRing, ReasonLine, textLabel,
} from './QuickAskParts'
import { recall, remember, type RememberKey } from './remember'
import { ExpandRow, OTHER_ACCOUNT_ICON, SaveToVaultBox, useVaultPart, VaultLogins } from './QuickAskVault'
import type { QuickAsk, QuickAskAction, QuickAskField, QuickAskValues, QuickAskVaultChoice } from './types'

/**
 * V2 -- the ask sheet (Muse-style). One dialog holds the whole ask. On a wide
 * screen it sits over the live frame, which dims except for a ring on the
 * bound element; on a phone it is a bottom sheet.
 *
 * Esc and "Not now" FOLD the sheet to the one-line hint (nothing is sent);
 * Decline tells the agent no. Tab stays inside the sheet while it is open.
 *
 * Secrets. Every typed value lives only in its native input (uncontrolled:
 * never React state, so never in devtools or a re-render's props). On submit
 * the values are read once, the inputs are emptied at once, and the values go
 * into the single answer POST. Unmounting (an ask replaced, the sheet folded,
 * the card scrolled away) empties them too.
 */
export default function QuickAskSheet({ ask, sessionKey, mode, onCollapse }: {
  ask: QuickAsk
  sessionKey: string
  mode: 'overlay' | 'bottom'
  onCollapse: () => void
}) {
  // Bottom mode leaves the card's animated (transformed) box through the
  // overlay root; inside the big view's own dialog it stays put, or the
  // dialog's focus trap and pointer lock would shut it out.
  const inDialog = useInsideDialog()
  const Icon = askIcon(ask.kind)
  const titleId = useId()
  const ref = useRef<HTMLDivElement | null>(null)
  // Whether the sheet hides fields below its fold: a fade then shows, so a short
  // card does not read as complete.
  const [attachScroll, edges, , attachContent] = useScrollEdgesY<HTMLDivElement>()
  // Whether the sheet is tall enough to pin its answer row (see SHEET_PIN_MIN_PX).
  const [tall, setTall] = useState(true)
  // Whether the answer row is pinned: it then carries the fade, else the sheet's bottom edge does.
  const [rowPinned, setRowPinned] = useState(false)
  const stopMeasure = useRef<(() => void) | null>(null)
  const panelRef = useCallback((node: HTMLDivElement | null) => {
    ref.current = node
    attachScroll(node)
    stopMeasure.current?.()
    stopMeasure.current = null
    if (!node) return
    const measure = () => setTall(node.clientHeight >= SHEET_PIN_MIN_PX)
    measure()
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    ro?.observe(node)
    stopMeasure.current = () => ro?.disconnect()
  }, [attachScroll])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(true)

  // Focus moves in, but never out of a box the person is typing in elsewhere
  // (the composer): their next keys would land in this sheet's fields. On a
  // phone the panel takes focus, not a field, so no keyboard pops up unasked.
  useEffect(() => {
    alive.current = true
    const before = document.activeElement as HTMLElement | null
    const typing = !!before && before !== document.body && (before.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(before.tagName))
    if (!typing) {
      const first = mode === 'overlay' ? ref.current?.querySelector<HTMLElement>('input:not([type=checkbox]):not([readonly]), button[data-primary]') : null
      ;(first ?? ref.current)?.focus()
    }
    return () => {
      alive.current = false
      if (!typing && before && before.isConnected) before.focus?.()
    }
  }, [mode])

  const send = async (action: QuickAskAction, values?: QuickAskValues, vault?: QuickAskVaultChoice): Promise<boolean> => {
    setBusy(true)
    setError('')
    const r = await browserLiveStore.answerAsk(sessionKey, ask.id, action, values, vault)
    // The sheet goes with the ask; a save that did not land is said by the card.
    if (r.ok && vault?.save && r.saved === false) askUi.markUnsaved(sessionKey)
    if (!alive.current) return r.ok
    // take_over stays busy: the gateway's next event moves the ask to taken_over.
    if (!r.ok || action !== 'take_over') setBusy(false)
    if (r.ok) {
      if (action !== 'take_over') askUi.markAnswered(ask.id)
    } else setError(answerErrorText(r.error ?? ''))
    return r.ok
  }

  // Esc folds, Tab stays inside. Keys typed here are the sheet's: the page's
  // shortcuts (and the big view's replay keys) must not act on them, so the
  // listener stops them here; React's own handlers on ancestors never see them.
  const collapseRef = useRef(onCollapse)
  collapseRef.current = onCollapse
  // A Tab or Escape the IME owns is choosing a candidate, not leaving the field.
  const imeLatch = useDocumentImeLatch()
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const onKey = (e: KeyboardEvent) => {
      e.stopPropagation()
      if (e.key === 'Escape') {
        if (!imeLatch.claimKey(e)) return
        e.preventDefault(); collapseRef.current(); return
      }
      if (e.key !== 'Tab') return
      if (!imeLatch.claimKey(e)) return
      const items = [...el.querySelectorAll<HTMLElement>('input, button, [href], [tabindex]:not([tabindex="-1"])')]
        .filter(n => !(n as HTMLButtonElement).disabled && n.tabIndex !== -1)
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && (document.activeElement === first || document.activeElement === el)) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
    }
    el.addEventListener('keydown', onKey)
    return () => el.removeEventListener('keydown', onKey)
  }, [imeLatch])

  const panel = (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      tabIndex={-1}
      data-testid="quick-ask-sheet"
      data-quick-ask-sheet=""
      data-kind={ask.kind}
      className={mode === 'overlay'
        ? 'max-h-full w-[min(400px,100%)] overflow-y-auto rounded-2xl border border-border-strong bg-bg-elevated px-5 pt-5 shadow-2xl focus:outline-none'
        : 'max-h-[90dvh] w-full overflow-y-auto rounded-t-2xl border-t border-border-strong bg-bg-elevated px-[max(20px,var(--safe-area-x))] pt-2 shadow-2xl focus:outline-none'}
    >
      {mode === 'bottom' && <div aria-hidden="true" className="mx-auto mb-3 h-1 w-10 rounded-full bg-border-strong" />}
      <div className="flex items-start gap-3">
        <div className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent-subtle text-accent">
          <Icon size={20} aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 id={titleId} className="text-[16px] font-semibold leading-6 text-text-strong">{askTitle(ask)}</h2>
          {/* The sign-in sheet shows the origin as its own read-only first field. */}
          {!isSignInTakeover(ask) && <OriginLine ask={ask} className="mt-0.5" />}
        </div>
        <Btn type="button" className="!px-1.5 border-transparent shrink-0" onClick={onCollapse} aria-label={i18nT('pages.chat.quickAsk.not_now')} title={i18nT('pages.chat.quickAsk.not_now')} data-testid="quick-ask-not-now">
          <X size={16} aria-hidden="true" />
        </Btn>
      </div>
      <ReasonLine ask={ask} className="mt-3" />
      <div className="mt-3" ref={attachContent}>
        <SheetFit.Provider value={{ moreBelow: edges.bottom, tall, setRowPinned }}>
          <AskBody ask={ask} busy={busy} send={send} />
        </SheetFit.Provider>
      </div>
      {/* No hand-off: the sheet's inputs may hold an unsent password, code or field
          value, and handing off to the agent would leave the sheet and drop them. */}
      <ErrorNotice variant="inline" message={error} className="mt-3" testId="quick-ask-error" />
      {/* The sheet's bottom space (and the phone's bottom inset) lives here, not in
          the sheet's padding: the scroll then ends at the sheet's edge, so the
          pinned answer row leaves no strip for the fields to show through. */}
      <p className="mt-3 pb-[max(20px,env(safe-area-inset-bottom))] text-center text-[11px] text-muted">{i18nT('pages.chat.quickAsk.kiro_paused')}</p>
      {edges.bottom && !rowPinned && <MoreBelowFade className="sticky bottom-0 -mt-6" />}
    </div>
  )

  if (mode === 'overlay') {
    return (
      <div className="absolute inset-0 z-20" data-testid="quick-ask-overlay">
        <QuickAskTargetRing ask={ask} dim />
        {/* Right-aligned so the ringed element on the page tends to stay in sight. */}
        <div className="absolute inset-0 flex items-center justify-end overflow-hidden p-3">{panel}</div>
      </div>
    )
  }
  const bottom = (
    <div className="fixed inset-0 z-50 flex flex-col justify-end bg-black/50" data-testid="quick-ask-bottom">
      {/* The backdrop folds the sheet like Esc; it is not a control of its own. */}
      <Clickable aria-hidden="true" tabIndex={-1} className="flex-1" onClick={onCollapse} />
      {panel}
    </div>
  )
  return inDialog ? bottom : <OverlayPortal>{bottom}</OverlayPortal>
}

/**
 * Below this height (CSS px) the sheet does not pin its answer row: a pinned
 * row would cover the only field a short box shows (the side panel's picture,
 * about 200px), so the row scrolls with the fields and the more-below fade says
 * it is there. Taller sheets pin it, keeping the answer button in reach.
 */
export const SHEET_PIN_MIN_PX = 320

/** How the sheet fits its box: content hidden below the fold, and room to pin the answer row. */
const SheetFit = createContext<{ moreBelow: boolean; tall: boolean; setRowPinned?: (pinned: boolean) => void }>({ moreBelow: false, tall: true })

/** The fade that says the sheet hides content below its fold. */
function MoreBelowFade({ className }: { className: string }) {
  return <div aria-hidden="true" className={`pointer-events-none h-6 bg-gradient-to-t from-bg-elevated to-transparent ${className}`} data-testid="quick-ask-more-below" />
}

type Send = (action: QuickAskAction, values?: QuickAskValues, vault?: QuickAskVaultChoice) => Promise<boolean>

function AskBody({ ask, busy, send }: { ask: QuickAsk; busy: boolean; send: Send }) {
  switch (ask.kind) {
    case 'credentials': return isSignInTakeover(ask)
      ? <SignInBody ask={ask} busy={busy} send={send} />
      : <CredentialsBody ask={ask} busy={busy} send={send} />
    case 'text': return <TextBody ask={ask} busy={busy} send={send} />
    case 'otp': return <OtpBody ask={ask} busy={busy} send={send} />
    case 'human_check': return <TakeOverBody explainer={i18nT('pages.chat.quickAsk.human_check_explainer')} primary={i18nT('pages.chat.quickAsk.take_over_box')} busy={busy} send={send} />
    case 'confirm': return <ConfirmBody ask={ask} busy={busy} send={send} />
  }
}

// ---- shared bits --------------------------------------------------------------

/** The gateway refuses a value over 1024 characters (`too_large`). */
const VALUE_MAX = 1024

const inputCls = 'h-11 w-full min-w-0 rounded-md border border-border bg-bg px-2.5 text-[15px] text-text placeholder:text-muted focus:outline-none focus:border-accent focus:ring-2 focus:ring-accent/30'

/** Empty every input on unmount, so a value never outlives its sheet. */
function useEmptyOnUnmount(a: RefObject<HTMLInputElement>, b?: RefObject<HTMLInputElement>) {
  useEffect(() => {
    const els = [a.current, b?.current]
    return () => { for (const el of els) if (el) el.value = '' }
  }, [a, b])
}

/** Read an input once and empty it. */
function take(r: RefObject<HTMLInputElement>): string {
  const el = r.current
  if (!el) return ''
  const v = el.value
  el.value = ''
  return v
}

/** On a phone the soft keyboard rises over the sheet; keep the focused box in view. */
function keepInView(e: React.FocusEvent<HTMLInputElement>) {
  const el = e.currentTarget
  setTimeout(() => el.scrollIntoView?.({ block: 'nearest' }), 300)
}

function Field({ label, children }: { label: string; children: (id: string) => ReactNode }) {
  const id = useId()
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className="text-[13px] font-medium text-text">{label}</label>
      {children(id)}
    </div>
  )
}

function SecretHint({ extra }: { extra?: string }) {
  return (
    <div className="flex flex-col gap-0.5 text-[12px] text-muted" data-testid="quick-ask-secret-hint">
      <p className="flex items-center gap-1.5">
        <EyeOff size={12} aria-hidden="true" className="shrink-0 text-accent" />
        {i18nT('pages.chat.quickAsk.secret_hint')}
      </p>
      {extra && <p className="pl-[18px]">{extra}</p>}
    </div>
  )
}

function Actions({ primary, onDecline, busy, declineLabel, onPrimary }: {
  /** Omitted: Decline only (a vault match whose typed fields are closed). */
  primary?: string
  busy: boolean
  onDecline: () => void
  declineLabel?: string
  /** Omitted: the primary button submits the form. */
  onPrimary?: () => void
}) {
  const { moreBelow, tall, setRowPinned } = useContext(SheetFit)
  // Pinned to the bottom of the sheet's own scroll while the person types, so the
  // answer button is in reach; not while only Decline shows (it would cover the
  // login rows) or in a sheet too short to keep a field clear of it.
  const pinned = primary !== undefined && tall
  useEffect(() => {
    setRowPinned?.(pinned)
    return () => setRowPinned?.(false)
  }, [pinned, setRowPinned])
  return (
    // While fields sit below the fold, a fade above the pinned row shows there is
    // more; an unpinned row scrolls away, so the sheet's bottom edge shows it instead.
    <div className={`${pinned ? 'sticky bottom-0 z-10' : 'relative'} mt-4 flex flex-col-reverse gap-2 bg-bg-elevated py-2 sm:flex-row sm:justify-end`} data-testid="quick-ask-actions" data-pinned={pinned ? '' : undefined} data-more-below={moreBelow ? '' : undefined}>
      {moreBelow && pinned && <MoreBelowFade className="absolute inset-x-0 -top-6" />}
      <Btn type="button" className="h-11 justify-center px-4 text-[15px]" onClick={onDecline} disabled={busy} data-testid="quick-ask-decline">
        {declineLabel ?? i18nT('pages.chat.quickAsk.decline')}
      </Btn>
      {primary !== undefined && (
        <Btn primary type={onPrimary ? 'button' : 'submit'} onClick={onPrimary} className="h-11 justify-center px-4 text-[15px]" disabled={busy} data-primary="" data-testid="quick-ask-primary">
          {primary}
        </Btn>
      )}
    </div>
  )
}

/** "Remember on <host>" plus the saved value's one-click chip. */
function useRemember(origin: string, key: RememberKey | null) {
  const saved = key ? recall(origin, key) : ''
  const [on, setOn] = useState(!!saved)
  const commit = (value: string) => {
    if (!key) return
    if (on && value) remember(origin, key, value)
    else if (!on && saved) remember(origin, key, '')
  }
  return { saved, on, setOn, commit, enabled: key !== null }
}

function RememberBox({ host, on, setOn }: { host: string; on: boolean; setOn: (v: boolean) => void }) {
  return (
    <label className="flex w-fit cursor-pointer items-center gap-2 text-[12px] text-text">
      <input type="checkbox" aria-label={i18nT('pages.chat.quickAsk.remember_on', { host })} className="accent-[var(--accent)]" checked={on} onChange={e => setOn(e.target.checked)} data-testid="quick-ask-remember" />
      {i18nT('pages.chat.quickAsk.remember_on', { host })}
    </label>
  )
}

/** A saved value, offered; never filled by itself. Shown while its box is empty. */
function UseSavedChip({ value, target, onUsed }: { value: string; target: RefObject<HTMLInputElement>; onUsed: () => void }) {
  return (
    <Btn
      type="button"
      className="h-11 shrink-0 whitespace-nowrap"
      onClick={() => { if (target.current) { target.current.value = value; target.current.focus() } onUsed() }}
      data-testid="quick-ask-use-saved"
    >
      {i18nT('pages.chat.quickAsk.use_saved', { value })}
    </Btn>
  )
}

// ---- per kind -----------------------------------------------------------------

function CredentialsBody({ ask, busy, send }: { ask: QuickAsk; busy: boolean; send: Send }) {
  const hasUser = ask.fields.some(f => f.role === 'username')
  const userRef = useRef<HTMLInputElement>(null)
  const passRef = useRef<HTMLInputElement>(null)
  useEmptyOnUnmount(userRef, passRef)
  const rem = useRemember(ask.origin, hasUser ? 'username' : null)
  const [userEmpty, setUserEmpty] = useState(true)
  const vault = useVaultPart(ask)
  const [save, setSave] = useState(false)
  // A vault match offers its rows; typing another account opens behind "Other account".
  const matched = !!vault && vault.items.length > 0
  const [other, setOther] = useState(false)
  const typing = !matched || other
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!typing) return
    if (!passRef.current?.value) { passRef.current?.focus(); return }
    const username = hasUser ? take(userRef).trim() : ''
    const password = take(passRef)
    setUserEmpty(true)
    const ok = await send('fill', { ...(username ? { username } : {}), password }, save ? { save: true } : undefined)
    // With the vault part the Remember box is not shown, so nothing here is
    // kept or forgotten: a username remembered earlier stays as it was.
    if (ok && !vault) rem.commit(username)
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-2" noValidate>
      {vault && <VaultLogins items={vault.items} more={vault.more} busy={busy} onPick={name => { void send('fill', undefined, { vault_item: name }) }}
        other={matched && <ExpandRow label={i18nT('pages.chat.quickAsk.other_account')} open={other} onToggle={() => setOther(o => !o)} busy={busy} testId="quick-ask-other-account" icon={OTHER_ACCOUNT_ICON} />} />}
      {typing && hasUser && (
        <Field label={i18nT('pages.chat.quickAsk.label_username_optional')}>
          {id => (
            <div className="flex min-w-0 gap-1.5">
              <input id={id} ref={userRef} aria-label={i18nT('pages.chat.quickAsk.label_username_optional')} className={inputCls} maxLength={VALUE_MAX} autoComplete="off" autoCapitalize="none" spellCheck={false}
                onInput={e => setUserEmpty(!e.currentTarget.value)} onFocus={keepInView} data-testid="quick-ask-username" />
              {rem.saved && userEmpty && !matched && <UseSavedChip value={rem.saved} target={userRef} onUsed={() => setUserEmpty(false)} />}
            </div>
          )}
        </Field>
      )}
      {typing && (
        <Field label={i18nT('pages.chat.quickAsk.label_password')}>
          {id => (
            <input id={id} ref={passRef} aria-label={i18nT('pages.chat.quickAsk.label_password')} type="password" className={inputCls} maxLength={VALUE_MAX} autoComplete="new-password" required
              onFocus={keepInView} data-testid="quick-ask-password" />
          )}
        </Field>
      )}
      {rem.enabled && !vault && <RememberBox host={originHost(ask.origin)} on={rem.on} setOn={rem.setOn} />}
      {vault && typing && <SaveToVaultBox on={save} setOn={setSave} />}
      <SecretHint />
      <Actions primary={typing ? i18nT('pages.chat.quickAsk.fill_in_page') : undefined} busy={busy} onDecline={() => { void send('decline') }} />
    </form>
  )
}

/**
 * A sign-in the gateway walks (credentials with no field bound): the person
 * types everything here, up front, and the gateway types each value into the
 * screen of `origin` that asks for it. Nothing here is saved anywhere. Taking
 * over stays for what the gateway cannot fill (a passkey, a puzzle).
 */
function SignInBody({ ask, busy, send }: { ask: QuickAsk; busy: boolean; send: Send }) {
  const mode = ask.steps?.username ?? 'optional'
  const userRef = useRef<HTMLInputElement>(null)
  const passRef = useRef<HTMLInputElement>(null)
  useEmptyOnUnmount(userRef, passRef)
  // Locked only when the page around the frame is https too: an http page can swap the frame.
  const https = [ask.origin, ask.top_origin ?? ask.origin].every(o => o.toLowerCase().startsWith('https:'))
  const SiteIcon = https ? Lock : Unlock
  const site = ask.top_origin ? i18nT('pages.chat.quickAsk.origin_inside', { origin: ask.origin, top: ask.top_origin }) : ask.origin
  const userLabel = mode === 'required' ? i18nT('pages.chat.quickAsk.label_email_or_phone') : i18nT('pages.chat.quickAsk.label_email_or_phone_optional')
  const vault = useVaultPart(ask)
  const [save, setSave] = useState(false)
  const matched = !!vault && vault.items.length > 0
  const [other, setOther] = useState(false)
  const typing = !matched || other
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!typing) return
    if (mode === 'required' && !userRef.current?.value.trim()) { userRef.current?.focus(); return }
    if (!passRef.current?.value) { passRef.current?.focus(); return }
    const username = mode === 'none' ? '' : take(userRef).trim()
    const password = take(passRef)
    void send('fill', { ...(username ? { username } : {}), password }, save ? { save: true } : undefined)
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-2" noValidate aria-busy={busy} data-testid="quick-ask-sign-in">
      <Field label={i18nT('pages.chat.quickAsk.label_website')}>
        {id => (
          <div className="relative min-w-0" data-testid="quick-ask-origin">
            <SiteIcon size={14} aria-hidden="true" className={`pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 ${https ? 'text-ok' : 'text-warn'}`} />
            <input id={id} readOnly tabIndex={-1} value={site} aria-label={i18nT('pages.chat.quickAsk.label_website')} className={`${inputCls.replace('bg-bg ', 'bg-bg-hover ')} pl-8 font-semibold text-text-strong`} data-testid="quick-ask-origin-text" />
          </div>
        )}
      </Field>
      {vault && <VaultLogins items={vault.items} more={vault.more} busy={busy} onPick={name => { void send('fill', undefined, { vault_item: name }) }}
        other={matched && <ExpandRow label={i18nT('pages.chat.quickAsk.other_account')} open={other} onToggle={() => setOther(o => !o)} busy={busy} testId="quick-ask-other-account" icon={OTHER_ACCOUNT_ICON} />} />}
      {typing && mode !== 'none' && (
        <Field label={userLabel}>
          {id => (
            <input id={id} ref={userRef} aria-label={userLabel} className={inputCls} maxLength={VALUE_MAX} inputMode="email" autoComplete="off" autoCapitalize="none" spellCheck={false}
              onFocus={keepInView} data-testid="quick-ask-username" />
          )}
        </Field>
      )}
      {typing && (
        <Field label={i18nT('pages.chat.quickAsk.label_password')}>
          {id => (
            <input id={id} ref={passRef} aria-label={i18nT('pages.chat.quickAsk.label_password')} type="password" className={inputCls} maxLength={VALUE_MAX} autoComplete="new-password"
              onFocus={keepInView} data-testid="quick-ask-password" />
          )}
        </Field>
      )}
      {vault && typing && <SaveToVaultBox on={save} setOn={setSave} />}
      {/* "Never saved" stays true only while the save box is not ticked. */}
      <SecretHint extra={save ? undefined : i18nT('pages.chat.quickAsk.sign_in_not_saved', { host: originHost(ask.origin) })} />
      <Actions primary={!typing ? undefined : busy ? i18nT('pages.chat.quickAsk.signing_in') : i18nT('pages.chat.quickAsk.sign_in')} busy={busy} onDecline={() => { void send('decline') }} />
      <p className="mt-1 text-center text-[12px] text-muted">
        {i18nT('pages.chat.quickAsk.take_over_instead_hint')}{' '}
        <button type="button" className="cursor-pointer border-0 bg-transparent p-0 font-medium text-accent underline-offset-2 hover:underline disabled:opacity-50" disabled={busy}
          onClick={() => { void send('take_over') }} data-testid="quick-ask-take-over">
          {i18nT('pages.chat.quickAsk.take_over_instead')}
        </button>
      </p>
    </form>
  )
}

/** The input type, keyboard and autofill hint a text field's label calls for. */
function textInputProps(f: QuickAskField | undefined): { type: string; inputMode: 'text' | 'email' | 'tel'; autoComplete: string; secret: boolean } {
  if (f?.input === 'password') return { type: 'password', inputMode: 'text', autoComplete: 'off', secret: true }
  const label = f?.label ?? 'other'
  if (f?.input === 'email' || label === 'email') return { type: 'email', inputMode: 'email', autoComplete: 'email', secret: false }
  if (f?.input === 'tel' || label === 'phone') return { type: 'tel', inputMode: 'tel', autoComplete: 'tel', secret: false }
  if (label === 'name') return { type: 'text', inputMode: 'text', autoComplete: 'name', secret: false }
  if (label === 'username') return { type: 'text', inputMode: 'text', autoComplete: 'username', secret: false }
  return { type: 'text', inputMode: 'text', autoComplete: 'off', secret: false }
}

function TextBody({ ask, busy, send }: { ask: QuickAsk; busy: boolean; send: Send }) {
  const field = ask.fields.find(f => f.role === 'text')
  const label = textLabel(ask)
  const props = textInputProps(field)
  const key: RememberKey | null = !props.secret && (label === 'name' || label === 'email' || label === 'username') ? label : null
  const ref = useRef<HTMLInputElement>(null)
  useEmptyOnUnmount(ref)
  const rem = useRemember(ask.origin, key)
  const [empty, setEmpty] = useState(true)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!ref.current?.value.trim()) { ref.current?.focus(); return }
    const text = take(ref)
    setEmpty(true)
    const ok = await send('fill', { text })
    if (ok) rem.commit(text)
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-2" noValidate>
      <Field label={labelWord(label)}>
        {id => (
          <div className="flex min-w-0 gap-1.5">
            <input id={id} ref={ref} aria-label={labelWord(label)} className={inputCls} maxLength={VALUE_MAX} type={props.type} inputMode={props.inputMode} autoComplete={props.autoComplete}
              spellCheck={false} onInput={e => setEmpty(!e.currentTarget.value)} onFocus={keepInView} data-testid="quick-ask-text" />
            {rem.saved && empty && <UseSavedChip value={rem.saved} target={ref} onUsed={() => setEmpty(false)} />}
          </div>
        )}
      </Field>
      {rem.enabled && <RememberBox host={originHost(ask.origin)} on={rem.on} setOn={rem.setOn} />}
      {props.secret && <SecretHint />}
      <Actions primary={i18nT('pages.chat.quickAsk.fill_in_page')} busy={busy} onDecline={() => { void send('decline') }} />
    </form>
  )
}

function OtpBody({ ask, busy, send }: { ask: QuickAsk; busy: boolean; send: Send }) {
  const n = otpDigits(ask)
  const ref = useRef<HTMLInputElement>(null)
  useEmptyOnUnmount(ref)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const digits = (ref.current?.value ?? '').replace(/\D/g, '')
    if (digits.length !== n) { ref.current?.focus(); return }
    take(ref)
    void send('fill', { otp: digits })
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-2" noValidate>
      <Field label={i18nT('pages.chat.quickAsk.label_otp')}>
        {id => (
          <input id={id} ref={ref} aria-label={i18nT('pages.chat.quickAsk.label_otp')} className={`${inputCls} text-center font-mono text-[22px] tracking-[0.4em]`}
            inputMode="numeric" autoComplete="one-time-code" pattern={`[0-9]{${n}}`}
            // Digits only, at most N: a pasted "123 456" becomes "123456".
            onInput={e => { const el = e.currentTarget; el.value = el.value.replace(/\D/g, '').slice(0, n) }}
            onFocus={keepInView} data-testid="quick-ask-otp" />
        )}
      </Field>
      {/* OTP relay: a code from another site typed here signs someone else in. */}
      <SecretHint extra={i18nT('pages.chat.quickAsk.otp_only_this_site', { origin: ask.origin })} />
      <Actions primary={i18nT('pages.chat.quickAsk.fill_code')} busy={busy} onDecline={() => { void send('decline') }} />
    </form>
  )
}

/** A human check, or a sign-in in a form Kiro cannot reach: the person does it on the page. */
function TakeOverBody({ explainer, primary, busy, send }: { explainer: string; primary: string; busy: boolean; send: Send }) {
  return (
    <div>
      <p className="text-[13px] leading-5 text-text">{explainer}</p>
      <Actions primary={primary} busy={busy} onPrimary={() => { void send('take_over') }} onDecline={() => { void send('decline') }} />
    </div>
  )
}

function ConfirmBody({ ask, busy, send }: { ask: QuickAsk; busy: boolean; send: Send }) {
  const summary = sanitizeLlmOutput(ask.summary ?? '').replace(/\s+/g, ' ').trim().slice(0, 300)
  const button = sanitizeLlmOutput(ask.button ?? '').replace(/\s+/g, ' ').trim().slice(0, 60)
  return (
    <div data-testid="quick-ask-confirm">
      {/* The summary is the agent's own words, labelled as such like its reason;
          the button text below is the one line read from the page. */}
      {summary && (
        <p className="rounded-lg border border-border bg-bg px-3 py-2 text-[14px] text-text break-words" data-testid="quick-ask-summary">
          {i18nT('pages.chat.quickAsk.reason_line', { text: summary })}
        </p>
      )}
      {button && (
        <p className="mt-2 text-[13px] text-text" data-testid="quick-ask-confirm-button">
          {i18nT('pages.chat.quickAsk.confirm_then', { button })}
        </p>
      )}
      <Actions
        primary={i18nT('pages.chat.quickAsk.confirm')}
        declineLabel={i18nT('pages.chat.quickAsk.cancel')}
        busy={busy}
        onPrimary={() => { void send('confirm') }}
        onDecline={() => { void send('cancel') }}
      />
    </div>
  )
}
