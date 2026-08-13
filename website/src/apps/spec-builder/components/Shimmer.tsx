import { i18nT } from '../../../i18n/t'
// Shimmer / skeleton primitives for Spec Builder's loading states.
//
// Follows the CX pattern established by the Issue Radar builtin: a loading state
// is a SKELETON THAT KEEPS THE LAYOUT, not a spinner or a "Loading…" line. Each
// placeholder occupies the same box as the real content, so when data lands the
// column does not jump, and the shimmer makes the wait legible as "content is
// coming" rather than "nothing is here".
//
// Reuses the shared `animate-shimmer` utility (pre-existing global, legacy
// status) — no new keyframes, per the frontend style guide.
//
// NOTE: this duplicates Issue Radar's `ShimmerLine` rather than importing it,
// because reaching across app packages would couple two builtins. Promoting the
// primitive into the shared `components/ui.tsx` is the right long-term move, but
// that edits Issue Radar too, so it is deliberately left as a follow-up.

/** A single skeleton bar: a soft muted base with a faint accent sweep drifting
 *  across it. `delay` offsets each bar so a group flows as a gentle wave. */
export function ShimmerLine({ w, h = '12px', delay = 0 }: { w: string; h?: string; delay?: number }) {
  return (
    <div
      className="relative rounded overflow-hidden"
      style={{ width: w, height: h, backgroundColor: 'color-mix(in srgb, var(--muted) 18%, transparent)' }}
    >
      <div
        className="absolute inset-0 animate-shimmer"
        style={{
          backgroundImage:
            'linear-gradient(90deg, transparent,' +
            ' color-mix(in srgb, var(--accent) 18%, transparent),' +
            ' color-mix(in srgb, var(--aim) 18%, transparent), transparent)',
          backgroundSize: '200% 100%',
          animationDelay: `${delay}s`,
        }}
      />
    </div>
  )
}

/** Widths cycled so a stack reads as text of varying length, not a uniform grid. */
const ROW_WIDTHS = ['78%', '54%', '86%', '62%', '70%']

/** Placeholder rows for the specs rail while the first list load is in flight.
 *  Mirrors a real row: status dot + name + phase label. */
export function SpecListSkeleton({ count = 4 }: { count?: number }) {
  return (
    <>
      {/* Announced OUTSIDE the aria-hidden subtree — aria-hidden removes the
          whole tree from the a11y tree, so a status nested inside never reaches
          a screen reader. */}
      <span className="sr-only" role="status">{i18nT('apps.specBuilder.components.shimmer.loading_specs')}</span>
      <div aria-hidden="true" className="flex flex-col gap-0.5">
        {Array.from({ length: count }, (_, i) => (
          <div key={i} className="flex items-center gap-2 px-2.5 py-2 rounded-lg">
            <ShimmerLine w="7px" h="7px" delay={i * 0.06} />
            <ShimmerLine w={ROW_WIDTHS[i % ROW_WIDTHS.length]} delay={i * 0.06 + 0.04} />
          </div>
        ))}
      </div>
    </>
  )
}

/** Placeholder prose for the document pane while the agent is writing the file.
 *  Shaped like a spec document (heading, paragraph, a short list) so the panel
 *  holds its rhythm instead of collapsing to a centred spinner. */
export function DocSkeleton() {
  return (
    <>
      <span className="sr-only" role="status">{i18nT('apps.specBuilder.components.shimmer.writing_this_document')}</span>
      <div aria-hidden="true" className="flex flex-col gap-3 px-5 py-[18px]">
        <ShimmerLine w="42%" h="18px" />
        <div className="flex flex-col gap-2 mt-1">
          <ShimmerLine w="94%" delay={0.06} />
          <ShimmerLine w="88%" delay={0.1} />
          <ShimmerLine w="66%" delay={0.14} />
        </div>
        <ShimmerLine w="32%" h="15px" delay={0.2} />
        <div className="flex flex-col gap-2">
          {['80%', '72%', '86%'].map((w, i) => (
            <div key={w} className="flex items-center gap-2">
              <ShimmerLine w="6px" h="6px" delay={0.24 + i * 0.04} />
              <ShimmerLine w={w} delay={0.26 + i * 0.04} />
            </div>
          ))}
        </div>
      </div>
    </>
  )
}

/** Placeholder for the conversation column while the spec's detail loads.
 *  The chat is deliberately withheld until then: the embedded chat talks to
 *  /api/chat, and for a spec discovered on disk the worker slot does not exist
 *  yet — whichever endpoint creates it decides whether it is scoped to this app
 *  and to the project directory. Bubble-shaped rows so the column holds its
 *  layout instead of snapping in. */
export function ChatColumnSkeleton() {
  return (
    <>
      <span className="sr-only" role="status">{i18nT('apps.specBuilder.components.shimmer.loading_the_conversation')}</span>
      <div aria-hidden="true" className="flex-1 flex flex-col gap-4 px-5 py-[18px]">
        {[
          { w: '58%', self: false },
          { w: '76%', self: true },
          { w: '64%', self: false },
        ].map((row, i) => (
          <div key={row.w} className={`flex ${row.self ? 'justify-end' : 'justify-start'}`}>
            <div className="flex flex-col gap-2" style={{ width: row.w }}>
              <ShimmerLine w="100%" delay={i * 0.08} />
              <ShimmerLine w="72%" delay={i * 0.08 + 0.04} />
            </div>
          </div>
        ))}
      </div>
    </>
  )
}
