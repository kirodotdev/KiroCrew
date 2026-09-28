/**
 * The redaction UI inside a rendered reply: a neutral lock tag where a
 * credential was removed, a Blocked link chip where a suspicious URL was
 * removed, the one details card either opens, and the one-time coach.
 *
 * Built to the RFC prototype (docs/request-for-change/assets/
 * redaction-explain-reveal-prototype.html, variants C then A): routine
 * protection uses the info colour, never danger red; a card opens after the
 * block that holds its marker; every action answers back; Cancel is the
 * default in every confirmation.
 *
 * Every record arrives from a transcript line an attacker could have edited
 * at rest, so each is re-validated here before any field is rendered, and
 * every retained string renders as TEXT, never HTML.
 */
import React, { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { flushSync } from 'react-dom'
import { noteInPlaceResize } from '../hooks/virtualizer/inPlaceResize'
import { Check, ChevronRight, Copy, Link2Off, Lock, Shield, TriangleAlert, X } from 'lucide-react'
import { Trans } from 'react-i18next'
import type { Element as HastElement, Root as HastRoot, RootContent, Text as HastText } from 'hast'
import { copyToClipboard } from '../utils/clipboard'
import ErrorNotice from './ErrorNotice'
import { api } from '../api/client'
import { falsePositiveIssueUrl } from '../prompts/redactionReport'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'


/** Where the RFC explains redaction; every "About" link opens it. */
export const ABOUT_REDACTION_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/request-for-change/rfc-redaction-explain-and-reveal.md'

// ── records ──────────────────────────────────────────────────────────────

export type CredentialSource =
  | { type: 'file'; path: string; section: string | null }
  | { type: 'command'; command: string }

export interface CredentialRecord {
  ordinal: number
  rule: string
  label: string
  source: CredentialSource | null
  view_command: string | null
  profile_command: string | null
}

export interface BlockedLink {
  domain: string
  rule: string
  path: string | null
  query_chars: number
  url: string | null
  url_withheld: 'credential' | 'length' | null
}

const RULE_RE = /^[a-z0-9_]{1,48}$/
const LABEL_RE = /^[A-Za-z_]{1,32}["']?\s{0,4}[:=]\s{0,4}["']?$/
const COMMAND_MAX = 1000
const PATH_MAX = 512
const SECTION_RE = /^[A-Za-z0-9 _./:@-]{1,64}$/
const CRED_KEYS = ['ordinal', 'rule', 'label', 'source', 'view_command', 'profile_command']

/** Control, format and bidi characters (Unicode category C): a command or
 *  path carrying one can read differently on screen from what it runs. */
const INVISIBLE_RE = /\p{C}/u

function okCommand(v: unknown): v is string {
  return typeof v === 'string' && v.length > 0 && v.length <= COMMAND_MAX && !INVISIBLE_RE.test(v) && !v.includes('[REDACTED')
}

function okSource(v: unknown): v is CredentialSource | null {
  if (v === null) return true
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false
  const s = v as Record<string, unknown>
  if (s.type === 'command') return Object.keys(s).length === 2 && okCommand(s.command)
  if (s.type === 'file') {
    return Object.keys(s).length === 3
      && typeof s.path === 'string' && s.path.length > 0 && s.path.length <= PATH_MAX && !INVISIBLE_RE.test(s.path) && !s.path.includes('[REDACTED')
      && (s.section === null || (typeof s.section === 'string' && SECTION_RE.test(s.section)))
  }
  return false
}

/** Keep only well-formed credential records, dropping a bad one individually. */
export function normalizeCredentialRecords(raw: unknown): CredentialRecord[] {
  if (!Array.isArray(raw)) return []
  const out: CredentialRecord[] = []
  for (const item of raw.slice(0, 64)) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const r = item as Record<string, unknown>
    const keys = Object.keys(r)
    if (keys.length !== CRED_KEYS.length || !CRED_KEYS.every(k => k in r)) continue
    const { ordinal, rule, label, source, view_command: view, profile_command: profile } = r
    if (typeof ordinal !== 'number' || !Number.isInteger(ordinal) || ordinal < 0) continue
    if (typeof rule !== 'string' || !RULE_RE.test(rule)) continue
    if (typeof label !== 'string' || (label !== '' && !LABEL_RE.test(label))) continue
    if (!okSource(source)) continue
    if (view !== null && !okCommand(view)) continue
    if (profile !== null && !okCommand(profile)) continue
    out.push({
      ordinal,
      rule,
      label,
      source: source as CredentialSource | null,
      view_command: view as string | null,
      profile_command: profile as string | null,
    })
  }
  return out
}

/** Strict host and rule shapes, mirroring the backend's own gates. */
const BLOCKED_LINK_HOST_RE =
  /^(?:[a-z0-9._-]{1,253}\.[a-z]{2,63}|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:.]{1,45}\])$/i
const BLOCKED_LINK_KEYS = ['domain', 'rule', 'path', 'query_chars', 'url', 'url_withheld']
const BLOCKED_LINK_URL_MAX = 8192

/**
 * The address a record may open, or null. Checked on the way in AND again at
 * the click: an http(s) address whose host is exactly the record's domain and
 * that carries no userinfo, so the host the card names is the host that opens.
 */
export function openableBlockedLinkUrl(url: unknown, domain: string): string | null {
  if (typeof url !== 'string' || url.length > BLOCKED_LINK_URL_MAX) return null
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null
  if (parsed.username !== '' || parsed.password !== '') return null
  if (parsed.hostname.toLowerCase() !== domain.toLowerCase()) return null
  return url
}

/** Every control, format or bidi character as visible percent-escapes, so the
 *  text on screen cannot read differently from where it points. */
export function escapeInvisibleChars(text: string): string {
  const bytes = new TextEncoder()
  return text.replace(/\p{C}/gu, ch =>
    Array.from(bytes.encode(ch), b => `%${b.toString(16).toUpperCase().padStart(2, '0')}`).join(''),
  )
}

/** Keep only well-formed blocked-link records, dropping a bad one individually. */
export function normalizeBlockedLinks(raw: unknown): BlockedLink[] {
  if (!Array.isArray(raw)) return []
  const out: BlockedLink[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue
    const r = item as Record<string, unknown>
    const keys = Object.keys(r)
    if (keys.length !== BLOCKED_LINK_KEYS.length || !BLOCKED_LINK_KEYS.every(k => k in r)) continue
    const { domain, rule, path, query_chars: qc, url, url_withheld: withheld } = r
    if (typeof domain !== 'string' || !BLOCKED_LINK_HOST_RE.test(domain)) continue
    if (typeof rule !== 'string' || !RULE_RE.test(rule)) continue
    if (typeof qc !== 'number' || !Number.isInteger(qc) || qc < 0) continue
    if (path !== null && typeof path !== 'string') continue
    let keptUrl: string | null = null
    if (url !== null) {
      keptUrl = openableBlockedLinkUrl(url, domain)
      if (keptUrl === null || withheld !== null) continue
    } else if (withheld !== 'credential' && withheld !== 'length') {
      continue
    }
    out.push({
      domain,
      rule,
      path: path as string | null,
      query_chars: qc,
      url: keptUrl,
      url_withheld: keptUrl === null ? (withheld as 'credential' | 'length') : null,
    })
  }
  return out
}

/** The distinct records for one domain, in first-appearance order. */
export function blockedLinksForDomain(records: readonly BlockedLink[], domain: string): BlockedLink[] {
  const seen = new Set<string>()
  const out: BlockedLink[] = []
  for (const r of records) {
    if (r.domain !== domain) continue
    const sig = JSON.stringify([r.rule, r.path, r.query_chars, r.url, r.url_withheld])
    if (seen.has(sig)) continue
    seen.add(sig)
    out.push(r)
  }
  return out
}

// ── hosts allowed from an open card ─────────────────────────────────────

/**
 * A host allowed from a card that is still open. Allowing reloads the reply,
 * and the server then serves this host's links as plain links with their
 * records dropped, so the card would lose what it renders from. The hold
 * keeps those records, and the "Allowed · Undo" outcome, for this one reply
 * until its card closes.
 */
export interface AllowHold {
  workspace: string
  records: readonly BlockedLink[]
  outcome: 'allowed' | 'undone'
}

const EMPTY_HOLDS: ReadonlyMap<string, AllowHold> = new Map()
let allowHolds: ReadonlyMap<string, ReadonlyMap<string, AllowHold>> = new Map()
const holdListeners = new Set<() => void>()

function subscribeHolds(cb: () => void): () => void {
  holdListeners.add(cb)
  return () => { holdListeners.delete(cb) }
}

/** Set or clear the hold for one domain of one reply. */
export function setAllowHold(replyKey: string, domain: string, hold: AllowHold | null): void {
  const cur = allowHolds.get(replyKey) ?? EMPTY_HOLDS
  if (hold === null && !cur.has(domain)) return
  const next = new Map(cur)
  if (hold === null) next.delete(domain)
  else next.set(domain, hold)
  const all = new Map(allowHolds)
  if (next.size) all.set(replyKey, next)
  else all.delete(replyKey)
  allowHolds = all
  holdListeners.forEach(l => l())
}

/** The holds of one reply, by domain. */
export function useAllowHolds(replyKey: string | undefined): ReadonlyMap<string, AllowHold> {
  return useSyncExternalStore(subscribeHolds, () => (replyKey && allowHolds.get(replyKey)) || EMPTY_HOLDS)
}

/** The held domain an anchor points at (matched with and without its port),
 *  or null. */
export function heldAnchorDomain(href: unknown, held: ReadonlySet<string>): string | null {
  if (typeof href !== 'string' || held.size === 0) return null
  try {
    const u = new URL(href)
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    for (const d of [u.host.toLowerCase(), u.hostname.toLowerCase()]) if (held.has(d)) return d
    return null
  } catch {
    return null
  }
}

// ── shared UI state ──────────────────────────────────────────────────────

interface RedactionUi {
  credentials: ReadonlyMap<number, CredentialRecord>
  firstCredential: number
  blockedLinks: readonly BlockedLink[]
  slotKey?: string
  /** Names this reply for the allow holds (slot and message time). */
  replyKey?: string
  /** This is the session's first reply with removed values: it carries the
   *  coach after the first block it explains. */
  coached: boolean
  openId: string | null
  toggle: (id: string, trigger: HTMLElement) => void
  close: () => void
}

const RedactionUiCtx = createContext<RedactionUi>({
  credentials: new Map(),
  firstCredential: -1,
  coached: false,
  blockedLinks: [],
  openId: null,
  toggle: () => {},
  close: () => {},
})

/** One per rendered reply: holds its records and which card is open. */
export function RedactionProvider({ credentials, blockedLinks, slotKey, replyKey, coached = false, children }: {
  credentials: readonly CredentialRecord[]
  blockedLinks: readonly BlockedLink[]
  slotKey?: string
  replyKey?: string
  coached?: boolean
  children: React.ReactNode
}) {
  // A reply that remounts while a card of an allowed host is held (the reload
  // after Allow can rebuild the row) reopens that card, so Undo stays up.
  const [openId, setOpenId] = useState<string | null>(() => {
    const held = replyKey ? allowHolds.get(replyKey) : undefined
    const domain = held ? held.keys().next().value : undefined
    return domain ? `rx-link-${domain}` : null
  })
  const triggerRef = useRef<HTMLElement | null>(null)
  const close = useCallback(() => {
    setOpenId(null)
    const el = triggerRef.current
    triggerRef.current = null
    el?.focus()
  }, [])
  const toggle = useCallback((id: string, trigger: HTMLElement) => {
    setOpenId(cur => {
      if (cur === id) {
        triggerRef.current = null
        return null
      }
      triggerRef.current = trigger
      return id
    })
  }, [])
  // A hold lasts while its card is open: closing it, or opening another card
  // of this reply, lets the reply render from its own records again.
  const holds = useAllowHolds(replyKey)
  useEffect(() => {
    if (!replyKey) return
    for (const domain of holds.keys()) if (openId !== `rx-link-${domain}`) setAllowHold(replyKey, domain, null)
  }, [openId, replyKey, holds])
  useEffect(() => {
    if (openId === null) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [openId, close])
  const value = useMemo(() => {
    const map = new Map(credentials.map(r => [r.ordinal, r]))
    const first = credentials.length ? Math.min(...credentials.map(r => r.ordinal)) : -1
    return { credentials: map, firstCredential: first, blockedLinks, slotKey, replyKey, coached, openId, toggle, close }
  }, [credentials, blockedLinks, slotKey, replyKey, coached, openId, toggle, close])
  return <RedactionUiCtx.Provider value={value}>{children}</RedactionUiCtx.Provider>
}

export function useRedactionUi(): RedactionUi {
  return useContext(RedactionUiCtx)
}

// ── shared primitives (the prototype's .btn / .pop / .act / .disc / .foot) ──

// Touch targets grow to 44px on hover-less (touch) screens, as the prototype asks.
const touchTarget = '[@media(hover:none)]:min-h-11'
const btnBase = 'inline-flex items-center justify-center whitespace-nowrap rounded-md border bg-card px-2.5 py-[3px] text-[12px] leading-[18px] min-h-7 text-card-fg cursor-pointer hover:border-border-strong hover:bg-bg-hover disabled:cursor-default disabled:opacity-70'
const btnCls = [btnBase, 'border-border', touchTarget].join(' ')
const btnPrimaryCls = [btnBase, 'border-accent bg-accent-subtle', touchTarget].join(' ')
const btnWarnCls = [btnBase, 'border-warn', touchTarget].join(' ')
// inline-flex so the label centres inside the 44px touch box instead of
// sitting at its top; an <a> otherwise lays its text out from the top edge.
const linkBtnCls = ['inline-flex items-center justify-start border-0 bg-transparent px-0.5 py-1 min-h-6 text-left text-accent cursor-pointer', touchTarget].join(' ')
const pillCls = 'inline-block whitespace-nowrap rounded-full border border-border px-[7px] text-[11px] leading-4 text-muted'
const CODE = 'rounded border border-border bg-bg-elevated px-1 font-mono text-[12px]'

/** A button whose label swaps to a confirmation for a moment ("Copied"). */
function FeedbackButton({ className, label, done, onClick, testId }: {
  className: string
  label: string
  done: string
  onClick: () => Promise<boolean> | boolean
  testId?: string
}) {
  const [busy, setBusy] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  return (
    <button
      type="button"
      className={className}
      disabled={busy}
      data-testid={testId}
      onClick={async () => {
        if (busy) return
        const ok = await onClick()
        if (!ok) return
        setBusy(true)
        timer.current = setTimeout(() => setBusy(false), 1600)
      }}
    >
      <span aria-live="polite">{busy ? done : label}</span>
    </button>
  )
}

/** A persistent status line with its own action (the prototype's .fbline). */
function StatusLine({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return (
    <div role="status" data-testid={testId} className="mt-1.5 flex flex-wrap items-center gap-2 rounded-lg border border-ok bg-ok-subtle px-2 py-1.5 text-[12px] leading-[18px] text-card-fg">
      {children}
    </div>
  )
}

function ErrorLine({ message, testId }: { message: string; testId?: string }) {
  return (
    <div className="mt-1.5">
      <ErrorNotice variant="inline" askAgent message={message} testId={testId} />
    </div>
  )
}

/** How long a reveal takes; the exit keeps its content mounted this long. */
export const REVEAL_MS = 180
/** Control points of the reveal's curves: a decelerating open and a gentle
 *  ease-in-out close. */
const EASE_OPEN = [0.2, 0, 0, 1] as const
const EASE_CLOSE = [0.4, 0, 0.2, 1] as const
const bezier = (p: readonly number[]) => `cubic-bezier(${p.join(', ')})`

/**
 * Opens and closes its content so the card pushes the text below it down as
 * it appears, and draws that text back up as it closes.
 *
 * The layout changes ONCE, at the start of an open or the end of a close; the
 * movement in between is a transform on the card and on everything after it
 * up to the scroll container (FLIP). Transform and opacity animations run on
 * the compositor, so they keep a steady frame rate while the main thread is
 * busy with the commit that inserted the card. A height transition moves on
 * the main thread instead, and a phone recording showed it skipping straight
 * from closed to nearly open. The card slides out from under the line above
 * inside a clipping box, and the text below moves by the same amount, so the
 * two edges stay together on every frame. A reveal mounted already open
 * animates in unless `appear` is false (the coach, which is part of the reply
 * rather than something the reader opened). Reduced-motion users get the
 * instant switch.
 */
export function Reveal(props: RevealProps) {
  return <PushReveal {...props} />
}

interface RevealProps {
  show: boolean
  children: React.ReactNode
  className?: string
  appear?: boolean
  /** Names what the open reveal is showing. A change while open swaps the
   *  content in place and moves the text below by the height difference,
   *  instead of closing one panel while another opens. */
  contentKey?: string
}

function PushReveal({ show, children, className = '', appear = true, contentKey }: RevealProps) {
  const [mounted, setMounted] = useState(show)
  const [clip, setClip] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const running = useRef<Animation[]>([])
  const firstRender = useRef(true)
  // The box's height as last laid out. A resize observer keeps it, so when a
  // content change commits, this still holds the height from before it.
  const laidOutH = useRef(0)
  const lastKey = useRef(contentKey)
  if (show && !mounted) setMounted(true)
  useEffect(() => {
    const box = boxRef.current
    if (!box || typeof ResizeObserver === 'undefined') return
    laidOutH.current = box.offsetHeight
    const ro = new ResizeObserver(() => { laidOutH.current = box.offsetHeight })
    ro.observe(box)
    return () => ro.disconnect()
  }, [mounted])
  useLayoutEffect(() => {
    const prevKey = lastKey.current
    lastKey.current = contentKey
    const box = boxRef.current
    const inner = box?.firstElementChild as HTMLElement | null
    if (prevKey === contentKey || !show || !box || !inner || running.current.length || !canAnimate(box)) return
    const d = box.offsetHeight - laidOutH.current
    const timing: KeyframeAnimationOptions = { duration: REVEAL_MS, easing: bezier(EASE_OPEN), fill: 'forwards' }
    noteInPlaceResize(box.parentElement ?? box, box.getBoundingClientRect().top)
    const shift = `translateY(${-d}px)`
    const moving = [
      inner.animate([{ opacity: 0 }, { opacity: 1 }], timing),
      ...(d === 0 ? [] : followingContent(box, Math.abs(d)).map(el => el.animate([{ transform: shift }, { transform: 'none' }], { ...timing, composite: 'add' }))),
      // The frame to resize is the card this area sits in, or, when the area
      // IS a card (one card replacing another), that card's own frame.
      ...(d === 0 ? [] : frameAnimations(box, d, true, { ...timing, composite: 'add' },
        box.parentElement?.closest<HTMLElement>('[data-card-frame-host]')
          ?? inner.querySelector<HTMLElement>(':scope > [data-card-frame-host]'))),
    ]
    running.current = moving
    moving[0].onfinish = () => {
      if (running.current !== moving) return
      running.current = []
      settle(moving)
    }
  }, [contentKey, show])
  useLayoutEffect(() => {
    const initial = firstRender.current
    firstRender.current = false
    const box = boxRef.current
    const inner = box?.firstElementChild as HTMLElement | null
    settle(running.current)
    running.current = []
    if (!box || !inner) return
    // A box that starts above the top of the viewport changes nothing the
    // reader is looking at: the virtualizer holds their view still across it,
    // so it swaps without motion instead of dragging the visible text with it.
    const boxTop = box.getBoundingClientRect().top
    const above = boxTop < viewportTop(box)
    if ((initial && !appear) || !canAnimate(box) || above) {
      // Tell the virtualizer how much of this row changed above the fold, so
      // a change on screen in the same row at the same moment does not hide
      // it (see inPlaceResize).
      if (above && !initial) {
        const travel = measuredTravel(box, followingContent(box, 0)[0]) ?? box.getBoundingClientRect().height
        noteInPlaceResize(box.parentElement ?? box, boxTop, 0, show ? travel : -travel)
      }
      if (!show) setMounted(false)
      return
    }
    const h = box.getBoundingClientRect().height
    // The box's own height change is on screen, so the virtualizer must not
    // treat it as a reprice above the reader (see inPlaceResize).
    const anchor = box.parentElement ?? box
    if (show) noteInPlaceResize(anchor, box.getBoundingClientRect().top)
    const shift = `translateY(${-h}px)`
    const from = show ? shift : 'none'
    const to = show ? 'none' : shift
    const timing: KeyframeAnimationOptions = {
      duration: REVEAL_MS,
      // Open decelerates into place; close eases in so it does not jump at its first frame.
      easing: bezier(show ? EASE_OPEN : EASE_CLOSE),
      fill: 'forwards',
    }
    setClip(true)
    // Opening fades the content in as it slides out. Closing a whole card
    // keeps it opaque, so it visibly retracts under the line above as the
    // text below draws up over its place; closing a section inside a card
    // fades its text out as it retracts, which reads more gently in the small
    // space.
    const inCard = !!box.parentElement?.closest('[data-card-frame-host]')
    const card = inner.animate(
      show
        ? [{ transform: from, opacity: 0 }, { transform: to, opacity: 1 }]
        : inCard
          ? [{ transform: from, opacity: 1 }, { transform: to, opacity: 0 }]
          : [{ transform: from }, { transform: to }],
      timing,
    )
    const followers = followingContent(box, h)
    const travel = measuredTravel(box, followers[0]) ?? h
    const follow = show ? [`translateY(${-travel}px)`, 'none'] : ['none', `translateY(${-travel}px)`]
    const moving = [
      card,
      // `add`: when one card closes while another opens further down, the
      // text below both moves by the sum of the two, not by whichever
      // animation happened to start last.
      ...followers.map(el => el.animate([{ transform: follow[0] }, { transform: follow[1] }], { ...timing, composite: 'add' })),
      ...frameAnimations(box, travel, show, { ...timing, composite: 'add' }),
    ]
    running.current = moving
    card.onfinish = () => {
      if (running.current !== moving) return
      running.current = []
      if (show) {
        setClip(false)
        settle(moving)
        return
      }
      // Remove the box and drop the transforms in one commit, so the text
      // below lands where the transforms were holding it.
      noteInPlaceResize(anchor, box.getBoundingClientRect().top)
      flushSync(() => setMounted(false))
      settle(moving)
    }
  }, [show, mounted, appear])
  useEffect(() => () => settle(running.current), [])
  if (!mounted) return null
  return (
    // flow-root keeps the content's margins inside the box whether or not it
    // clips, so the height the transforms travel is the height it occupies.
    <div ref={boxRef} data-reveal={show ? 'open' : 'closed'} className={`flow-root ${clip ? 'overflow-hidden' : ''} ${className}`}>
      <div>{children}</div>
    </div>
  )
}

/**
 * Ends each animation and removes its effect. Native animations with
 * `fill: 'forwards'` hold their last frame until cancelled and never write
 * the element's inline style, so cancelling hands every element back to the
 * page exactly as it was, with no inline style left to race a cleanup
 * (Framer's `animate()` writes its final frame into the inline style).
 */
function settle(moving: Animation[]): void {
  for (const anim of moving) anim.cancel()
}

/**
 * A section opening inside a details card also has to grow the card's frame
 * with it. The frame is drawn by three layers behind the content (see
 * CardFrame) so it can follow with transforms alone: the middle layer scales
 * from its top edge, the bottom cap slides with the text below, and the
 * shadow layer scales with the whole card. The layout is already at its
 * opened height when this runs, so an open starts the frame `h` shorter and a
 * close ends it `h` shorter, matching the content.
 */
function frameAnimations(
  box: HTMLElement,
  h: number,
  show: boolean,
  timing: KeyframeAnimationOptions,
  host?: HTMLElement | null,
): Animation[] {
  const card = host ?? box.parentElement?.closest<HTMLElement>('[data-card-frame-host]')
  if (!card) return []
  const mid = card.querySelector<HTMLElement>(':scope > [data-card-frame] > [data-frame="mid"]')
  const cap = card.querySelector<HTMLElement>(':scope > [data-card-frame] > [data-frame="bottom"]')
  const shadow = card.querySelector<HTMLElement>(':scope > [data-card-frame] > [data-frame="shadow"]')
  if (!mid || !cap || !shadow) return []
  const midH = mid.offsetHeight
  const cardH = shadow.offsetHeight
  if (midH <= 0 || cardH <= 0) return []
  const shortMid = `scaleY(${Math.max(0, (midH - h) / midH)})`
  const shortCard = `scaleY(${Math.max(0, (cardH - h) / cardH)})`
  const up = `translateY(${-h}px)`
  const pair = (el: HTMLElement, short: string) =>
    el.animate(show ? [{ transform: short }, { transform: 'none' }] : [{ transform: 'none' }, { transform: short }], timing)
  return [pair(mid, shortMid), pair(shadow, shortCard), pair(cap, up)]
}

/**
 * How far the first block below moves when `box` appears or goes: its top
 * with the box laid out, minus its top with the box taken out of the layout
 * for one synchronous measurement. This is the box's height plus whatever
 * margin its neighbours stop sharing, however deeply those margins collapse,
 * which no reading of the box alone can know.
 */
function measuredTravel(box: HTMLElement, first: HTMLElement | undefined): number | null {
  if (!first) return null
  const withBox = first.getBoundingClientRect().top
  const display = box.style.display
  box.style.display = 'none'
  const without = first.getBoundingClientRect().top
  box.style.display = display
  return withBox - without
}

/** The viewport-y of the top of `el`'s scroll container (0 for the page). */
function viewportTop(el: HTMLElement): number {
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (scrollsVertically(p)) return p.getBoundingClientRect().top
  }
  return 0
}

/**
 * Whether `el` is a vertical scroll container. A computed `overflow-y: auto`
 * alone is not enough: `overflow-x: auto` on a code block computes the other
 * axis to `auto` too, and the body's overflow belongs to the viewport.
 */
function scrollsVertically(el: HTMLElement): boolean {
  if (el === document.body || el === document.documentElement) return false
  const overflowY = getComputedStyle(el).overflowY
  return (overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight
}

function canAnimate(el: HTMLElement): boolean {
  return typeof el.animate === 'function' && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/** Upper bound on the elements a reveal moves; past it the rest is off screen. */
const MAX_FOLLOWERS = 60

/**
 * Everything laid out after `box` inside its scroll container: its later
 * siblings, then its parent's later siblings, and so on up to the scroller.
 * Those are the boxes the reveal's height change moves, so they are the ones
 * that must ride along with it. Content that stays below the viewport throughout is left
 * alone.
 */
function followingContent(box: HTMLElement, travel: number): HTMLElement[] {
  const out: HTMLElement[] = []
  // Tops are measured with the card in place, so anything within `travel` of
  // the viewport's bottom edge is on screen at some point of the movement.
  const limit = window.innerHeight + travel
  // A `display: contents` wrapper draws no box and ignores a transform, so its
  // children are the boxes that move.
  const take = (s: Element): boolean => {
    if (out.length >= MAX_FOLLOWERS || !(s instanceof HTMLElement)) return true
    const display = getComputedStyle(s).display
    if (display === 'none') return true
    if (display === 'contents') {
      for (const c of Array.from(s.children)) if (!take(c)) return false
      return true
    }
    if (s.getBoundingClientRect().top > limit) return false
    out.push(s)
    return true
  }
  let el: HTMLElement = box
  while (el.parentElement && out.length < MAX_FOLLOWERS) {
    for (let s = el.nextElementSibling; s; s = s.nextElementSibling) if (!take(s)) break
    const parent = el.parentElement
    if (scrollsVertically(parent)) break
    el = parent
  }
  return out
}

/** The last non-null ``value``, so a reveal can keep showing what it is closing. */
function useLast<T>(value: T | null): T | null {
  const ref = useRef<T | null>(value)
  if (value !== null) ref.current = value
  return ref.current
}

function Disclosure({ summary, children, testId }: { summary: string; children: React.ReactNode; testId?: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-2 border-t border-border pt-0.5" data-testid={testId} data-open={open ? '' : undefined}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        className="flex min-h-7 w-full cursor-pointer select-none items-center gap-1.5 border-0 bg-transparent p-0 text-left text-[12px] leading-[18px] text-muted hover:text-text [@media(hover:none)]:min-h-11"
      >
        <ChevronRight aria-hidden size={12} className={`shrink-0 transition-transform duration-150 motion-reduce:transition-none ${open ? 'rotate-90' : ''}`} />
        {summary}
      </button>
      <Reveal show={open}>
        <div className="pb-1 pt-0.5 text-[13px] leading-5 text-card-fg">{children}</div>
      </Reveal>
    </div>
  )
}

function CardShell({ id, title, children, testId }: {
  id: string
  title: string
  children: React.ReactNode
  testId: string
}) {
  const { close } = useRedactionUi()
  const ref = useRef<HTMLDivElement>(null)
  // Focus once the reveal has finished: focusing forces a synchronous style
  // and layout pass over the whole transcript, and doing it on mount turns
  // the click into a long task that holds back the animation's first frame.
  useEffect(() => {
    const t = setTimeout(() => ref.current?.focus({ preventScroll: true }), REVEAL_MS)
    return () => clearTimeout(t)
  }, [])
  const titleId = `${id}-h`
  return (
    <div
      ref={ref}
      id={id}
      role="region"
      aria-labelledby={titleId}
      tabIndex={-1}
      data-testid={testId}
      data-card-frame-host=""
      className="relative isolate my-2 max-w-[560px] rounded-xl p-3 text-[13px] leading-5 text-card-fg outline-none"
    >
      <CardFrame />
      <button
        type="button"
        onClick={close}
        aria-label={i18nT('components.redaction.close')}
        className="absolute right-1.5 top-1.5 flex h-7 w-7 cursor-pointer items-center justify-center rounded-md border-0 bg-transparent text-base leading-none text-muted hover:bg-bg-hover hover:text-text [@media(hover:none)]:h-11 [@media(hover:none)]:w-11"
      >
        <X aria-hidden size={14} />
      </button>
      <h4 id={titleId} className="m-0 mb-0.5 mr-7 text-[13px] font-semibold">{title}</h4>
      {children}
    </div>
  )
}

/**
 * The card's border, background and shadow, drawn behind its content as
 * three pieces instead of on the card box itself: a top cap, a middle that
 * can scale vertically without distorting anything (it has no rounded
 * corners and its borders are vertical lines), and a bottom cap. A section
 * that opens inside the card then moves the frame with transforms alone (see
 * frameAnimations), on the compositor like the rest of the reveal.
 */
function CardFrame() {
  const piece = 'pointer-events-none absolute inset-x-0 border-border bg-bg-elevated'
  return (
    <div data-card-frame="" aria-hidden="true" className="pointer-events-none absolute inset-0 -z-10">
      <div data-frame="shadow" className="absolute inset-0 origin-top rounded-xl shadow-lg" />
      <div data-frame="top" className={`${piece} top-0 h-3 rounded-t-xl border border-b-0`} />
      <div data-frame="mid" className={`${piece} bottom-3 top-3 origin-top border-x`} />
      <div data-frame="bottom" className={`${piece} bottom-0 h-3 rounded-b-xl border border-t-0`} />
    </div>
  )
}

/** The prototype's .act row: what an action does, and its button. */
function ActionRow({ title, note, command, warning, children }: {
  title: React.ReactNode
  note?: string
  command?: string | null
  /** A red line under the command, for a command the reader must check. */
  warning?: string
  children: React.ReactNode
}) {
  return (
    <div className="mt-1.5 flex items-center justify-between gap-2.5 rounded-lg border border-border bg-card px-2 py-1.5 text-[12px] leading-[18px] text-card-fg max-[640px]:flex-col max-[640px]:items-stretch">
      <div className="min-w-0">
        <div>
          {title}
          {note && <span className="text-[11px] text-muted"> {note}</span>}
        </div>
        {command && <code className="inline-block break-words rounded bg-bg-elevated px-[.4em] py-[.15em] font-mono text-[12px] text-text">{escapeInvisibleChars(command)}</code>}
        {warning && (
          <div data-testid="redaction-command-warning" className="mt-1 flex items-start gap-1 text-[11px] font-medium leading-4 text-danger">
            <TriangleAlert size={12} aria-hidden="true" className="mt-0.5 shrink-0" />
            <span>{warning}</span>
          </div>
        )}
      </div>
      {children}
    </div>
  )
}

function Foot({ children }: { children: React.ReactNode }) {
  return <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] leading-4 text-muted">{children}</div>
}

function ReportLink({ rule, kind, label }: { rule: string; kind: 'credential' | 'link'; label: string }) {
  return (
    <a
      href={falsePositiveIssueUrl(rule, kind)}
      target="_blank"
      rel="noopener noreferrer"
      data-testid="redaction-report"
      className={`${linkBtnCls} text-[11px] no-underline`}
    >
      {label}
    </a>
  )
}

function AboutLink({ label }: { label: string }) {
  return (
    <a href={ABOUT_REDACTION_URL} target="_blank" rel="noopener noreferrer" className={`${linkBtnCls} text-[11px] no-underline`}>
      {label}
    </a>
  )
}

/** Hand a command to the built-in Terminal WITHOUT running it. Resolves with
 *  whether the Terminal took it. */
export function prefillTerminal(command: string): Promise<boolean> {
  return new Promise(resolve => {
    const reqId = `rx-${Math.random().toString(36).slice(2)}`
    let done = false
    const finish = (ok: boolean) => {
      if (done) return
      done = true
      window.removeEventListener('mc:prefill-terminal-result', onResult)
      resolve(ok)
    }
    const onResult = (e: Event) => {
      const d = (e as CustomEvent).detail || {}
      if (d.reqId === reqId) finish(!!d.ok)
    }
    window.addEventListener('mc:prefill-terminal-result', onResult)
    window.dispatchEvent(new CustomEvent('mc:prefill-terminal', { detail: { command, reqId } }))
    setTimeout(() => finish(false), 20_000)
  })
}

/** Put text in the chat composer for the reader to review; never sends it. */
function prefillComposer(text: string): boolean {
  window.dispatchEvent(new CustomEvent('mc:prefill-composer', { detail: { text } }))
  return true
}

/** An action row whose button adds `command` to the Terminal, with the
 *  outcome line right below the row, as the prototype places it. */
function TerminalAction({ title, note, command, label, primary = true, agentWritten = false }: {
  title: React.ReactNode
  note?: string
  command: string
  label?: string
  primary?: boolean
  /** The command is the one the agent ran, not a fixed template of ours. */
  agentWritten?: boolean
}) {
  const [state, setState] = useState<'idle' | 'added' | 'failed'>('idle')
  const shownState = useLast(state === 'idle' ? null : state)
  return (
    <>
      <ActionRow title={title} note={note} command={command} warning={agentWritten ? i18nT('components.redaction.term_check') : undefined}>
        <button
          type="button"
          className={primary ? btnPrimaryCls : btnCls}
          data-testid="redaction-open-terminal"
          onClick={async () => setState((await prefillTerminal(command)) ? 'added' : 'failed')}
        >
          {label ?? i18nT('components.redaction.open_in_terminal')}
        </button>
      </ActionRow>
      <Reveal show={state !== 'idle'} contentKey={shownState ?? undefined}>
        {shownState === 'added' && (<><StatusLine testId="redaction-terminal-added">
          <span>{i18nT('components.redaction.term_added')}</span>
          <button type="button" className={linkBtnCls} onClick={() => setState('idle')}>{i18nT('components.redaction.clear')}</button>
        </StatusLine></>)}
        {shownState === 'failed' && <ErrorLine message={i18nT('components.redaction.term_failed')} />}
      </Reveal>
    </>
  )
}

// ── credential ───────────────────────────────────────────────────────────

const SESSION_TOKEN_RULES = new Set(['aws_session_token'])
/** The "Matched:" sentence per credential rule the redactor names. */
const MATCHED_KEY: Record<string, string> = {
  bare_aws_secret: 'components.redaction.matched_bare_aws_secret',
  aws_secret_access_key: 'components.redaction.matched_aws_secret_access_key',
  aws_session_token: 'components.redaction.matched_aws_session_token',
  aws_access_key_id: 'components.redaction.matched_aws_access_key_id',
  encoded_credential: 'components.redaction.matched_encoded_credential',
  token_parameter: 'components.redaction.matched_token_parameter',
  url_userinfo: 'components.redaction.matched_url_userinfo',
  private_key: 'components.redaction.matched_private_key',
  jwt: 'components.redaction.matched_jwt',
}

function credentialMatched(rule: string): string {
  return i18nT(Object.hasOwn(MATCHED_KEY, rule) ? MATCHED_KEY[rule] : 'components.redaction.matched_generic')
}

/** The neutral lock tag standing where a credential was removed. */
export function CredentialTag({ ordinal, placeholder }: { ordinal: number; placeholder: string }) {
  useLanguageGeneration()
  const { credentials, firstCredential, openId, toggle } = useRedactionUi()
  const record = credentials.get(ordinal)
  if (!record) return <>{placeholder}</>
  const id = `rx-cred-${ordinal}`
  return (
    <>
      {record.label}
      <button
        type="button"
        data-testid="credential-tag"
        aria-expanded={openId === id}
        aria-controls={id}
        aria-label={i18nT('components.redaction.cred_tag_aria')}
        onClick={e => toggle(id, e.currentTarget)}
        className="relative inline-flex min-h-[18px] cursor-pointer items-center gap-1 whitespace-nowrap rounded-[5px] border border-info bg-info-subtle px-1.5 align-[-1px] font-sans text-[11px] leading-4 text-card-fg hover:bg-bg-hover max-[640px]:whitespace-normal [@media(hover:none)]:after:absolute [@media(hover:none)]:after:-inset-[13px] [@media(hover:none)]:after:content-['']"
      >
        <Lock size={12} aria-hidden="true" className="shrink-0" />
        {i18nT(SESSION_TOKEN_RULES.has(record.rule) ? 'components.redaction.cred_tag_session' : 'components.redaction.cred_tag')}
        {ordinal === firstCredential && <span className="text-muted">· {i18nT('components.redaction.cred_why')}</span>}
      </button>
    </>
  )
}

function sourceLine(source: CredentialSource | null) {
  const code = (text: string) => <code className={CODE}>{escapeInvisibleChars(text)}</code>
  if (source === null) return i18nT('components.redaction.cred_source_none')
  if (source.type === 'command') {
    return <Trans i18nKey="components.redaction.cred_source_command" components={{ command: code(source.command) }} />
  }
  if (source.section) {
    return (
      <Trans
        i18nKey="components.redaction.cred_source_file_section"
        components={{ path: code(source.path), section: code(`[${source.section}]`) }}
      />
    )
  }
  return <Trans i18nKey="components.redaction.cred_source_file" components={{ path: code(source.path) }} />
}

function CredentialCard({ record }: { record: CredentialRecord }) {
  useLanguageGeneration()
  const session = SESSION_TOKEN_RULES.has(record.rule)
  const id = `rx-cred-${record.ordinal}`
  const src = record.source
  const primary = session ? record.profile_command ?? record.view_command : record.view_command
  const section = src?.type === 'file' && src.section ? `[${src.section}]` : null
  const [copyFailed, setCopyFailed] = useState(false)
  return (
    <CardShell id={id} title={i18nT(session ? 'components.redaction.cred_title_session' : 'components.redaction.cred_title')} testId="credential-card">
      <div className="mb-2 mt-1 text-[13px] leading-5 text-card-fg">
        <Trans i18nKey={session ? 'components.redaction.cred_body_session' : 'components.redaction.cred_body'} components={[<b key="0" />, <b key="1" />]} />
        <br />
        {sourceLine(src)}
      </div>
      {primary && (
        <TerminalAction
          title={session ? i18nT('components.redaction.cred_profile_title') : i18nT('components.redaction.cred_view_title')}
          note={session ? undefined : i18nT('components.redaction.cred_view_note')}
          command={primary}
          agentWritten={src?.type === 'command'}
        />
      )}
      <Disclosure summary={i18nT('components.redaction.technical_details')} testId="credential-technical">
        {credentialMatched(record.rule)} <span className={pillCls}>{i18nT('components.redaction.rule_pill', { rule: record.rule })}</span>
        <br />
        {i18nT(session ? 'components.redaction.cred_scope_session' : 'components.redaction.cred_scope')}
      </Disclosure>
      <Disclosure summary={i18nT('components.redaction.more_ways')} testId="credential-more">        {session && record.view_command && (
          <TerminalAction title={i18nT('components.redaction.cred_view_token_title')} note={i18nT('components.redaction.cred_view_note')} command={record.view_command} primary={false} agentWritten={src?.type === 'command'} />
        )}
        {src?.type === 'file' && (
          <ActionRow title={i18nT('components.redaction.copy_path_title')} command={src.path}>
            <FeedbackButton
              className={btnCls}
              label={i18nT('components.redaction.copy_path')}
              done={i18nT('components.redaction.copied')}
              testId="credential-copy-path"
              onClick={async () => {
                const ok = await copyToClipboard(src.path)
                setCopyFailed(!ok)
                return ok
              }}
            />
          </ActionRow>
        )}
        {src?.type === 'file' && (
          <Reveal show={copyFailed}>
            <ErrorLine testId="credential-copy-path-error" message={i18nT('components.redaction.copy_failed')} />
          </Reveal>
        )}
        {!session && (
          <ActionRow title={i18nT('components.redaction.prefill_title')} note={i18nT('components.redaction.prefill_note')} command={prefillText(section)}>
            <FeedbackButton
              className={btnCls}
              label={i18nT('components.redaction.prefill')}
              done={i18nT('components.redaction.prefill_done')}
              testId="credential-prefill"
              onClick={() => prefillComposer(prefillText(section))}
            />
          </ActionRow>
        )}
      </Disclosure>
      <Foot>
        <ReportLink rule={record.rule} kind="credential" label={i18nT('components.redaction.cred_report')} />
        <AboutLink label={i18nT('components.redaction.about_redaction')} />
      </Foot>
    </CardShell>
  )
}

function prefillText(section: string | null): string {
  return section ? i18nT('components.redaction.prefill_text_section', { section }) : i18nT('components.redaction.prefill_text')
}

// ── coach ────────────────────────────────────────────────────────────────

const COACH_KEY = 'kc.redaction.coach.dismissed.'
/** Set by *Don't show again*: no session shows the coach after it. */
const COACH_NEVER_KEY = 'kc.redaction.coach.never'

function coachDismissed(slotKey: string | undefined): boolean {
  try {
    if (window.localStorage.getItem(COACH_NEVER_KEY) === '1') return true
    return !!slotKey && window.localStorage.getItem(COACH_KEY + slotKey) === '1'
  } catch {
    return false
  }
}

function remember(key: string): void {
  try {
    window.localStorage.setItem(key, '1')
  } catch {
    /* storage refused: the coach still closes for this view */
  }
}

/**
 * The one-time coach, after the first reply in a session with removed values.
 * *Got it* closes it for this session; *Don't show again* closes it in every
 * session. Either way only the lock tags remain.
 */
export function RedactionCoach({ count, slotKey }: { count: number; slotKey?: string }) {
  useLanguageGeneration()
  const [hidden] = useState(() => coachDismissed(slotKey))
  const [closed, setClosed] = useState(false)
  if (hidden || count <= 0) return null
  const lock = <Lock size={12} aria-hidden="true" className="inline align-[-2px]" />
  return (
    <Reveal show={!closed} appear={false}>
    <div
      role="region"
      aria-label={i18nT('components.redaction.coach_region')}
      data-testid="redaction-coach"
      className="my-2 grid max-w-[640px] grid-cols-[auto_1fr] gap-2.5 rounded-xl border border-info bg-bg-elevated p-3 text-[13px] leading-5 text-card-fg max-[640px]:grid-cols-1"
    >
      <div className="max-[640px]:hidden"><Shield size={22} aria-hidden="true" className="text-info" /></div>
      <div>
        <div className="font-semibold">{i18nT('components.redaction.coach_title', { count })}</div>
        <ol className="mb-0 mt-1 list-decimal pl-[18px]">
          <li className="my-0.5"><Trans i18nKey="components.redaction.coach_fact_saved" components={[<b key="0" />, <b key="1" />]} /></li>
          <li className="my-0.5"><Trans i18nKey="components.redaction.coach_fact_always" components={[<b key="0" />, <b key="1" />]} /></li>
          <li className="my-0.5"><Trans i18nKey="components.redaction.coach_fact_source" components={[<b key="0" />, lock]} /></li>
        </ol>
        <div className="mt-1.5 text-[12px] leading-[18px] text-muted">{i18nT('components.redaction.coach_foot')}</div>
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className={btnPrimaryCls}
            disabled={closed}
            data-testid="redaction-coach-got-it"
            onClick={() => {
              if (slotKey) remember(COACH_KEY + slotKey)
              setClosed(true)
            }}
          >
            {i18nT('components.redaction.coach_got_it')}
          </button>
          <button
            type="button"
            className={btnCls}
            disabled={closed}
            data-testid="redaction-coach-never"
            onClick={() => {
              remember(COACH_NEVER_KEY)
              setClosed(true)
            }}
          >
            {i18nT('components.redaction.coach_never')}
          </button>
          <AboutLink label={i18nT('components.redaction.coach_learn_more')} />
        </div>
      </div>
    </div>
    </Reveal>
  )
}

// ── blocked link ─────────────────────────────────────────────────────────

/** Rules an allowed host relaxes: the host exemption skips only these. */
const ALLOWABLE_RULES = new Set(['exfil_query_length', 'exfil_query_pattern'])

function linkReason(record: BlockedLink): string {
  switch (record.rule) {
    case 'exfil_query_length':
      return i18nT('components.redaction.link_reason_query_length', { chars: record.query_chars })
    case 'exfil_query_pattern':
      return i18nT('components.redaction.link_reason_query_pattern')
    case 'exfil_percent_encoding':
    case 'exfil_decode_saturated':
      return i18nT('components.redaction.link_reason_encoding')
    default:
      return record.rule.includes('credential') ? i18nT('components.redaction.link_reason_credential') : i18nT('components.redaction.link_reason_generic')
  }
}

/** The Blocked link chip standing where a suspicious URL was removed. */
export function BlockedLinkChip({ domain, placeholder }: { domain: string; placeholder: string }) {
  useLanguageGeneration()
  const { blockedLinks, openId, toggle } = useRedactionUi()
  const group = blockedLinksForDomain(blockedLinks, domain)
  if (group.length === 0) return <>{placeholder}</>
  const single = group.length === 1 ? group[0] : null
  const id = `rx-link-${domain}`
  return (
    <span
      role="group"
      aria-label={i18nT('components.redaction.link_tag')}
      data-testid="blocked-link-chip"
      className="inline-flex min-h-[26px] max-w-full cursor-default flex-wrap items-center gap-1.5 rounded-lg border border-dashed border-warn bg-warn-subtle py-0.5 pl-2 pr-1.5 align-middle text-[12px] leading-5 text-card-fg"
    >
      <Link2Off size={12} aria-hidden="true" className="shrink-0" />
      <span className="text-[10px] font-bold tracking-[.04em] text-warn [text-box:trim-both_cap_alphabetic]">{i18nT('components.redaction.link_tag')}</span>
      <span className="break-all font-mono text-[12px] text-card-fg [text-box:trim-both_cap_alphabetic]" data-testid="blocked-link-target">
        {single?.path != null ? `${domain}${escapeInvisibleChars(single.path)}` : domain}
      </span>
      {single && single.query_chars > 0 && (
        <span className="text-muted [text-box:trim-both_cap_alphabetic]" data-testid="blocked-link-query">{i18nT('components.redaction.link_query', { chars: single.query_chars })}</span>
      )}
      <button
        type="button"
        aria-expanded={openId === id}
        aria-controls={id}
        onClick={e => toggle(id, e.currentTarget)}
        data-testid="blocked-link-inspect"
        className="relative inline-flex min-h-[18px] cursor-pointer items-center rounded-[5px] border border-warn bg-transparent px-1.5 text-[11px] leading-4 text-card-fg hover:bg-bg-hover [@media(hover:none)]:after:absolute [@media(hover:none)]:after:-inset-[13px] [@media(hover:none)]:after:content-['']"
      >
        <span className="[text-box:trim-both_cap_alphabetic]">{i18nT('components.redaction.link_inspect')}</span>
      </button>
    </span>
  )
}

/** The kept address with the escaped query marked, the way the prototype shows it. */
function MarkedUrl({ url }: { url: string }) {
  const q = url.indexOf('?')
  const shown = escapeInvisibleChars(url)
  if (q < 0) return <>{shown}</>
  const head = escapeInvisibleChars(url.slice(0, q + 1))
  const tail = escapeInvisibleChars(url.slice(q + 1))
  return (
    <>
      {head}
      <mark className="border-b-2 border-warn bg-warn-subtle text-card-fg">{tail}</mark>
    </>
  )
}

type Confirming = null | 'open' | 'allow'
type Feedback = null | { kind: 'opened' } | { kind: 'allowed'; workspace: string } | { kind: 'undone' }

function BlockedLinkEntry({ record, showTarget }: { record: BlockedLink; showTarget: boolean }) {
  const { slotKey, replyKey, blockedLinks } = useRedactionUi()
  const hold = useAllowHolds(replyKey).get(record.domain)
  const [confirming, setConfirming] = useState<Confirming>(null)
  // A card that re-renders after the reply reloads picks its outcome up from
  // the hold, so "Allowed · Undo" stays on screen.
  const [feedback, setFeedback] = useState<Feedback>(() => hold ? (hold.outcome === 'allowed' ? { kind: 'allowed', workspace: hold.workspace } : { kind: 'undone' }) : null)
  const [error, setError] = useState<string | null>(null)
  const shownFeedback = useLast(feedback)
  const shownError = useLast(error)
  const panel = confirming ?? (error !== null ? 'error' : feedback ? `feedback-${feedback.kind}` : null)
  const shownPanel = useLast(panel)
  const cancelRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { if (confirming) cancelRef.current?.focus() }, [confirming])
  const url = record.url
  const allowable = url !== null && ALLOWABLE_RULES.has(record.rule) && !!slotKey
  const openOnce = () => {
    // Re-derived at the click, so what opens is the address on screen and it
    // passes the same shape check. noopener/noreferrer: the page gets no handle
    // on this window and does not learn which conversation it came from.
    const target = openableBlockedLinkUrl(record.url, record.domain)
    setConfirming(null)
    if (target === null) return
    window.open(target, '_blank', 'noopener,noreferrer')
    setFeedback({ kind: 'opened' })
  }
  // The confirmation stays up until the request settles and is then replaced
  // by its outcome in one step, so the area swaps in place instead of
  // closing and reopening a moment later.
  const allow = async () => {
    if (!slotKey) { setConfirming(null); return }
    try {
      const res = await api.redactionAllowHost(slotKey, record.domain)
      setError(null)
      setFeedback({ kind: 'allowed', workspace: res.workspace })
      // The reply reloads with this host's links shown as links again; the
      // hold keeps this card open over them with its Undo.
      if (replyKey) setAllowHold(replyKey, record.domain, { workspace: res.workspace, records: blockedLinksForDomain(blockedLinks, record.domain), outcome: 'allowed' })
      window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: slotKey } }))
    } catch {
      setError(i18nT('components.redaction.link_allow_failed'))
    }
    setConfirming(null)
  }
  const undo = async (workspace: string) => {
    try {
      await api.redactionRevokeHost(workspace, record.domain)
      setError(null)
      setFeedback({ kind: 'undone' })
      if (replyKey && hold) setAllowHold(replyKey, record.domain, { ...hold, outcome: 'undone' })
      if (slotKey) window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: slotKey } }))
      setTimeout(() => setFeedback(null), 1600)
    } catch {
      setError(i18nT('components.redaction.link_undo_failed'))
    }
  }
  return (
    <div data-testid="blocked-link-entry">
      {showTarget && (
        <div className="mt-1 font-mono text-[12px]" data-testid="blocked-link-entry-target">
          {record.path != null ? `${record.domain}${escapeInvisibleChars(record.path)}` : record.domain}
        </div>
      )}
      <div className="mb-2 mt-1 text-[13px] leading-5 text-card-fg">
        <Trans i18nKey="components.redaction.link_destination" components={{ host: <code className={CODE}>{record.domain}</code> }} />
        <br />
        {linkReason(record)}
      </div>
      {url !== null ? (
        <>
          <Disclosure summary={i18nT('components.redaction.link_review_full_url')} testId="blocked-link-review">
            <div className="my-1.5 overflow-hidden rounded-xl border border-border bg-bg-elevated">
              <pre className="m-0 whitespace-pre-wrap break-all px-3 py-2 font-mono text-[11px] leading-4 text-text" data-testid="blocked-link-url">
                <MarkedUrl url={url} />
              </pre>
            </div>
            <span className={pillCls}>
              {record.rule === 'exfil_query_length'
                ? i18nT('components.redaction.link_rule_threshold', { rule: record.rule })
                : i18nT('components.redaction.rule_pill', { rule: record.rule })}
            </span>
          </Disclosure>
          <div className="mt-2 flex flex-wrap gap-2">
            <FeedbackButton
              className={btnCls}
              label={i18nT('components.redaction.link_copy_url')}
              done={i18nT('components.redaction.copied')}
              testId="blocked-link-copy"
              onClick={async () => {
                const ok = await copyToClipboard(url)
                setError(ok ? null : i18nT('components.redaction.link_copy_failed'))
                return ok
              }}
            />
            <button type="button" className={btnCls} data-testid="blocked-link-open-once" onClick={() => setConfirming('open')}>
              {i18nT('components.redaction.link_open_once')}
            </button>
            {allowable && (
              <button type="button" className={btnWarnCls} data-testid="blocked-link-allow" onClick={() => setConfirming('allow')}>
                {i18nT('components.redaction.link_allow')}
              </button>
            )}
          </div>
        </>
      ) : (
        <div className="text-[13px] leading-5" data-testid="blocked-link-withheld">
          {i18nT(record.url_withheld === 'length' ? 'components.redaction.link_withheld_length' : 'components.redaction.link_withheld_credential')}
        </div>
      )}
      {/* One area for the step after a button: a confirmation, then what it
          did. Moving from one to the next swaps the content in place, so the
          card changes height once instead of one panel closing while another
          opens. */}
      <Reveal show={panel !== null} contentKey={shownPanel ?? undefined}>
        {shownPanel === 'open' && (<><div role="alertdialog" aria-label={i18nT('components.redaction.link_open_confirm_aria')} className="mt-2 rounded-lg border border-border bg-bg p-2.5 text-[12px] leading-[18px]" data-testid="blocked-link-open-confirm">
          <b>{i18nT('components.redaction.link_open_confirm_title', { host: record.domain })}</b>
          <br />
          {record.query_chars > 0
            ? i18nT('components.redaction.link_open_confirm_body', { chars: record.query_chars })
            : i18nT('components.redaction.link_open_confirm_body_plain')}
          <div className="mt-2 flex flex-wrap gap-2">
            <button ref={confirming === 'open' ? cancelRef : undefined} type="button" className={btnPrimaryCls} onClick={() => setConfirming(null)}>{i18nT('components.redaction.cancel')}</button>
            <button type="button" className={btnCls} data-testid="blocked-link-open-confirmed" onClick={openOnce}>{i18nT('components.redaction.link_open')}</button>
          </div>
        </div></>)}
        {shownPanel === 'allow' && (<><div role="alertdialog" aria-label={i18nT('components.redaction.link_allow_confirm_aria')} className="mt-2 rounded-lg border border-border bg-bg p-2.5 text-[12px] leading-[18px]" data-testid="blocked-link-allow-confirm">
          <b>{i18nT('components.redaction.link_allow_confirm_title', { host: record.domain })}</b>
          <br />
          <Trans i18nKey="components.redaction.link_allow_confirm_body" components={[<b key="0" />, <b key="1" />]} />
          <div className="mt-2 flex flex-wrap gap-2">
            <button ref={confirming === 'allow' ? cancelRef : undefined} type="button" className={btnPrimaryCls} onClick={() => setConfirming(null)}>{i18nT('components.redaction.cancel')}</button>
            <button type="button" className={btnWarnCls} data-testid="blocked-link-allow-confirmed" onClick={() => void allow()}>{i18nT('components.redaction.link_allow_short')}</button>
          </div>
        </div></>)}
        {shownPanel === 'error' && shownError && <ErrorLine testId="blocked-link-error" message={shownError} />}
        {shownPanel?.startsWith('feedback') && (<>{shownFeedback && (
          <StatusLine testId="blocked-link-feedback">
            <span>
              {shownFeedback.kind === 'opened' && i18nT('components.redaction.link_opened')}
              {shownFeedback.kind === 'allowed' && i18nT('components.redaction.link_allowed', { host: record.domain })}
              {shownFeedback.kind === 'undone' && i18nT('components.redaction.link_undone')}
            </span>
            {shownFeedback.kind === 'allowed' && (
              <>
                <button type="button" className={linkBtnCls} data-testid="blocked-link-undo" onClick={() => void undo(shownFeedback.workspace)}>{i18nT('components.redaction.undo')}</button>
                <a href="/settings/security/redaction" className={`${linkBtnCls} no-underline`}>{i18nT('components.redaction.link_manage')}</a>
              </>
            )}
          </StatusLine>
        )}</>)}
      </Reveal>
    </div>
  )
}

function BlockedLinkCard({ domain }: { domain: string }) {
  useLanguageGeneration()
  const { blockedLinks } = useRedactionUi()
  const group = blockedLinksForDomain(blockedLinks, domain)
  if (group.length === 0) return null
  return (
    <CardShell id={`rx-link-${domain}`} title={i18nT('components.redaction.link_title')} testId="blocked-link-card">
      {group.map((record, i) => (
        <div key={i} className={i > 0 ? 'mt-2 border-t border-border pt-2' : undefined}>
          <BlockedLinkEntry record={record} showTarget={group.length > 1} />
        </div>
      ))}
      <Foot>
        <ReportLink rule={group[0].rule} kind="link" label={i18nT('components.redaction.link_report')} />
        <AboutLink label={i18nT('components.redaction.about_blocked_links')} />
      </Foot>
    </CardShell>
  )
}

/** Where a block's cards open: right after the block holding their markers. */
export function RedactionCardSlot({ ids }: { ids: string }) {
  const { openId, credentials, coached, firstCredential, slotKey, replyKey } = useRedactionUi()
  const list = ids.split(' ')
  // A held card this slot mounts with is the one already on screen before the
  // reload, so it shows without animating open again.
  const [heldAtMount] = useState(() => openId !== null && list.includes(openId) && !!replyKey
    && !!allowHolds.get(replyKey)?.has(openId.slice('rx-link-'.length)))
  // The coach follows the first block holding a removed value, before any card.
  const coach = coached && list.includes(`rx-cred-${firstCredential}`)
    ? <RedactionCoach count={credentials.size} slotKey={slotKey} />
    : null
  const mine = openId !== null && list.includes(openId) ? openId : null
  // The card last shown here stays rendered while it animates closed, and a
  // switch between two cards of this block replaces the content in place.
  const [shownId, setShownId] = useState<string | null>(mine)
  useEffect(() => { if (mine) setShownId(mine) }, [mine])
  const renderId = mine ?? shownId
  let card: React.ReactNode = null
  if (renderId) {
    if (renderId.startsWith('rx-cred-')) {
      const record = credentials.get(Number(renderId.slice('rx-cred-'.length)))
      card = record ? <CredentialCard key={renderId} record={record} /> : null
    } else {
      card = <BlockedLinkCard key={renderId} domain={renderId.slice('rx-link-'.length)} />
    }
  }
  if (!coach && !card) return null
  return <>{coach}<Reveal show={mine !== null} appear={!heldAtMount} contentKey={renderId ?? undefined}>{card}</Reveal></>
}

// ── markdown wiring ──────────────────────────────────────────────────────

/** Every credential tag the redactor writes, in the order they occur. */
export const CREDENTIAL_TAG_RE = /\[REDACTED: (?:encoded )?credential\]/g
/** A suspicious-URL placeholder, capturing its domain (a bracketed IPv6
 *  literal first, so the capture never stops inside `[::1]`). */
export const BLOCKED_LINK_PLACEHOLDER_RE = /\[REDACTED: suspicious URL to (\[[^\]]+\]|[^\]]+)\]/g
const MARKER_RE = /\[REDACTED: (?:encoded )?credential\]|\[REDACTED: suspicious URL to (\[[^\]]+\]|[^\]]+)\]/g

type HastParent = HastRoot | HastElement

/** What one rendered block needs to place markers: the records it may pair
 *  with and the ordinal its first credential tag carries. */
export interface RedactionMarkers {
  ordinals: ReadonlySet<number>
  domains: ReadonlySet<string>
  /** Domains allowed from a still-open card: their links now render as plain
   *  links, and the block holding one keeps that card's slot. */
  held?: ReadonlySet<string>
  credentials: ReadonlyMap<number, CredentialRecord>
  base: number
}

function countCredentialTags(text: string): number {
  return (text.match(CREDENTIAL_TAG_RE) ?? []).length
}

/**
 * Replace every placeholder that has a record with the element that renders
 * it, number credential tags in document order (the ordinal the backend
 * gave each), and put one card slot after each top-level block holding a
 * marker so its card opens right below that block.
 *
 * Runs after `rehypeSanitize`, so the injected elements are not escaped while
 * an agent's literal tag in prose still is. Text inside a fenced block keeps
 * its placeholder text: the code component renders those tags itself from the
 * `data-cred-base` stamped here. Inside an anchor or inline code nothing is
 * injected -- a control inside an anchor would follow the anchor -- but the
 * tags there still count, so later ordinals stay aligned.
 */
export function rehypeRedactionMarkers(options: { ordinals: ReadonlySet<number>; domains: ReadonlySet<string>; held?: ReadonlySet<string>; base: number }) {
  const { ordinals, domains, held = new Set<string>(), base: start } = options
  return (tree: HastRoot) => {
    let next = start
    const walk = (parent: HastParent, mode: 'inject' | 'count', ids: string[]) => {
      const children = parent.children
      for (let i = 0; i < children.length; i++) {
        const child = children[i]
        if (child.type === 'text') {
          if (mode === 'count') {
            next += countCredentialTags(child.value)
            continue
          }
          const pieces: Array<HastElement | HastText> = []
          let last = 0
          let changed = false
          MARKER_RE.lastIndex = 0
          let m: RegExpExecArray | null
          while ((m = MARKER_RE.exec(child.value)) !== null) {
            const isCred = m[1] === undefined
            const ordinal = isCred ? next++ : -1
            const known = isCred ? ordinals.has(ordinal) : domains.has(m[1])
            if (!known) continue
            changed = true
            if (m.index > last) pieces.push({ type: 'text', value: child.value.slice(last, m.index) })
            if (isCred) {
              pieces.push({ type: 'element', tagName: 'cred-tag', properties: { ordinal: String(ordinal), placeholder: m[0] }, children: [] })
              ids.push(`rx-cred-${ordinal}`)
            } else {
              pieces.push({ type: 'element', tagName: 'blocked-link', properties: { domain: m[1], placeholder: m[0] }, children: [] })
              ids.push(`rx-link-${m[1]}`)
            }
            last = m.index + m[0].length
          }
          if (!changed) continue
          if (last < child.value.length) pieces.push({ type: 'text', value: child.value.slice(last) })
          if (parent.type === 'root') parent.children.splice(i, 1, ...pieces)
          else parent.children.splice(i, 1, ...pieces)
          i += pieces.length - 1
          continue
        }
        if (child.type !== 'element') continue
        if (child.tagName === 'pre') {
          const code = child.children.find((c): c is HastElement => c.type === 'element' && c.tagName === 'code')
          if (code) {
            const base = next
            walk(code, 'count', ids)
            if (next > base && Array.from({ length: next - base }, (_, k) => base + k).some(o => ordinals.has(o))) {
              code.properties = { ...code.properties, dataCredBase: String(base) }
            }
            continue
          }
        }
        if (child.tagName === 'a') {
          const domain = heldAnchorDomain(child.properties?.href, held)
          if (domain !== null) ids.push(`rx-link-${domain}`)
        }
        const inert = child.tagName === 'a' || child.tagName === 'code'
        walk(child, mode === 'count' || inert ? 'count' : 'inject', ids)
      }
    }
    const root = tree.children
    for (let i = 0; i < root.length; i++) {
      const block = root[i]
      const ids: string[] = []
      if (block.type === 'element') walk({ type: 'root', children: [block] } as HastRoot, 'inject', ids)
      else if (block.type === 'text') {
        const holder: HastRoot = { type: 'root', children: [block] }
        walk(holder, 'inject', ids)
        root.splice(i, 1, ...(holder.children as RootContent[]))
        i += holder.children.length - 1
      }
      if (ids.length) {
        const slot: HastElement = { type: 'element', tagName: 'redaction-card-slot', properties: { ids: Array.from(new Set(ids)).join(' ') }, children: [] }
        root.splice(i + 1, 0, slot)
        i += 1
      }
    }
  }
}

/** A fenced block's text with each credential tag as its lock tag. */
export function RedactedCodeText({ code, base }: { code: string; base: number }) {
  const parts: React.ReactNode[] = []
  let last = 0
  let ordinal = base
  CREDENTIAL_TAG_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = CREDENTIAL_TAG_RE.exec(code)) !== null) {
    if (m.index > last) parts.push(code.slice(last, m.index))
    parts.push(<CredentialTag key={m.index} ordinal={ordinal} placeholder={m[0]} />)
    ordinal++
    last = m.index + m[0].length
  }
  parts.push(code.slice(last))
  return <>{parts}</>
}

/** The token a copied lock tag becomes: `<REDACTED>`, built from its parts so
 *  the angle brackets stay literal. */
const redactedMark = ['<', 'REDACTED', '>'].join('')

/** What copying a fenced block with lock tags puts on the clipboard: each tag
 *  becomes its label plus `<REDACTED>`, so the copy never reads as a value. */
export function copyTextForRedactedCode(code: string, base: number, credentials: ReadonlyMap<number, CredentialRecord>): string {
  let ordinal = base
  return code.replace(CREDENTIAL_TAG_RE, () => {
    const record = credentials.get(ordinal++)
    return (record?.label ?? '') + redactedMark
  })
}

/** Whether a fenced block holds a credential tag this reply has a record for. */
export function codeHasRedactionMarkers(code: string, base: number, credentials: ReadonlyMap<number, CredentialRecord>): boolean {
  const n = countCredentialTags(code)
  for (let o = base; o < base + n; o++) if (credentials.has(o)) return true
  return false
}

/**
 * A fenced block that holds lock tags. Rendered as plain text rather than
 * through the highlighter, because the tags are controls living inside the
 * text; copying turns each tag into its label plus `<REDACTED>`.
 */
export function RedactedCodeBlock({ code, lang, base }: { code: string; lang?: string; base: number }) {
  useLanguageGeneration()
  const { credentials } = useRedactionUi()
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const ids: string[] = []
  const n = countCredentialTags(code)
  for (let o = base; o < base + n; o++) if (credentials.has(o)) ids.push(`rx-cred-${o}`)
  return (
    <>
      <div className="code-block group/code my-2 overflow-hidden rounded-xl border border-border bg-bg-elevated" data-testid="redacted-code-block">
        <div className="flex min-h-7 items-center justify-between py-1 pl-3 pr-1 text-[13px] text-muted">
          <span className="font-mono">{lang || 'code'}</span>
          <button
            type="button"
            className="cursor-pointer rounded p-1 text-muted opacity-0 transition-opacity hover:bg-bg-hover hover:text-text group-hover/code:opacity-100 group-focus-within/code:opacity-100 [@media(hover:none)]:opacity-100"
            aria-label={i18nT(copied ? 'components.codeBlock.copied' : 'components.codeBlock.copy')}
            onClick={async () => {
              const ok = await copyToClipboard(copyTextForRedactedCode(code, base, credentials))
              setCopyFailed(!ok)
              if (!ok) return
              setCopied(true)
              setTimeout(() => setCopied(false), 1500)
            }}
          >
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </div>
        <pre className="m-0 overflow-x-auto whitespace-pre px-3 py-2 font-mono text-[13px] leading-5 text-text">
          <RedactedCodeText code={code} base={base} />
        </pre>
      </div>
      {copyFailed && <ErrorLine testId="redacted-code-copy-error" message={i18nT('components.redaction.copy_failed')} />}
      {ids.length > 0 && <RedactionCardSlot ids={ids.join(' ')} />}
    </>
  )
}

/** How many credential tags a block's source holds, for the next block's base. */
export { countCredentialTags }
