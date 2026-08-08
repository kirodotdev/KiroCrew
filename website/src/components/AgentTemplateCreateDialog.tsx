import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Btn, SendBtn, Input } from './ui'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody, DialogFooter } from './ui/dialog'
import SimpleSelect from './SimpleSelect'
import InfoTip from './InfoTip'

import { i18nT } from '../i18n/t'
/**
 * The one authoring surface for a kiro agent template (`~/.kiro/agents/<name>.json`).
 *
 * Both entry points share it: "New template" opens it with no source, and
 * "Duplicate" opens it with `initialFrom` set. They differ only in that one
 * field, and a second component for the copy case would have to keep the same
 * name validation, the same conflict messages, and the same tool-surface
 * explainer in sync.
 *
 * The dialog deliberately does NOT offer `tools` / `allowedTools` /
 * `deniedCommands`. Those are the privilege surface — the auto-approve list and
 * the bash deny patterns — and the create endpoint refuses them from a request
 * body for that reason. A blank template gets a read-only auto-approve baseline;
 * copying a template inherits whatever that one was already trusted with.
 */
export default function AgentTemplateCreateDialog({
  open,
  templates,
  initialFrom,
  onClose,
  onCreated,
}: {
  open: boolean
  /** Names of installed templates offered as a starting point. */
  templates: string[]
  /** Preselected source, for the Duplicate entry point. */
  initialFrom?: string
  onClose: () => void
  onCreated: (name: string) => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [prompt, setPrompt] = useState('')
  const [from, setFrom] = useState(initialFrom || '')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Reset on each open so a dismissed attempt does not resurface half-typed,
  // and so Duplicate always reflects the row the user actually clicked.
  useEffect(() => {
    if (!open) return
    setName(initialFrom ? `${initialFrom}-copy` : '')
    setDescription('')
    setPrompt('')
    setFrom(initialFrom || '')
    setError('')
    setSubmitting(false)
  }, [open, initialFrom])

  const submit = async () => {
    setError('')
    const n = name.trim()
    if (!n) { setError(i18nT('components.agentTemplateCreateDialog.name_is_required')); return }
    setSubmitting(true)
    try {
      const body: Record<string, string> = { name: n }
      if (description.trim()) body.description = description.trim()
      if (prompt.trim()) body.prompt = prompt
      if (from) body.from = from
      const r: { ok?: boolean; name?: string; error?: string } = await api.agentCreate(body)
      // The server owns the rules the form cannot check (reserved names, a name
      // already claimed by a package spec), so its message is shown verbatim
      // rather than replaced with a guess.
      if (r.error) { setError(r.error); setSubmitting(false); return }
      onCreated(r.name || n)
    } catch (e) {
      setError(e instanceof Error ? e.message : i18nT('components.agentTemplateCreateDialog.failed_to_create_template'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent maxWidth={520} aria-label={i18nT('components.agentTemplateCreateDialog.new_agent_template')}>
        <DialogHeader>
          <DialogTitle>{initialFrom ? i18nT('components.agentTemplateCreateDialog.duplicate_agent_template') : i18nT('components.agentTemplateCreateDialog.new_agent_template')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-3.5">
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-1">
                {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
                {/* eslint-disable-next-line jsx-a11y/label-has-for */}
                <label htmlFor="tpl-name" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreateDialog.name')}</label>
                <InfoTip text={i18nT('components.agentTemplateCreateDialog.also_the_config_filename_and_the_value_passed_to')} />
              </div>
              <Input id="tpl-name" placeholder={i18nT('components.agentTemplateCreateDialog.e_g_researcher')} value={name} onChange={e => setName(e.target.value)} autoFocus />
            </div>
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-1">
                <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreateDialog.start_from')}</span>
                <InfoTip text={i18nT('components.agentTemplateCreateDialog.copies_an_existing_template_s_tools_mcp_servers')} />
              </div>
              <SimpleSelect
                options={templates}
                value={from}
                onChange={setFrom}
                clearLabel={i18nT('components.agentTemplateCreateDialog.blank_template')}
                triggerFallback={i18nT('components.agentTemplateCreateDialog.blank_template')}
                aria-label={i18nT('components.agentTemplateCreateDialog.start_from_template')}
              />
            </div>
            <div className="flex flex-col gap-1">
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
              <label htmlFor="tpl-desc" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreateDialog.description')}</label>
              <Input id="tpl-desc" placeholder={i18nT('components.agentTemplateCreateDialog.what_this_agent_is_for')} value={description} onChange={e => setDescription(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-1">
                {/* eslint-disable-next-line jsx-a11y/label-has-for */}
                <label htmlFor="tpl-prompt" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreateDialog.system_prompt')}</label>
                <InfoTip text={i18nT('components.agentTemplateCreateDialog.leave_empty_to_keep_the_copied_template_s_prompt')} />
              </div>
              <textarea
                id="tpl-prompt"
                className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2.5 text-text text-[13px] font-mono outline-none transition-colors focus-ring resize-y min-h-[110px]"
                rows={6}
                placeholder={i18nT('components.agentTemplateCreateDialog.you_are_a_research_assistant')}
                value={prompt}
                onChange={e => setPrompt(e.target.value)}
              />
            </div>
            {error && <div className="text-danger text-[13px]" role="alert">{error}</div>}
          </div>
        </DialogBody>
        <DialogFooter>
          <Btn onClick={onClose}>{i18nT('components.agentTemplateCreateDialog.cancel')}</Btn>
          <SendBtn onClick={submit} disabled={submitting}>{submitting ? i18nT('components.agentTemplateCreateDialog.creating') : i18nT('components.agentTemplateCreateDialog.create_template')}</SendBtn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
