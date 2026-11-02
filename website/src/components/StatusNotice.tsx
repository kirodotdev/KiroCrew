import { Info, TriangleAlert, X } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/**
 * The non-error sibling of `ErrorNotice`'s block variant: the same boxed shape,
 * announced as a polite `role="status"` in the warn / info tokens instead of an
 * alert in danger. For a notice where nothing failed and nothing was lost (an
 * answer handed back to the composer, a message that did send). A failure the
 * user must act on stays an `ErrorNotice`; this is never a quieter error.
 */
export default function StatusNotice({
  message,
  tone = 'warn',
  onDismiss,
  className = '',
  testId,
  action,
}: {
  /** Human notice text. Falsy renders nothing. */
  message?: string | null
  tone?: 'warn' | 'info'
  onDismiss?: () => void
  className?: string
  testId?: string
  /** The one remedy for the state this notice reports, drawn INSIDE the box
   *  under the message so it stays next to what it fixes. */
  action?: { label: string; onClick: () => void; testId?: string }
}) {
  // Rendered by React-Compiler files (ChatPaneNotices), whose cached elements
  // are a memo boundary: subscribe so the dismiss label follows a language switch.
  useLanguageGeneration()
  if (!message) return null
  const dismissName = i18nT('components.errorNotice.dismiss')
  const Icon = tone === 'warn' ? TriangleAlert : Info
  return (
    <div
      role="status"
      data-testid={testId}
      data-tone={tone}
      className={`rounded-lg border px-3 py-2 flex items-start gap-2 text-[13px] text-text ${
        tone === 'warn' ? 'border-warn/30 bg-warn/10' : 'border-border bg-card'
      } ${className}`}
    >
      <Icon size={14} className={`mt-[2px] shrink-0 ${tone === 'warn' ? 'text-warn' : 'text-muted'}`} aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block whitespace-pre-wrap" style={{ overflowWrap: 'anywhere' }}>{message}</span>
        {action && (
          <button
            type="button"
            className="mt-1 font-medium text-accent hover:text-accent-hover bg-transparent border-none p-0 cursor-pointer underline decoration-accent/30 hover:decoration-accent underline-offset-2 text-left"
            data-testid={action.testId}
            onClick={action.onClick}
          >
            {action.label}
          </button>
        )}
      </span>
      {onDismiss && (
        <button
          type="button"
          className="shrink-0 bg-transparent border-none p-0 cursor-pointer text-muted hover:text-text transition-colors"
          aria-label={dismissName}
          title={dismissName}
          onClick={onDismiss}
        >
          <X size={13} aria-hidden="true" />
        </button>
      )}
    </div>
  )
}
