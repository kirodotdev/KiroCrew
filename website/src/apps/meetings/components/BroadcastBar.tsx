// The bottom bar of a live meeting: shows what transcription just heard, and
// lets the user type a correction or an aside to every listening agent.
//
// A typed line reaches the agents marked `[chat]`, so they know it is the user
// speaking to them rather than another meeting participant.

import { useRef } from 'react'
import { Send } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { Input, SendBtn } from '../../../components/ui'

interface Props {
  onSend: (text: string) => void
  caption?: string
  disabled?: boolean
}

export default function BroadcastBar({ onSend, caption, disabled }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)

  const send = () => {
    const text = inputRef.current?.value.trim()
    if (!text) return
    onSend(text)
    if (inputRef.current) inputRef.current.value = ''
  }

  return (
    <div className="flex-none px-6 py-3 border-t border-border bg-bg">
      {caption && (
        <div
          // Wraps rather than clipping to one line: `truncate` set
          // `white-space: nowrap`, so a caption longer than the bar was cut with
          // an ellipsis — and `text-overflow` shows a string's HEAD, which pinned
          // the display to the oldest speech. `line-clamp-2` keeps this bar from
          // growing without limit and pushing the composer off-screen, since the
          // bar is `flex-none` and cannot shrink. Pairing matches
          // `issue-radar/components/IssueDetail.tsx`.
          className="text-[12px] text-muted break-words line-clamp-2 mb-2"
          aria-live="polite"
          data-testid="meetings-caption"
        >
          {i18nT('apps.meetings.broadcastBar.heard', { text: caption })}
        </div>
      )}
      <div className="flex items-center gap-2">
        <Input
          ref={inputRef}
          type="text"
          placeholder={i18nT('apps.meetings.broadcastBar.placeholder')}
          aria-label={i18nT('apps.meetings.broadcastBar.placeholder')}
          disabled={disabled}
          className="flex-1"
          onKeyDown={e => {
            if (e.key === 'Enter') send()
          }}
        />
        <SendBtn
          onClick={send}
          disabled={disabled}
          aria-label={i18nT('apps.meetings.broadcastBar.send')}
        >
          <Send className="lucide-inline" />
          {i18nT('apps.meetings.broadcastBar.send')}
        </SendBtn>
      </div>
    </div>
  )
}
