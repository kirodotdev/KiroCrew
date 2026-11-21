import type { ReactNode } from 'react'

/**
 * The frame both release-notes dialogs share: the update-available dialog
 * (`UpdateFoundModal`) and the post-update changelog dialog (`updateFlow`).
 * One width and one viewport cap, so the two read as the same dialog before
 * and after an update. Each dialog adds its own padding, overflow and z-index.
 */
export const RELEASE_DIALOG_FRAME =
  'bg-card border border-border rounded-xl shadow-xl w-full mx-4 max-w-3xl max-h-[85vh] flex flex-col'

/** The inner scroller's bottom fade: a line cut by the box edge reads as "more below". */
const FADE = '[mask-image:linear-gradient(to_bottom,black_calc(100%-1.5rem),transparent)]'

/**
 * A bordered release-notes box whose content scrolls inside it. The fade sits
 * on an inner scroller so the box's own border stays intact, and the bottom
 * padding lets the last line scroll clear of the fade. `testId` names the box;
 * the scroller is `${testId}-scroll`. `className` carries the box's height cap
 * and margins.
 */
export function ReleaseNotesBox({ testId, className = '', children }: { testId: string; className?: string; children: ReactNode }) {
  return (
    <div className={`bg-bg rounded-lg border border-border min-h-0 flex flex-col ${className}`} data-testid={testId}>
      <div className={`p-3 pb-6 min-h-0 overflow-y-auto ${FADE}`} data-testid={`${testId}-scroll`}>
        {children}
      </div>
    </div>
  )
}
