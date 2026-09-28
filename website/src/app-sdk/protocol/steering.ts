// kiro-cli emits a steering acknowledgment inline in the model's output when it
// consumes a mid-turn steer: `[STEERING steer-<id>: <what it did in response>]`.
// Showing that raw marker is ugly; instead we pull it out and render it as a
// distinct "Steered" chip (mirrors KiRoom's stripSteeringTag display-parity).
// The id part is `steer-<hex>` (no ']' or ':'); the summary is non-greedy up to
// the first ']' (matching KiRoom's behavior — a literal ']' inside a summary ends
// it early, which producers avoid).
const STEER_ACK_RE = /\[STEERING\s+steer-[^\]:]+:\s*([\s\S]*?)\]/g

export function extractSteeringAcks(content: string): { cleaned: string; acks: string[] } {
  const acks: string[] = []
  const cleaned = content.replace(STEER_ACK_RE, (_m, summary) => {
    const s = String(summary).trim()
    if (s) acks.push(s)
    return ''
  })
  // Collapse the blank line the removed marker leaves behind.
  return { cleaned: cleaned.replace(/\n{3,}/g, '\n\n').trimEnd(), acks }
}
