/**
 * Inline copy button for CLI command text in SettingRef popovers.
 * Reuses the shared clipboard utility and follows the copy/check icon
 * pattern established by DiffBlock.
 *
 * Also holds `ConfigSetCommandRow`, the file-setting command editor built on it, so
 * the clipboard path stays the one implementation.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Check, Copy, X } from 'lucide-react'
import { copyCode } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'
import type { ConfigSetHintsV1 } from '../../api/client'
import { Input } from '../ui'
import {
  cliValuePlaceholder,
  configSetCommand,
  configSetCommandV1,
  configSetDecodedValue,
  configSetSampleValue,
} from './envShellCommands'

type CopyState = 'idle' | 'copied' | 'failed'

export function CopyCommandButton({ text }: { text: string }) {
  const [state, setState] = useState<CopyState>('idle')
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )

  const handleCopy = useCallback(async () => {
    setState(await copyCode(text) ? 'copied' : 'failed')
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setState('idle'), 1500)
  }, [text])

  const title =
    state === 'copied'
      ? i18nT('components.settingRef.copied')
      : state === 'failed'
        ? i18nT('components.settingRef.copyFailed')
        : i18nT('components.settingRef.copyCommand')

  return (
    <button
      type="button"
      className="p-0.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer shrink-0"
      onClick={handleCopy}
      title={title}
      aria-label={title}
      aria-live="polite"
    >
      {state === 'copied' ? <Check size={11} /> : state === 'failed' ? <X size={11} /> : <Copy size={11} />}
    </button>
  )
}

export interface ConfigSetCommandRowProps {
  /** The schema key the command writes. The caller resolved it against the config schema, so it is trusted CLI text. */
  configKey: string
  /** Backend schema type: `array` selects the list editor, `integer`/`number` a numeric field. */
  schemaType: string | undefined
  /** A meaningful starting value (retention days: `7`); otherwise the type's own sample. */
  initialValue?: string
  /** Positional placeholder for the legacy command shown when the gateway sent no launcher hints. */
  valuePlaceholder?: string
  /** Launcher prefixes from the gateway. Absent on an older gateway, for a non-owner, or before the status loaded. */
  hints?: ConfigSetHintsV1
}

/** The label a shell's command row carries. One literal key per branch so the catalog scanner sees each. */
function shellLabel(id: ConfigSetHintsV1['shells'][number]['id']): string {
  if (id === 'powershell') return i18nT('components.settingRef.commandShellPowerShell')
  if (id === 'cmd') return i18nT('components.settingRef.commandShellCmd')
  return i18nT('components.settingRef.commandShellPosix')
}

const CONTROL_CLASS =
  'w-full rounded border border-border bg-bg px-2 py-1 text-[12px] font-mono text-text focus-visible:border-accent focus:outline-hidden'

/**
 * The command a person pastes to set a file-only setting.
 *
 * With launcher hints, the person edits the value in a control chosen by the schema
 * type and labelled as the command's input (it saves nothing itself), sees for a list
 * the exact text that will be stored, and gets one command per shell the gateway named
 * (the displayed text is what the copy button copies). The value travels as a single
 * `--value-base64=<token>` argument, so no shell quoting, empty argument or non-ASCII
 * character can change it on the way to the CLI. Without hints the pre-existing
 * positional command is shown with a neutral note that some shells may need its quoting
 * changed. An absent hint does not say the gateway is old (a non-owner and a tree
 * without the cached status see none either), and a CLI that predates the encoded
 * flag could not parse it, so the positional form is the one every gateway runs.
 */
export function ConfigSetCommandRow({
  configKey,
  schemaType,
  initialValue,
  valuePlaceholder,
  hints,
}: ConfigSetCommandRowProps) {
  const [text, setText] = useState(() => initialValue ?? configSetSampleValue(schemaType))
  const baseId = useId()
  const controlId = `${baseId}-value`
  const descriptionId = `${baseId}-description`
  const decodedLabelId = `${baseId}-decoded-label`

  if (!hints) {
    const legacyCommand = configSetCommand(configKey, cliValuePlaceholder(schemaType, valuePlaceholder))
    return (
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-1">
          <code className="text-[13px] bg-bg px-1.5 py-1 rounded border border-border block font-mono break-all flex-1">
            {legacyCommand}
          </code>
          <CopyCommandButton text={legacyCommand} />
        </div>
        <p className="text-[11px] text-muted">{i18nT('components.settingRef.legacyCommandShellNote')}</p>
      </div>
    )
  }

  const isList = schemaType === 'array'
  const isNumber = schemaType === 'integer' || schemaType === 'number'
  const label = isList
    ? i18nT('components.settingRef.commandListValueLabel')
    : i18nT('components.settingRef.commandValueLabel')
  const description = isList
    ? i18nT('components.settingRef.commandListValueDescription')
    : i18nT('components.settingRef.commandValueDescription')

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-1">
        <label htmlFor={controlId} className="text-[11px] font-semibold text-text">
          {label}
        </label>
        <p id={descriptionId} className="text-[11px] text-muted">
          {description}
        </p>
        {isList ? (
          <textarea
            id={controlId}
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-label={label}
            aria-describedby={descriptionId}
            rows={3}
            spellCheck={false}
            className={`${CONTROL_CLASS} resize-y`}
          />
        ) : (
          <Input
            id={controlId}
            type={isNumber ? 'number' : 'text'}
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-describedby={descriptionId}
            spellCheck={false}
            className={CONTROL_CLASS}
          />
        )}
      </div>
      {/* Only a list has a stored form that differs from what was typed (trimmed lines as
          a JSON array); for any other type the readout would repeat the field. */}
      {isList && (
        <div role="group" aria-labelledby={decodedLabelId} className="flex flex-col gap-0.5">
          <span id={decodedLabelId} className="text-[11px] text-muted">
            {i18nT('components.settingRef.commandDecodedValueLabel')}
          </span>
          {/* Not an <output>: its implicit status role would announce every keystroke. */}
          <pre
            aria-live="off"
            className="text-[11px] bg-bg px-1.5 py-1 rounded border border-border font-mono whitespace-pre-wrap break-all m-0"
          >
            {configSetDecodedValue(schemaType, text)}
          </pre>
        </div>
      )}
      {hints.shells.map(({ id, prefix }) => {
        const command = configSetCommandV1(prefix, configKey, schemaType, text)
        return (
          <div key={id} className="flex flex-col gap-0.5">
            <span className="text-[11px] text-muted">{shellLabel(id)}</span>
            <div className="flex items-center gap-1">
              <code className="text-[13px] bg-bg px-1.5 py-1 rounded border border-border block font-mono break-all flex-1">
                {command}
              </code>
              <CopyCommandButton text={command} />
            </div>
          </div>
        )
      })}
    </div>
  )
}
