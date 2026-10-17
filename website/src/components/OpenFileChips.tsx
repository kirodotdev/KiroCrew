import { memo, useMemo } from 'react'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { openFileMarkerPaths } from '../utils/detectOpenFileMarker'
import { fileIcon } from '../utils/fileIcons'
import { pathBasename } from '../utils/pathBasename'

/**
 * One "Open file" chip per file an assistant reply offers with an open-file
 * marker (`<!-- kirocrew:open-file path="/abs/notes.md" -->`, see
 * detectOpenFileMarker). The marker only decides that the chip is shown:
 * rendering, streaming and reloading the transcript open nothing.
 *
 * A click calls `onFileOpen` with the path and nothing else, which is exactly
 * how a click on a path chip calls it (`activatePath` in
 * components/markdown/pathReferences.ts). The host passes the same handler both
 * get, so the file opens through today's read, with its owner gate,
 * sensitive-path refusal and redaction, as a plain file tab, never a diff.
 *
 * Renders nothing without a handler (a host with no file viewer) or without a
 * usable marker.
 */
const OpenFileChips = memo(function OpenFileChips({ content, onFileOpen }: {
  /** The reply's text as the bubble shows it. */
  content: string
  /** The host's file opener: the same function the reply's path chips call. */
  onFileOpen?: (path: string) => void
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const paths = useMemo(() => openFileMarkerPaths(content), [content])
  if (!onFileOpen || paths.length === 0) return null
  return (
    // One chip per row: these are peer actions, and a row of more than two
    // buttons is a toolbar (and wrapping one does not make it less of one).
    <div className="ft-block-reveal flex flex-col items-start gap-1.5 mt-2 mb-1.5" data-testid="open-file-chips">
      {paths.map(path => {
        const Glyph = fileIcon(path)
        const name = pathBasename(path) || path
        return (
          <button
            key={path}
            type="button"
            onClick={() => onFileOpen(path)}
            className="glass-surface file-chip inline-flex max-w-full min-w-0 min-h-[22px] h-auto items-center gap-1 px-2.5 py-1 whitespace-normal text-left rounded-full text-[11px] font-medium cursor-pointer"
            data-path={path}
            // The accessible name is the visible text ("Open file notes.md"), so
            // a voice command that reads the label reaches the chip; the full
            // path is the tooltip and, through it, the accessible description.
            title={path}
          >
            <Glyph size={12} aria-hidden="true" className="shrink-0 opacity-70" />
            <span className="shrink-0 text-text">{i18nT('components.openFileChips.open_file')}</span>
            {/* A space in the accessible name between the label and the file name;
                whitespace-only text is not a flex item, so the layout is unchanged. */}
            {' '}
            <span className="min-w-0 font-mono text-muted [overflow-wrap:anywhere]">{name}</span>
          </button>
        )
      })}
    </div>
  )
})

export default OpenFileChips
