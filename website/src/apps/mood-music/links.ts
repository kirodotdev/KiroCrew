/**
 * The two song links Mood Music plays, and nothing else.
 *
 * Mirrors `src/kiro_crew/apps/builtins/mood_music/backend/links.py`: the page
 * drops a pick the backend would refuse, so one bad link from the agent loses
 * only that song. A YouTube pick keeps only its 11-character video id, and the
 * embed URL is built here on the one host the dashboard CSP frames. A bilibili
 * pick must be https on an exact bilibili host.
 */

export const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/

export const BILIBILI_HOSTS: ReadonlySet<string> = new Set(['www.bilibili.com', 'm.bilibili.com', 'b23.tv'])

const YOUTUBE_WATCH_HOSTS: ReadonlySet<string> = new Set(['www.youtube.com', 'youtube.com', 'm.youtube.com', 'music.youtube.com'])

const MAX_URL = 2048

/** The embed origin and path the dashboard CSP `frame-src` admits. */
export const YOUTUBE_EMBED_BASE = 'https://www.youtube-nocookie.com/embed/'

export function validVideoId(value: unknown): string | null {
  return typeof value === 'string' && VIDEO_ID_RE.test(value) ? value : null
}

/** A plain https URL: no user info, no port, no character a browser reads differently. */
function parseHttps(raw: unknown): URL | null {
  if (typeof raw !== 'string' || raw.length > MAX_URL) return null
  if (/[\\\u0000-\u0020\u007f]/.test(raw)) return null
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return null
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) return null
  return url
}

/** The video id a YouTube watch or share link names, or null. */
export function youtubeVideoId(raw: unknown): string | null {
  const url = parseHttps(raw)
  if (!url) return null
  if (url.hostname === 'youtu.be') return validVideoId(url.pathname.slice(1))
  if (YOUTUBE_WATCH_HOSTS.has(url.hostname) && url.pathname === '/watch') {
    const ids = url.searchParams.getAll('v')
    return ids.length === 1 ? validVideoId(ids[0]) : null
  }
  return null
}

/** The link rebuilt from its parts when it is an https bilibili link, else null. */
export function bilibiliUrl(raw: unknown): string | null {
  const url = parseHttps(raw)
  if (!url || !BILIBILI_HOSTS.has(url.hostname)) return null
  return `https://${url.hostname}${url.pathname}${url.search}`
}

/** The privacy-enhanced embed URL for a checked id; null for anything else. */
export function youtubeEmbedUrl(videoId: unknown, autoplay: boolean): string | null {
  const id = validVideoId(videoId)
  if (!id) return null
  return `${YOUTUBE_EMBED_BASE}${id}?rel=0${autoplay ? '&autoplay=1' : ''}`
}
