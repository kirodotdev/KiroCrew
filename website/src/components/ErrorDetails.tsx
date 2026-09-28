// ErrorDetails — the collapsed half of a two-audience error (issue #12816).
//
// Deploy refusals used to put everything in one red sentence: "Finite-TTL
// deploys require the reaper base stack (kirocrew-deploy-base). Use ttl_hours=0
// for persistent or install the reaper (install-reaper.sh)." Three product nouns
// and a parameter name, in the one place a user cannot avoid reading, none of it
// meaning anything to someone who just clicked Deploy.
//
// The backend now splits that into `error` (the banner sentence) and `details`
// (stack, field and directory names) plus an optional `remediation` command.
// This renders the second half: collapsed by default, one click away, with the
// command copyable — so an operator loses nothing while the banner stays
// readable. Renders nothing at all when there is no detail to show, so a caller
// can drop it in unconditionally.
import { useState } from 'react'
import { Check, ChevronDown, ChevronRight, Copy } from 'lucide-react'
import { Btn } from './ui'
import Clickable from './Clickable'
import { copyCode } from '../utils/clipboard'

import { i18nT } from '../i18n/t'

export default function ErrorDetails({
  details,
  remediation,
  className,
}: {
  /** Technical explanation — stack names, field names, resolved paths. */
  details?: string
  /** A runnable command, shown in a monospace row with its own Copy button. */
  remediation?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  if (!details && !remediation) return null
  return (
    <div className={className}>
      <Clickable
        className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-text cursor-pointer"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {i18nT('pages.artifactDeployPage.details')}
      </Clickable>
      {open && (
        <div className="mt-1.5 flex flex-col gap-1.5">
          {details && (
            <div className="text-[11px] leading-relaxed text-muted whitespace-pre-line">
              {details}
            </div>
          )}
          {remediation && (
            <div
              className="flex items-center justify-between gap-2 rounded border border-border bg-bg px-2 py-1.5"
              style={{ fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 11 }}
            >
              <code style={{ overflow: 'auto', whiteSpace: 'nowrap' }}>{remediation}</code>
              <Btn onClick={async () => {
                if (await copyCode(remediation)) {
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1500)
                }
              }}>
                {copied ? <Check size={11} className="text-ok" /> : <Copy size={11} />}
                {' '}{i18nT('pages.artifactDeployPage.copy')}
              </Btn>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
