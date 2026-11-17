/**
 * The prompt a channel challenge link carries, held for the one send that delivers it.
 *
 * Opening the link, the signed-link anchor (`useSignedLinkAnchor`) links the session
 * to the channel thread and auto-sends the link's prompt. Those words are already in
 * the thread, so that send tags its row `meta.origin = CHANNEL_CHALLENGE_ORIGIN` and
 * the gateway's mirror does not quote them back there (`dashboard/channel_prompt.py`).
 *
 * The mark belongs to the page that armed it. The anchor marks the page's own
 * auto-send ref (`markChallengeAutoSend`), so a page that unmounts before sending
 * takes its mark with it. The page's auto-send intake takes the mark and runs the
 * send inside `sendAsChallenge`, and the send reads it (`takeChallengeSend`) before
 * its first await, so no other send can ever see it.
 */
export const CHANNEL_CHALLENGE_ORIGIN = 'channel_challenge'

const pending = new WeakMap<object, string>()
let sending = false

/** Mark *owner*'s pending auto-send of *prompt* as a challenge link's. */
export function markChallengeAutoSend(owner: object, prompt: string): void {
  pending.set(owner, prompt)
}

/** Whether *owner*'s auto-send of *text* is the marked one; clears the mark either way. */
export function takeChallengeAutoSend(owner: object, text: string): boolean {
  const marked = pending.get(owner)
  pending.delete(owner)
  return marked !== undefined && marked === text
}

/** Run *start* (a send's synchronous start) with the challenge mark visible to it. */
export function sendAsChallenge<T>(start: () => T): T {
  sending = true
  try {
    return start()
  } finally {
    sending = false
  }
}

/** Whether the send now starting runs inside `sendAsChallenge`; consumes it. */
export function takeChallengeSend(): boolean {
  const hit = sending
  sending = false
  return hit
}
