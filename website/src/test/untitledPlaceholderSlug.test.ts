/**
 * Test: the backend's English placeholder for an open untitled slot
 * ("New Session…") is no title in the session's URLs and short-name matches.
 *
 * `isUntitledSession` already treats it as none for the sidebar rows; these
 * helpers compared the title with the key only, so an untitled session's share
 * link and popout URL read `/chat/new-session`, and its short-name match was
 * titled with the English text, in every language.
 */
import { describe, expect, it } from 'vitest'
import { buildShareableUrl } from '../utils/shareUrl'
import { buildPopoutUrl } from '../utils/chatPopout'
import { shortNameMatches } from '../utils/sessionKeys'

const KEY = 'chat-1-1792348860'

describe('the untitled placeholder in session URLs and short names', () => {
  it.each(['New Session…', 'New Session...'])('leaves %s out of the share link slug', placeholder => {
    expect(new URL(buildShareableUrl(KEY, placeholder)).pathname).toBe('/chat')
  })

  it('leaves the placeholder out of the popout URL slug', () => {
    expect(new URL(buildPopoutUrl(KEY, 'New Session…')).pathname).toBe('/popout/chat')
  })

  it('still slugs a real title', () => {
    expect(new URL(buildShareableUrl(KEY, 'Plan the release')).pathname).toBe('/chat/plan-the-release')
    expect(new URL(buildPopoutUrl(KEY, 'Plan the release')).pathname).toBe('/popout/chat/plan-the-release')
  })

  it('titles an open untitled session by its key in a short-name match', () => {
    const [match] = shortNameMatches('chat-1', [{ key: KEY, title: 'New Session…' }], [])
    expect(match?.title).toBe(KEY)
  })
})
