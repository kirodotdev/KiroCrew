import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/**
 * The nav rail participates in the Content Text Size ladder, and its labels
 * have to survive doing so. This file fences the two layout facts that decide
 * whether they do.
 *
 * The rail cannot widen to help. `RAIL_W_EXPANDED` in `hooks/useRailWidth.ts`
 * is 236 pixels, published rather than measured because `ChatPage` subtracts it
 * to size the activity panel and a DOM read returns intermediate widths during
 * the 150ms collapse animation. So every pixel the row spends on something
 * other than its label is a pixel the label loses at the larger tiers, and the
 * label is the only child that can give ground — it is the `flex-1 min-w-0
 * truncate` span.
 *
 * Two assertions, each for a defect that was live:
 *
 *  1. The keyboard chord stays IN FLOW, and takes no width while it is hidden.
 *     Both halves matter, and each was a live defect on its own.
 *
 *     Width: `opacity-0` hides the chord but does not remove it from layout, so
 *     as a `shrink-0` flex sibling it reserved its full width permanently. Rows
 *     with a shortcut handed roughly 25px to a hint nobody could see:
 *     "Sessions" and "Schedule" truncated to initials while "Artifacts" and
 *     "Customize", longer but chordless, read in full. `max-w-0
 *     overflow-hidden` gives that width back, and the hover and focus escapes
 *     return it only while the chord is actually readable.
 *
 *     In flow: taking the chord OUT of flow instead — `absolute right-2.5` —
 *     also frees the width, and reintroduces a different defect that had
 *     already been fixed once. `NavBadge` is the last in-flow `shrink-0` child
 *     and the row's content edge is `pr-2.5`, so an absolute chord pinned to
 *     that same edge paints on top of the unread count: on a hovered Sessions
 *     row neither the keystroke nor the count is legible. `src/test/
 *     App.test.tsx` pins the badge in flow and after the chord in document
 *     order, but document order survives `position: absolute`, so that test
 *     passes against this bug — verified by running it. The browser probe that
 *     would have caught the geometry, `scripts/capture-nav-badge-chord.mjs`,
 *     cannot mount: its capture entry omits `ThemeProvider` and `AppIcon`
 *     throws, identically on `upstream/main`. So this is the only fence between
 *     that regression and the rail.
 *  2. Each rail group's grid constrains its track. A single-column implicit
 *     grid sizes to `max-content` and a grid item's `min-width` is `auto`, so
 *     the row refused to shrink: the span believed it fitted, never triggered
 *     its own `truncate`, and the RAIL clipped it mid-word with no ellipsis.
 *     Flex children shrink by default, which is why the top group was fine.
 *
 * Neither pins a size or a label. They pin that the label gets the room.
 */
const read = (relative: string) =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')

const NAV_ROW = read('../shell/nav/navRow.tsx')
const NAV_RAIL = read('../shell/nav/navRail.tsx')

describe('a nav rail label keeps its room at every content text size', () => {
  it('the keyboard chord stays in flow, and takes no width while it is hidden', () => {
    const lines = NAV_ROW.split('\n')
    const chordLine = lines.findIndex((line) => line.includes('nav-shortcut-'))
    expect(chordLine, 'the chord element moved; this fence no longer points at it').toBeGreaterThan(-1)

    // The className VALUE only. A text slice would also read the comment above
    // it, which names `absolute` and `shrink-0` while explaining them.
    //
    // Scanned to the element's closing `>` rather than a fixed number of lines:
    // a line budget silently becomes "found no className" the moment somebody
    // writes a longer comment above it, which is exactly what happened to the
    // first version of this fence.
    const elementEnd = lines.findIndex((line, index) => index > chordLine && line.trim() === '>')
    expect(elementEnd, 'the chord element has no closing line; this fence cannot read it').toBeGreaterThan(chordLine)

    const classAttr = lines
      .slice(chordLine, elementEnd)
      .map((line) => /className="([^"]*)"/.exec(line)?.[1])
      .find((value): value is string => typeof value === 'string')
    expect(classAttr, 'found no className on the chord element').toBeDefined()

    expect(
      /\babsolute\b/.test(classAttr!),
      'the chord is out of flow: pinned to the row content edge it paints over the unread badge, ' +
        'which no runnable test catches — see this file header',
    ).toBe(false)
    expect(
      /\bmax-w-0\b/.test(classAttr!),
      'the chord has no zero-width rest state: hidden by opacity alone it reserves its width ' +
        'permanently and the label pays for a hint nobody can see',
    ).toBe(true)
    expect(
      /\bgroup-hover\/nav:max-w-none\b/.test(classAttr!),
      'the chord never regains its width on hover, so it is clipped to nothing when it is meant to be read',
    ).toBe(true)
    expect(
      /\bgroup-focus-visible\/nav:max-w-none\b/.test(classAttr!),
      'the chord regains its width on hover but not on keyboard focus, so a keyboard user cannot read it',
    ).toBe(true)
  })

  it('every rail group grid constrains its track, so a long label truncates instead of overflowing', () => {
    const unconstrained = NAV_RAIL.split('\n').filter(
      (line) => /className=["'`][^"'`]*\bgrid\b/.test(line) && !line.includes('grid-cols-[minmax'),
    )
    expect(
      unconstrained,
      'a rail grid has no minmax track: its column sizes to max-content, so the rail clips the row mid-word',
    ).toEqual([])
  })
})
