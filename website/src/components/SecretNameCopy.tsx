import { useEffect, useRef, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'

/**
 * A vault secret name the reader has to store, shown as code with a copy
 * control beside it.
 *
 * A provider that reads its own REST API (Bitbucket Server) names the secret its
 * token must be stored under, and with two or more hosts listed that name is a
 * per-host one past 60 characters. Inside a wrapped sentence the only way to get
 * it is to select it by hand, and one wrong character makes the next read fail
 * the same way, so the name the gateway ships as its own field is set apart and
 * copied whole.
 *
 * The name wraps at any character rather than truncating: it is the value the
 * reader types into Settings, so every character stays visible. The copy reports
 * success only after the clipboard write resolves. A copy that fails says so in
 * its own notice, naming the way out (the name is on screen to select by hand),
 * and the next successful copy clears it.
 */
export default function SecretNameCopy({ name }: { name: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )
  const handleCopy = async () => {
    if (resetTimer.current) clearTimeout(resetTimer.current)
    if (!(await copyToClipboard(name))) {
      setState('failed')
      return
    }
    setState('copied')
    resetTimer.current = setTimeout(() => setState('idle'), 1500)
  }
  const copied = state === 'copied'
  return (
    <div className="mt-2 flex flex-col gap-1" data-testid="secret-name-copy">
      <span className="text-[11px] text-muted">{i18nT('components.secretNameCopy.label')}</span>
      <div className="flex items-start gap-2">
        <code className="min-w-0 flex-1 px-2 py-1 rounded bg-bg-hover text-[12px] text-text break-all">{name}</code>
        <Btn
          type="button"
          onClick={handleCopy}
          // Named by its visible text: "Copy name" says the button copies the
          // name above, never a token value the reader might paste in its place.
          // Full-strength text on the elevated chip: this is the block's one
          // control, and a muted transparent button reads as switched off.
          className="shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded border border-border bg-bg-elevated text-[12px] text-text hover:bg-bg-hover cursor-pointer"
        >
          {copied
            ? <Check className="lucide-inline text-ok" aria-hidden="true" />
            : <Copy className="lucide-inline" aria-hidden="true" />}
          {/* Both labels share one grid cell and the hidden one keeps its room,
              so the button never changes width when it flips to "Copied" and the
              name beside it never re-wraps while the reader is checking it. */}
          <span className="grid" aria-live="polite">
            <span className={`col-start-1 row-start-1${copied ? ' invisible' : ''}`} aria-hidden={copied || undefined}>
              {i18nT('components.secretNameCopy.copy')}
            </span>
            <span className={`col-start-1 row-start-1${copied ? '' : ' invisible'}`} aria-hidden={!copied || undefined}>
              {i18nT('components.secretNameCopy.copied')}
            </span>
          </span>
        </Btn>
      </div>
      {state === 'failed' && (
        // The block sits inside the load-error notice, which has its own
        // hand-off. A second bare "Ask the agent" a few pixels below reads as the
        // same link twice, so this one names the problem it asks about. That
        // label is long in some locales, so the hand-off goes on its own line
        // under the message: side by side in one row, a narrow panel crushed the
        // message and pushed the link past the edge.
        <ErrorNotice
          actionPlacement="below"
          askAgent
          askAgentLabel={i18nT('components.secretNameCopy.ask_agent_about_copy')}
          testId="secret-name-copy-error"
          message={i18nT('components.secretNameCopy.copy_failed')}
        />
      )}
    </div>
  )
}
