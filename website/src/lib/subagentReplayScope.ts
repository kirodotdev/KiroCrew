/** Which slots this window shows subagent cards for, so the gateway's
 *  durable subagent replay (finished runs rebuilt from the crew log) covers
 *  those slots and no others.
 *
 *  The gateway replays a slot's finished runs the first time a subscription
 *  names it: in the `slots` list of `subscribe_subagents`, in a later
 *  `subagent_replay` frame, or as the slot a `slot_focused` frame announces.
 *  The focused slot and the split panes are named by the connect catch-up;
 *  a view that shows other slots' cards (the command center) registers them
 *  here, and each newly registered slot is asked for once while connected. */

type Sender = (slots: string[]) => void

/** The most slots the gateway reads from one frame (`MAX_REQUEST_SLOTS`). */
export const REPLAY_SLOTS_PER_FRAME = 256

/** *slots* cut into frame-sized lists, so a fleet view's names all arrive. */
export function replaySlotFrames(slots: readonly string[]): string[][] {
  const out: string[][] = []
  for (let i = 0; i < slots.length; i += REPLAY_SLOTS_PER_FRAME) out.push(slots.slice(i, i + REPLAY_SLOTS_PER_FRAME))
  return out
}

const viewed = new Map<string, number>()
let sender: Sender | null = null

/** Bind the sender that asks the open socket for more slots; null unbinds. */
export function bindSubagentReplaySender(next: Sender | null): void {
  sender = next
}

/** Every slot a mounted view has registered, for the connect catch-up. */
export function viewedSubagentSlots(): string[] {
  return [...viewed.keys()]
}

/** Register *slots* as shown by one view; returns the release for that view.
 *  A slot no other view held is asked for at once when a socket is bound. */
export function viewSubagentSlots(slots: readonly string[]): () => void {
  const held = [...new Set(slots.filter(s => typeof s === 'string' && s))]
  const added: string[] = []
  for (const slot of held) {
    const n = viewed.get(slot) ?? 0
    if (n === 0) added.push(slot)
    viewed.set(slot, n + 1)
  }
  if (added.length && sender) for (const frame of replaySlotFrames(added)) sender(frame)
  return () => {
    for (const slot of held) {
      const n = (viewed.get(slot) ?? 1) - 1
      if (n <= 0) viewed.delete(slot)
      else viewed.set(slot, n)
    }
  }
}

/** The `slots` a `subscribe_subagents` frame names: focused slot first,
 *  then panes and registered views, without duplicates. */
export function subagentReplaySlots(focused: string | null, panes: readonly string[]): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const slot of [focused, ...panes, ...viewed.keys()]) {
    if (!slot || seen.has(slot)) continue
    seen.add(slot)
    out.push(slot)
  }
  return out
}
