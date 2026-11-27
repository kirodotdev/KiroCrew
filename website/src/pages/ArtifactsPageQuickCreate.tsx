/** The Artifacts page's quick-create row: a Doc tile and a Canvas tile, each an
 *  icon with its name under it, for the two ways to start from blank, and a
 *  "more" trigger whose menu holds the other ways in (a new folder, importing a
 *  file). Each runs the action the page already owns; the side list's + also
 *  creates a folder.
 *
 *  The tiles are a palette of document types, like FigJam's or Figma's new-file
 *  row, so they read as one group of kinds rather than a row of peer actions;
 *  everything that is not a kind stays behind the overflow trigger.
 *
 *  Each `uiLocation` spread and label text sit on their own element: one id
 *  names one render site, and the index generator reads the label there. */
import { FilePlus, FileUp, FolderPlus, Loader2, MoreHorizontal, Shapes } from 'lucide-react'
import { Btn } from '../components/ui'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../components/ui/dropdown-menu'
import { uiLocation } from '../uiLocations/uiLocation'
import { useMenuOpensDialog } from '../hooks/useMenuOpensDialog'
import { i18nT } from '../i18n/t'

const TILE = 'flex-col items-center gap-1 w-[76px] px-1 py-2 border-transparent hover:border-border text-[12px] text-text'
const ICON_BOX = 'w-10 h-10 rounded-lg border border-border bg-bg-elevated flex items-center justify-center text-accent'
const LABEL = 'truncate min-w-0 max-w-full'

export function ArtifactsQuickCreate({ onNewDoc, newDocBusy, onNewCanvas, newCanvasBusy, onImport, importBusy, onNewFolder }: {
  onNewDoc: () => void
  newDocBusy: boolean
  onNewCanvas: () => void
  newCanvasBusy: boolean
  onImport: () => void
  importBusy: boolean
  onNewFolder: () => void
}) {
  // New folder mounts the side list's autofocused name field. Opened straight
  // from `onSelect`, the menu's focus trap and its close-time focus restore
  // pull focus out of that field, whose blur cancels it before a key is typed.
  // `openLayer` opens it after the close and keeps the close from restoring.
  const menuLayer = useMenuOpensDialog()
  return (
    <div role="group" aria-label={i18nT('pages.artifactsPage.create_new')} className="flex items-start gap-1 mb-4 -ml-1">
      <Btn
        onClick={onNewDoc}
        busy={newDocBusy}
        title={i18nT('pages.artifactsPage.start_a_new_blank_document_in_the_library')}
        className={TILE}
        {...uiLocation('artifacts.new')}
      >
        <span className={ICON_BOX}>{newDocBusy ? <Loader2 size={18} className="animate-spin" /> : <FilePlus size={18} />}</span>
        <span className={LABEL}>{i18nT('pages.artifactsPage.quick_doc')}</span>
      </Btn>
      <Btn
        onClick={onNewCanvas}
        busy={newCanvasBusy}
        title={i18nT('pages.artifactsPage.new_canvas')}
        className={TILE}
        data-testid="artifacts-quick-new-canvas"
      >
        <span className={ICON_BOX}>{newCanvasBusy ? <Loader2 size={18} className="animate-spin" /> : <Shapes size={18} />}</span>
        <span className={LABEL}>{i18nT('pages.artifactsPage.quick_canvas')}</span>
      </Btn>
      <DropdownMenu guideScope="menu:artifacts.add-menu">
        <DropdownMenuTrigger asChild>
          {/* Sits level with the tiles' icon boxes; icon-only, so its name is
            * the aria-label a screen reader announces. */}
          <Btn
            aria-label={i18nT('pages.artifactsPage.more_ways_to_add_an_artifact')}
            title={i18nT('pages.artifactsPage.more_ways_to_add_an_artifact')}
            busy={importBusy}
            className="mt-2 w-8 h-10 justify-center px-0 border-transparent hover:border-border text-muted"
            {...uiLocation('artifacts.add-menu')}
          >
            {importBusy ? <Loader2 size={16} className="animate-spin" /> : <MoreHorizontal size={16} />}
          </Btn>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" onCloseAutoFocus={menuLayer.onCloseAutoFocus}>
          <DropdownMenuItem onSelect={() => menuLayer.openLayer(onNewFolder)} {...uiLocation('artifacts.new-folder')}>
            <FolderPlus size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.new_folder')}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={onImport} {...uiLocation('artifacts.import')}>
            <FileUp size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.import_from_a_file')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
