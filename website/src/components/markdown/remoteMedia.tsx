import React, { createContext, useContext, useEffect, useRef, useState } from 'react'
import { Trans } from 'react-i18next'
import { Film, ImageOff, Volume2 } from 'lucide-react'
import type { Element as HastElement } from 'hast'
import { i18nT } from '../../i18n/t'
import { spa } from './elements'

/**
 * REMOTE (http/https) markdown images always render as click-to-load
 * placeholders instead of fetching automatically. Agent-written markdown is
 * untrusted, and an auto-loading `<img src="https://…?d=<data>">` is a
 * zero-click request — the browser sends it the moment the message renders, so
 * prompt-injected content could exfiltrate conversation data through the URL
 * with nobody clicking anything. The placeholder keeps the destination host
 * visible and loads only on the user's explicit click. Local images
 * (`/api/file-raw` same-origin reads of files on this machine) are unaffected —
 * they make no outbound request. Deferral is UNCONDITIONAL: there is no
 * context, prop, or flag through which any caller could disable it. See
 * docs/request-for-change/rfc-redaction-explain-and-reveal.md §5.
 */

function remoteHost(value: string): string {
  try { return new URL(value, window.location.href).host } catch { return value }
}

/** Hosts shown to the user are derived from the same remote URL collection
 * that controls the gate and approval scope. Preserve first-seen order while
 * removing duplicate hosts. */
function distinctHosts(remotes: readonly string[]): string[] {
  return [...new Set(remotes.map(remoteHost))]
}

/** The disclosed hosts as ONE string. A span per host bought nothing a single
 *  `break-all` span does not, and the label and value now share one
 *  translatable sentence, so the value has to be a single interpolated node. */
function hostSentence(remotes: readonly string[]) {
  return distinctHosts(remotes).join(', ')
}

/** `Site: <host/>` as ONE key. A key that ends in a colon leaves the rest of its
 *  own sentence outside it, so a translator who needs the value first — or a
 *  narrow no-break space before the colon, as French does — cannot express that
 *  without a code change. The placeholder is SELF-CLOSING because a closing tag
 *  in a catalog value reads as raw JSX to the catalog's integrity check; Trans
 *  fills it with the styled host span.
 *
 *  *loaded* switches to the past tense, because the same host carries two
 *  different facts either side of the click: on the chip it is where the file
 *  WOULD be fetched from, and under a mounted image or player it is where the
 *  file DID come from. One string for both reads as the first sense in a place
 *  that means the second. */
export function RemoteHostFact({
  remotes,
  className,
  loaded = false,
}: {
  remotes: readonly string[]
  className?: string
  loaded?: boolean
}) {
  const hosts = distinctHosts(remotes)
  return (
    <span className={className ?? 'mt-0.5 block basis-full text-start text-[11px] leading-relaxed text-muted'}>
      <Trans
        i18nKey={loaded
          ? 'components.markdownRenderer.remote_media_loaded_from'
          : hosts.length > 1
            ? 'components.markdownRenderer.remote_media_sites'
            : 'components.markdownRenderer.remote_media_site'}
        components={{
          host: (
            <span className="break-all font-mono text-[12px] font-medium text-text">
              {hostSentence(remotes)}
            </span>
          ),
        }}
      />
    </span>
  )
}

/** One class for BOTH click-to-load chips, so the image gate and the media gate cannot
 *  drift apart the way their predicates once did.
 *
 *  The resting state carries the button affordance: a `border-strong` boundary and a
 *  raised `bg-hover` surface make the clickable area obvious before the pointer arrives
 *  (a reader of the previous hairline-on-flat version read it as a callout panel and
 *  could not tell what was clickable -- bad on a security-critical control). The label
 *  deliberately stays plain text at rest and picks up accent only on hover: this app's
 *  anchor style IS accent text, so accenting a label inside a button reads as a nested
 *  hyperlink, which is the defect that de-linking fixed. */
/** Approving remote media UNMOUNTS the button that was focused, so a keyboard
 *  user is left with focus on nothing: the browser falls back to <body> and the
 *  next Tab restarts from the top of the chat, losing their place in a
 *  transcript that can be hundreds of messages long. A sighted mouse user never
 *  notices, which is why this only shows up when you drive the gate from the
 *  keyboard. So the element that REPLACES the button takes the focus, making the
 *  approval behave like every other in-place expansion: focus stays where the
 *  content appeared. Only on a real approval -- an element that mounts already
 *  approved (a local image, a re-render after the message scrolled back into
 *  view) must NOT steal focus from wherever the user actually is. */
export function useApprovalFocus<T extends HTMLElement>(approved: boolean) {
  const target = useRef<T | null>(null)
  const approvedBefore = useRef(approved)
  useEffect(() => {
    const justApproved = approved && !approvedBefore.current
    approvedBefore.current = approved
    if (justApproved) target.current?.focus()
  }, [approved])
  return target
}

export const REMOTE_MEDIA_CHIP_CLASS =
  // NAMED group, and the label below pairs with the same name. A bare `group`
  // compiles to `.group:hover .group-hover\:…`, which ANY hovered ancestor
  // carrying `group` satisfies -- the message wrapper carries one, so hovering
  // anywhere in the message lit up EVERY chip's label at once. On a control whose
  // whole job is to say which single element you are about to approve, a
  // highlight that fires for a pointer nowhere near it is a false affordance.
  'group/remote-media inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md'
  + ' border border-border-strong bg-bg-hover px-2.5 py-1.5 text-sm text-muted'
  + ' cursor-pointer transition-colors hover:border-accent hover:bg-bg-elevated'

export function RemoteMediaDisclosure({
  description,
  remotes,
}: { description?: string; remotes: readonly string[] }) {
  const hostCount = distinctHosts(remotes).length
  return (
    <>
      {/* The host is disclosure, not a second control. It used to sit on the
          action row ahead of the label, where a monospace token in front of
          "click to load" read as a separate, possibly-clickable link — a reader
          said they "would not dare" click it. Here it is plainly the value of a
          labelled fact, while still being the full host, never truncated, and
          still derived from the same collector output the approval unlocks. */}
      <RemoteHostFact remotes={remotes} />
      {/* One consequence line, not two. The deleted second line ("Other
          external content stays blocked.") restated the scope this sentence
          already carries in the word "only", and the pair repeated under every
          chip -- four times in one reply in the review capture. Repetition
          reads as boilerplate, and boilerplate is what a reader skips; the
          sentence they DO read has to be the one that carries the fact. */}
      <span className="block basis-full text-start text-[11px] leading-relaxed text-muted">
        {i18nT(hostCount > 1
          ? 'components.markdownRenderer.remote_media_loads_once_plural'
          : 'components.markdownRenderer.remote_media_loads_once')}
      </span>
      {description && (
        <span className="block basis-full text-start text-[11px] leading-relaxed text-muted">
          {i18nT('components.markdownRenderer.remote_media_model_description', { description })}
        </span>
      )}
    </>
  )
}

/** Same-origin routes proven to serve local media bytes without proxying a
 *  model-selected remote URL. Keep this list narrow: every other http(s) URL,
 *  including same-origin gateway routes such as `/api/link-meta`, is deferred. */
const SAFE_LOCAL_MEDIA_PATH_PREFIXES = ['/api/file-raw'] as const

/** True for an http(s) media URL that must wait for an explicit click.
 *  Classified with the browser's own URL parser (`new URL(value,
 *  location.href)`) rather than a hand-written prefix check, because the fetch
 *  will use that parser too. Cross-origin URLs always defer. Same-origin URLs
 *  also defer unless their pathname is an explicitly allowlisted local-bytes
 *  route. Non-http(s) schemes make no request and are left to the markdown URL
 *  transform's existing policy. */
export function isRemoteMediaUrl(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const s = value.trim()
  if (!s) return false
  let u: URL
  try { u = new URL(s, window.location.href) } catch { return false }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false
  if (u.origin !== window.location.origin) return true
  return !SAFE_LOCAL_MEDIA_PATH_PREFIXES.some(prefix =>
    u.pathname === prefix || u.pathname.startsWith(`${prefix}/`),
  )
}

/** True for a same-origin http(s) URL outside the local-bytes allowlist: a
 *  gateway route such as `/api/link-meta?url=…` that may make the server fetch
 *  some other, model-chosen host. The chip can only name the host the browser
 *  contacts, which for these is the dashboard itself, so a click-to-load chip
 *  would ask for consent while naming the wrong destination. These are refused
 *  outright instead of deferred: nothing loads, and there is no button. */
export function isGatewayRouteMediaUrl(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const s = value.trim()
  if (!s) return false
  let u: URL
  try { u = new URL(s, window.location.href) } catch { return false }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false
  if (u.origin !== window.location.origin) return false
  return isRemoteMediaUrl(s)
}

/** The refusal shown in place of media whose address is a gateway route. No
 *  button and no host: there is nothing honest to consent to. */
export function GatewayMediaRefused() {
  return (
    <span
      className="inline-flex max-w-full items-center gap-2 rounded-md border border-border bg-bg-hover px-2 py-1 text-[12px] text-muted"
      data-testid="remote-media-gateway-refused"
    >
      <ImageOff size={14} aria-hidden="true" className="shrink-0" />
      {i18nT('components.markdownRenderer.remote_media_gateway_refused')}
    </span>
  )
}

/** `srcset` is a comma-separated candidate list (`a.webp 1x, b.webp 2x`), so
 *  it is checked per candidate — a remote SECOND candidate must not hide
 *  behind a local first one. Splitting on whitespace AND commas over-splits a
 *  URL that itself contains a comma, but every fragment is still URL-tested,
 *  so over-splitting can only classify MORE values as remote, never fewer. */
function srcsetHasRemote(value: unknown): boolean {
  if (typeof value !== 'string') return false
  return value.split(/[\s,]+/).some(isRemoteMediaUrl)
}

/** Approval scope for a media element's children: a `<source>` inside a
 *  video/audio the user clicked to load may render; one outside stays
 *  dropped. See DeferredMedia. */
const MediaApprovedCtx = createContext<boolean>(false)

/** `<video>` / `<audio>` under the unconditional remote-media deferral.
 *
 *  The image deferral (ImgWithFallback) covers only `img`, but the sanitizer's
 *  tag allowlist also admits raw-HTML `video`/`audio`/`source`, whose `src`,
 *  `poster` and child-source URLs the browser fetches on mount — `poster`
 *  unconditionally, `src` per `preload`. An agent-written
 *  `<video poster="https://…?d=<data>">` would therefore be the same
 *  zero-click request the img gate exists to stop. So a media element that
 *  references ANY remote URL renders as the same click-to-load chip, and only
 *  the user's click mounts the native element (children included, via
 *  MediaApprovedCtx). Media with only non-remote references mounts directly.
 */
function remoteSrcsetUrls(value: unknown): string[] {
  if (typeof value !== 'string') return []
  return value
    .split(',')
    .map(candidate => candidate.trim().split(/\s+/, 1)[0])
    .filter(isRemoteMediaUrl)
}

function collectRemotes(node?: HastElement): string[] {
  const props = node?.properties ?? {}
  const remotes: string[] = []
  for (const key of ['src', 'poster']) {
    const value = props[key]
    if (isRemoteMediaUrl(value)) remotes.push(String(value))
  }
  // Every DESCENDANT `<source>`, not only direct children: `<picture>` is an
  // admitted tag, so `<video><picture><source srcset=…>` puts a remote source one
  // level down. A collector that stops at depth one would leave that host out of
  // the chip while a click still fetched it -- the disclosed set has to be the
  // set the approval unlocks, or the approval is for something else.
  const walk = (parent?: HastElement): void => {
    for (const child of parent?.children ?? []) {
      if (child.type !== 'element') continue
      if (child.tagName === 'source') {
        const src = child.properties?.src
        if (isRemoteMediaUrl(src)) remotes.push(String(src))
        remotes.push(...remoteSrcsetUrls(child.properties?.srcSet))
      }
      walk(child as HastElement)
    }
  }
  walk(node)
  return remotes
}

export function DeferredMedia({ tag, node, children }: { tag: 'video' | 'audio'; node?: HastElement; children?: React.ReactNode }) {
  const [approved, setApproved] = useState(false)
  // <video controls>/<audio controls> are focusable in their own right, so the
  // approved element itself receives the focus the button gave up.
  const approvedMediaRef = useApprovalFocus<HTMLVideoElement & HTMLAudioElement>(approved)
  const props = node?.properties ?? {}
  // One collection controls whether the gate renders, exactly what an approval
  // unlocks, its reset signature, and every host disclosed on the button.
  const remotes = collectRemotes(node)
  const description = [props.alt, props.title, props.ariaLabel]
    .find(value => typeof value === 'string' && value.trim()) as string | undefined
  // Approval belongs to THIS set of remote URLs, so it must not outlive it.
  // React reuses the instance at a stable render position (a streaming
  // message re-renders in place), so a media element the user approved for
  // URL X that is then swapped to URL Y would otherwise mount Y with no
  // click — the exact zero-click fetch this gate exists to stop. Same
  // render-time bail-out pattern as ImgWithFallback's outcomeSrc reset.
  //
  // The signature must be INJECTIVE, not merely derived from the collector: a
  // delimiter join is not. An HTML attribute may contain a newline, so
  // ['a\nb'] and ['a','b'] share one `join('\n')` — and two different remote
  // sets with one signature is approval inheritance, which is this gate's own
  // failure mode wearing the collector's clothes. JSON.stringify escapes the
  // separator, so distinct sets have distinct signatures by construction.
  const sig = JSON.stringify(remotes)
  const [approvedSig, setApprovedSig] = useState(sig)
  if (approvedSig !== sig) {
    setApprovedSig(sig)
    setApproved(false)
  }
  if (remotes.some(isGatewayRouteMediaUrl)) return <GatewayMediaRefused />
  if (!approved && remotes.length > 0) {
    return (
      <button
        type="button"
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); setApproved(true) }}
        title={remotes.join('\n')}
        className={REMOTE_MEDIA_CHIP_CLASS}
      >
        {tag === 'video'
          ? <Film size={14} aria-hidden="true" className="shrink-0" />
          : <Volume2 size={14} aria-hidden="true" className="shrink-0" />}
        <span className="font-medium text-text transition-colors group-hover/remote-media:text-accent">
          {i18nT(tag === 'video'
            ? 'components.markdownRenderer.remote_video_click_to_load'
            : 'components.markdownRenderer.remote_audio_click_to_load')}
        </span>
        <RemoteMediaDisclosure description={description} remotes={remotes} />
      </button>
    )
  }
  const El = tag
  return (
    // `approved`, never a literal: this branch is also where a media element with
    // NOTHING remote to gate mounts, and a context that claims approval there
    // tells `MdSourceEl` to keep a remote `<source>` it would otherwise drop.
    // Approval has one source of truth, and this is a reader of it.
    <MediaApprovedCtx.Provider value={approved}>
      {/* No referrer suppression here, unlike the approved <img>: referrerPolicy
          is a content attribute of a/area/img/iframe/link/script only, and a
          media element has no per-element equivalent — its fetch follows the
          document policy. So the redirect residual is narrower for images than
          for media, which the RFC records rather than papers over. */}
      <El {...spa(tag, node)} ref={approvedMediaRef}>{children}</El>
      {/* Provenance outlives the approval here for the same reason it does under
          an approved image: a player with a poster frame carries no visible
          trace of which host served it, and the chip that said so is gone. A
          disclosure that only exists before the click is a disclosure the reader
          cannot go back and check. A player with nothing remote gets no caption:
          a local file was never fetched from a host, so naming one would be a
          claim about a request that did not happen. */}
      {remotes.length > 0 && (
        <RemoteHostFact remotes={remotes} loaded className="mt-1 block text-[11px] leading-relaxed text-muted" />
      )}
    </MediaApprovedCtx.Provider>
  )
}

/** `<source>` under the unconditional remote-media deferral: a remote source may render only
 *  inside a media element the user approved (MediaApprovedCtx). A stray or
 *  `<picture>`-hosted remote source is dropped — the sibling `<img>` already
 *  goes through ImgWithFallback's own gate, and a `srcset` swap must not
 *  smuggle an ungated remote fetch past it. */
export function MdSourceEl({ node }: { node?: HastElement }) {
  const approved = useContext(MediaApprovedCtx)
  const props = node?.properties ?? {}
  const remote = isRemoteMediaUrl(props.src) || srcsetHasRemote(props.srcSet)
  if (!approved && remote) return null
  return <source {...spa('source', node)} />
}
