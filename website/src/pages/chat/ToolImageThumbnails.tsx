import { useState } from 'react'
import { ImageOff } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import { dispatchLightbox } from '../../components/MarkdownRenderer'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { pathBasename } from '../../utils/pathBasename'

const NO_FAILURES: ReadonlySet<string> = new Set()

/** Thumbnail strip for the images an image-mode `read` call looked at, drawn
 *  under the folded tool row so the reader can see WHICH picture the agent read
 *  without expanding the Input panel.
 *
 *  The strip is ONE control: a click on a thumbnail opens the shared image
 *  viewer at that image, with every loaded thumbnail of this row as one
 *  navigable set (the button's `data-image-scope` is the set's boundary, see
 *  `dispatchLightbox`); a click elsewhere on it, or Enter, opens the first.
 *
 *  Bytes come from `/api/file-raw` at VIEW time, like a reply's local image.
 *  `version` keys the URL per tool call, so a file the agent rewrote and read
 *  again does not show the earlier call's cached bytes in the new row.
 *
 *  An image that does not load keeps its place as a same-size tile with an
 *  icon, so the strip keeps one shape, and the row names the files that did
 *  not load in an inline error notice. The `<img>` error event carries no
 *  status, so the notice says only that the image did not load, never why. */
export function ToolImageThumbnails({ paths, version }: { paths: readonly string[]; version?: string }) {
  const suffix = version ? `&v=${encodeURIComponent(version)}` : ''
  const srcOf = (path: string) => `/api/file-raw?path=${encodeURIComponent(path)}${suffix}`
  // Failures belong to the srcs that produced them: a reused instance handed
  // other paths must not keep the previous row's failed tiles. Reset during
  // render (React's guidance for state derived from a prop), so no frame shows
  // the stale outcome.
  const key = paths.map(srcOf).join('\n')
  const [failedKey, setFailedKey] = useState(key)
  const [failed, setFailed] = useState<ReadonlySet<string>>(NO_FAILURES)
  if (failedKey !== key) {
    setFailedKey(key)
    setFailed(NO_FAILURES)
  }
  if (!paths.length) return null
  const names = paths.map(p => pathBasename(p) || p)
  const failedNames = paths.filter(p => failed.has(srcOf(p))).map(p => pathBasename(p) || p)
  const allFailed = failedNames.length === paths.length
  return (
    // ml-5 = the pill's icon (12px) + its gap (8px): the strip starts where
    // the label text does, the same anchor as the row's status lines.
    <div className="ml-5 mt-1 flex flex-col items-start gap-1">
      <button
        type="button"
        data-testid="tool-image-thumbnails"
        data-image-scope=""
        disabled={allFailed}
        aria-label={i18nT('components.chatInput.open_preview_of', { name: fmtList(names, { style: 'narrow' }) })}
        className="flex flex-wrap gap-1.5 rounded cursor-pointer disabled:cursor-default focus-ring-accent"
        onClick={(e) => {
          // The row's pill toggles on click and TurnBlock binds a fold handler
          // on an ancestor; opening a preview must do neither.
          e.stopPropagation()
          const target = e.target instanceof Element ? e.target.closest('img[data-lightbox-image]') : null
          const img = (target ?? e.currentTarget.querySelector('img[data-lightbox-image]')) as HTMLImageElement | null
          if (img) dispatchLightbox(img)
        }}
      >
        {paths.map(path => {
          const src = srcOf(path)
          if (failed.has(src)) {
            return (
              <span
                key={path}
                data-testid="tool-image-thumb-missing"
                title={path}
                className="w-12 h-12 inline-flex items-center justify-center rounded border border-dashed border-border text-muted"
              >
                <ImageOff size={16} aria-hidden="true" />
              </span>
            )
          }
          // Fixed square tile, center-cropped: the strip only has to make the
          // image recognisable, and the full image is one click away. The
          // error listener swaps in the failed tile; the image sits inside the
          // strip's button and is not itself interactive.
          return (
            // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
            <img
              key={path}
              data-testid="tool-image-thumb"
              src={src}
              alt={path}
              title={path}
              loading="lazy"
              data-lightbox-image=""
              className="w-12 h-12 rounded border border-border object-cover bg-bg-hover hover:opacity-80 transition-opacity"
              onError={() => setFailed(prev => new Set(prev).add(src))}
            />
          )
        })}
      </button>
      {/* askAgent on: a transcript row holds no draft, and a missing file is
          one the agent can look for or regenerate. */}
      {failedNames.length > 0 && (
        <ErrorNotice
          variant="inline"
          message={`${i18nT('components.markdownRenderer.image_failed_to_load')}: ${fmtList(failedNames, { style: 'narrow' })}`}
          askAgent
          testId="tool-image-thumb-error"
        />
      )}
    </div>
  )
}
