/* The composer's one-row layout numbers (`components/ChatInput.tsx`): the box
   radius, the text line's padding, the clusters' edge padding and the glass
   rim's reference height. */

// Half the one-row box: the 44px text line plus its 1px border each side.
export const COMPOSER_ONE_ROW_RADIUS = 23
// The one-row text line's padding, equal above and below so the line sits on
// the buttons' centre whatever its height. INPUT_TYPO's 12px over 4px puts it
// lower by half of (line height - 20px): 2px at the 16px touch floor. Set on
// the text column, so the textarea, its paste mirror and the Lexical editor
// all take it; `1.5em` is INPUT_TYPO's `leading-normal` line, and resolves on
// each of them at its own font size. Not `1lh`: Safari before 16.4 has no
// such unit, and an invalid value would drop the padding to 0.
export const ONE_ROW_TEXT_PAD = '[--composer-pt:max(0px,calc((44px_-_1.5em)_/_2))] [--composer-pb:max(0px,calc((44px_-_1.5em)_/_2))]'
// The one-row clusters' edge padding (pl-1.5 + pr-1.5), which the text column
// gives up besides the clusters themselves.
export const ONE_ROW_CLUSTER_PAD_PX = 12
// The glass rim is a slice of the pane's smaller side. A one-row composer
// would get half the rim of a two-row one, so the rim is sized from the
// two-row height (one wrapped line of text plus the toolbar) in both shapes.
export const COMPOSER_RIM_FROM_PX = 102
