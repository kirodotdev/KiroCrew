import { MessagesSquare, X } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'
import type { SessionRef } from '../utils/sessionRefs'

/**
 * Staged session references, rendered in the composer as chips.
 *
 * Deliberately mirrors `FilePreviewStrip`'s container geometry (same padding,
 * top border, chrome tint, single scrolling row) so a staged session reads as
 * the same class of thing as a staged attachment rather than a second,
 * differently-styled mechanism. Like attachments, the chip is a real element
 * ABOVE the textarea — not a token painted inside it. A `<textarea>` cannot
 * render styled inline children, which is why the collapsed-paste chip has to
 * fake itself with a transparent mirror layer; a chip strip has no such limit
 * and can carry an icon and a secondary line.
 *
 * Height must stay in sync with `SESSION_REF_STRIP_H` in ChatInput, which the
 * manual-resize floor adds when refs are staged.
 */
export default function SessionRefStrip({ refs, onRemove }: {
  refs: SessionRef[]
  onRemove?: (key: string) => void
}) {
  if (!refs.length) return null
  return (
    // NOTE: rendered height must match SESSION_REF_STRIP_H, update both together
    <div
      className="flex gap-2 px-5 py-2 border-t border-border bg-chrome/50 overflow-x-auto items-center"
      data-testid="session-ref-strip"
    >
      {refs.map(ref => (
        <div
          key={ref.key}
          data-testid="session-ref-chip"
          data-session-ref={ref.key}
          className="relative group/sessref shrink-0 flex items-center gap-1.5 max-w-[320px] px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text"
          title={ref.title || ref.key}
        >
          <MessagesSquare size={13} className="shrink-0 text-accent" aria-hidden="true" />
          <span className="truncate">{ref.title || ref.key}</span>
          {ref.messages !== undefined && (
            <span className="shrink-0 text-muted tabular-nums">
              {/* `n`, not `count`: `count` is i18next's reserved plural selector
                  and would send this key through plural resolution it is not
                  registered for. The abbreviated unit needs no plural form. */}
              {i18nT('components.sessionRefStrip.n_messages', { n: fmtNumber(ref.messages) })}
            </span>
          )}
          {onRemove && (
            <button
              type="button"
              className="shrink-0 text-muted hover:text-danger cursor-pointer bg-transparent border-none p-0"
              onClick={() => onRemove(ref.key)}
              title={i18nT('components.sessionRefStrip.remove_reference')}
              aria-label={i18nT('components.sessionRefStrip.remove_reference_to', { name: ref.title || ref.key })}
            >
              <X size={12} />
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
