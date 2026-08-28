/**
 * Pieces both AWS Control surfaces render: the per-account console and the cloud
 * drive page.
 *
 * They live here rather than in either surface because importing across the two
 * would be circular - the console navigates INTO the drive page, so the drive
 * page cannot import from the console.
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import { Copy, Check } from 'lucide-react'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/** Copy-to-clipboard button that flips to a check for ~1.5s. */
export function CopyBtn({ text, testId, ariaLabel }: { text: string; testId?: string; ariaLabel?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable — the text is still selectable by hand */ }
  }
  return (
    <Btn onClick={copy} data-testid={testId} aria-label={ariaLabel}>
      {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
      {copied ? i18nT('apps.awsControl.console.copied') : i18nT('apps.awsControl.console.copy')}
    </Btn>
  )
}

/* ── shared section header ───────────────────────────────────────────────── */

export function SectionHeader({ icon, title, actions }: { icon: ReactNode; title: string; actions?: ReactNode }) {
  return (
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h2 className="flex items-center gap-1.5 text-sm font-semibold text-text-strong">
        <span className="text-accent">{icon}</span>
        {title}
      </h2>
      {actions}
    </div>
  )
}
