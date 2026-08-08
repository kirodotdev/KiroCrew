import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Check, Lock } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import InfoTip from './InfoTip'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'
interface Props {
  /** Agent template name (the `{name}` in `/api/agents/detail/{name}`). */
  agentName: string
  description: string
  /**
   * The spec's `prompt`. A `file://…` value is a reference to a prompt file
   * rather than prose; it is shown as-is so replacing it is a deliberate act.
   */
  prompt: string
  /**
   * True when Kiro Crew owns the spec and rewrites it on install. Served by the
   * backend rather than derived here, so the editor disables exactly the fields
   * PATCH refuses instead of keeping a second copy of the owned-file list.
   */
  managed?: boolean
  /**
   * Called after a successful save with the agent the save was issued FOR. A
   * slow PATCH can resolve after the selection has moved on, and the caller must
   * drop a response that no longer matches what is on screen — otherwise agent
   * A's prompt renders under agent B and the next edit writes it into B's spec.
   */
  onSaved: (agentName: string, patch: { description?: string; prompt?: string }) => void
}

/**
 * Edit an agent template's description and system prompt.
 *
 * Unlike the model picker and skills editor on this page, these two save on an
 * explicit button rather than on change: a prompt is prose that is typed over
 * many keystrokes, and a save-per-keystroke would write a spec kiro-cli reads
 * for every half-finished sentence.
 */
export default function AgentTemplateEditor({ agentName, description, prompt, managed, onSaved }: Props) {
  const [desc, setDesc] = useState(description)
  const [text, setText] = useState(prompt)
  const [error, setError] = useState('')

  // Re-seed when the selection changes OR when a value arrives from the server
  // (the detail fetch resolves after first paint). Keyed on the agent as well as
  // the values so switching to a template whose prompt happens to be identical
  // still resets any unsaved edit.
  useEffect(() => {
    setDesc(description)
    setText(prompt)
    setError('')
  }, [agentName, description, prompt])

  const saveMut = useMutation({
    mutationFn: (patch: { description?: string; prompt?: string }) =>
      api.agentPatch(agentName, patch).then((r: { error?: string }) => {
        // A 400 still resolves the fetch, so the error has to be read off the
        // body rather than left to a rejected promise.
        if (r?.error) throw new Error(r.error)
        return r
      }),
    onMutate: () => setError(''),
    onSuccess: (_r, patch) => onSaved(agentName, patch),
    onError: (e: unknown) => setError(e instanceof Error ? e.message : i18nT('components.agentTemplateEditor.save_failed')),
  })

  const dirtyDesc = desc !== description
  const dirtyPrompt = text !== prompt
  const dirty = dirtyDesc || dirtyPrompt

  const save = () => {
    const patch: { description?: string; prompt?: string } = {}
    if (dirtyDesc) patch.description = desc
    if (dirtyPrompt) patch.prompt = text
    if (Object.keys(patch).length) saveMut.mutate(patch)
  }

  const revert = () => { setDesc(description); setText(prompt); setError('') }

  if (managed) {
    return (
      <div className="mb-3 flex flex-col gap-3">
        {description && <div className="text-[13px] text-muted leading-relaxed">{description}</div>}
        {prompt && (
          <div className="flex flex-col gap-1">
            <div className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.agentTemplateEditor.system_prompt')}</div>
            <pre className="text-[12px] text-text font-mono bg-bg-elevated rounded-md p-2.5 border border-border overflow-x-auto max-h-[160px] overflow-y-auto whitespace-pre-wrap leading-relaxed">{prompt.startsWith('file://') ? prompt : prompt.slice(0, 2000)}</pre>
          </div>
        )}
        <div className="text-[11.5px] text-muted flex items-start gap-1">
          <Lock className="lucide-inline shrink-0" /> <span>{i18nT('components.agentTemplateEditor.kiro_crew_rewrites_this_template_on_every_instal')}</span>
        </div>
      </div>
    )
  }

  return (
    <div className="mb-3 flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1">
          {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
          {/* eslint-disable-next-line jsx-a11y/label-has-for */}
          <label htmlFor="agent-desc" className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.agentTemplateEditor.description')}</label>
          <InfoTip text={i18nT('components.agentTemplateEditor.shown_in_the_agent_picker_and_the_crew_editor')} />
        </div>
        <Input id="agent-desc" className="w-full" placeholder={i18nT('components.agentTemplateEditor.what_this_agent_is_for')} value={desc} onChange={e => setDesc(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1">
          {/* eslint-disable-next-line jsx-a11y/label-has-for */}
          <label htmlFor="agent-prompt" className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.agentTemplateEditor.system_prompt')}</label>
          <InfoTip text={i18nT('components.agentTemplateEditor.a_file_value_points_at_a_prompt_file_on_disk_rep')} />
        </div>
        <textarea
          id="agent-prompt"
          className="w-full bg-bg-elevated border border-border rounded-md p-2.5 text-text text-[12px] font-mono outline-none resize-y leading-relaxed transition-colors focus-ring min-h-[140px]"
          rows={8}
          placeholder={i18nT('components.agentTemplateEditor.you_are_a_research_assistant')}
          value={text}
          onChange={e => setText(e.target.value)}
        />
      </div>
      {error && <ErrorNotice message={error} variant="inline" onDismiss={() => setError('')} />}
      <div className="flex items-center gap-2">
        <Btn primary onClick={save} disabled={!dirty || saveMut.isPending}>
          {saveMut.isPending ? i18nT('components.agentTemplateEditor.saving') : saveMut.isSuccess && !dirty ? <><Check className="lucide-inline" /> {i18nT('components.agentTemplateEditor.saved')}</> : i18nT('components.agentTemplateEditor.save')}
        </Btn>
        {dirty && <Btn onClick={revert}>{i18nT('components.agentTemplateEditor.revert')}</Btn>}
      </div>
    </div>
  )
}
