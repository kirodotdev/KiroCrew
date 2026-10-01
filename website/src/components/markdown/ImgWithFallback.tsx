import React, { useContext, useEffect, useState } from 'react'
import type { ExtraProps } from 'react-markdown'
import { Check, Copy, Image as ImageIcon, ImageOff } from 'lucide-react'
import { getImageDims, rememberImageDims } from '../../utils/imageDims'
import { WINDOWS_ABS_PATH_RE, decodeLocalPath } from '../../utils/urlTransform'
import { i18nT } from '../../i18n/t'
import { BasePathCtx, CompactImagesCtx, ImageVersionCtx, MdSourceCtx } from './contexts'
import { useTitleCuedCopy } from './copyFeedback'
import {
  GatewayMediaRefused,
  REMOTE_MEDIA_CHIP_CLASS,
  RemoteHostFact,
  RemoteMediaDisclosure,
  isGatewayRouteMediaUrl,
  isRemoteMediaUrl,
  useApprovalFocus,
} from './remoteMedia'
import { dispatchLightbox } from './Lightbox'
import { relocateRequestUrl } from '../../lib/dashboardRuntime'

/** Markdown image with a React-rendered fallback chip when the URL is broken
 *  (see `BrokenImage`). The fallback is React-rendered rather than a hand-built
 *  SVG swapped in via .replaceWith(), so it never mutates DOM React owns —
 *  which could otherwise trigger "removeChild on Node" reconciliation crashes. */
/** Fallback chip for an image whose bytes failed to load.
 *
 * Chat images are read from disk at VIEW time (`/api/file-raw`), not stored in
 * the message — so the dominant failure is a local file that no longer exists
 * (a screenshot written to a temp directory that has since been cleaned), long
 * after the message rendered fine for its author. The chip names that
 * condition, and the whole chip is click-to-copy for the on-disk path:
 * recovery starts from knowing WHICH file is gone, and the path is the one
 * thing the transcript still holds.
 *
 * The `<img>` error event carries no status, so "file no longer exists" is
 * NOT asserted from the error alone — a backend hiccup, a sensitive-path
 * denial (403), or a file still being written all fire the same event. A
 * cheap HEAD probe re-asks the endpoint, and only a confirmed 404 (the
 * backend's not-found refusal) earns the missing-file wording; every other
 * outcome — including a failed probe — keeps the generic load-failure line,
 * so the chip never states a cause it did not verify. Remote URLs are never
 * probed: a cross-origin HEAD says nothing reliable and the generic wording
 * is already honest there.
 */
function BrokenImage({ path, alt, probeUrl }: { path: string; alt?: string; probeUrl?: string }) {
  const { copied, press, failureBubble } = useTitleCuedCopy(path, i18nT('components.markdownRenderer.couldnt_copy_the_image_path'))
  const [confirmedGone, setConfirmedGone] = useState(false)
  useEffect(() => {
    // The verdict belongs to THIS probeUrl. A reused instance handed a different
    // one must not keep the previous path's "confirmed missing" wording while its
    // own probe is still in flight.
    setConfirmedGone(false)
    if (!probeUrl) return
    let cancelled = false
    fetch(probeUrl, { method: 'HEAD' })
      .then(r => { if (!cancelled && r.status === 404) setConfirmedGone(true) })
      .catch(() => { /* unknown stays unknown — generic wording */ })
    return () => { cancelled = true }
  }, [probeUrl])
  const handleCopy = (e: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
    press(e.currentTarget)
  }
  // The path leads the tooltip (same rule as the file-path chip) so a
  // truncated chip still discloses the real target — except when alt is
  // empty: the visible label already IS the path, and repeating it in the
  // tooltip adds nothing.
  const idle = alt
    ? `${path}\n${i18nT('components.markdownRenderer.click_to_copy')}`
    : i18nT('components.markdownRenderer.click_to_copy')
  return (
    <>
    <span
      className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-bg-elevated px-2 py-1 text-sm text-muted cursor-pointer hover:text-text"
      role="button"
      tabIndex={0}
      onClick={handleCopy}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') handleCopy(e) }}
      title={copied ? i18nT('components.markdownRenderer.copied') : idle}
    >
      <ImageOff size={14} aria-hidden="true" className="shrink-0" />
      <span className="truncate">{alt || path}</span>
      <span className="shrink-0 opacity-75">
        {confirmedGone
          ? i18nT('components.markdownRenderer.image_file_no_longer_exists')
          : i18nT('components.markdownRenderer.image_failed_to_load')}
      </span>
      {copied
        ? <Check size={12} aria-hidden="true" className="shrink-0 text-ok" />
        : <Copy size={12} aria-hidden="true" className="shrink-0 opacity-70" />}
    </span>
    {failureBubble}
    </>
  )
}
/** Style reserving a not-yet-loaded transcript image's EXACT display box.
 *
 * The loaded layout follows the replaced-element min/max rules, which
 * BACK-PROPAGATE a max-height cap into the width (a tall screenshot capped at
 * 60vh also narrows). Neither width/height attributes nor a bare aspect-ratio
 * reproduce that transfer — with either, max-height clamps the box's height
 * while the width stays at max-width, leaving the image letterboxed centered
 * inside a full-width border band. So spell the native resolution out:
 * width = min(natural, heightCap × ratio), the class's max-width still capping
 * on top; aspect-ratio derives the height. Same expression the loaded image
 * resolves to, so the reserve is invisible — same size, same left edge,
 * border hugging the image.
 */
export function reservedImageStyle(dims: { w: number; h: number }): React.CSSProperties {
  // NUMBERS only — the min()/calc()/aspect-ratio arithmetic lives in the
  // `.mc-img-reserve` rule (index.css), which is where a CSS value belongs and
  // keeps this component free of CSS-shaped string literals.
  return { '--mc-img-w': dims.w, '--mc-img-h': dims.h } as React.CSSProperties
}

/** Class pair applying `reservedImageStyle`'s custom properties: the shared
 *  reserve arithmetic plus the mode's height cap (see index.css). */
export function reservedImageClass(compact: boolean): string {
  return compact ? 'mc-img-reserve mc-img-reserve-compact' : 'mc-img-reserve'
}

/** Fixed placeholder box for an image whose dimensions are not yet known
 *  (first-ever load, nothing learned). An unloaded <img> has NO intrinsic
 *  size — the max-w/max-h classes are only caps, so without a definite box it
 *  collapses to a 0-wide border sliver. A fixed ~16:9 box (not full width —
 *  full-width placeholders stack into a wall when a message carries several
 *  images) reserves believable space; the compact box matches the sent-prompt
 *  thumbnail caps exactly. Numbers are the DISPLAY size, so they sit under
 *  each mode's max-w/max-h caps. */
export function pendingImageBoxStyle(compact: boolean): React.CSSProperties {
  return compact ? { width: '240px', height: '180px' } : { width: '420px', height: '236px' }
}

export function ImgWithFallback({
  node,
  src,
  alt,
  ...props
}: React.ImgHTMLAttributes<HTMLImageElement> & ExtraProps) {
  const [errored, setErrored] = useState(false)
  const [loaded, setLoaded] = useState(false)
  // A remote image the user explicitly chose to load.
  // Per-src like the outcome flags: a reused instance handed a different src
  // must not inherit the previous image's approval.
  const [remoteApproved, setRemoteApproved] = useState(false)
  const approvedImageRef = useApprovalFocus<HTMLSpanElement>(remoteApproved)
  // Both flags describe the outcome of loading THIS `src`, so neither may
  // outlive it. React reuses an instance whenever the element at a key keeps its
  // type, so a reused image can be handed a different `src`; without this a good
  // image inherits a previous one's failure and renders as broken, with nothing
  // to clear it short of a full remount. Adjusted during render rather than in an
  // effect, per React's own guidance for resetting state on a prop change: an
  // effect runs after paint, so it would show one frame of the previous image's
  // outcome. Setting state here is a bail-out when the value is unchanged.
  const [outcomeSrc, setOutcomeSrc] = useState(src)
  if (outcomeSrc !== src) {
    setOutcomeSrc(src)
    setErrored(false)
    setLoaded(false)
    setRemoteApproved(false)
  }
  const basePath = useContext(BasePathCtx)
  const compact = useContext(CompactImagesCtx)
  const version = useContext(ImageVersionCtx)
  const source = useContext(MdSourceCtx)
  if (!src) return null
  // A Windows drive/UNC path (`C:/…` — urlTransform passes it through for
  // image src) is as local as a POSIX `/…` path and must route to
  // /api/file-raw the same way; it must NOT take the basePath-relative branch
  // below, which is only for genuinely relative paths (issue #3497).
  const isWinAbs = WINDOWS_ABS_PATH_RE.test(src)
  // Root-relative gateway routes are URLs, not on-disk paths. Keeping them
  // out of the file-path rewrite lets the media gate defer proxy endpoints
  // while allowing only its explicit local-bytes routes through.
  const isGatewayRoute = src.startsWith('/api/')
  const isLocal = (!isGatewayRoute && (src.startsWith('/') || src.startsWith('~') || src.startsWith('.') || isWinAbs))
    || (basePath && !src.startsWith('http') && !isGatewayRoute)
  let url: string
  // The on-disk path the backend is asked to read — what the broken-image
  // fallback discloses and copies. Stays `src` verbatim for remote URLs.
  let diskPath = src
  if (isLocal) {
    // micromark percent-encodes destinations in BOTH forms, so wrap-ness is
    // recovered from the source text at this node's position: only a
    // `<…>`-wrapped destination is producer-emitted (mdImageDest) and safe to
    // decode back to the on-disk path. An unwrapped one is legacy content —
    // a file literally named `photo%20copy.png` must stay verbatim, exactly
    // as it resolved before destinations were ever encoded. decodeLocalPath
    // keeps the raw form on malformed sequences and on decoded control
    // characters (a `%00` NUL would crash the backend's realpath).
    const start = node?.position?.start?.offset
    const end = node?.position?.end?.offset
    const wrapped = source != null && start != null && end != null
      && /\]\(\s*</.test(source.slice(start, end))
    const localPath = wrapped ? decodeLocalPath(src) : src
    if (basePath && !src.startsWith('/') && !src.startsWith('~') && !isWinAbs) {
      const resolved = basePath.replace(/\/[^/]*$/, '') + '/' + localPath
      diskPath = resolved
      url = `/api/file-raw?path=${encodeURIComponent(resolved)}`
    } else {
      diskPath = localPath
      url = `/api/file-raw?path=${encodeURIComponent(localPath)}`
    }
    // See ImageVersionCtx: without this every impression of a rewritten file
    // shares one cache entry and a new message renders the previous bytes. The
    // backend reads only `path`, so the extra parameter is inert server-side.
    if (version) url += `&v=${encodeURIComponent(version)}`
  } else {
    url = src
  }
  // Media checks use the logical URL; only byte requests use the pane URL.
  const loadUrl = relocateRequestUrl(url)
  if (isGatewayRouteMediaUrl(url)) return <GatewayMediaRefused />
  if (isRemoteMediaUrl(url) && !remoteApproved) {
    return (
      <button
        type="button"
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); setRemoteApproved(true) }}
        title={src}
        className={REMOTE_MEDIA_CHIP_CLASS}
      >
        <ImageIcon size={14} aria-hidden="true" className="shrink-0" />
        <span className="font-medium text-text transition-colors group-hover/remote-media:text-accent">
          {i18nT('components.markdownRenderer.remote_image_click_to_load')}
        </span>
        <RemoteMediaDisclosure description={alt || undefined} remotes={[src]} />
      </button>
    )
  }
  if (errored) {
    return <BrokenImage path={diskPath} alt={alt} probeUrl={isLocal ? loadUrl : undefined} />
  }
  // SVGs authored with only a `viewBox` (no width/height) carry no intrinsic
  // size. Under the max-w/max-h-only CSS below they collapse to ~0px and look
  // missing — so uploading several SVGs appears to render only the ones that
  // happen to declare width/height. Give SVGs a definite width basis; the
  // viewBox aspect ratio then derives the height, clamped by max-h.
  const isSvg = /\.svg([?#]|$)/i.test(src)
  // Reserve layout space BEFORE the bytes decode. A markdown image has no
  // intrinsic dimensions in the source, so without this it lays out at ~0px
  // (zero WIDTH too — an unloaded <img> has no intrinsic size and max-width is
  // only a cap, so the element collapses to a border-thin sliver) until the
  // network/decode completes, then snaps to its natural size — shoving every
  // sibling below it (still-streaming text, the next block) down in one
  // discrete jump. For a user reading a streaming message (or lazily loading
  // an image below the fold) that reads as a "flash". Holding a placeholder
  // box until `onLoad` reserves the space up front and bounds the on-load
  // shift; the placeholder is released once loaded so the final layout is
  // pixel-exact and history/completed images carry no reserve.
  // The box is a FIXED size, not full-width (a deliberate product decision:
  // a full-width band reads as a much larger pending change than the image
  // usually is, and several loading images stack into a wall). The size is a
  // heuristic (markdown gives us no aspect ratio): a ~16:9 box near the
  // common screenshot case, sized under each mode's max-w/max-h caps so the
  // pending box never exceeds what the loaded image could occupy. See
  // MarkdownRenderer.streamingImageShift.test.tsx.
  // Learned exact dimensions trump the heuristic box: a transcript image
  // remounts every time the virtualized window scrolls back over it, and a
  // heuristic box under a 400-600px screenshot still realizes the difference
  // as a visible jump on every (re)load. Recording naturalWidth/Height on
  // first successful load (keyed by resolved URL, same mechanism as the
  // artifact gallery's thumbnails) lets every later mount reserve the real
  // aspect box before any bytes arrive.
  const learned = !isSvg ? getImageDims(url) : undefined
  // The reserved box must resolve to EXACTLY the size the loaded image will
  // take, or the difference shows as a border wrapping empty space with the
  // image floated centered inside (object-contain letterboxing). The loaded
  // layout follows the replaced-element min/max rules, which BACK-PROPAGATE a
  // max-height cap into the width (a tall screenshot capped at 60vh also
  // narrows). Neither width/height attributes nor an explicit aspect-ratio
  // reproduce that transfer — with either, max-height clamps the box's height
  // while the width stays at max-width, leaving a wide letterboxed band. So
  // spell the native resolution out: width = min(natural, heightCap × ratio),
  // with the class's max-width still capping on top; aspect-ratio then derives
  // the height. Same expression the loaded image resolves to, so the reserve
  // is invisible — same size, same left edge, border hugging the image.
  const imgStyle: React.CSSProperties | undefined = isSvg
    ? { width: compact ? '240px' : '760px', height: 'auto' }
    : learned
      ? reservedImageStyle(learned)
      : (loaded ? undefined : pendingImageBoxStyle(compact))
  // Sent-prompt (user message) images render as a small preview so an attached
  // screenshot doesn't dominate the bubble; the lightbox still opens full size
  // on click. Response images keep the large inline size. See CompactImagesCtx.
  // The className stays inline in the JSX attribute (rather than hoisted to a
  // variable) so the i18n lint's className exemption still recognizes these as
  // class strings, not untranslated copy.
  return (
    // tabIndex=-1: focusable by script (the approval hand-off) but never a Tab
    // stop of its own, so the gate adds no new stop for users who never
    // approve anything.
    <span className="relative block my-2" ref={approvedImageRef} tabIndex={-1}>
      {/* Loading skeleton: a decorative overlay ON TOP of the (still
          transparent) <img>, never a wrapper around it — the img's own layout
          contract (ms-auto on the IMG, definite max-w caps, no shrink-to-fit
          wrapper; see the className comment below) must not change shape
          between loading and loaded. The overlay is a SIBLING that replicates
          the img's box (same reserve class/style, same caps, same edge
          alignment) and unmounts on load, so the img itself never remounts.
          pointer-events-none keeps hover/click reaching the img. */}
      {!loaded && !isSvg && (
        <span
          aria-hidden="true"
          className={`pointer-events-none absolute top-0 ${compact ? 'end-0' : 'start-0'} flex items-center justify-center overflow-hidden rounded-md border border-border bg-bg-accent ${learned ? reservedImageClass(compact) + ' ' : ''}${compact ? 'max-w-[240px] max-h-[180px]' : 'max-w-[min(100%,760px)] max-h-[60vh]'}`}
          style={learned ? reservedImageStyle(learned) : pendingImageBoxStyle(compact)}
        >
          <span className="absolute inset-0 animate-pulse bg-bg-hover" />
          <ImageIcon size={28} className="relative animate-pulse text-muted" aria-hidden="true" />
        </span>
      )}
      {/* The <img> is the lightbox trigger; dispatchLightbox needs the image
          element itself as currentTarget and the [data-lightbox-image] query
          relies on it being an <img>, so it can't be a <button>. Keyboard users
          reach the same lightbox via other focusable controls; a visible <img>
          preview is presentational here. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions */}
      <img
        src={loadUrl} alt={alt || ''} loading="lazy"
        // A remote image the user approved is fetched with NO referrer. The
        // approval binds the request this renderer initiates, but a server can
        // still 302 it onward, and a redirect target that receives the
        // dashboard URL learns the conversation it was embedded in. Suppressing
        // the referrer costs nothing here (no remote host needs it to serve an
        // image) and is the part of the redirect residual a renderer CAN close;
        // binding the final host needs the fetch under our control, which the
        // RFC records as a step-3 decision.
        {...(isRemoteMediaUrl(url) ? { referrerPolicy: 'no-referrer' as const } : {})}
        // Sent-prompt images align to the END edge, matching the bubble they
        // were sent from. `ms-auto` (logical, RTL-correct) sits on the IMG, never
        // on its wrapper: preflight makes <img> display:block so text-align is
        // inert here, and a shrink-to-fit wrapper makes the percentage in
        // `max-w-[min(100%,240px)]` resolve against its own content — silently
        // dropping the 240px cap and scattering mixed-width images. It reads
        // right only because the bubble shrink-wraps (`w-fit` in UserMessage):
        // inside a bubble stretched to its cap, moving the image to one edge
        // only moves the empty band to the other. The cap is a DEFINITE 240px,
        // not `min(100%,240px)`: a percentage max-width makes the image's
        // max-content contribution indefinite, so the bubble's `w-fit` falls
        // back to the full available width and the band never closes. 240px sits
        // below the bubble's own cap at every width the app supports, so the
        // percentage guard was redundant.
        className={`${learned && !isSvg ? reservedImageClass(compact) + ' ' : ''}${compact
          ? 'ms-auto max-w-[240px] max-h-[180px] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity'
          : 'max-w-[min(100%,760px)] max-h-[60vh] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity'}`}
        style={imgStyle}
        onClick={(e) => dispatchLightbox(e.currentTarget)}
        data-lightbox-image=""
        title={alt || src}
        onLoad={(e) => {
          const el = e.currentTarget
          if (el.naturalWidth > 0 && el.naturalHeight > 0) rememberImageDims(url, el.naturalWidth, el.naturalHeight)
          setLoaded(true)
        }}
        onError={() => setErrored(true)}
        {...props}
      />
      {/* Provenance survives the click. Once loaded, a remote image is pixel
          for pixel indistinguishable from a local one, so the only record of
          where it came from would be a chip that no longer exists — and a
          reader coming back to the conversation, or reading it with a screen
          reader, has no way to tell that this chart was fetched from the
          network. The same one-key sentence the chip used, in a muted caption. */}
      {isRemoteMediaUrl(url) && <RemoteHostFact remotes={[url]} loaded className="mt-1 block text-[11px] leading-relaxed text-muted" />}
    </span>
  )
}
