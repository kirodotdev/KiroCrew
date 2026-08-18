/** Simplified task creation — just "What do you want?" */
import { Sparkles, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { i18nT } from '../../../i18n/t'

interface CreateTaskFormProps {
  onSubmit: (prompt: string) => void
  onCancel: () => void
  /** Seed text. Carries a failed submission's prompt back into the form. */
  initialPrompt?: string
}

export function CreateTaskForm({ onSubmit, onCancel, initialPrompt = '' }: CreateTaskFormProps) {
  const [prompt, setPrompt] = useState(initialPrompt)
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  /**
   * Escape and the close button ASK to close; a typed prompt is what decides.
   *
   * The textarea invites a paragraph of natural language, and it exists nowhere
   * else -- so an accidental Escape would destroy the only copy. A failed create
   * hands the prompt back, but a cancel has nothing to hand it back from, which
   * is why this needs the same guard the detail modal uses on its edits.
   */
  const requestClose = useCallback(() => {
    if (prompt.trim()) {
      setConfirmDiscard(true)
      return
    }
    onCancel()
  }, [prompt, onCancel])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') requestClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [requestClose])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!prompt.trim()) return
    onSubmit(prompt.trim())
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-lg bg-bg-elevated border border-border rounded-xl shadow-lg px-4 py-6 md:px-6 space-y-4"
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Sparkles size={16} className="text-accent" />
            <h3 className="text-sm font-semibold text-text-strong">{i18nT('apps.kanban.createTaskForm.new_task')}</h3>
          </div>
          <button
            type="button"
            className="p-1 rounded hover:bg-bg-hover text-muted"
            onClick={requestClose}
            aria-label={i18nT('apps.kanban.createTaskForm.close')}
          >
            <X size={16} />
          </button>
        </div>

        <div>
          <label className="text-xs text-muted block mb-2">{i18nT('apps.kanban.createTaskForm.what_do_you_want')}</label>
          <textarea
            autoFocus
            className="w-full bg-bg border border-border rounded-lg px-4 py-3 text-sm text-text-strong placeholder:text-muted min-h-[120px] resize-y focus:outline-none focus:ring-2 focus:ring-accent/50 focus:border-accent"
            placeholder={i18nT('apps.kanban.createTaskForm.prompt_placeholder')}
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
          />
        </div>

        <p className="text-[11px] text-muted">
          {i18nT('apps.kanban.createTaskForm.auto_generate_note')}
        </p>

        <div className="flex gap-2 pt-1">
          <button
            type="submit"
            disabled={!prompt.trim()}
            className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-lg bg-accent text-accent-fg text-sm font-medium hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <Sparkles size={14} />
            {i18nT('apps.kanban.createTaskForm.create_task')}
          </button>
          <button
            type="button"
            className="px-4 py-2.5 rounded-lg border border-border text-sm text-text hover:bg-bg-hover transition-colors"
            onClick={requestClose}
          >
            {i18nT('apps.kanban.createTaskForm.cancel')}
          </button>
        </div>

        {/* Discard guard, mirroring the detail modal: rendered inside the dialog
            rather than as a native confirm() so it is reachable, themed, and
            cannot be suppressed by a browser that blocks dialogs. */}
        {confirmDiscard && (
          <div
            className="flex items-center gap-3 -mx-4 md:-mx-6 -mb-6 mt-1 px-4 md:px-6 py-3 border-t border-border bg-warn-subtle rounded-b-xl"
            role="alertdialog"
            aria-label={i18nT('apps.kanban.createTaskForm.discard_prompt_title')}
          >
            <span className="flex-1 text-xs text-text">
              {i18nT('apps.kanban.createTaskForm.discard_prompt_body')}
            </span>
            <button
              type="button"
              className="px-3 py-1.5 rounded-md bg-bg-hover text-text text-xs font-medium hover:bg-bg-elevated transition-colors"
              onClick={() => setConfirmDiscard(false)}
            >
              {i18nT('apps.kanban.createTaskForm.keep_editing')}
            </button>
            <button
              type="button"
              className="px-3 py-1.5 rounded-md bg-danger-subtle text-danger text-xs font-medium hover:bg-danger/20 transition-colors"
              onClick={onCancel}
            >
              {i18nT('apps.kanban.createTaskForm.discard')}
            </button>
          </div>
        )}
      </form>
    </div>
  )
}
