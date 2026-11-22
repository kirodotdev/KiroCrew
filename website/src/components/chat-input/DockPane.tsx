import type { HTMLAttributes } from 'react'
import { Glass } from '../Glass'

/** The hue mixed into the pane's tint (index.css `glass-<tone>`); none is the plain pane. */
export type DockPaneTone = 'accent' | 'warn' | 'danger'

const TONE_CLASS: Record<DockPaneTone, string> = {
  accent: 'glass-accent',
  warn: 'glass-warn',
  danger: 'glass-danger',
}

/**
 * The pane a status or error row in the composer dock sits on. The dock floats
 * over the transcript, so a row drawn as bare text or a translucent tint lets
 * the transcript scrolled under it show through its words. The `thick` step is
 * the one the progress panes above the composer wear (#16299). A block
 * `ErrorNotice` inside a `danger` pane passes `nested`, so it draws no second
 * box; an inline one goes on the plain pane. `ErrorNotice` itself keeps its
 * look outside the dock.
 */
export function DockPane({ tone, className, ...rest }: HTMLAttributes<HTMLDivElement> & {
  tone?: DockPaneTone
  /** Layout only (margin, padding, flex). */
  className?: string
  'data-testid'?: string
}) {
  const classes = [tone ? TONE_CLASS[tone] : '', className ?? ''].filter(Boolean).join(' ')
  return <Glass {...rest} variant="chip" thickness="thick" radius={8} className={classes || undefined} />
}
