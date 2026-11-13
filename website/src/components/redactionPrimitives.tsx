/**
 * The shared buttons, status lines and action row the redaction cards draw
 * (the prototype's .btn / .act / .fbline). Split out of RedactionCards.tsx so
 * the owner reveal (RedactedValueReveal.tsx) uses the same controls.
 */
import React, { useEffect, useRef, useState } from 'react'
import { TriangleAlert } from 'lucide-react'
import ErrorNotice from './ErrorNotice'

/** Every control, format or bidi character as visible percent-escapes, so the
 *  text on screen cannot read differently from where it points. */
export function escapeInvisibleChars(text: string): string {
  const bytes = new TextEncoder()
  return text.replace(/\p{C}/gu, ch =>
    Array.from(bytes.encode(ch), b => `%${b.toString(16).toUpperCase().padStart(2, '0')}`).join(''),
  )
}

// Touch targets grow to 44px on hover-less (touch) screens, as the prototype asks.
export const touchTarget = '[@media(hover:none)]:min-h-11'
export const btnBase = 'inline-flex items-center justify-center whitespace-nowrap rounded-md border bg-card px-2.5 py-[3px] text-[12px] leading-[18px] min-h-7 text-card-fg cursor-pointer hover:border-border-strong hover:bg-bg-hover disabled:cursor-default disabled:opacity-70'
export const btnCls = [btnBase, 'border-border', touchTarget].join(' ')
export const btnPrimaryCls = [btnBase, 'border-accent bg-accent-subtle', touchTarget].join(' ')
export const btnWarnCls = [btnBase, 'border-warn', touchTarget].join(' ')
// inline-flex so the label centres inside the 44px touch box instead of
// sitting at its top; an <a> otherwise lays its text out from the top edge.
export const linkBtnCls = ['inline-flex items-center justify-start border-0 bg-transparent px-0.5 py-1 min-h-6 text-left text-accent cursor-pointer', touchTarget].join(' ')
export const pillCls = 'inline-block whitespace-nowrap rounded-full border border-border px-[7px] text-[11px] leading-4 text-muted'
export const CODE = 'rounded border border-border bg-bg-elevated px-1 font-mono text-[12px]'

/** A button whose label swaps to a confirmation for a moment ("Copied"). */
export function FeedbackButton({ className, label, done, onClick, testId }: {
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
export function StatusLine({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return (
    <div role="status" data-testid={testId} className="mt-1.5 flex flex-wrap items-center gap-2 rounded-lg border border-ok bg-ok-subtle px-2 py-1.5 text-[12px] leading-[18px] text-card-fg">
      {children}
    </div>
  )
}

export function ErrorLine({ message, testId }: { message: string; testId?: string }) {
  return (
    <div className="mt-1.5">
      <ErrorNotice variant="inline" askAgent message={message} testId={testId} />
    </div>
  )
}

/** The prototype's .act row: what an action does, and its button. */
export function ActionRow({ title, note, command, warning, children }: {
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
