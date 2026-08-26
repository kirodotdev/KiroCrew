import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

/** A titled section card, matching the dashboard's card chrome. */
export default function Card({ title, icon: Icon, right, children }: {
  title: string
  icon?: LucideIcon
  right?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="cc-card">
      <div className="cc-card-head">
        {Icon ? <Icon size={13} style={{ color: 'var(--muted)' }} aria-hidden /> : null}
        <h3 className="cc-card-title">{title}</h3>
        {right ? <div className="cc-card-right">{right}</div> : null}
      </div>
      {children}
    </section>
  )
}
