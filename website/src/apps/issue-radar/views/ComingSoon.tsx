import type { LucideIcon } from 'lucide-react'

/** Shared empty-state used by dashboard views that aren't built yet. Each
 * "soon" view is still its own file so a separate agent can replace this body
 * with the real implementation without touching any other view. */
export default function ComingSoon({
  icon: Icon, title, blurb,
}: {
  icon: LucideIcon
  title: string
  blurb: string
}) {
  return (
    <div className="max-w-4xl mx-auto p-6">
      <h1 className="text-[20px] font-semibold mb-5">{title}</h1>
      <div className="rounded-xl border border-dashed border-border bg-bg-elevated/50 p-10 flex flex-col items-center justify-center text-center gap-3">
        <Icon size={30} className="text-accent opacity-70" />
        <div className="text-[14px] font-medium text-text">Coming soon</div>
        <p className="text-[13px] text-muted max-w-sm leading-relaxed">{blurb}</p>
      </div>
    </div>
  )
}
