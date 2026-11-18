/**
 * The two pieces every Home list shares: a row that separates from the one
 * above it, and the accent "Open" link on its right. One definition, so the
 * board, the inbox, the roster and the schedule cannot drift apart.
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight } from 'lucide-react'

export function ListRow({ children, className = '', ...rest }: { children: ReactNode; className?: string } & Record<`data-${string}`, string | undefined>) {
  return <li className={`flex items-center gap-3 min-w-0 py-2 border-t border-border first:border-t-0 ${className}`} {...rest}>{children}</li>
}

export function RowLink({ to, children }: { to: string; children: ReactNode }) {
  return <Link to={to} className="text-accent text-[12px] inline-flex min-h-8 items-center gap-1 shrink-0">{children}<ArrowUpRight size={13} /></Link>
}
