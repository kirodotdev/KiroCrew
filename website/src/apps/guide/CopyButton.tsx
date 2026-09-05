// A copy-to-clipboard button with a transient "Copied" confirmation.
import { useCallback, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Copy, Check } from 'lucide-react'

export default function CopyButton({
  text,
  className,
  ariaLabel,
}: {
  text: string
  className?: string
  ariaLabel?: string
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const onClick = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      return
    }
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1500)
  }, [text])

  return (
    <button
      type="button"
      onClick={() => void onClick()}
      aria-label={ariaLabel || t('apps.guide.copy')}
      className={
        'inline-flex items-center gap-1 text-xs rounded px-2 py-1 focus-visible:ring-1 focus-visible:ring-[var(--accent)] ' +
        (className || '')
      }
      style={{ color: 'var(--muted)', border: '1px solid var(--border)', background: 'var(--card)' }}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
      <span>{copied ? t('apps.guide.copied') : t('apps.guide.copy')}</span>
    </button>
  )
}
