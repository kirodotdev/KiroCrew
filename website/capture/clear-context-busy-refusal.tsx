/**
 * Evidence for the clear-context busy refusal on a channel.
 *
 * Clearing a channel's context while a role's session had a turn in flight reported
 * success and cleared nothing for that role: the server answers 200 with the refusing
 * roles in `busy`, and no caller read that field.
 *
 * Mounts the REAL `Btn` and the REAL `ErrorNotice` the page renders, with the copy
 * resolved through the REAL `clearContextBusyMessage` exported from `ChannelPage`,
 * against the real stylesheet and live i18n catalog. The refusal surface shipped here
 * is the in-page banner, so that is what these frames show -- an earlier revision of
 * this scene mirrored a native `alert()`, which the page no longer raises.
 *
 *   ?theme=dark|light&scope=all|agent|clean|total|failure
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { RotateCcw } from 'lucide-react'

import { clearContextBusyMessage, clearContextBusyRefusal, AgentControlRow } from '../src/pages/ChannelPage'
import type { ChannelAgent } from '../src/pages/ChannelPage'
import ErrorNotice from '../src/components/ErrorNotice'
import { Btn } from '../src/components/ui'
import { ApiError } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

/** Response shapes `api_channel_clear_context` can actually answer AFTER this change.
 * A partial refusal is 200 with `busy`; a TOTAL refusal is a 409 that arrives as a throw,
 * so it is exercised through the catch rather than this map. */
const RESPONSES: Record<string, { cleared: string[]; busy: string[] }> = {
  // Clear-all with two of three roles mid-turn -- partial, so 200 with `busy`.
  all: { cleared: ['Scribe'], busy: ['Researcher', 'Analyst'] },
  // Contrast: nothing refused, so no banner is owed at all.
  clean: { cleared: ['Researcher', 'Analyst', 'Scribe'], busy: [] },
}

/** The two THROWN paths, both rendered by the real `clearContextBusyRefusal`. The 409
 * must read as the localized refusal and NOT as the backend's English prose; anything
 * else falls back to the generic copy. */
const THROWN: Record<string, unknown> = {
  // `scope=agent` processes ONLY the addressed member, so `cleared` and `busy` hold at most
  // one name between them and a refusal is always `busy && !cleared` -- a 409, never a 200.
  agent: new ApiError(
    409,
    'conflict',
    JSON.stringify({
      error:
        'context not cleared: Researcher had a turn in flight. Nothing was cleared — retry when idle.',
      code: 'turn_in_flight',
      busy: ['Researcher'],
    }),
  ),
  total: new ApiError(
    409,
    'conflict',
    JSON.stringify({
      error:
        'context not cleared: Researcher, Analyst had a turn in flight. Nothing was cleared — retry when idle.',
      code: 'turn_in_flight',
      busy: ['Researcher', 'Analyst'],
    }),
  ),
  failure: new ApiError(500, 'channel store unavailable', ''),
}

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scope = params.get('scope') || 'all'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

// The clear-context dialogs are NATIVE `confirm()` calls, so no screenshot can show their
// chrome. This raises the real ones, so the harness reads the copy off the dialog event.
const CONFIRM_KEYS: [string, Record<string, string>][] = [
  ['pages.channelPage.this_will_reset_conversation_history_for_all_age', {}],
  ['pages.channelPage.reset_role_s_llm_session_the_channel_s_shared_me', { role: 'Researcher' }],
]

const thrown = THROWN[scope]
const response = RESPONSES[scope] || RESPONSES.all
const label =
  scope === 'agent'
    ? i18nT('pages.channelPage.clear_context')
    : i18nT('pages.channelPage.clear_context_2')

const HEADERS: Record<string, string> = {
  agent: 'per-agent control, @Researcher mid-turn: ',
  clean: 'clear-all, every role idle: ',
  total: 'clear-all, EVERY role mid-turn: ',
  failure: 'clear-all, channel store down: ',
  retrying: 'partial refusal, retry in flight: ',
  confirms: 'the three native confirm() bodies this change rewrote or added: ',
  all: 'clear-all, @Researcher and @Analyst mid-turn: ',
}

const WIRE: Record<string, string> = {
  total: 'POST /api/channels/ch-ops/clear-context -> 409 {"error":"...","code":"turn_in_flight","busy":["Researcher","Analyst"]}',
  failure: 'POST /api/channels/ch-ops/clear-context -> 500 channel store unavailable',
  retrying: 'POST /api/channels/ch-ops/clear-context -> in flight; the retry is disabled until it answers',
  confirms: 'no request yet -- each line is the message the real confirm() was given',
}

function Scene() {
  const [notice, setNotice] = useState<{ title: string; message: string; warn?: boolean } | null>(
    null,
  )
  const [done, setDone] = useState(false)
  const [asked, setAsked] = useState<string[]>([])

  // The page's composed path: a PARTIAL refusal answers 200 and never throws, a TOTAL one
  // answers 409 and is localized by the real helper, and anything else is a generic failure.
  const failTitle = i18nT('pages.channelPage.failed_to_clear_context')
  const onClick = () => {
    if (scope === 'confirms') {
      // The REAL browser primitive the page calls, so the harness reads the shipped copy off
      // the dialog rather than off a string this scene retyped.
      setAsked(
        CONFIRM_KEYS.map(([key, vars]) => {
          const text = i18nT(key, vars)
          confirm(text)
          return text
        }),
      )
      return
    }
    if (thrown !== undefined) {
      const busy = clearContextBusyRefusal(thrown)
      // Mirrors `failClearContext`: a 409 is a WITHHELD clear, so it leads with the
      // not-cleared title in warn chrome rather than claiming an outright failure.
      setNotice({
        title: busy ? i18nT('pages.channelPage.clear_context_not_cleared_yet') : failTitle,
        message: busy || (thrown instanceof Error ? thrown.message : failTitle),
        warn: Boolean(busy),
      })
      return
    }
    const busy = clearContextBusyMessage(response)
    // Mirrors `noteClearRefusal`: a partial clear leads with the PARTIAL title, because a
    // bold "Failed" over a body ending "Cleared for Scribe" contradicts itself.
    const cleared = (response as { cleared?: unknown } | undefined)?.cleared
    const partial = Array.isArray(cleared) && cleared.length > 0
    // Mirrors the page: a clean clear is ACKNOWLEDGED rather than answered with nothing,
    // so silence here would evidence a surface the app no longer renders.
    setDone(!busy)
    setNotice(
      busy
        ? {
            title: partial
              ? i18nT('pages.channelPage.clear_context_partially_cleared')
              : failTitle,
            message: busy,
            // Mirrors the page here too: without it these frames show a partial clear in
            // danger chrome the app never renders, and the evidence contradicts the ship.
            warn: partial,
          }
        : null,
    )
  }

  if (scope === 'rowmarks') {
    // The real row, not a copy: these three marks are the ONLY signal a reader watching the
    // agents panel gets, and a hand-rolled stand-in would evidence the stand-in.
    const member: ChannelAgent = {
      id: 'a1',
      role: 'Researcher',
      agentName: 'researcher',
      state: 'working',
      listenMode: 'all',
      approvalPolicy: 'writes',
    }
    const noop = () => undefined
    return (
      <div data-capture-root className="bg-bg text-text p-5 w-[720px] flex flex-col gap-4">
        <div className="text-[11px] text-muted font-mono">
          in-row states — pending (disabled, aria-busy), cleared, kept
        </div>
        <div data-capture-notice className="flex flex-col gap-3">
          <div data-capture-row="pending">
            <AgentControlRow
              agent={member}
              onDismiss={noop}
              onListenChange={noop}
              onClearContext={() => new Promise<void>(() => undefined)}
            />
          </div>
          <div data-capture-row="cleared">
            <AgentControlRow
              agent={member}
              onDismiss={noop}
              onListenChange={noop}
              onClearContext={noop}
              justCleared
            />
          </div>
          <div data-capture-row="kept">
            <AgentControlRow
              agent={member}
              onDismiss={noop}
              onListenChange={noop}
              onClearContext={noop}
              justRefused
            />
          </div>
        </div>
      </div>
    )
  }

  return (
    <div data-capture-root className="bg-bg text-text p-5 w-[720px] flex flex-col gap-3">
      <div className="text-[11px] text-muted font-mono break-all">
        <span className="not-italic text-subtle">{HEADERS[scope] || HEADERS.all}</span>
        {WIRE[scope] ||
          `POST /api/channels/ch-ops/clear-context -> 200 ${JSON.stringify(response)}`}
      </div>

      <Btn onClick={onClick} data-capture-clear>
        <RotateCcw className="lucide-inline" /> {label}
      </Btn>

      {/* NO hand-off, as the page has it: this notice can sit above an unsent composer
        * draft, and the hand-off would unmount the page and destroy it. */}
      <div data-capture-notice className="min-h-[1px]">
        {done && (
          <p
            role="status"
            className={
              scope === 'clean'
                ? 'text-[13px] text-text-strong font-medium mb-2'
                : 'text-[12px] text-muted mb-2'
            }
            data-testid="clear-context-done"
          >
            {i18nT('pages.channelPage.clear_context_done')}
            {scope === 'clean' && ` ${i18nT('pages.channelPage.clear_context_messages_deleted')}`}
          </p>
        )}
        {asked.length > 0 && (
          <ol className="text-[12px] text-text m-0 pl-5 flex flex-col gap-1" data-testid="confirm-copy">
            {asked.map(text => (
              <li key={text} className="whitespace-pre-wrap">{text}</li>
            ))}
          </ol>
        )}
        <ErrorNotice
          title={notice && notice.message !== notice.title ? notice.title : undefined}
          message={notice?.message}
          warn={notice?.warn}
          onDismiss={() => setNotice(null)}
          testId="clear-context-error"
        />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
