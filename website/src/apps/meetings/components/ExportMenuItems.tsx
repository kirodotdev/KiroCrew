// The meeting menu's Export items (#21180): one per notes agent, saving what its
// panel shows (the user's saved edit when there is one) as a Markdown file, and
// one per sketch agent, saving its frame's document as one self-contained HTML
// file. An export is a snapshot, so it works mid-meeting and after the meeting
// has ended.

import { Download } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { DropdownMenuItem, DropdownMenuSeparator } from '../../../components/ui/dropdown-menu'
import { saveBlob } from '../../../utils/download'
import type { AgentDef } from '../api'
import { buildSketchExport } from '../lib/sketchSrcdoc'
import { buildMeetingBundle } from '../lib/meetingBundle'

interface Props {
  agents: AgentDef[]
  /** The EFFECTIVE outputs: the agent's text, or the user's saved edit. */
  outputs: Record<string, string>
  title: string
  /** ISO 8601 start (else creation) time; dates the file name. */
  date?: string
  /** A failed save's message; `null` when the next export starts. */
  onError: (message: string | null) => void
}

/** `<title>-<YYYY-MM-DD>-<agent id>.<extension>`, dated in the viewer's own time zone. */
export function exportFileName(title: string, date: string | undefined, agentId: string, extension = 'md'): string {
  // Letters, digits and plain punctuation only: the desktop app saves a download
  // only under the name it announced, and Chromium rewrites more than a file
  // system refuses (`~`, formatting marks, dots that make a reserved name). 50
  // code points keep a CJK title and any agent id inside a 255-byte file name.
  const kept = title.replace(/[^\p{L}\p{M}\p{N} _,&'()+-]+/gu, '-')
  const name = Array.from(kept).slice(0, 50).join('').replace(/^[\s-]+|[\s-]+$/g, '')
  const when = date ? new Date(date) : null
  const day = when && !Number.isNaN(when.getTime())
    ? `${when.getFullYear()}-${String(when.getMonth() + 1).padStart(2, '0')}-${String(when.getDate()).padStart(2, '0')}`
    : ''
  return `${[name, day, agentId].filter(Boolean).join('-')}.${extension}`
}

export default function ExportMenuItems({ agents, outputs, title, date, onError }: Props) {
  // The same notes-agent test MeetingView applies to the minutes editor.
  const notesAgents = agents.filter(agent => agent.widget_type === 'markdown' || agent.widget_type == null)
  const sketchAgents = agents.filter(agent => agent.widget_type === 'html')
  if (notesAgents.length === 0 && sketchAgents.length === 0) return null
  return (
    <>
      <DropdownMenuSeparator />
      {[...notesAgents, ...sketchAgents].map(agent => {
        const output = outputs[agent.id] ?? ''
        const sketch = agent.widget_type === 'html'
        return (
          // Disabled, not hidden, while there is no output to save: the export
          // is there, and there is nothing in it yet.
          <DropdownMenuItem
            key={agent.id}
            disabled={!output}
            onSelect={() => {
              onError(null)
              const failed = () => onError(i18nT('components.markdownPanel.download_failed'))
              if (sketch) {
                buildSketchExport(output)
                  .then(file => saveBlob(
                    new Blob([file], { type: 'text/html;charset=utf-8' }),
                    exportFileName(title, date, agent.id, 'html'),
                  ))
                  .catch(failed)
              } else {
                saveBlob(
                  new Blob([output], { type: 'text/plain;charset=utf-8' }),
                  exportFileName(title, date, agent.id),
                ).catch(failed)
              }
            }}
          >
            <Download size={13} className="shrink-0 text-muted" />
            <span>
              {sketch
                ? i18nT('apps.meetings.meeting.exportHtml', { name: agent.name })
                : i18nT('apps.meetings.meeting.exportMarkdown', { name: agent.name })}
            </span>
          </DropdownMenuItem>
        )
      })}
      {/* One item that saves every enabled output together (#32982), as one
          self-contained HTML file named after the meeting. Same availability as
          the single items: disabled, not hidden, while NO agent has output yet. */}
      <DropdownMenuItem
        disabled={[...notesAgents, ...sketchAgents].every(agent => !outputs[agent.id])}
        onSelect={() => {
          onError(null)
          buildMeetingBundle(
            [...notesAgents, ...sketchAgents], outputs, title, date,
            i18nT('apps.meetings.meeting.exportAllNoOutput'),
          )
            .then(file => saveBlob(
              new Blob([file], { type: 'text/html;charset=utf-8' }),
              exportFileName(title, date, '', 'html'),
            ))
            .catch(() => onError(i18nT('components.markdownPanel.download_failed')))
        }}
      >
        <Download size={13} className="shrink-0 text-muted" />
        <span>{i18nT('apps.meetings.meeting.exportAll')}</span>
      </DropdownMenuItem>
    </>
  )
}
