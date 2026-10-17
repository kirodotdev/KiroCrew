import { ArrowUp } from 'lucide-react'
import { Glass } from '../../components/Glass'

/**
 * What the "Translucent panels" switch changes, shown with the real thing.
 *
 * A miniature of the chat page's bottom: skeleton lines of a transcript (bare
 * bars, no words, so nothing here needs a translation) with the composer dock
 * and two follow-up chips floating over them -- rendered by the SAME `Glass`
 * primitive the app uses, so the preview follows the switch live. Off (the
 * default) the panes are the solid `--bg-elevated` cards the mirror block in
 * index.css paints; on, the bars under the dock show through the frost.
 *
 * Purely illustrative: `aria-hidden`, no pointer events, nothing focusable.
 * The composer placeholder reuses the real one so the box reads as the message
 * box, not as a card.
 */
export function TranslucentPanelsPreview({ placeholder }: { placeholder: string }) {
  // Skeleton transcript: alternating "assistant" and "user" runs of bars, with
  // widths that vary so the strip reads as text under the dock, not stripes.
  const lines: Array<{ side: 'left' | 'right'; w: string }> = [
    { side: 'left', w: '62%' }, { side: 'left', w: '48%' }, { side: 'right', w: '36%' },
    { side: 'left', w: '70%' }, { side: 'left', w: '55%' }, { side: 'left', w: '30%' },
    { side: 'right', w: '44%' }, { side: 'left', w: '66%' }, { side: 'left', w: '40%' },
  ]
  return (
    <div
      aria-hidden="true"
      data-testid="translucent-panels-preview"
      className="relative mt-2 h-[132px] overflow-hidden rounded-lg border border-border bg-bg pointer-events-none select-none"
    >
      <div className="absolute inset-x-4 top-3 flex flex-col gap-2">
        {lines.map((l, i) => (
          <div key={i} className={`flex ${l.side === 'right' ? 'justify-end' : ''}`}>
            <div className="h-2 rounded-full bg-text/25" style={{ width: l.w }} />
          </div>
        ))}
      </div>
      <div className="absolute inset-x-4 bottom-3">
        <div className="mb-1.5 flex gap-1.5">
          {['40%', '28%'].map((w, i) => (
            <Glass key={i} as="span" variant="chip" radius={8} className="glass-hover inline-flex h-6 items-center rounded-lg border border-transparent px-2.5">
              <span className="block h-1.5 rounded-full bg-text/40" style={{ width: w === '40%' ? 44 : 30 }} />
            </Glass>
          ))}
        </div>
        <Glass variant="panel" radius={12} className="glass-shadow">
          <div className="flex items-end gap-2 px-3 py-2.5">
            <div className="min-w-0 flex-1 truncate text-[13px] text-muted">{placeholder}</div>
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-accent">
              <ArrowUp size={12} strokeWidth={2.6} className="text-bg" />
            </span>
          </div>
        </Glass>
      </div>
    </div>
  )
}
