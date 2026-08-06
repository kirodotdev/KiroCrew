/**
 * Inline copy button for CLI command text in SettingRef popovers.
 * Reuses the shared clipboard utility and follows the copy/check icon
 * pattern established by MonacoCodeBlock and DiffBlock.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Copy, X } from 'lucide-react'
import { copyCode } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'

type CopyState = 'idle' | 'copied' | 'failed'

export function CopyCommandButton({ text }: { text: string }) {
  const [state, setState] = useState<CopyState>('idle')
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )

  const handleCopy = useCallback(async () => {
    try {
      await copyCode(text)
      setState('copied')
    } catch {
      setState('failed')
    }
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setState('idle'), 1500)
  }, [text])

  const title =
    state === 'copied'
      ? i18nT('components.settingRef.copied')
      : state === 'failed'
        ? i18nT('components.settingRef.copyFailed')
        : i18nT('components.settingRef.copyCommand')

  return (
    <button
      type="button"
      className="p-0.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer shrink-0"
      onClick={handleCopy}
      title={title}
      aria-label={title}
      aria-live="polite"
    >
      {state === 'copied' ? <Check size={11} /> : state === 'failed' ? <X size={11} /> : <Copy size={11} />}
    </button>
  )
}
