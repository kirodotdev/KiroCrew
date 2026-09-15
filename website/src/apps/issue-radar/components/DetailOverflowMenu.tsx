// The detail panes' overflow menu — the "everything past the second control"
// half of their toolbar.
//
// WHY IT EXISTS
//
// `AUTOSDE.yaml` -> `max-two-buttons-per-row` caps a button row at two, and both
// panes were over it once the toolbar became a single sticky row: the issue pane
// put a copy-link button, Investigate, Close and Refresh on one line, the PR pane
// a copy-link button, Review and Refresh. The row was ALSO wrapping onto a second
// line at 390px — measured across the shipped locales, 9 of 13 exceeded the 342px
// line, English among them — which is the failure mode the cap exists to prevent.
// Widening or accepting the wrap is explicitly not the fix; collapsing the row is.
//
// So each pane keeps exactly its primary action in the row (Investigate on an
// issue, Review on a pull request) and everything else moves in here. A trigger
// counts as one control however many items it holds, so the row is back at two
// with nothing removed from the product: Close keeps its FULL TEXT LABEL as a
// menu item, which is the part an icon-only Close could not do — a lone
// check-in-a-circle glyph cannot say "close this issue", and an `aria-label`
// rescues a screen reader while leaving a sighted user guessing.
//
// This is composition, not configuration: the panes pass `DropdownMenuItem`
// children, because their menus genuinely differ (only the issue pane can close
// or reopen). Following `components/CronRowActions.tsx` and
// `components/SessionActionsMenu.tsx` rather than inventing a second overflow
// shape, including the labelled icon trigger that `icon-buttons-need-labels`
// requires.
import type { ReactNode } from 'react'
import { MoreHorizontal, Loader2 } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
} from '../../../components/ui/dropdown-menu'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'

export default function DetailOverflowMenu(
  { children, pending }: { children: ReactNode; pending?: boolean },
) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Btn
          className="!px-1.5"
          aria-label={i18nT('apps.issueRadar.components.detailOverflowMenu.more_actions')}
          title={i18nT('apps.issueRadar.components.detailOverflowMenu.more_actions')}
        >
          {/* The trigger carries the in-flight state of the writes it now hosts.
              The old header `Close` button swapped its own glyph for a spinner
              while the mutation ran; moving it into a menu that closes on select
              would otherwise leave a state write with NO acknowledgment anywhere
              until the pill flips, which on any latency reads as a dead tap and
              invites a second one. The glyph is the only thing that changes, so
              the trigger's width — and the row's fit — is unaffected. */}
          {pending
            ? <Loader2 size={14} className="animate-spin" />
            : <MoreHorizontal size={14} />}
        </Btn>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[200px]">
        {children}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
