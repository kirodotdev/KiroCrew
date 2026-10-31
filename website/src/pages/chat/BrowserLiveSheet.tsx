import { useCallback, useContext, type ReactNode } from 'react'
import { QueryClient, QueryClientContext, QueryClientProvider } from '@tanstack/react-query'

import { Dialog, DialogContent, DialogTitle } from '../../components/ui/dialog'
import { i18nT } from '../../i18n/t'
import { browserLiveStore } from '../../hooks/useBrowserLive'
import { browserStageStore, useBrowserStage } from '../../hooks/useBrowserLive.stage'
import BrowserLiveStage from './BrowserLiveStage'

/**
 * The big view as a full-screen sheet: on a phone (where the side panel is not a
 * column beside the chat), and on any page with no side panel to open.
 *
 * Mounted by the card of the turn the stage shows, so there is one per stage.
 * A Radix dialog: it owns the focus trap, focus restore and Escape. Escape while
 * the person holds the browser goes to the canvas first (which hands back) and
 * stops there, so one Esc never both hands back and closes the sheet.
 */
/** The sheet can open where no query provider exists (the app-sdk embed mounts the
 *  card with none); it then reads recordings through a client of its own. */
let ownClient: QueryClient | null = null
function WithQueryClient({ children }: { children: ReactNode }) {
  const host = useContext(QueryClientContext)
  if (host) return <>{children}</>
  ownClient ??= new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={ownClient}>{children}</QueryClientProvider>
}

export default function BrowserLiveSheet({ sessionKey }: { sessionKey: string }) {
  const stage = useBrowserStage(sessionKey)
  const close = useCallback(() => browserStageStore.close(sessionKey), [sessionKey])
  const open = stage?.surface === 'sheet'
  return (
    <Dialog open={open} onOpenChange={o => { if (!o) close() }}>
      {open && stage && (
        <DialogContent
          maxWidth={100_000}
          style={{
            maxHeight: '100dvh',
            width: '100vw',
            height: '100dvh',
            borderRadius: 0,
            border: 0,
            background: 'var(--bg)',
            paddingTop: 'env(safe-area-inset-top)',
            paddingBottom: 'env(safe-area-inset-bottom)',
          }}
          data-testid="browser-live-sheet"
          // While the person holds the browser, Esc is the canvas's hand back, and
          // inside a quick ask's sheet it folds that sheet; the dialog listens in
          // the capture phase, so it must stand aside for both.
          onEscapeKeyDown={e => {
            const inAsk = e.target instanceof Element && !!e.target.closest('[data-quick-ask-sheet]')
            if (inAsk || browserLiveStore.inControl(sessionKey)) e.preventDefault()
          }}
        >
          <DialogTitle className="sr-only">{i18nT('pages.chat.browserLiveStage.sheet_title')}</DialogTitle>
          {/* Only the stage's header row makes room for the dialog's close button;
              the picture, timeline and steps keep the full width of the phone. */}
          <div className="flex-1 min-h-0 [&_[data-stage-header]]:pr-12">
            <WithQueryClient>
              <BrowserLiveStage sessionKey={sessionKey} stage={stage} active />
            </WithQueryClient>
          </div>
        </DialogContent>
      )}
    </Dialog>
  )
}
