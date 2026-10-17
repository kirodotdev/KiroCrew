import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'

import { copyToClipboard } from '../../utils/clipboard'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'

/**
 * A shell command rendered as a click-to-copy block.
 *
 * Shared by first-run setup (`KiroPrerequisiteGate`) and Settings > Agent
 * Harness, so an install command looks and behaves the same in both places.
 * The catalog keys keep the gate's namespace: the block was extracted from it,
 * and a key rename across every locale buys nothing a reader would see.
 *
 * The whole block is the target rather than a small trailing glyph: this command
 * has to be retyped on the gateway host, and one typo restarts the loop the user
 * is already stuck in. The glyph uses the muted token at full weight, not faded
 * or hover-only, because a recovery screen is the wrong place to hide an affordance.
 *
 * The text is read back out of the DOM rather than taken as a prop. A command is
 * not translatable copy, and the i18n gate's exemption covers a literal that is
 * lexically a child of `code`/`pre` — passing it as `command="..."` would make it
 * a JSX attribute string and trip the zero-tolerance [added-lines] check.
 */
export function CopyCommand({ children }: { children: ReactNode }) {
  const hostRef = useRef<HTMLSpanElement>(null)
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )
  const handleCopy = async () => {
    const text = hostRef.current?.textContent?.trim() ?? ''
    if (!text) return
    // Both clipboard paths failed (no clipboard API, execCommand denied): say
    // so under the box and leave the glyph alone, rather than announcing a copy
    // that did not happen. The notice stays until a copy succeeds.
    if (!(await copyToClipboard(text))) {
      setCopyFailed(true)
      return
    }
    setCopyFailed(false)
    setCopied(true)
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setCopied(false), 1500)
  }
  const label = copied
    ? i18nT('components.kiroPrerequisiteGate.copied')
    : i18nT('components.kiroPrerequisiteGate.copy_command')
  return (
    <>
      <button
        type="button"
        onClick={handleCopy}
        aria-label={label}
        title={label}
        className="group/cmd mt-1 flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg border-none bg-bg-elevated px-2 py-1.5 text-left hover:bg-bg-hover focus-ring"
      >
        <span
          ref={hostRef}
          className="min-w-0 overflow-x-auto text-xs text-text-strong [&_code]:font-mono"
        >
          {children}
        </span>
        {copied ? (
          <Check className="lucide-inline shrink-0 text-ok" />
        ) : (
          <Copy className="lucide-inline shrink-0 text-muted" />
        )}
      </button>
      {/* No hand-off: on the setup gate this stands between the user and the
          chat the hand-off would open, and in Settings the remedy is on screen
          either way: select the text and copy it. */}
      {copyFailed && (
        <ErrorNotice
          variant="inline"
          className="mt-1"
          message={i18nT('components.kiroPrerequisiteGate.copy_failed')}
          testId="kiro-gate-copy-failed"
        />
      )}
    </>
  )
}
