/**
 * What the installed app's share target does with a share. The manifest's
 * `share_target` sends a GET to `/share?title=&text=&url=`; the route turns
 * that into a new chat whose composer holds the shared text. The text is
 * untrusted: it only ever fills the composer for the person to read and send,
 * so the chat path below never carries `autoSend`.
 *
 * Nothing here cuts the text. The service worker answers `/share` from the
 * cached shell, so the long query is read in the browser and the redirect to
 * `/chat` is a client-side navigation; the chat page moves the text into
 * session storage and replaces the URL with `?sid=` once the session exists.
 */

/** Whether `link` is one whole whitespace-separated word of `text`. */
function hasWord(text: string, link: string): boolean {
  let start = -1
  for (let i = 0; i <= text.length; i++) {
    const gap = i === text.length || text.charAt(i).trim() === ''
    if (!gap) {
      if (start < 0) start = i
    } else if (start >= 0) {
      if (text.slice(start, i) === link) return true
      start = -1
    }
  }
  return false
}

/**
 * The composer text for a share: its title, text and link, one per line,
 * with a link the text already holds as a whole word and a title that repeats
 * the text or the link left out (a phone browser puts the link inside `text`).
 * A link that only starts like a longer link in the text is kept. Empty when
 * the share carried nothing but blanks.
 */
export function sharedComposerText(search: string): string {
  const params = new URLSearchParams(search)
  const title = (params.get('title') ?? '').trim()
  const text = (params.get('text') ?? '').trim()
  const link = (params.get('url') ?? '').trim()
  const lines: string[] = []
  if (title && title !== text && title !== link) lines.push(title)
  if (text) lines.push(text)
  if (link && !hasWord(text, link)) lines.push(link)
  return lines.join('\n')
}

/**
 * The query of the chat path a share opens (`/chat` + this): a new session,
 * composer prefilled, never sent.
 * `/chat?new=1&prefill=` is the deep link the routed chat already honours.
 * The text is encoded as one query value, so shared text that looks like other
 * parameters stays text.
 */
export function shareTargetChatQuery(search: string): string {
  const prefill = sharedComposerText(search)
  if (!prefill) return '?new=1'
  return '?' + new URLSearchParams({ new: '1', prefill }).toString()
}
