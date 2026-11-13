/** The session row's line classes and glyph size, shared by the row and the
 *  Ultra Mode root's summary line (./CrewRootSummaryLine). */
import { ROW_STATUS_CLS } from '../../components/listShell'

/**
 * Session-row type scale, quantized to a 4px baseline grid.
 *
 * Every line box is a multiple of 4 and the inter-line gaps are zero — the
 * leading carries the breathing room — so a row is a whole number of grid
 * units (12px padding + 12 + 20 + 16 = 60) and consecutive rows stack on the
 * grid instead of drifting. The previous scale mixed three RATIOS
 * (`leading-tight` / `leading-snug`) over 11/13/12px text, which produced
 * 13.75 / 17.875 / 16.5px boxes: no line landed on the grid and the row height
 * was an arbitrary 62.125px.
 *
 * The three sizes are also spread far enough apart to READ as a hierarchy.
 * 11/13/12 sat within 2px of each other, and CJK glyphs fill their em box, so
 * the secondary line competed with the headline instead of yielding to it.
 *
 * The three boxes need NOT be equal to each other. Row-centring the status
 * marker did require the first and last to match — it is the only way
 * headline-centre can coincide with row-centre — and that constraint is gone
 * because the marker now leads the secondary line and centres on IT.
 * Which is what buys the meta line its 12px box: the tightest of the three,
 * spent on the least important line.
 */

/* ROW_META_CLS / ROW_TITLE_CLS / ROW_STATUS_CLS come from components/listShell,
 * shared with the Crew Members roster so the two lists sit on one type scale. */
/* A SECOND surface tracks the three listShell row sizes: the Notes app's left rail
 * (`apps/md-notebook/constants.ts`, `RAIL_TYPE`) mirrors them so the two
 * sidebars read as one scale. The agreement is by copied value, not a shared
 * token — nothing goes red if these move. Change a size in listShell and update
 * `RAIL_TYPE` in the same commit, or the rail silently diverges. */

/** The secondary line's three shapes, as whole class strings. The eight status
 *  branches that render this line each used to spell the type classes out, so a
 *  ninth state was one copy-paste away from re-introducing a size the grid does
 *  not contain — which is how the line ended up at 12px against an 11px meta
 *  line in the first place. Colour is what actually differs between them.
 *
 *  All three are FLEX rows, because all three lead with the row's status marker
 *  (the muted one carries the `unread` dot). A `w-2 h-2` dot only gets its box as
 *  a flex item — as an inline child both dimensions are dropped and it vanishes. */
export const ROW_STATUS_LINE_CLS = `${ROW_STATUS_CLS} flex items-center gap-1.5 min-w-0`
export const ROW_STATUS_LINE_ACCENT_CLS = `${ROW_STATUS_CLS} text-accent truncate flex items-center gap-1`
export const ROW_STATUS_LINE_MUTED_CLS = `${ROW_STATUS_CLS} text-muted flex items-center gap-1.5 min-w-0`

/** Every glyph in a session row is drawn at ONE size — the status marker, the
 *  meta line's mode/channel markers, and the pin. Three sizes (9 / 10 / 12) read
 *  as accidental variation rather than as a hierarchy, since none of these
 *  glyphs outranks another. */
export const ROW_ICON_PX = 10
