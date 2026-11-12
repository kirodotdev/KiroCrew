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
