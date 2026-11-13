/** The lane grammar under a `SessionLaneChanged` glob matcher, and a menu of the
 *  board's lanes by name that writes a selector with the lane's tag id. */
import { useQuery } from '@tanstack/react-query'
import { ChevronDown } from 'lucide-react'
import { api } from '../api/client'
import type { ChatTag } from '../types'
import { Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../components/ui/dropdown-menu'
import { i18nT } from '../i18n/t'

/** The glob selector for one direction of a lane move. The dispatcher's matcher
 *  context is `added:<id>;  removed:<id>;` (`_session_lane_matcher_context`), and a
 *  glob compares the whole string, so both stars are needed. */
export const laneSelector = (direction: 'added' | 'removed', id: string) => `*${direction}:${id};*`

export default function HookLaneMatcherHelp({ onInsert }: { onInsert: (selector: string) => void }) {
  const { data: tags, error } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags() })
  // A lane is a status tag, the same truthiness the dispatcher and the board read.
  const lanes = (tags ?? []).filter(t => t.status).sort((a, b) => a.order - b.order)
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 flex-wrap text-[13px] text-muted">
        <span>
          {i18nT('pages.hooksPage.matcher_lane_grammar', {
            added: laneSelector('added', '<id>'),
            removed: laneSelector('removed', '<id>'),
          })}
        </span>
        {!error && lanes.length > 0 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Btn className="h-7 px-2 text-[13px] gap-1">
                {i18nT('pages.hooksPage.insert_a_lane')}
                <ChevronDown size={13} aria-hidden />
              </Btn>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-[160px]">
              {lanes.map(lane => (
                <DropdownMenuItem key={lane.id} onSelect={() => onInsert(laneSelector('added', lane.id))}>
                  {lane.name}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {/* No hand-off: the hook form around this holds an unsaved name, command and matcher. */}
      <ErrorNotice
        title={i18nT('pages.hooksPage.lanes_failed_to_load')}
        message={error ? (error instanceof Error ? error.message : String(error)) : null}
      />
    </div>
  )
}
