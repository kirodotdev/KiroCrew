import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Copy, SquareTerminal } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { useTerminalEnabled } from '../utils/terminalRegistry'
import ErrorNotice from './ErrorNotice'
import { useRunInTerminal, WithTerminalCopySetting } from './RunInTerminalBtn'
import { i18nT } from '../i18n/t'

/**
 * Fence language of the run-it-yourself card. An agent writes
 * ```run-yourself around a command a safety rule stopped it from running (a
 * one-line installer, say), and an assistant chat turn draws this card
 * instead of a code block: the command verbatim, why the agent could not run
 * it, a Run in terminal button and a Copy button.
 *
 * Only an assistant chat turn draws it (MarkdownRenderer `runYourselfCards`,
 * set by AssistantMessage). On every other surface (an issue, a PR, a skill
 * description) the fence is someone else's text and renders as a plain code
 * block, so outside text cannot pass as the agent's own hand-over.
 *
 * It grants the agent nothing new. An agent can already put a ```bash fence
 * in a reply, and every such block carries the same Run in terminal button.
 * The card goes through the same `useRunInTerminal` flow: the button only
 * opens the confirm dialog (`RunInTerminalConfirm`, with its sensitive-command
 * warning and its Copy wording when `dashboard.terminal.reuse_current` is on),
 * and nothing reaches a terminal until the person confirms there. The command
 * then runs in the person's own terminal tab, outside the agent's tool gate,
 * exactly as a click on a bash block does today.
 */
export const RUN_YOURSELF_LANG = 'run-yourself'

/** Longest summary the card shows; a longer one is cut with an ellipsis. */
export const RUN_YOURSELF_SUMMARY_MAX = 120

/**
 * The agent's one-line account of the command, from the fence's info string:
 * ```run-yourself summary="Installs Rust from rustup.rs."
 * It is the agent's words, so it is shown as plain text only, trimmed and cut
 * to RUN_YOURSELF_SUMMARY_MAX. No summary means none is shown; nothing is
 * made up in its place.
 */
export function runYourselfSummary(info: string | undefined): string | undefined {
  const m = /(?:^|\s)summary="([^"]*)"/.exec(info ?? '')
  const text = m?.[1].replace(/\s+/g, ' ').trim()
  if (!text) return undefined
  return text.length > RUN_YOURSELF_SUMMARY_MAX ? `${text.slice(0, RUN_YOURSELF_SUMMARY_MAX - 1).trimEnd()}…` : text
}

/** The card for a ```run-yourself fence whose close has arrived. */
export default function RunYourselfCard({ code, summary }: { code: string; summary?: string }) {
  return <WithTerminalCopySetting>{willCopy => <RunYourselfCardInner code={code} summary={summary} willCopy={willCopy} />}</WithTerminalCopySetting>
}

function RunYourselfCardInner({ code, summary, willCopy }: { code: string; summary?: string; willCopy: boolean }) {
  const terminalEnabled = useTerminalEnabled()
  // No `lang`: the fence names no shell, so the handler runs it as written.
  const { status, noAnswer, clearNoAnswer, askToRun, dialog } = useRunInTerminal({ code, willCopy })
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  // Only a run that got NO answer counts. An explicit refusal (a chat on a
  // Session Environment, a cwd still loading) is explained by the host's own
  // notice, often with "use the workspace terminal" or "try again", so the
  // card must not tell the person to paste into their own computer, and Run
  // stays. A run with no answer is sticky for this card: the terminal that
  // did not answer has not come back, so the card drops Run and leaves Copy.
  const runFailed = noAnswer
  const timerRef = useRef<ReturnType<typeof setTimeout>>()
  useEffect(() => () => clearTimeout(timerRef.current), [])

  const copy = useCallback(async () => {
    if (!(await copyCode(code))) {
      setCopyFailed(true)
      return
    }
    setCopyFailed(false)
    setCopied(true)
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopied(false), 1500)
  }, [code])

  // Copy is the way through whenever the terminal cannot take the command:
  // the dashboard has no terminal, or the host did not answer the run.
  const copyIsPrimary = !terminalEnabled || runFailed
  const primary = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] bg-accent text-accent-fg hover:opacity-90 cursor-pointer'
  const secondary = 'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] border border-border text-text hover:bg-bg-hover cursor-pointer'

  const runLabel = status === 'sent'
    ? i18nT('components.runInTerminalBtn.sent_to_terminal')
    : status === 'copied'
      ? i18nT('components.runInTerminalBtn.copied_paste_into_terminal')
      : i18nT('components.runInTerminalBtn.run_in_terminal')

  return (
    <div className="my-2 rounded-xl border border-border bg-bg-elevated overflow-hidden" data-testid="run-yourself-card">
      <div className="flex items-start gap-2 px-3 pt-3">
        <SquareTerminal size={16} className="text-muted shrink-0 mt-0.5" aria-hidden />
        <div className="min-w-0">
          <div className="text-[13px] font-medium text-text">{i18nT('components.runYourselfCard.title')}</div>
          {summary && <div className="text-[12px] text-text" data-testid="run-yourself-summary">{summary}</div>}
          <div className="text-[12px] text-muted">{i18nT('components.runYourselfCard.reason')}</div>
        </div>
      </div>
      {/* Wrapped, never clipped: the person must see every character before
          running it, and a horizontal scroller hides the tail of a long line.
          `overflow-wrap: anywhere` breaks a word only when it cannot fit, so a
          short token such as `sh` is never split into two lines. */}
      <pre className="mx-3 mt-2 p-2 rounded-md bg-bg border border-border text-[12px] font-mono whitespace-pre-wrap [overflow-wrap:anywhere] text-text">{code}</pre>
      {/* Two different states. A dashboard with no terminal is how this host
          is set up, not a failure, so it is a plain line. A terminal that did
          not answer the run IS a failure, so it goes through ErrorNotice. */}
      {runFailed && terminalEnabled ? (
        <ErrorNotice
          variant="inline"
          className="mx-3 mt-1"
          testId="run-yourself-run-unanswered"
          message={i18nT('components.runYourselfCard.run_unanswered')}
          askAgent
          // Closing it, like closing the copy error, clears the state it
          // reports: Run comes back for another try.
          onDismiss={clearNoAnswer}
        />
      ) : !terminalEnabled ? (
        <div className="px-3 pt-1 text-[12px] text-muted" role="status">{i18nT('components.runYourselfCard.no_terminal')}</div>
      ) : null}
      {/* No hint line in the normal state: the confirm dialog shows the whole
          command and its own Run or Copy wording (`willCopy`), so a line here
          would only repeat it. */}
      <div className="flex items-center gap-2 px-3 pb-3 pt-2">
        {/* Hidden once the terminal failed to answer, so the card never offers a
            button its own status line says will not work. A remount (a new
            reply, a reload) offers it again. */}
        {terminalEnabled && !runFailed && (
          <button type="button" className={primary} onClick={askToRun}>
            <SquareTerminal size={14} aria-hidden />{runLabel}
          </button>
        )}
        <button type="button" className={copyIsPrimary ? primary : secondary} onClick={copy}>
          {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
          {copied ? i18nT('components.monacoCodeBlock.copied') : i18nT('components.monacoCodeBlock.copy')}
        </button>
      </div>
      {copyFailed && (
        <ErrorNotice
          variant="inline"
          className="mx-3 mb-3"
          testId="run-yourself-copy-failed"
          message={i18nT('components.runYourselfCard.copy_failed')}
          askAgent
          onDismiss={() => setCopyFailed(false)}
        />
      )}
      {dialog}
    </div>
  )
}
