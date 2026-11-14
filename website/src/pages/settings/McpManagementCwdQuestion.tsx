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
 * Kiro Crew's own: does it work on the files of the project a chat is in? A
 * shared server runs in one fixed folder outside the project, so only "No"
 * shares it. "Not sure" is answered as "Yes", the safe choice: the server keeps
 * running inside each chat. The answer is stored per server
 * (`mcp_gateway.cwd_use`).
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
  const choices: Array<{ answer: McpCwdUse; label: string; hint: string }> = [
    {
      answer: 'uses_cwd',
      label: i18nT('pages.mcpManagement.cwd_question_yes'),
      hint: i18nT('pages.mcpManagement.cwd_question_yes_hint'),
    },
    {
      answer: 'not_used',
      label: i18nT('pages.mcpManagement.cwd_question_no'),
      hint: i18nT('pages.mcpManagement.cwd_question_no_hint'),
    },
    {
      answer: 'uses_cwd',
      label: i18nT('pages.mcpManagement.cwd_question_unsure'),
      hint: i18nT('pages.mcpManagement.cwd_question_unsure_hint'),
    },
  ]
  return (
    <Dialog
      open={name !== null}
      onOpenChange={next => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent maxWidth={520}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.mcpManagement.cwd_question_title', { name: name ?? '' })}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <DialogDescription>{i18nT('pages.mcpManagement.cwd_question_help')}</DialogDescription>
          <p className="mt-2 text-[13px] text-muted">{i18nT('pages.mcpManagement.cwd_question_why')}</p>
          <div className="mt-3 flex flex-col gap-2">
            {choices.map(c => (
              <button
                key={c.label}
                type="button"
                disabled={busy}
                onClick={() => onAnswer(c.answer)}
                className="rounded-md border border-border px-3.5 py-2 text-left text-[13.5px] text-text hover:bg-[var(--hover)] disabled:opacity-60"
              >
                <span className="block font-medium">{c.label}</span>
                <span className="block text-[12.5px] text-muted">{c.hint}</span>
              </button>
            ))}
          </div>
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
