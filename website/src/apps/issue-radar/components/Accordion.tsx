import { ChevronDown, type LucideIcon } from 'lucide-react'
import { useEffect, useState, type ReactNode, type UIEvent } from 'react'

/** One collapsible section of the left-rail accordion. Collapsed → just the
 * title bar; expanded → title bar + a scrollable body that grows to fill the
 * remaining rail height. The parent (LeftRail) guarantees exactly one section
 * is expanded at a time. */
export default function AccordionSection({
  title, icon: Icon, expanded, onToggle, children,
}: {
  title: string
  icon: LucideIcon
  expanded: boolean
  onToggle: () => void
  children: ReactNode
}) {
  // Show the top fade only once the body is scrolled away from the top, so it
  // hints at hidden content above without dimming the first row at rest. The
  // bottom fade stays static (there is almost always more below when open).
  const [scrolledDown, setScrolledDown] = useState(false)
  const onScroll = (e: UIEvent<HTMLDivElement>) => setScrolledDown(e.currentTarget.scrollTop > 4)
  // Re-expanding mounts a fresh body scrolled to the top, so clear the flag.
  useEffect(() => { setScrolledDown(false) }, [expanded])

  return (
    <div
      className={`relative mx-2 overflow-hidden rounded-xl border border-border bg-bg-elevated shadow-sm transition-[flex] ${
        expanded ? 'flex-1 min-h-0 flex flex-col' : 'flex-shrink-0'
      }`}
    >
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-1.5 px-3 py-2.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em] hover:text-text cursor-pointer bg-transparent flex-shrink-0"
      >
        <Icon size={13} className="flex-shrink-0" />
        <span className="flex-1 text-left">{title}</span>
        <ChevronDown size={14} className={`transition-transform ${expanded ? '' : '-rotate-90'}`} />
      </button>

      {expanded && (
        <div className="relative flex-1 min-h-0">
          <div onScroll={onScroll} className="absolute inset-0 overflow-y-auto scrollbar-none pb-3" style={{ scrollbarWidth: 'none' }}>
            {children}
          </div>
          {/* Top fade — appears once scrolled, hints there is more above. */}
          <div className={`pointer-events-none absolute top-0 left-0 right-0 h-6 bg-gradient-to-b from-bg-elevated to-transparent transition-opacity duration-200 ${scrolledDown ? 'opacity-100' : 'opacity-0'}`} />
          {/* Bottom fade — hints there is more to scroll. */}
          <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-6 bg-gradient-to-t from-bg-elevated to-transparent" />
        </div>
      )}
    </div>
  )
}
