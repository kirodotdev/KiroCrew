import { useEffect, useRef } from 'react'
import { SquareTerminal, ShieldAlert } from 'lucide-react'
import Modal from './Modal'

import { i18nT } from '../i18n/t'

/**
 * Confirmation dialog shown before a code block's command is sent to a terminal.
 *
 * The code block itself renders inside a horizontally scrolling <pre>, so a long
 * line can be visually clipped — the user may never have seen the tail of the
 * command they are about to run. This dialog re-renders the exact string that
 * will be sent, soft-wrapped so nothing is off-screen, and requires an explicit
 * confirmation.
 */
export default function RunInTerminalConfirm(
  { open, command, warnReason, onConfirm, onCancel }: {
    open: boolean
    /** The exact string that will be sent to the terminal (prompt chars already stripped). */
    command: string
    /** Non-empty when the command tripped a sensitive-command pattern. */
    warnReason?: string
    onConfirm: () => void
    onCancel: () => void
  },
) {
  const lines = command.split('\n')
  const sensitive = !!warnReason
  const confirmRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)

  // Focus the safe choice for a flagged command, the primary action otherwise —
  // so a stray Enter can never confirm a sensitive command.
  useEffect(() => {
    if (!open) return
    const t = setTimeout(() => (sensitive ? cancelRef : confirmRef).current?.focus(), 0)
    return () => clearTimeout(t)
  }, [open, sensitive])

  return (
    <Modal
      open={open}
      onClose={onCancel}
      maxWidth={560}
      title={
        <span className="inline-flex items-center gap-2">
          <SquareTerminal size={15} className="text-muted" />
          {i18nT('components.runInTerminalConfirm.title')}
        </span>
      }
      footer={
        <>
          <button
            ref={cancelRef}
            className="px-3 py-1.5 rounded-md text-[13px] text-muted hover:text-text hover:bg-bg-hover border border-border cursor-pointer"
            onClick={onCancel}
          >
            {i18nT('components.runInTerminalConfirm.cancel')}
          </button>
          <button
            ref={confirmRef}
            className={`px-3 py-1.5 rounded-md text-[13px] font-medium border-none cursor-pointer ${
              sensitive
                ? 'bg-warn text-warn-fg hover:bg-warn/90'
                : 'bg-accent text-accent-fg hover:bg-accent-hover'
            }`}
            onClick={onConfirm}
          >
            {sensitive
              ? i18nT('components.runInTerminalConfirm.run_anyway')
              : i18nT('components.runInTerminalConfirm.run')}
          </button>
        </>
      }
    >
      <p className="text-[12px] text-muted mb-2.5">
        {lines.length > 1
          ? i18nT('components.runInTerminalConfirm.body_multi', { lines: lines.length })
          : i18nT('components.runInTerminalConfirm.body_single')}
      </p>

      {sensitive && (
        <div className="flex items-start gap-2 mb-2.5 px-2.5 py-2 rounded-md bg-warn/10 border border-warn/30">
          <ShieldAlert size={14} className="text-warn shrink-0 mt-[1px]" />
          <span className="text-[12px] text-warn">
            {i18nT('components.runInTerminalConfirm.flagged')} {warnReason}
          </span>
        </div>
      )}

      {/* Full command, soft-wrapped: never clipped, never scrolled off to the right. */}
      <div className="rounded-lg border border-border bg-bg-elevated max-h-[40vh] overflow-y-auto">
        {/* break-words, not break-all: wrap at spaces so a token is never split
            mid-word (`rm` rendered as `r` / `m` misreads badly), while a single
            unbreakable long token still breaks rather than overflowing. */}
        <pre className="px-3 py-2 text-[12.5px] font-mono leading-relaxed whitespace-pre-wrap break-words text-text">
          {lines.map((line, i) => (
            <div key={i} className="flex gap-2.5">
              {lines.length > 1 && (
                <span className="shrink-0 select-none text-muted/60 text-right w-4 tabular-nums">{i + 1}</span>
              )}
              <span className="min-w-0 flex-1">{line || '\u00a0'}</span>
            </div>
          ))}
        </pre>
      </div>
    </Modal>
  )
}
