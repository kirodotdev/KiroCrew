import { memo } from 'react'
import { GitFork } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import type { ForkChild } from './forkPoints'

/**
 * A row holds at most two actions: up to two forks are named inline, and a
 * third or later puts the first one inline beside a "Show all" menu listing
 * every fork.
 */
const MAX_INLINE = 2

/**
 * The line under a parent row that a session was forked at, naming each fork
 * and opening it on click. Head and tail forks are listed separately because
 * they continue different halves of the conversation: a head fork carries
 * everything up to here, a tail fork everything after.
 */
const ForkPointMarker = memo(function ForkPointMarker({ forks, onOpen }: {
  forks: readonly ForkChild[]
  onOpen: (key: string) => void
}) {
  const heads = forks.filter(f => f.direction === 'head')
  const tails = forks.filter(f => f.direction === 'tail')
  return (
    <div data-testid="fork-point-marker" className="flex flex-col gap-0.5 my-1 px-1">
      {heads.length > 0 && <ForkLine label={i18nT('pages.chatPage.fork_point_forked_into')} forks={heads} onOpen={onOpen} />}
      {tails.length > 0 && <ForkLine label={i18nT('pages.chatPage.fork_point_continued_in')} forks={tails} onOpen={onOpen} />}
    </div>
  )
})

const LINK = 'truncate max-w-[16rem] text-accent hover:underline bg-transparent border-none p-0 cursor-pointer'

function ForkLine({ label, forks, onOpen }: { label: string; forks: readonly ForkChild[]; onOpen: (key: string) => void }) {
  const folded = forks.length > MAX_INLINE
  const inline = folded ? forks.slice(0, 1) : forks
  return (
    <div className="flex items-center gap-1.5 min-w-0 text-[11px] leading-4 text-muted">
      <span className="h-px flex-1 min-w-4 bg-border" aria-hidden="true" />
      <GitFork size={12} className="shrink-0" aria-hidden="true" />
      <span className="shrink-0">{label}</span>
      <span className="flex items-center gap-1 min-w-0">
        {inline.map((f, i) => (
          <span key={f.key} className="flex items-center min-w-0">
            <button
              type="button"
              data-testid="fork-point-link"
              className={LINK}
              title={i18nT('pages.chatPage.fork_point_open', { name: f.title })}
              onClick={() => onOpen(f.key)}
            >
              {f.title}
            </button>
            {i < inline.length - 1 || folded ? <span aria-hidden="true">,</span> : null}
          </span>
        ))}
        {folded && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" data-testid="fork-point-show-all" className="shrink-0 text-accent hover:underline bg-transparent border-none p-0 cursor-pointer">
                {i18nT('pages.chatPage.fork_point_show_all')}
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-[180px] max-w-[20rem]">
              {forks.map(f => (
                <DropdownMenuItem key={f.key} data-testid="fork-point-menu-item" onSelect={() => onOpen(f.key)}>
                  <GitFork size={13} className="shrink-0 text-muted" aria-hidden="true" />
                  <span className="truncate">{f.title}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </span>
      <span className="h-px flex-1 min-w-4 bg-border" aria-hidden="true" />
    </div>
  )
}

export default ForkPointMarker
