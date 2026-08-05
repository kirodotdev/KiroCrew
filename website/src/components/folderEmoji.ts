/** Folder icon helpers shared by the sidebar and the folder-config modal.
 *
 *  Both surfaces let a user pick a folder emoji, and both must agree on the
 *  candidate set and on what counts as "exactly one emoji" — a divergence would
 *  let one surface accept a value the other (and the backend) rejects. They
 *  previously lived as file-locals in ChatSidebar.tsx; hoisting them keeps the
 *  modal from re-deriving a second, drifting copy.
 */

export const FOLDER_EMOJIS = ['📁', '📂', '🗂️', '📋', '📝', '💼', '🚀', '⭐', '🔥', '💡', '🎯', '✅', '🐛', '🔧', '🧪', '📦', '🎨', '🔬', '🌟', '🧠', '⚙️', '🛠️', '📊', '🔒', '🌈', '🎉', '🤖', '☁️', '🧩', '📌'] as const

/** True if `s` is exactly one emoji grapheme — no letters, digits, or multiple emoji. */
export function isSingleEmoji(s: string): boolean {
  if (!s) return false
  if (/[\p{L}\p{N}]/u.test(s)) return false // reject any letter/digit (i.e. text)
  const hasEmoji = /\p{Extended_Pictographic}/u.test(s) || /[\u{1F1E6}-\u{1F1FF}]/u.test(s)
  if (!hasEmoji) return false
  const Seg = (Intl as unknown as { Segmenter?: new (l?: string, o?: { granularity: string }) => { segment: (x: string) => Iterable<unknown> } }).Segmenter
  if (Seg) return [...new Seg(undefined, { granularity: 'grapheme' }).segment(s)].length === 1
  return true // older engines: backend remains authoritative
}
