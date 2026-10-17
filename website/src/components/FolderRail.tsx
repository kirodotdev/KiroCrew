import { i18nT } from '../i18n/t'

/**
 * The clickable connector line down the left edge of an open folder's body.
 *
 * The line itself is still the body's own `border-l` — this component draws
 * nothing at rest, so the sidebar's alignment geometry (pinned by
 * ChatSidebar.folderAlignment.test.tsx and the measured playwright spec) is
 * untouched. It lays an out-of-flow hit strip over that border and paints a
 * wider accent bar on hover, so the thin line becomes a target for collapsing
 * the folder it groups.
 *
 * Geometry, relative to the body's padding box (the border sits at -1..0):
 * the strip spans -5..3. Left of the border is the body's own margin and inset;
 * right of it is the body's 3px left pad (5px in the board view), so the strip
 * never overlaps a row and never steals a click meant for one. The hover bar is
 * 3px wide, centred on the border (-2..1).
 *
 * The styles live in index.css (`.folder-rail`), not in utility classes: the
 * rail renders in the App chunk, which sits at its bundle-size budget, and a
 * stylesheet rule costs that chunk nothing.
 *
 * Mouse-only by design: the folder header's toggle button is the keyboard and
 * screen-reader control for the same action, so the strip takes no tab stop
 * rather than doubling every folder's stop in the tab order, and is
 * `aria-hidden` so assistive technology does not expose a second collapse
 * control. It still carries an `aria-label`, because every icon-only button
 * must (AUTOSDE `icon-buttons-need-labels`), and the same text as a `title`
 * tooltip naming the action for mouse users.
 */
export function FolderRail({ name, id, onToggle }: {
  /** The folder's name, for the accessible label. */
  name: string
  /** Test id suffix: the folder id, or a column-qualified one in the board view. */
  id: string
  /** Receives the click, so a caller can measure from the pressed rail. */
  onToggle: (e: React.MouseEvent<HTMLButtonElement>) => void
}) {
  const label = i18nT('pages.chatSidebar.collapse_folder_name', { name })
  return (
    <button
      type="button"
      tabIndex={-1}
      aria-label={label}
      aria-hidden="true"
      title={label}
      data-testid={`folder-rail-${id}`}
      onClick={e => {
        // The body sits inside the folder's drop target and, for nested folders,
        // inside the parent's body; the toggle belongs to THIS folder only.
        e.stopPropagation()
        onToggle(e)
      }}
      className="folder-rail"
    >
      <span />
    </button>
  )
}

export default FolderRail
