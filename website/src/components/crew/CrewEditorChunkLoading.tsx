/**
 * The fallback a host shows while the lazy `CrewEditorModal` chunk downloads on
 * a cold-cache first open, so an Edit click gives immediate feedback instead of
 * rendering nothing until the chunk lands. Deliberately plain (no Radix dialog)
 * and NOT lazy, so it stays out of the editor's chunk. The editor's own
 * `crew-editor-next-loading` dialog takes over once the chunk is in and the
 * roster read is still pending.
 */
import { Loader2 } from 'lucide-react'
import { i18nT } from '../../i18n/t'

export default function CrewEditorChunkLoading() {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/60"
      role="dialog"
      aria-busy="true"
      aria-label={i18nT('pages.chatPage.loading')}
      data-testid="crew-editor-chunk-loading"
    >
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-5 py-4 text-[13px] text-muted shadow-lg">
        <Loader2 size={16} className="lucide-inline animate-spin" />
        {i18nT('pages.chatPage.loading')}
      </div>
    </div>
  )
}
