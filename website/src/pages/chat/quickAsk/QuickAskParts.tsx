import type { CSSProperties, ReactNode } from 'react'
import { KeyRound, Lock, MousePointerClick, ShieldCheck, TextCursorInput, Unlock } from 'lucide-react'

import InfoTip from '../../../components/InfoTip'
import { i18nT } from '../../../i18n/t'
import { sanitizeLlmOutput } from '../../../utils/sanitize'
import { REASON_MAX, type QuickAsk, type QuickAskField, type QuickAskKind, type QuickAskLabel } from './types'

/**
 * Pieces the sheet (V2) and the folded hint (V1) share: the icon and our own
 * title per kind, the full origin line, the agent's reason, and the ring over
 * the bound element on the frame.
 */

/** `https://github.com` -> `github.com`, for sentences; the origin line shows the whole origin. */
export function originHost(origin: string): string {
  try { return new URL(origin).host } catch { return origin }
}

export function askIcon(kind: QuickAskKind) {
  switch (kind) {
    case 'credentials': return KeyRound
    case 'text': return TextCursorInput
    case 'otp': return Lock
    case 'human_check': return ShieldCheck
    case 'confirm': return MousePointerClick
  }
}

/** The label a text ask's single field carries. */
export function textLabel(ask: QuickAsk): QuickAskLabel {
  return ask.fields.find(f => f.role === 'text')?.label ?? 'other'
}

export function labelWord(label: QuickAskLabel): string {
  switch (label) {
    case 'name': return i18nT('pages.chat.quickAsk.label_name')
    case 'email': return i18nT('pages.chat.quickAsk.label_email')
    case 'username': return i18nT('pages.chat.quickAsk.label_username')
    case 'phone': return i18nT('pages.chat.quickAsk.label_phone')
    case 'other': return i18nT('pages.chat.quickAsk.label_other')
  }
}

export function otpDigits(ask: QuickAsk): number { return ask.digits ?? 6 }

/** Our title for the kind. The agent never writes this line. */
export function askTitle(ask: QuickAsk): string {
  const host = originHost(ask.origin)
  switch (ask.kind) {
    case 'credentials': return i18nT('pages.chat.quickAsk.title_credentials', { host })
    case 'text': return i18nT('pages.chat.quickAsk.title_text', { what: labelWord(textLabel(ask)) })
    case 'otp': return i18nT('pages.chat.quickAsk.title_otp', { n: otpDigits(ask) })
    case 'human_check': return i18nT('pages.chat.quickAsk.title_human_check')
    case 'confirm': return i18nT('pages.chat.quickAsk.title_confirm')
  }
}

/** The whole origin, wrapped, never cut: it is the one thing a person must read. */
export function OriginLine({ ask, className = '' }: { ask: QuickAsk; className?: string }) {
  const https = ask.origin.toLowerCase().startsWith('https:')
  const Icon = https ? Lock : Unlock
  return (
    <div className={`flex min-w-0 items-start gap-1.5 text-[12px] ${className}`} data-testid="quick-ask-origin">
      <Icon size={12} aria-hidden="true" className={`mt-[3px] shrink-0 ${https ? 'text-ok' : 'text-warn'}`} />
      <span className="min-w-0 break-all font-semibold text-text-strong" data-testid="quick-ask-origin-text">
        {ask.top_origin ? i18nT('pages.chat.quickAsk.origin_inside', { origin: ask.origin, top: ask.top_origin }) : ask.origin}
      </span>
      <InfoTip text={i18nT('pages.chat.quickAsk.origin_tip')} />
    </div>
  )
}

/** The agent's own words: plain text, "Kiro says:", at most two lines. */
export function ReasonLine({ ask, className = '' }: { ask: QuickAsk; className?: string }) {
  const text = sanitizeLlmOutput(ask.reason).replace(/\s+/g, ' ').trim().slice(0, REASON_MAX)
  if (!text) return null
  return (
    <p className={`min-w-0 line-clamp-2 break-words text-[12px] text-muted ${className}`} data-testid="quick-ask-reason" title={text}>
      {i18nT('pages.chat.quickAsk.reason_line', { text })}
    </p>
  )
}

/** A frame-pixel length as a percent of *of* (a bare number; the unit is added in the style). */
const share = (v: number, of: number) => (v / of) * 100

/** Rings on the page elements the ask is bound to, over a dimmed frame. */
export function QuickAskTargetRing({ ask, dim }: { ask: QuickAsk; dim?: boolean }) {
  return (
    <div aria-hidden="true" className={`pointer-events-none absolute inset-0 ${dim ? 'bg-black/45' : ''}`} data-testid="quick-ask-target">
      {ask.fields.map((f, i) => (
        <div
          key={`${f.role}${i}`}
          className="absolute rounded-md ring-2 ring-accent shadow-[0_0_0_4px_var(--accent-glow)]"
          style={{
            left: `${share(f.rect.x, ask.frame.w)}%`,
            top: `${share(f.rect.y, ask.frame.h)}%`,
            width: `${share(f.rect.w, ask.frame.w)}%`,
            height: `${share(f.rect.h, ask.frame.h)}%`,
          }}
        />
      ))}
    </div>
  )
}

/** The widget a human check hands over, or the form a sign-in takeover zooms on. */
export function widgetField(ask: QuickAsk): QuickAskField | null {
  return ask.fields.find(f => f.role === 'widget' || f.role === 'region') ?? null
}

/**
 * Credentials with no field bound: the person types into the sheet and the
 * gateway walks the sign-in screen by screen; taking over the page is the way
 * out for what it cannot fill.
 */
export function isSignInTakeover(ask: QuickAsk): boolean {
  return ask.kind === 'credentials' && !ask.fields.some(f => f.role === 'password')
}

/** The ask is answered by the person's own hands on the page. */
export function takesOver(ask: QuickAsk): boolean {
  return ask.kind === 'human_check' || isSignInTakeover(ask)
}

/** Most a scoped takeover zooms in; past this a small box turns to mush. */
const ZOOM_MAX = 3

/**
 * The transform that zooms the live frame onto a taken-over widget (with
 * some room around it). Pointer mapping reads the canvas's on-screen box, so
 * clicks still land in frame pixels. Null when there is nothing to zoom to.
 */
export function zoomStyle(ask: QuickAsk | null): CSSProperties | null {
  if (!ask || !takesOver(ask) || ask.phase !== 'taken_over') return null
  const w = widgetField(ask)
  if (!w || w.rect.w <= 0 || w.rect.h <= 0) return null
  const scale = Math.max(1, Math.min(ZOOM_MAX, ask.frame.w / (w.rect.w * 1.6), ask.frame.h / (w.rect.h * 1.6)))
  const cx = ((w.rect.x + w.rect.w / 2) / ask.frame.w) * 100
  const cy = ((w.rect.y + w.rect.h / 2) / ask.frame.h) * 100
  // About the default centre origin: move the widget's centre to the middle, then
  // scale. The same mapping as scaling about the widget and then translating.
  return {
    transform: `scale(${scale.toFixed(3)}) translate(${50 - cx}%, ${50 - cy}%)`,
  }
}

/** The live canvas, zoomed onto a taken-over widget while `zoom` is set. */
export function QuickAskZoom({ zoom, children }: { zoom: CSSProperties | null; children: ReactNode }) {
  if (!zoom) return <>{children}</>
  return <div className="absolute inset-0 motion-safe:transition-transform" style={zoom} data-testid="quick-ask-zoom">{children}</div>
}

/** What a refused answer says. The gateway's code is never shown raw. */
export function answerErrorText(code: string): string {
  if (code === 'no_field') return i18nT('pages.chat.quickAsk.error_no_field')
  if (code === 'too_long') return i18nT('pages.chat.quickAsk.error_too_long')
  if (code === 'policy_denied') return i18nT('pages.settings.browserPanel.policy_off')
  return code === 'origin_changed' || code === 'stale'
    ? i18nT('pages.chat.quickAsk.error_page_changed')
    : i18nT('pages.chat.quickAsk.error_send_failed')
}
