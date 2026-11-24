import { useRef } from 'react'

import type { McpCwdUse } from '../../api/client'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog'
import { i18nT } from '../../i18n/t'

/**
 * The one question MCP Management asks before it shares a server that is not
 * Kiro Crew's own: can it run in one folder for every chat? A shared server runs
 * in one fixed folder outside the project, so only "Share across chats" shares
 * it; "Keep one per chat" leaves the server running inside each chat. The answer
 * is stored per server (`mcp_gateway.cwd_use`).
 */
export function CwdQuestionDialog({
  name,
  busy,
  onAnswer,
  onCancel,
}: {
  /** The server asked about; null keeps the dialog closed. */
  name: string | null
  busy: boolean
  onAnswer: (answer: McpCwdUse) => void
  onCancel: () => void
}) {
  // Opening focus goes to the safe choice, so Enter never shares by accident.
  const keepRef = useRef<HTMLButtonElement>(null)
  const choices: Array<{ answer: McpCwdUse; label: string }> = [
    { answer: 'not_used', label: i18nT('pages.mcpManagement.cwd_question_share') },
    { answer: 'uses_cwd', label: i18nT('pages.mcpManagement.cwd_question_keep') },
  ]
  return (
    <Dialog
      open={name !== null}
      onOpenChange={next => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent
        maxWidth={520}
        onOpenAutoFocus={e => {
          e.preventDefault()
          keepRef.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle>{i18nT('pages.mcpManagement.cwd_question_title', { name: name ?? '' })}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <DialogDescription>{i18nT('pages.mcpManagement.cwd_question_help')}</DialogDescription>
          <div className="mt-3 flex flex-col gap-2">
            {choices.map(c => (
              <button
                key={c.label}
                ref={c.answer === 'uses_cwd' ? keepRef : undefined}
                type="button"
                disabled={busy}
                onClick={() => onAnswer(c.answer)}
                className="rounded-md border border-border px-3.5 py-2 text-left text-[13.5px] text-text hover:bg-[var(--hover)] disabled:opacity-60"
              >
                <span className="block font-medium">{c.label}</span>
              </button>
            ))}
          </div>
          <p className="mt-2 text-[12.5px] text-muted">{i18nT('pages.mcpManagement.cwd_question_not_sure')}</p>
        </DialogBody>
        <DialogFooter>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-md border border-border px-3.5 py-2 text-[13.5px] text-text"
          >
            {i18nT('pages.mcpManagement.cancel')}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
