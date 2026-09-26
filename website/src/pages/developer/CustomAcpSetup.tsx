import { useEffect, useId, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldAlert } from 'lucide-react'

import { api } from '../../api/client'
import { Btn, Input } from '../../components/ui'
import { SettingsField } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

// Saving the launch pair never writes agent.acp_backend; Use owns that action.
export const CUSTOM_ACP_CONFIG_KEY = 'agent.custom_acp'
export const CUSTOM_ACP_MUTATION_KEY = ['customAcpConfig']

export interface CustomAcpConfig {
  command: string
  args: string[]
}

// Server validation remains authoritative; these bounds keep errors beside the draft.
const MAX_COMMAND_LEN = 4096
const MAX_ARGS = 128
const MAX_ARG_LEN = 8192

/** One argument per line, without shell parsing; the final newline is a separator. */
export function argsFromText(text: string): string[] {
  if (text === '') return []
  const lines = text.split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  return lines
}

/** Preserve a trailing empty argument with an additional separator newline. */
export function textFromArgs(args: string[]): string {
  if (args.length === 0) return ''
  const joined = args.join('\n')
  return args[args.length - 1] === '' ? joined + '\n' : joined
}

/** Textareas normalize CR/LF, so arguments containing either cannot be edited here. */
export function hasUneditableArg(args: string[]): boolean {
  return args.some(a => a.includes('\n') || a.includes('\r'))
}

export function validateDraft(command: string, argsText: string): { error: string } | { config: CustomAcpConfig } {
  const trimmed = command.trim()
  if (!trimmed) return { error: i18nT('pages.developer.customAcpSetup.error_command_required') }
  if (trimmed.length > MAX_COMMAND_LEN)
    return { error: i18nT('pages.developer.customAcpSetup.error_command_too_long', { max: MAX_COMMAND_LEN }) }

  const args = argsFromText(argsText)
  if (args.length > MAX_ARGS)
    return { error: i18nT('pages.developer.customAcpSetup.error_too_many_args', { max: MAX_ARGS }) }
  if (args.some(a => a.length > MAX_ARG_LEN))
    return { error: i18nT('pages.developer.customAcpSetup.error_arg_too_long', { max: MAX_ARG_LEN }) }

  return { config: { command: trimmed, args } }
}

/** Inline experimental setup. Warnings precede inputs; saving never activates it. */
export function CustomAcpSetup({
  saved,
  disabled = false,
  onDraftState,
}: {
  saved: CustomAcpConfig | undefined
  disabled?: boolean
  onDraftState?: (state: { dirty: boolean; saving: boolean }) => void
}) {
  const qc = useQueryClient()
  const commandId = useId()
  const argsId = useId()
  // Seed once per visit. A config refetch must not erase a newer edit.
  const [command, setCommand] = useState(saved?.command ?? '')
  const [argsText, setArgsText] = useState(textFromArgs(saved?.args ?? []))
  const [error, setError] = useState('')
  const commandRef = useRef<HTMLInputElement>(null)
  const uneditable = saved ? hasUneditableArg(saved.args) : false

  const saveMut = useMutation({
    mutationKey: CUSTOM_ACP_MUTATION_KEY,
    mutationFn: (config: CustomAcpConfig) => api.patchConfig(CUSTOM_ACP_CONFIG_KEY, config),
    onSuccess: async () => {
      setError('')
      // Keep Save/Use pending until both the stored pair and readiness are reread.
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
        qc.invalidateQueries({ queryKey: ['acpBackends'] }),
      ])
    },
    onError: () => setError(i18nT('pages.developer.customAcpSetup.could_not_save')),
  })

  // Compare argv rather than textarea formatting: a separator newline is not an arg.
  const dirty = command.trim() !== (saved?.command ?? '') ||
    (!uneditable && JSON.stringify(argsFromText(argsText)) !== JSON.stringify(saved?.args ?? []))
  useEffect(() => {
    onDraftState?.({ dirty, saving: saveMut.isPending })
  }, [dirty, saveMut.isPending, onDraftState])

  const controlsDisabled = disabled || saveMut.isPending
  const onSave = () => {
    if (controlsDisabled) return
    const result = validateDraft(command, argsText)
    if ('error' in result) {
      setError(result.error)
      commandRef.current?.focus()
      return
    }
    setError('')
    saveMut.mutate(result.config)
  }

  return (
    <div className="mt-2">
      <div className="rounded-md border border-warn/40 bg-warn/5 px-2.5 py-2 text-[12px] leading-relaxed text-warn">
        <div className="flex items-start gap-1.5">
          <ShieldAlert size={13} aria-hidden className="mt-0.5 shrink-0" />
          <div className="space-y-1.5">
            <p className="m-0 font-semibold text-text-strong">
              {i18nT('pages.developer.customAcpSetup.warn_title')}
            </p>
            <p className="m-0">{i18nT('pages.developer.customAcpSetup.warn_no_approval')}</p>
            <p className="m-0">{i18nT('pages.developer.customAcpSetup.warn_sandbox_floor')}</p>
            <p className="m-0">{i18nT('pages.developer.customAcpSetup.warn_trust')}</p>
            <p className="m-0">{i18nT('pages.developer.customAcpSetup.warn_platform')}</p>
          </div>
        </div>
      </div>

      <div className="mt-2 space-y-1 text-[12px] leading-relaxed text-muted">
        <p className="m-0">{i18nT('pages.developer.customAcpSetup.feature_no_projection')}</p>
        <p className="m-0">{i18nT('pages.developer.customAcpSetup.feature_no_tools')}</p>
        <p className="m-0">{i18nT('pages.developer.customAcpSetup.feature_model_in_harness')}</p>
        <p className="m-0">{i18nT('pages.developer.customAcpSetup.feature_no_continuation')}</p>
        <p className="m-0">{i18nT('pages.developer.customAcpSetup.feature_logs_not_cleaned')}</p>
      </div>

      {uneditable ? (
        <p className="mt-3 text-[12px] leading-relaxed text-warn">
          {i18nT('pages.developer.customAcpSetup.uneditable_argv')}
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          <SettingsField
            label={i18nT('pages.developer.customAcpSetup.executable_label')}
            description={i18nT('pages.developer.customAcpSetup.executable_help')}
            configKey={CUSTOM_ACP_CONFIG_KEY}
            controlId={commandId}
          >
            <Input
              id={commandId}
              ref={commandRef}
              type="text"
              aria-label={i18nT('pages.developer.customAcpSetup.executable_label')}
              spellCheck={false}
              autoComplete="off"
              value={command}
              disabled={controlsDisabled}
              onChange={e => setCommand(e.target.value)}
              className="w-full font-mono disabled:opacity-50"
              placeholder={i18nT('pages.developer.customAcpSetup.executable_placeholder')}
            />
          </SettingsField>

          <SettingsField
            label={i18nT('pages.developer.customAcpSetup.arguments_label')}
            description={i18nT('pages.developer.customAcpSetup.arguments_help')}
            configKey={CUSTOM_ACP_CONFIG_KEY}
            controlId={argsId}
          >
            <textarea
              id={argsId}
              aria-label={i18nT('pages.developer.customAcpSetup.arguments_label')}
              spellCheck={false}
              autoComplete="off"
              rows={4}
              value={argsText}
              disabled={controlsDisabled}
              onChange={e => setArgsText(e.target.value)}
              className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-hidden resize-y leading-normal transition-colors focus-ring disabled:opacity-50"
              placeholder={i18nT('pages.developer.customAcpSetup.arguments_placeholder')}
            />
          </SettingsField>

          {/* No hand-off: navigating to chat would discard the executable/argv draft. */}
          <ErrorNotice message={error} variant="inline" onDismiss={() => setError('')} />
          <div className="flex justify-end">
            <Btn type="button" disabled={controlsDisabled || command.trim() === ''} onClick={onSave}>
              {saveMut.isPending
                ? i18nT('pages.developer.customAcpSetup.saving')
                : i18nT('pages.developer.customAcpSetup.save_configuration')}
            </Btn>
          </div>
        </div>
      )}
    </div>
  )
}
