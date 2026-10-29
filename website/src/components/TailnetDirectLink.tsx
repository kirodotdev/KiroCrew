/** The plain share link of the direct tailnet listener.
 *
 *  Renders only while the gateway reports `tailnet_direct.state === 'active'`.
 *  The link carries no token. Tailscale access rules decide who can reach it;
 *  with local authorization on, the gateway also checks its access set.
 *  The first URL is the MagicDNS name; the rest are the
 *  machine's tailnet addresses, as a fallback when MagicDNS is off. */
import { useState } from 'react'
import { Check, Copy, Link2 } from 'lucide-react'
import type { TailnetDirect } from '../api/sharedCrew'
import { copyToClipboard } from '../utils/clipboard'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { i18nT } from '../i18n/t'

export function tailnetDirectActive(direct: TailnetDirect | undefined): direct is TailnetDirect {
  return !!direct && direct.state === 'active' && direct.urls.length > 0
}

function CopyUrl({ url, askAgent }: { url: string; askAgent: boolean }) {
  const [copied, setCopied] = useState(false)
  const [failed, setFailed] = useState(false)
  const copy = async () => {
    let ok = false
    try {
      ok = await copyToClipboard(url)
    } catch {
      ok = false
    }
    setCopied(ok)
    setFailed(!ok)
    if (ok) setTimeout(() => setCopied(false), 2000)
  }
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <div className="flex items-center gap-2 min-w-0">
        <input
          readOnly
          value={url}
          onFocus={e => e.currentTarget.select()}
          aria-label={i18nT('sharedCrew.direct_link_label')}
          className="flex-1 min-w-0 bg-bg border border-border rounded-md text-text text-[12px] px-2.5 py-2 font-mono overflow-hidden text-ellipsis"
        />
        <Btn onClick={copy} className="whitespace-nowrap" aria-label={i18nT('sharedCrew.direct_link_copy')}>
          {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {i18nT('sharedCrew.direct_link_copy')}
        </Btn>
      </div>
      {failed && (
        <ErrorNotice
          variant="inline"
          message={i18nT('sharedCrew.direct_link_copy_failed')}
          askAgent={askAgent}
          testId="direct-link-copy-error"
        />
      )}
    </div>
  )
}

/** `askAgent` offers the agent hand-off on a copy failure. A host that keeps
 *  unsaved drafts beside the link passes false, since the hand-off leaves it. */
export function TailnetDirectLink({ direct, askAgent = true }: { direct: TailnetDirect | undefined; askAgent?: boolean }) {
  if (!tailnetDirectActive(direct)) return null
  const [primary, ...fallbacks] = direct.urls
  return (
    <div className="flex flex-col gap-2" data-testid="tailnet-direct-link">
      <div className="flex items-center gap-2 text-[13px] font-medium text-text-strong">
        <Link2 size={14} className="lucide-inline shrink-0" aria-hidden />
        {i18nT('sharedCrew.direct_link_title')}
      </div>
      <CopyUrl url={primary} askAgent={askAgent} />
      {fallbacks.length > 0 && (
        <details className="text-[12px] text-muted">
          <summary className="cursor-pointer">{i18nT('sharedCrew.direct_link_fallbacks')}</summary>
          <div className="flex flex-col gap-2 pt-2">
            {fallbacks.map(url => <CopyUrl key={url} url={url} askAgent={askAgent} />)}
          </div>
        </details>
      )}
      <p className="text-[12px] text-muted">{i18nT('sharedCrew.direct_link_note')}</p>
    </div>
  )
}
