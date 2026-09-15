/**
 * Loading placeholder for the chat transcript.
 *
 * A skeleton earns its keep by previewing the SHAPE the reader is about to get --
 * that is the whole difference between it and a spinner, which says only "wait".
 * So this is built as a transcript: full-width assistant blocks whose last line
 * is short, alternating with narrower right-aligned user bubbles. Six equal
 * full-width bars (what this replaces) preview a table, and a reader watching one
 * turn into a conversation has been told nothing by it.
 *
 * It covers two waits that used to look different from each other -- a slot being
 * fetched (a centred spinner) and a saved reading position being restored (bars).
 * One placeholder for both, because they are the same event to the reader: the
 * transcript is not ready yet.
 *
 * DECORATIVE, and marked so: `aria-hidden` keeps the bars out of the
 * accessibility tree, and the caller carries `aria-busy` on the container that
 * swaps between this and the transcript. Nothing here announces itself, so a
 * screen reader is not read a wall of empty boxes.
 *
 * The sweep is STAGGERED per line, so it reads as one wave travelling down the
 * transcript rather than every bar flashing together -- which is also what tells
 * the reader the placeholder is a sequence of messages and not one block.
 *
 * It pauses under `prefers-reduced-motion: reduce` via the global rule in
 * index.css, and `.skeleton` is named in the companion rule that zeroes
 * animation-DELAY there, because the global one covers duration only.
 */

/** One row's silhouette. Widths are percentages of the content column.
 *
 *  Fixed rather than random: a random shape re-rolls on every render, so the
 *  placeholder would visibly reflow while the reader is looking at it -- and
 *  because this mounts and unmounts on every switch, it would look different
 *  every time for no reason. Two turns plus the head of a third, which is about
 *  what one phone viewport holds. */
const ROWS: ReadonlyArray<{ role: 'assistant' | 'user'; lines: readonly number[] }> = [
  { role: 'assistant', lines: [100, 96, 62] },
  { role: 'user', lines: [46] },
  { role: 'assistant', lines: [100, 92, 100, 54] },
  { role: 'user', lines: [32] },
  { role: 'assistant', lines: [100, 78] },
]

/** Line box height, and the gap between lines within one block. Matches the
 *  transcript's own line rhythm closely enough that the swap is not a jolt;
 *  exact typographic parity is not the goal, the silhouette is. */
const LINE_H = 14
const LINE_GAP = 8
const BLOCK_GAP = 22

/** Per-line delay, so the sweep reads as ONE wave travelling down the transcript
 *  rather than every bar flashing in unison. Chosen against the 1.6s cycle: at
 *  110ms the last of ~11 lines starts about 1.1s after the first, which keeps the
 *  wave continuous instead of letting a visible gap open behind it.
 *
 *  The reduced-motion rule in index.css zeroes animation-DURATION but not DELAY,
 *  which is why `.skeleton` is named in the delay-zeroing rule beside it -- a
 *  stagger left in place there holds bars invisible in sequence forever. */
const LINE_STAGGER_MS = 110

/** The delay travels as a custom property because the animation lives on the
 *  bar's pseudo-element, which no inline style can reach. */
const delayStyle = (seq: number): React.CSSProperties =>
  ({ ['--sk-delay']: `${seq * LINE_STAGGER_MS}ms` }) as React.CSSProperties

export function ChatTranscriptSkeleton() {
  return (
    <div
      aria-hidden
      className="absolute inset-0 z-[2] bg-bg overflow-hidden pointer-events-none px-4 pt-5"
    >
      <div
        className="mx-auto flex w-full flex-col"
        style={{ maxWidth: 'var(--mc-content-width, 900px)', gap: BLOCK_GAP }}
      >
        {(() => {
          // One running index across every line of every block, so the wave is
          // continuous down the whole placeholder instead of restarting per turn.
          let seq = -1
          return ROWS.map((row, i) => (
            <div
              key={i}
              className={`flex w-full flex-col${row.role === 'user' ? ' items-end' : ''}`}
              style={{ gap: LINE_GAP }}
            >
              {row.role === 'user' ? (
                // One rounded box, sized to its content the way a real bubble is
                // -- not a full-width bar, which is what made the old placeholder
                // read as a document rather than a conversation.
                <div
                  className="skeleton"
                  style={{
                    width: `${row.lines[0]}%`,
                    height: LINE_H * 2 + LINE_GAP,
                    borderRadius: 'var(--radius-lg, 14px)',
                    ...delayStyle(++seq),
                  }}
                />
              ) : (
                row.lines.map((w, j) => (
                  <div
                    key={j}
                    className="skeleton"
                    style={{
                      width: `${w}%`,
                      height: LINE_H,
                      borderRadius: 'var(--radius-sm, 4px)',
                      ...delayStyle(++seq),
                    }}
                  />
                ))
              )}
            </div>
          ))
        })()}
      </div>
    </div>
  )
}
