import { Plus, ChevronDown, LayoutGrid, MessageSquare } from 'lucide-react'
import { Btn } from './ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import { i18nT } from '../i18n/t'

/**
 * Split button for creating a job: primary half starts blank, the ▾ half offers
 * the template gallery.
 *
 * They used to be two sibling buttons in the header strip, which read as two
 * unrelated actions — they are one intent ("make a new job") with two starting
 * points, and only one of them is the common path.
 *
 * Both halves are `Btn primary`, NOT `SendBtn`, for two reasons:
 *
 *   - `SendBtn` is a plain function component (`ui.tsx`), so
 *     `<DropdownMenuTrigger asChild>` cannot attach a ref to it. Radix's
 *     `Popper.Anchor` would then report a null anchor, leave `isPositioned`
 *     false and render the menu at `translate(0, -200%)` — off screen. With the
 *     old toolbar `Templates` button gone, that made the gallery unreachable
 *     whenever the page had a job. `Btn` is a `forwardRef`, which every other
 *     `asChild` trigger in this repo relies on.
 *   - `Btn`'s height matches the search field and `New folder` sitting beside it
 *     in the same toolbar row; `SendBtn` is a 36px control and stood a step
 *     taller than its neighbours.
 */
export default function AddJobSplitButton({ onBlank, onBrowseTemplates }: {
  onBlank: () => void
  onBrowseTemplates: () => void
}) {
  return (
    <span className="inline-flex items-stretch">
      <Btn primary className="!rounded-r-none !border-r-0" onClick={onBlank}>
        <span className="flex items-center gap-1.5">
          <Plus size={14} aria-hidden="true" />
          {i18nT('pages.schedulePage.add_job')}
        </span>
      </Btn>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Btn
            primary
            className="!rounded-l-none !px-1.5"
            aria-label={i18nT('pages.schedulePage.browse_schedule_templates')}
            title={i18nT('pages.schedulePage.browse_schedule_templates')}
          >
            <ChevronDown size={14} aria-hidden="true" />
          </Btn>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-[200px]">
          <DropdownMenuItem onSelect={onBrowseTemplates}>
            <LayoutGrid size={13} className="shrink-0 text-accent" />
            <span>{i18nT('pages.schedulePage.browse_all_templates')}</span>
          </DropdownMenuItem>
          {/* The third creation route, and the home of the affordance the deleted
              "you can also create schedules by chatting" banner carried. A menu
              item is one text unit, unlike the subtitle sentence it replaced,
              which the i18n render gate flagged as assembled from two keys. */}
          <DropdownMenuItem onSelect={() => { window.location.href = '/chat' }}>
            <MessageSquare size={13} className="shrink-0 text-muted" />
            <span>{i18nT('pages.schedulePage.open_chat')}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </span>
  )
}
