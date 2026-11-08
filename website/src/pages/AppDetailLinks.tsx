/**
 * The app's own `links` (from its app.json) as buttons in the Details card.
 *
 * The gateway already keeps only valid entries (`apps/store_links.py`), but
 * the row can come from an older gateway or an external index, so the same
 * rule is applied again here before anything becomes an `href`: https only, a
 * plain host, no userinfo, no non-default port. Each button shows the URL's
 * host under its label, which is how the reader sees where it goes.
 */
import { ExternalLink } from 'lucide-react'
import { safeHttpUrl } from '../lib/safeUrl'

export interface AppLink {
  label: string
  url: string
}

const MAX_LINKS = 6
const MAX_LABEL_CHARS = 40
const MAX_URL_CHARS = 2048

/** The usable entries of an untrusted `links` value, each paired with its host. */
export function usableAppLinks(raw: unknown): (AppLink & { host: string })[] {
  if (!Array.isArray(raw)) return []
  const out: (AppLink & { host: string })[] = []
  for (const entry of raw) {
    if (out.length >= MAX_LINKS) break
    if (!entry || typeof entry !== 'object') continue
    const { label, url } = entry as { label?: unknown; url?: unknown }
    if (typeof label !== 'string' || typeof url !== 'string') continue
    const text = label.trim()
    if (!text || text.length > MAX_LABEL_CHARS || url.length > MAX_URL_CHARS) continue
    try {
      const u = new URL(url)
      if (u.protocol !== 'https:' || u.username || u.password || u.port || !u.hostname) continue
      out.push({ label: text, url, host: u.hostname })
    } catch {
      continue
    }
  }
  return out
}

/**
 * The Details card's source-repo and issue-tracker links. `repo` is third-party
 * manifest or registry input, and it is the value the card already displays, so
 * a link's text and its href come from the same string. Otherwise the text could
 * name one repo while the click opened another, because `repoUrl` may come from
 * manifest.repo, trustRepository or source rather than app.repo. A link is made
 * only from a string that parses as an http(s) URL with no userinfo:
 * safeHttpUrl rejects javascript:, data: and credential-smuggling forms. The
 * `typeof` guard comes first, because an entry can ship `"repo": ["…"]`, which
 * safeHttpUrl would coerce to validate and then return as the array itself, and
 * `.replace` on that array would throw and blank the whole detail page.
 * Anything else stays plain text. The tracker is a GitHub repo's /issues page,
 * the only host whose tracker path this knows. It is a plain outbound link,
 * never an in-app flow that files on the user's behalf, which would need new
 * manifest metadata and a credential decision the maintainer owns.
 */
export function appRepoLinks(repo: unknown): { repoUrl: string | null; issuesUrl: string } {
  const repoUrl = typeof repo === 'string' && repo ? safeHttpUrl(repo) : null
  let host = ''
  try { host = repoUrl ? new URL(repoUrl).hostname : '' } catch { host = '' }
  const issuesUrl = repoUrl && host === 'github.com'
    ? `${repoUrl.replace(/\/+$/, '').replace(/\.git$/, '')}/issues`
    : ''
  return { repoUrl, issuesUrl }
}

export default function AppDetailLinks({ links }: { links: unknown }) {
  const usable = usableAppLinks(links)
  if (usable.length === 0) return null
  return (
    <div className="flex items-center gap-x-3 gap-y-1.5 flex-wrap" data-testid="app-detail-links">
      {usable.map((link, i) => (
        <a
          key={`${i}-${link.url}`}
          href={link.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex flex-col items-start text-accent no-underline hover:underline focus-visible:underline"
        >
          <span className="inline-flex items-center gap-1">
            {link.label}
            <ExternalLink size={12} className="shrink-0" aria-hidden="true" />
          </span>
          <span className="text-[11px] text-muted break-all">{link.host}</span>
        </a>
      ))}
    </div>
  )
}
