/**
 * Evidence for the fork-failure banner this PR introduces.
 *
 * THE CHANGE: a failed fork already rendered through `ErrorNotice` on the page's SHARED
 * `action-error` slot, but that slot could not carry a structured report. It now can, so
 * the fork keeps code-specific copy and the agent hand-off on the notice already there.
 *
 * Both copies are covered, since different failures reach them:
 *   ?scene=too-large   the over-capacity refusal (backend `fork_corpus_too_large`)
 *   ?scene=generic     any other fork failure, carrying the raw wire message
 *   ?theme=dark|light  ?direction=head|tail
 *
 * The notice is the REAL component and the strings come from the live i18n catalog, so a
 * frame proves the copy AND where the banner lands relative to the composer.
 */
import { createRoot } from 'react-dom/client'

import ErrorNotice from '../src/components/ErrorNotice'
import { forkFailureMessageForCode } from '../src/utils/forkFailure'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const SCENES = ['generic', 'sidebar-duplicate', 'grid-pane'] as const
const requested = params.get('scene') ?? ''
const scene = (SCENES as readonly string[]).includes(requested) ? requested : 'too-large'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const direction = params.get('direction') === 'tail' ? 'tail' : 'head'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

// The SHIPPED branch, not a copy of it: a frame that photographs its own spelling
// proves nothing about the sentence a reader gets.
const message = forkFailureMessageForCode(
  scene === 'generic' ? undefined : 'fork_corpus_too_large',
  'slot is closed',
  direction,
  scene === 'sidebar-duplicate' || scene === 'grid-pane' ? 'offsite' : 'transcript',
)

// The props below are ChatSidebar's own, so the frame attests to the shipped rendering:
// an inline notice in a narrow, resizable lane, with no title above the sentence.
function SidebarScene() {
  return (
    <div className="flex h-screen bg-canvas text-body" data-capture-root>
      <div className="flex w-[320px] shrink-0 flex-col border-r border-subtle p-2">
        <div className="mb-2 text-sm font-medium">{i18nT('pages.chatSidebar.sessions')}</div>
        <ErrorNotice
          message={message}
          onDismiss={() => undefined}
          variant="inline"
          askAgent
          className="mx-1 mt-1 mb-1 min-w-0 flex-wrap"
          messageClassName="min-w-0 break-words"
          testId="sidebar-duplicate-error-chat-1"
        />
        <div className="mt-3 rounded-lg bg-surface p-2 text-sm">A long conversation</div>
        <div className="mt-1 rounded-lg p-2 text-sm text-muted">Another session</div>
      </div>
      <div className="flex-1 p-4 text-sm text-muted">Transcript…</div>
    </div>
  )
}

// The session grid's empty placeholder pane: the refusal lands above the picker's
// own search row, inside a dashed cell that is narrower than the transcript.
function GridPaneScene() {
  return (
    <div className="flex h-screen items-stretch gap-2 bg-canvas p-2 text-body" data-capture-root>
      <div className="flex flex-1 flex-col rounded-lg border border-subtle bg-surface p-3 text-sm text-muted">
        Existing session
      </div>
      <div className="flex flex-1 flex-col h-full overflow-hidden rounded-lg border-[1.5px] border-dashed border-border bg-bg m-1">
        <ErrorNotice
          message={message}
          onDismiss={() => undefined}
          askAgent
          className="mx-2 mt-2 mb-0"
          testId="grid-fork-error"
        />
        <div className="flex items-center gap-1 border-b border-border p-2">
          <div className="flex-1 rounded border border-border bg-bg-elevated px-2 py-1 text-[13px] text-muted">
            Search sessions…
          </div>
        </div>
        <div className="p-2 text-sm text-muted">No session to fork</div>
      </div>
    </div>
  )
}

function Scene() {
  return (
    <div className="flex h-screen flex-col bg-canvas text-body" data-capture-root>
      <div className="flex-1 overflow-hidden px-4 pt-4">
        <div className="mb-3 text-sm text-muted">Earlier in this conversation…</div>
        <div className="mb-2 rounded-lg bg-surface p-3 text-sm">
          Can you fork this from the message where we changed the schema?
        </div>
        <div className="mb-2 rounded-lg p-3 text-sm">
          Forking from there now.
        </div>
      </div>

      {/* The banner's real position: between the transcript tail and the composer. */}
      <ErrorNotice
        message={message}
        onDismiss={() => undefined}
        variant="block"
        askAgent
        className="mx-4 mt-2 mb-0"
        testId="action-error"
      />

      <div className="p-4">
        <div className="rounded-xl border border-subtle bg-surface p-3">
          <div className="text-sm text-muted">Send a message…</div>
        </div>
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  scene === 'sidebar-duplicate' ? <SidebarScene />
    : scene === 'grid-pane' ? <GridPaneScene />
      : <Scene />,
)
