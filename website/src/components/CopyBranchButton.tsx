import { useEffect, useRef, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'

/** A branch name rendered as a click-to-copy affordance. Clicking copies the raw
 * branch name to the clipboard via the shared {@link copyToClipboard} helper
 * (which falls back to a hidden textarea + execCommand on non-secure origins
 * where navigator.clipboard is absent) and briefly swaps the trailing copy glyph
 * for a check. If the copy genuinely fails, the failure is swallowed and the
 * label stays put rather than falsely announcing success.
 *
 * Shared by the pull-request panel header and the composer's project chip.
 * Extracted to its own module so the composer does not have to import the
 * pull-request panel (and with it DOMPurify / hljs / the diff parser) just to
 * reuse one button.
 *
 * ``label`` overrides the accessible name's noun for callers where "branch" is
 * not accurate — the project chip shows a short commit on a detached HEAD.
 */
export default function CopyBranchButton({
  branch,
  label = 'branch name',
  className = '',
}: {
  branch: string
  label?: string
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )
  const handleCopy = async () => {
    try {
      await copyToClipboard(branch)
    } catch {
      // Both clipboard paths failed (no clipboard API and execCommand denied):
      // leave the label as-is rather than announcing a copy that did not happen.
      return
    }
    setCopied(true)
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setCopied(false), 1500)
  }
  return (
    <button
      type="button"
      onClick={handleCopy}
      className={`group/branch min-w-0 inline-flex items-center gap-1 truncate rounded px-1 -mx-1 border-none bg-transparent text-inherit hover:bg-bg-hover cursor-pointer ${className}`}
      aria-label={copied ? i18nT('components.copyBranchButton.copied', { label, branch }) : i18nT('components.copyBranchButton.copy', { label, branch })}
      title={copied ? i18nT('components.copyBranchButton.copied_2') : i18nT('components.copyBranchButton.copy_2', { label })}
    >
      <span className="truncate">{branch}</span>
      {copied ? (
        <Check className="lucide-inline shrink-0 text-ok" />
      ) : (
        <Copy className="lucide-inline shrink-0 opacity-0 group-hover/branch:opacity-70 transition-opacity" />
      )}
    </button>
  )
}
