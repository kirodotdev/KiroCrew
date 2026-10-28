/**
 * The pieces a KAS launch is built from: the project directory field and the
 * per-recipe input form with its Launch button.
 */
import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, FolderOpen, Loader2, MessageSquare, Play } from 'lucide-react'

import { KAS_EFFORT_LEVELS, type KasRecipe } from '../../../api/client'
import { Btn, Input } from '../../../components/ui'
import SimpleSelect from '../../../components/SimpleSelect'
import { useAvailableModels } from '../../../hooks/useAvailableModels'
import ErrorNotice from '../../../components/ErrorNotice'
import ProjectPicker from '../../../components/ProjectPicker'
import { i18nT } from '../../../i18n/t'
import { useKasLaunch, type KasLaunchResult } from './launchKasWorkflow'
import { effortLabel, humanizeInputName } from './kasModel'
import { useOpenChat } from './KasRunView'

/**
 * The project directory a recipe list and a launch are scoped to. The same
 * free-text field the Task Runner uses for its workspace, plus Browse, which
 * opens the dashboard's shared project picker.
 *
 * Typing edits a draft; the directory is COMMITTED (and the recipe list
 * re-read) on blur, Enter, or a pick, so a half-typed path never reaches the
 * backend as a project.
 */
export function KasProjectDirInput({
  id,
  value,
  onChange,
  onCommit,
  placeholder,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  onCommit: (value: string) => void
  placeholder?: string
}) {
  const browseRef = useRef<HTMLButtonElement>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor={id} className="text-[13px] text-muted font-medium">
        {i18nT('apps.workflows.kas.project')}
      </label>
      <Input
        id={id}
        type="text"
        value={value}
        onChange={e => onChange(e.target.value)}
        onBlur={() => onCommit(value.trim())}
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
            e.preventDefault()
            onCommit(value.trim())
          }
        }}
        placeholder={placeholder || i18nT('apps.workflows.kas.project_placeholder')}
        className="min-w-[200px] flex-1 px-2.5 py-1.5 text-[13px] font-mono"
      />
      <Btn ref={browseRef} onClick={() => setPickerOpen(true)}>
        <FolderOpen className="lucide-inline" /> {i18nT('apps.workflows.kas.browse')}
      </Btn>
      {pickerOpen ? (
        <ProjectPicker
          open
          onOpenChange={open => { if (!open) setPickerOpen(false) }}
          anchorRef={browseRef}
          startPath={value}
          onSelect={path => {
            onChange(path)
            onCommit(path)
            setPickerOpen(false)
          }}
        />
      ) : null}
    </div>
  )
}

/**
 * Collect a recipe's inputs and launch it into a new chat in the "Workflows"
 * folder. After a launch it says so and offers Open chat; the run itself shows
 * up in the run list below as soon as KAS reports it.
 */
export function KasLaunchForm({
  recipe,
  project,
  idPrefix,
  onLaunched,
}: {
  recipe: KasRecipe
  project: string
  idPrefix: string
  onLaunched?: (result: KasLaunchResult) => void
}) {
  const launch = useKasLaunch()
  const openChat = useOpenChat()
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [options, setOptions] = useState<LaunchOptions>({ name: '', model: '', effort: '' })
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => launch({ recipe, project, inputs, ...options }),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['workflows', 'kas'] })
      onLaunched?.(result)
    },
  })
  const pending = mutation.isPending
  const launched = mutation.data ?? null
  const error = mutation.error ? (mutation.error instanceof Error ? mutation.error.message : String(mutation.error)) : null
  // Every declared input is required (gap 17): a run must not start missing one,
  // and a blank would otherwise be dropped and the run fail after its chat was
  // already created. Launch stays blocked until each declared input is filled.
  const missingInputs = recipe.inputs.filter(input => !(inputs[input.name] ?? '').trim())
  const blocked = !!recipe.validationError || !project.trim() || missingInputs.length > 0 || !recipe.launchable
  const submit = () => mutation.mutate()

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={e => {
        e.preventDefault()
        if (!blocked && !pending) submit()
      }}
      data-testid="kas-launch-form"
    >
      {recipe.inputs.map(input => {
        const fieldId = `${idPrefix}-input-${input.name}`
        const missing = !(inputs[input.name] ?? '').trim()
        return (
          <label key={input.name} htmlFor={fieldId} className="block text-[12px] text-muted">
            <span className="block mb-1">
              <span className="text-text">{humanizeInputName(input.name)}</span>{' '}
              <span className="font-mono text-[11px] text-muted" data-testid={`kas-input-hint-${input.name}`}>
                {input.name} · {input.type}
              </span>
              {missing ? (
                <span className="ml-1.5 text-[11px] text-warn" data-testid={`kas-input-required-${input.name}`}>
                  {i18nT('apps.workflows.kas.input_required')}
                </span>
              ) : null}
            </span>
            <Input
              id={fieldId}
              className="w-full"
              required
              aria-required="true"
              aria-invalid={missing || undefined}
              value={inputs[input.name] ?? ''}
              onChange={e => setInputs(cur => ({ ...cur, [input.name]: e.target.value }))}
            />
          </label>
        )
      })}
      <KasLaunchOptions idPrefix={idPrefix} recipeName={recipe.name} value={options} onChange={setOptions} disabled={pending} />
      <div className="flex flex-wrap items-center gap-2">
        <Btn primary type="submit" disabled={blocked || pending}>
          {pending ? <Loader2 className="lucide-inline animate-spin" /> : <Play className="lucide-inline" />}
          {i18nT('apps.workflows.kas.launch')}
        </Btn>
        {!project.trim() ? (
          <span className="text-[12px] text-muted">{i18nT('apps.workflows.kas.project_required')}</span>
        ) : !recipe.launchable ? (
          <span className="text-[12px] text-muted" data-testid="kas-not-launchable">
            {i18nT('apps.workflows.kas.not_launchable')}
          </span>
        ) : missingInputs.length > 0 ? (
          <span className="text-[12px] text-muted" data-testid="kas-inputs-required-hint">
            {i18nT('apps.workflows.kas.inputs_required')}
          </span>
        ) : null}
        {launched ? (
          <span className="flex items-center gap-2 text-[12px] text-ok" role="status">
            {i18nT('apps.workflows.kas.launched')}
            <Btn type="button" onClick={() => openChat(launched.slot)}>
              <MessageSquare className="lucide-inline" /> {i18nT('apps.workflows.kas.open_chat')}
            </Btn>
          </span>
        ) : null}
      </div>
      {/* No hand-off: the input fields above are an unsaved draft. */}
      <ErrorNotice message={error} variant="inline" />
    </form>
  )
}

interface LaunchOptions {
  name: string
  model: string
  effort: string
}

/**
 * The launch options, folded under "Options": the run's display name, and the
 * model and effort its steps run on. Blank sends nothing, so each step keeps
 * its own. The model list is only read once the options are opened.
 */
function KasLaunchOptions({
  idPrefix,
  recipeName,
  value,
  onChange,
  disabled,
}: {
  idPrefix: string
  recipeName: string
  value: LaunchOptions
  onChange: (value: LaunchOptions) => void
  disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const models = useAvailableModels({ enabled: open }).map(m => m.name)
  const modelOptions = ['', ...models.filter(m => m && m !== 'auto')]
  const effortOptions: string[] = ['', ...KAS_EFFORT_LEVELS]
  const stepDefault = i18nT('apps.workflows.kas.launch_step_default')
  const effortName = (e: string) => (e ? effortLabel(e) : stepDefault)
  const nameId = `${idPrefix}-opt-name`
  const modelId = `${idPrefix}-opt-model`
  const effortId = `${idPrefix}-opt-effort`
  return (
    <div className="flex flex-col gap-2" data-testid="kas-launch-options">
      <button
        type="button"
        className="self-start inline-flex items-center gap-1 bg-transparent border-none p-0 text-[12px] text-muted cursor-pointer hover:text-text-strong rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        data-testid="kas-launch-options-toggle"
      >
        {open ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
        {i18nT('apps.workflows.kas.launch_options')}
      </button>
      {open ? (
        <div className="grid gap-2 sm:grid-cols-3">
          <label htmlFor={nameId} className="block text-[12px] text-muted">
            <span className="block mb-1">{i18nT('apps.workflows.kas.launch_name')}</span>
            <Input
              id={nameId}
              className="w-full"
              value={value.name}
              placeholder={recipeName}
              disabled={disabled}
              onChange={e => onChange({ ...value, name: e.target.value })}
            />
          </label>
          <div className="text-[12px] text-muted">
            <label htmlFor={modelId} className="block mb-1">
              {i18nT('apps.workflows.kas.launch_model')}
            </label>
            <SimpleSelect
              id={modelId}
              options={modelOptions}
              optionLabels={modelOptions.map(m => m || stepDefault)}
              value={value.model}
              onChange={v => onChange({ ...value, model: v })}
              disabled={disabled}
              aria-label={i18nT('apps.workflows.kas.launch_model')}
              triggerFallback={value.model || stepDefault}
            />
          </div>
          <div className="text-[12px] text-muted">
            <label htmlFor={effortId} className="block mb-1">
              {i18nT('apps.workflows.kas.launch_effort')}
            </label>
            <SimpleSelect
              id={effortId}
              options={effortOptions}
              optionLabels={effortOptions.map(effortName)}
              value={value.effort}
              onChange={v => onChange({ ...value, effort: v })}
              disabled={disabled}
              aria-label={i18nT('apps.workflows.kas.launch_effort')}
              triggerFallback={effortName(value.effort)}
            />
          </div>
        </div>
      ) : null}
    </div>
  )
}
