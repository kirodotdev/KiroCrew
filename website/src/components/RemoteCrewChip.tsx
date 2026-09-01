import { Server } from 'lucide-react'

/**
 * The marker that a session's turns run on ANOTHER MACHINE.
 *
 * Two consumers share this one definition on purpose. A federated *search* row
 * (a session that lives on a peer) and a local row bound to a peer for execution
 * are different things internally, but to the person reading the sidebar they
 * make the same claim — "this is not on my machine" — and a user who learned the
 * marker in one list must recognise it in the other.
 *
 * Why it is not the neutral meta chip every sibling uses: "runs on another
 * machine" is a different KIND of fact from "has this tag", and at neutral
 * weight it read as just another tag. The info tint raises it without shouting.
 *
 * The glyph is the non-colour half of the cue, so the distinction survives a
 * colour-vision deficiency, and it is `aria-hidden` because the crew name beside
 * it already names the target — a screen reader would otherwise announce a
 * decorative icon before the only informative part.
 */
export function RemoteCrewChip({ name, title }: { name: string; title?: string }) {
  return (
    <span
      className="shrink-0 inline-flex items-center gap-0.5 max-w-[7rem] text-[10px] px-1 rounded bg-info-subtle text-info border border-info/40"
      title={title || name}
      data-testid="remote-crew-chip"
    >
      <Server size={9} className="shrink-0" aria-hidden="true" />
      {/* The instance name is user-set and unbounded. The session row is a fixed
       *  height with the timestamp pinned at its end, so an unclamped chip pushes
       *  the timestamp out and overflows the row. Cap the chip and truncate the
       *  name inside it; the full value stays available in the title tooltip. */}
      <span className="truncate">{name}</span>
    </span>
  )
}
