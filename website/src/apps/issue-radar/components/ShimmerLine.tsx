// Shared shimmer primitive for Issue Radar's loading states (the AI summary
// card's generating placeholder and the list columns' skeleton cards).

/** A single skeleton bar: a soft GRAY base with only a FAINT teal/purple tint
 * drifting across it (a restrained "content is coming" cue). Reuses the shared
 * `animate-shimmer` utility (background-position sweep); `delay` offsets each
 * bar so a group of them flows as a gentle wave. No new keyframes. */
export default function ShimmerLine({ w, delay = 0 }: { w: string; delay?: number }) {
  return (
    <div
      className="relative h-3 rounded overflow-hidden"
      style={{ width: w, backgroundColor: 'color-mix(in srgb, var(--muted) 18%, transparent)' }}
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
