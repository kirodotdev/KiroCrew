import type { ReactNode } from 'react'
import { MoreHorizontal, PenLine, Sparkles } from 'lucide-react'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../ui/dropdown-menu'
import { DROPDOWN_ROW, PromptStashMenuItems } from '../PromptStashBadge'
import type { PromptStashState } from '../../hooks/usePromptStash'
import type { useMenuOpensDialog } from '../../hooks/useMenuOpensDialog'
import { i18nT } from '../../i18n/t'

/** The composer's ⋯ overflow: Sketch and the collapse on the collapsible touch
 *  host, mid-turn Optimize, and the prompt-stash rows wherever "+" cannot hold
 *  them. The caller decides whether it mounts. */
export function ComposerMoreMenu({
  directFilePicker, stashInOverflow, hasUpload, uploading, moreMenuLayer, setSketchOpen,
  collapseMenuRow, busyOptimizeOffered, optimizeBlocked, optimizeUnlessLocked, optimizeRowLabel,
  promptStash, promptStashChord, touch,
}: {
  directFilePicker: boolean
  stashInOverflow: boolean
  /** The host passed `onUploadFiles`: Sketch attaches through it. */
  hasUpload: boolean
  uploading: boolean
  moreMenuLayer: ReturnType<typeof useMenuOpensDialog>
  setSketchOpen: (open: boolean) => void
  collapseMenuRow?: ReactNode
  busyOptimizeOffered: boolean
  optimizeBlocked: boolean
  optimizeUnlessLocked: () => void
  optimizeRowLabel: ReactNode
  promptStash: PromptStashState
  promptStashChord: string
  touch: boolean
}) {
  return (
      /* The repo's own overflow mechanism, not a second spelling of it.
         `max-two-buttons-per-row` names the two files to copy for exactly
         this shape, and `DetailOverflowMenu.tsx` already answers the same
         rule the same way -- a labelled MoreHorizontal trigger holding
         "everything past the second control". This wrapper provides the
         top-side anchoring, viewport collision and outside-click natively.

         The TRIGGER stays enabled while an upload is in flight: it hosts
         the collapse as well as Sketch, and the collapse must stay
         reachable mid-upload. The upload guard sits on the item that
         needs it, just below. */
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid="composer-more-trigger"
            className="w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover data-[state=open]:text-text data-[state=open]:bg-bg-hover"
            aria-label={i18nT('components.chatInput.more_actions')}
            title={i18nT('components.chatInput.more_actions')}
          >
            <MoreHorizontal size={17} />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" onCloseAutoFocus={moreMenuLayer.onCloseAutoFocus}>
          {/* `DropdownMenuContent` takes no classes (shadcn/no-restyle), so the
              menu's width and extra inset sit on this plain wrapper. With the
              content's own `p-1` and border, the menu is 260px wide with the
              same 8px inset as before. */}
          <div className="w-[250px] p-1">
          {/* Only where "+" is a bare picker: elsewhere Sketch is a row in "+". */}
          {hasUpload && directFilePicker && (
            /* `disabled={uploading}`: Sketch attaches through the same
               `onUploadFiles` handler, and the in-flight flag is a single
               shared boolean rather than a counter -- so a sketch attached
               while another upload is still running would let whichever
               request finishes first clear the in-flight state for both.
               Disabling the item while uploading prevents that. */
            <DropdownMenuItem
              asChild
              disabled={uploading}
              /* `openLayer` defers the open one macrotask and skips the
                 menu's trigger restore; the ordering is documented on
                 `useMenuOpensDialog`. Opened inline, the dialog mounts
                 while the menu is STILL trapping focus, and in happy-dom
                 that fight shows up as an unbounded blur/focus recursion. */
              onSelect={() => moreMenuLayer.openLayer(() => setSketchOpen(true))}
            >
              <button
                type="button"
                className={DROPDOWN_ROW}
              >
                <PenLine size={14} className="w-4 shrink-0 text-muted lucide-inline group-data-[disabled]:opacity-40" />
                <div className="min-w-0">
                  <div className="text-[12px] font-medium text-text group-data-[disabled]:opacity-40">{i18nT('components.chatInput.sketch')}</div>
                  <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.sketch_desc')}</div>
                </div>
              </button>
            </DropdownMenuItem>
          )}
          {collapseMenuRow && (
            <DropdownMenuItem asChild>
              {collapseMenuRow}
            </DropdownMenuItem>
          )}
          {/* Mid-turn Optimize, as in the "+" menu (`busyOptimizeRow`). */}
          {busyOptimizeOffered && (
            <DropdownMenuItem asChild disabled={optimizeBlocked} onSelect={() => optimizeUnlessLocked()}>
              <div data-testid="composer-more-optimize" className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg cursor-pointer text-left data-[disabled]:opacity-40 data-[disabled]:cursor-not-allowed">
                <Sparkles size={14} className="w-4 shrink-0 text-muted lucide-inline" />
                <div className="min-w-0 text-[12px] font-medium text-text">{optimizeRowLabel}</div>
              </div>
            </DropdownMenuItem>
          )}
          {/* Same rows as the "+" menu's, in this menu's own item
              component; Radix closes the menu on select, so no
              onPicked work is needed here. */}
          {stashInOverflow && (
            <PromptStashMenuItems
              host="dropdown"
              touch={touch}
              divider={(hasUpload && directFilePicker) || !!collapseMenuRow}
              stash={promptStash}
              chordLabel={promptStashChord}
              onPicked={() => {}}
            />
          )}
          </div>
        </DropdownMenuContent>
      </DropdownMenu>
    
  )
}
