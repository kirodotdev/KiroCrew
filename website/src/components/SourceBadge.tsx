const sourceColorMap: Record<string, string> = {
  aim: 'bg-aim/15 text-aim',
  kirocrew: 'bg-bg-elevated text-muted border border-border',
  project: 'text-ok',
}
const defaultColor = 'bg-muted/10 text-muted'

export function SourceBadge({ source, children, className }: {
  source?: string
  children?: React.ReactNode
  className?: string
}) {
  const colorClass = sourceColorMap[source ?? ''] ?? defaultColor
  return (
    <span className={`px-1.5 py-[1px] rounded-full text-[12px] font-medium ${colorClass} ${className ?? ''}`}>
      {children ?? source}
    </span>
  )
}
