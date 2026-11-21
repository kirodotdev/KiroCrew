import type { ReactNode } from 'react'
import { ShieldCheck } from 'lucide-react'

import { Btn } from './ui'

/**
 * The one card every host memory sheet shows: plain rows, the question on the
 * left and its short answer on the right. Wraps under 320px without truncating.
 */
export function SheetCard({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <dl className="flex flex-col divide-y divide-border rounded-lg border border-border bg-bg" data-testid={testId}>
      {children}
    </dl>
  )
}

/** One row of a sheet card. `warn` paints the answer in the warning colour. */
export function SheetRow({ label, children, warn = false }: { label: string; children: ReactNode; warn?: boolean }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 px-3 py-2.5">
      <dt className="text-muted">{label}</dt>
      <dd className={`ml-auto text-right font-medium ${warn ? 'text-warn-text' : 'text-text-strong'}`}>{children}</dd>
    </div>
  )
}

/** A text-only button: the quiet second action under a sheet's primary, or a row link. */
export function QuietBtn({
  children,
  className = '',
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean }) {
  return (
    <Btn
      className={`justify-center border-transparent bg-transparent text-accent hover:border-transparent hover:bg-transparent hover:underline ${className}`}
      {...rest}
    >
      {children}
    </Btn>
  )
}

/** A sheet footer: one full-width primary action, then quiet actions under it. */
export function SheetActions({ primary, quiet }: { primary?: ReactNode; quiet: ReactNode }) {
  return <div className="flex w-full flex-col gap-1">{primary}{quiet}</div>
}

/**
 * The closing line of every host-drawn memory sheet: the owner's press here is the
 * only approval, and an app that asked for the sheet cannot press it for them.
 */
export function HostNote({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-start gap-2 text-[12px] text-muted">
      <ShieldCheck size={14} className="mt-[1px] shrink-0 text-ok" aria-hidden="true" />
      <span>{children}</span>
    </p>
  )
}

/** A memory's mark: the first letter of its name in a round accent chip. */
export function ProviderMark({ name, size = 32 }: { name: string; size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="flex shrink-0 items-center justify-center rounded-full bg-accent-subtle font-semibold text-accent"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.45) }}
    >
      {Array.from(name.trim())[0]?.toUpperCase() ?? '?'}
    </span>
  )
}

/** The box that holds the ticks an action needs before its primary button works. */
export function AckBox({ children }: { children: ReactNode }) {
  return <div className="flex flex-col gap-2.5 rounded-lg border border-warn bg-warn-subtle p-3">{children}</div>
}
