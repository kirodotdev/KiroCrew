/**
 * The markdown renderer, and the composition root of its pipeline.
 *
 * What must be decided in ONE place lives here: the ordered remark and rehype
 * chains, the element-to-renderer map (`MD_COMPONENTS`), the per-block source
 * passes and their order (`MarkdownBlock`), the fence dispatch (`BlockRenderer`)
 * and the root component with its context providers. The pieces it composes
 * live under `./markdown/`, and this module re-exports their public surface
 * unchanged. The owner map is in website/docs/frontend-conventions.md.
 */
import React, { useContext, memo, useEffect, useLayoutEffect, useMemo, useCallback, useRef, useState } from 'react'
import { GitPullRequest } from 'lucide-react'
import { capWhitespaceRuns, remarkBoundDepth, rehypeBoundRawDepth } from '../utils/markdownDepthBound'
import { canonicalChatHref, chatHrefSid, namesASession } from '../utils/sessionKeys'
import ReactMarkdown from 'react-markdown'
import type { Components, ExtraProps } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkAutolinkRules from '../utils/remarkAutolinkRules'
import { remarkLatexDelimiters } from '../utils/remarkLatexDelimiters'
import remarkCjkFriendly from 'remark-cjk-friendly'
import remarkCjkFriendlyGfmStrikethrough from 'remark-cjk-friendly-gfm-strikethrough'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import rehypeRaw from 'rehype-raw'
import rehypeKatex from 'rehype-katex'
import type { PluggableList } from 'unified'
import type { Element as HastElement } from 'hast'
import '../utils/hljs'
import { useBlockAssembler, maskInlineCode } from '../hooks/useBlockAssembler'
import SegmentedControl from './SegmentedControl'
import { urlTransform, ALLOWED_PROTOCOLS } from '../utils/urlTransform'
import { safeHttpUrl } from '../lib/safeUrl'
import { useLinkMeta, type LinkMeta } from '../lib/linkMeta'
import { LinkChip, LinkCard } from './LinkPreview'
import { parseSourceLinkUrl, forgeChipLabel, type PullRequestLink } from '../utils/pullRequestLinks'
import { sourceProviderMeta } from '../utils/sourceProviderMeta'
import { JiraHostsCtx } from '../lib/jiraHosts'
import { rearmConfigScanBudget } from '../utils/autolinkRules'
import JiraLogo from './icons/JiraLogo'
import GithubLogo from './icons/GithubLogo'
import GitlabLogo from './icons/GitlabLogo'
import DiffBlock from './DiffBlock'
import ErrorNotice from './ErrorNotice'
import {
  BlockedLinkChip,
  CredentialTag,
  RedactedCodeBlock,
  RedactionCardSlot,
  RedactionProvider,
  codeHasRedactionMarkers,
  countCredentialTags,
  normalizeBlockedLinks,
  normalizeCredentialRecords,
  rehypeRedactionMarkers,
  useAllowHolds,
  type RedactionMarkers,
} from './RedactionCards'
import FoldableDiffBlock from './FoldableDiffBlock'
import EditableCodeBlock from './EditableCodeBlock'
import { useRevealFailure } from './FilePathMenu'
import { SmoothResize } from './SmoothResize'
import type { ContentBlock } from '../types'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { CodeBlock } from './CodeBlock'
import { ExcalidrawBlock } from './ExcalidrawBlock'
import WidgetFrame from './WidgetFrame'
import WidgetPlaceholder from './WidgetPlaceholder'
import { i18nT } from '../i18n/t'
import { toDate } from '../i18n/format'
import {
  CompactImagesCtx,
  ImageVersionCtx,
  InsideLinkCtx,
  LinkOverrideCtx,
  LinkUnfurlCtx,
  MdSourceCtx,
  PathActionCtx,
  PathProbeCtx,
  ReadOnlyCodeCtx,
  SessionActionCtx,
  type LinkUnfurl,
  type PathActions,
  type SessionActions,
} from './markdown/contexts'
import { artifactSlugFromHref, soleLinkInParagraph, useSessionChip, useUnfurlHref } from './markdown/linkTargets'
import { dashboardPreviewRef, dashboardPreviewSlugFromHref } from '../utils/dashboardPreview'
import { ClosedSessionNotice } from './markdown/ClosedSessionNotice'
import { activatePath, usePathResolution } from './markdown/pathReferences'
import { ELEMENT_OVERRIDES, sp } from './markdown/elements'
import { InlineCode } from './markdown/InlineCode'
import { MarkdownTable } from './markdown/MarkdownTable'
import { ImgWithFallback } from './markdown/ImgWithFallback'
import { DeferredMedia, MdSourceEl } from './markdown/remoteMedia'
import { MermaidBlock } from './markdown/MermaidBlock'
import { isGenericLang, looksLikeMarkdown } from './markdown/markdownSniff'
import { ALLOWED_TAGS, VERBATIM_CONTENT_TAGS, rehypeSanitize, remarkVerbatimUnknownTags } from './markdown/sanitize'
import { rehypeMarkFencedCode, rehypeSourcepos, rehypeStableRootKeys, rehypeUnwrapBlocks, remarkSoftBreaks } from './markdown/treeTransforms'
import { GLOW_TAIL_CHARS, REVEAL_IDLE_SETTLE_MS, rehypeStreamingCaret, rehypeStreamingGlow, rehypeStreamingReveal } from './markdown/streamingEffects'
import { closeCjkAutolinkBoundaries, encodeRefusedLinkDestinations } from './markdown/linkBoundaryRepair'
import { guideUntrusted } from '../guide/guideMarkers'

export { artifactSlugFromHref, soleLinkInParagraph, unfurlableHref } from './markdown/linkTargets'
export { isPathCandidate, splitLineRef } from './markdown/pathReferences'
export { BasePathCtx, CompactImagesCtx, ImageVersionCtx, LinkOverrideCtx, LinkUnfurlCtx, MdSourceCtx } from './markdown/contexts'
export type { LinkOverride, LinkUnfurl } from './markdown/contexts'
export { MERMAID_FONTS_READY_CAP_MS } from './markdown/MermaidBlock'
export { COPIED_FLASH_MS, COPY_FAILED_FLASH_MS } from './markdown/copyFeedback'
export { pendingImageBoxStyle, reservedImageClass, reservedImageStyle } from './markdown/ImgWithFallback'
export { rehypeSanitize, remarkVerbatimUnknownTags } from './markdown/sanitize'
export { rehypeStableRootKeys } from './markdown/treeTransforms'
export { dispatchLightbox, Lightbox } from './markdown/Lightbox'

/** Default markdown anchor, unless a `LinkOverrideCtx` provider claims the href.
 *
 * Extracted from the inline `MD_COMPONENTS.a` so it can read context (it is a
 * component, so hooks are legal here). Only ALLOWED_PROTOCOLS links (editor
 * schemes) keep in-place navigation; everything else opens in a new tab. */
function MdAnchor({ node, href, children }: React.AnchorHTMLAttributes<HTMLAnchorElement> & ExtraProps) {
  const override = useContext(LinkOverrideCtx)
  const probeEnabled = useContext(PathProbeCtx)
  const actions = useContext(PathActionCtx)
  // The override is resolved FIRST and wins outright — Issue Radar's in-app
  // issue/PR affordance must keep beating a link preview. Feeding `null` into
  // the unfurl gate for a claimed href also means a claimed link is never
  // fetched, so the priority holds at the network boundary, not just visually.
  const claimed = href && override ? override({ href, children }) : null
  // Jira, GitHub, and GitLab issue / PR / MR URLs chip synchronously from the
  // URL alone (provider mark + reference) — no fetch, unlike the unfurl chip
  // below, so these chips render in user messages and with `link_previews`
  // off. Jira instances sit behind auth, so an unfurl of one can never
  // succeed; GitHub/GitLab pages unfurl fine but only in assistant messages
  // and only when the operator opted in, which left forge links as raw text
  // in most contexts (#2579). The parser matches hostnames EXACTLY
  // (`github.com` / `gitlab.com`, `www.` stripped) — a lookalike host such as
  // `evil-github.com.attacker.test` falls through to the plain anchor.
  // Self-hosted Jira instances come through `JiraHostsCtx` from the operator
  // allowlist. Forge chips additionally require `safeHttpUrl`: the chip keeps
  // the AUTHORED href (preserving e.g. `#issuecomment` fragments the parser's
  // canonical url drops), so a credential-smuggling `user:pass@github.com`
  // href must never be dressed up as a trusted-looking chip.
  const jiraHosts = useContext(JiraHostsCtx)
  const sessionActions = useContext(SessionActionCtx)
  const source = useMemo(() => {
    if (!href || claimed) return null
    const link = parseSourceLinkUrl(href, [], jiraHosts)
    if (!link) return null
    if (link.provider === 'jira') return link
    return safeHttpUrl(href) ? link : null
  }, [href, claimed, jiraHosts])
  // A chipped link is never handed to the unfurl gate — mirroring `claimed`,
  // so the no-fetch guarantee holds at the network boundary, not just visually.
  const target = useUnfurlHref(claimed || source ? null : href)
  const meta = useLinkMeta(target ?? undefined, target !== null)
  let localHref: string | null = null
  if (href?.startsWith('/')) {
    try {
      const decodedHref = decodeURIComponent(href)
      if (!decodedHref.startsWith('//')) localHref = decodedHref
    } catch { /* keep it a normal link */ }
  }
  // Decoded but NOT narrowed to root-relative: the app mints its own share links
  // absolute, and the recognizer's origin check is what refuses a foreign one.
  let sessionCandidate: string | null = null
  if (href) {
    try {
      sessionCandidate = decodeURIComponent(href)
    } catch { /* keep it a normal link */ }
  }
  // The session parameter this href carries, verbatim. Handed to
  // `resolveSessionChip` below rather than a pre-resolved key, because that
  // helper owns which spellings name a session — so a short `?sid=chat-1380`
  // resolves here exactly as the same short name does in a backtick chip.
  const sessionHrefSid = sessionCandidate ? chatHrefSid(sessionCandidate) : null
  // Whether this href NAMES a same-origin chat session at all, by SHAPE, and
  // independent of whether that session is currently reachable (open). A
  // closed/unknown one is still a chat-session href — it just does not resolve in
  // the open-tabs roster. Both spellings count, and the union is `namesASession`'s
  // rather than re-spelled here: declining only the full key left an authored
  // `?sid=chat-9999` to navigate to exactly the dead session view this interception
  // exists to prevent (#9914), and a spelling added to the resolver alone would
  // re-open that hole if this gate kept its own copy of the grammar.
  const sessionHrefNamesSession = !!sessionHrefSid && namesASession(sessionHrefSid)
  // Whether this renderer is wired to route sessions at all — the SAME predicate
  // `resolveSessionChip` guards on (`onSessionOpen` AND `sessions`), so the link
  // affordance and the click handler can never disagree. Both must be present:
  // `ChatPage` keeps `onSessionOpen` wired but WITHHOLDS `sessions` while offline
  // (`sessions={connected ? sessionTitles : undefined}`), and a no-controller
  // render (e.g. an SDK `user` message row) has neither. In either case there is
  // nothing that could switch sessions, so a `?sid=` link must stay an ordinary
  // navigating link rather than be swallowed.
  const sessionRouting = !!(sessionActions.onSessionOpen && sessionActions.sessions)
  // Same gate as the inline chip, so a link and a bare key naming one session
  // cannot disagree about whether it is reachable.
  // A closed session the page can resume counts too (`useSessionChip`).
  const sessionLink = useSessionChip(sessionHrefSid, sessionActions)
  // The attribute carries the canonical key: a modified click goes to the browser,
  // and an authored `dashboard_…` sid would open a session `?sid=` cannot resolve.
  const sessionHref = sessionLink && sessionCandidate ? canonicalChatHref(sessionCandidate, sessionLink.key) : null
  const onSessionClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Only the PLAIN click is reinterpreted; the href stays real so Cmd+click
    // still opens the session in its own tab.
    const plainPrimaryClick = e.button === 0 && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey
    if (!plainPrimaryClick) return
    // A resolvable session opens in place. A chat-session href that does NOT
    // resolve is swallowed rather than left to the browser. `useSessionChip`
    // returns null in two cases, both correctly declined here:
    //   - an unknown key, or a closed one whose probe has not answered yet — its
    //     raw `?sid=` would navigate to a session the controller cannot load,
    //     landing on a dead/blank view (#9914);
    //   - the ACTIVE session's own key (`resolveSessionChip` rejects
    //     `key === activeSession`) — a plain click is a no-op on the session you
    //     are already in, matching the backtick chip, which renders the active
    //     key as inert. Cmd/Ctrl/middle-click still opens the real href for
    //     anyone who actually wants a duplicate tab.
    //
    // Both branches require `sessionRouting` — the renderer must actually be
    // able to route sessions. When it cannot (offline: `sessions` withheld; or a
    // no-controller render: neither wired), a `?sid=` link is an ordinary
    // external link and keeps navigating as before, never a dead no-op.
    if (sessionLink) {
      e.preventDefault()
      sessionLink.open(sessionLink.key)
    } else if (sessionHrefNamesSession && sessionRouting) {
      e.preventDefault()
    }
  }
  const pathResolution = usePathResolution(
    localHref ?? '',
    probeEnabled
      && !claimed
      && !!localHref
      && !artifactSlugFromHref(localHref)
      && !!(actions.onFileOpen || actions.onFolderOpen),
  )
  // askAgent on: a transcript link holds no draft (the host composer's draft is
  // persisted per slot), and a blocked or failed reveal is gateway-side.
  const reveal = useRevealFailure(localHref ?? undefined)
  const onPathClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    const plainPrimaryClick = e.button === 0 && !e.metaKey && !e.ctrlKey && !e.altKey
    if (pathResolution.probePending && plainPrimaryClick) {
      e.preventDefault()
      return
    }
    if (!pathResolution.candidate
      || (pathResolution.kind !== 'file' && pathResolution.kind !== 'dir')
      || !plainPrimaryClick) return
    e.preventDefault()
    activatePath(
      pathResolution.path,
      pathResolution.kind,
      e.shiftKey,
      actions,
      reveal.onError,
      pathResolution.line,
      pathResolution.endLine,
    )
  }
  // A destination `urlTransform` REJECTED arrives here as '' — react-markdown's
  // defaultUrlTransform sentinel for a scheme outside its allowlist (a custom
  // scheme like `obsidian:`, a `javascript:`/`data:` payload the gate refuses
  // on purpose, or an empty `[x]()` destination). '' is not nullish, so the
  // plain-anchor return's `sessionHref ?? href` handed the DOM `<a href="">` —
  // an ordinary-looking link the browser resolves against the CURRENT page, so
  // "copy link address" yielded the dashboard's own session URL (issue #9925).
  // A rejected destination must not be an anchor at all: render the label as
  // inert text — no href, no target/rel, no click handlers — the same trade
  // TrustAppModal's `safeHref` makes for an untrusted destination. The absent
  // case (`<a>` with no href at all) is a non-navigating placeholder anyway and
  // takes the same path. `InsideLinkCtx` is deliberately NOT provided: it only
  // exists to keep a label's code span from stealing the enclosing anchor's
  // click, and with no anchor the label is ordinary prose — its code spans
  // re-enter the normal inline-code ladder (click-to-copy, and for session-key
  // or confirmed-path shaped spans, their usual chips), pinned by the
  // rejected-destinations tests.
  if (!href) return <span {...sp(node)}>{children}</span>
  if (claimed) return <>{claimed}</>
  if (source?.provider === 'jira') {
    const jira = source
    return (
      <span className="group inline-flex max-w-full items-center gap-1 rounded-md border border-border/60 bg-accent/10 px-1.5 py-px align-baseline text-[13px] mc-md-ref-chip transition-colors hover:border-border hover:bg-accent/20 focus-within:border-border">
        <a
          href={jira.url}
          target="_blank"
          rel="noopener noreferrer"
          title={href}
          className="inline-flex min-w-0 items-center gap-1.5 text-text no-underline focus-ring"
        >
          <JiraLogo size={12} className="shrink-0" />
          <span className="truncate max-w-[24ch]">{`${jira.repo}-${jira.number}`}</span>
        </a>
      </span>
    )
  }
  const forgeLabel = source ? forgeChipLabel(source) : null
  if (source && forgeLabel) {
    const forgeMeta = sourceProviderMeta(source.provider)
    const ForgeIcon = forgeMeta.icon
    return (
      <span className="group inline-flex max-w-full items-center gap-1 rounded-md border border-border/60 bg-accent/10 px-1.5 py-px align-baseline text-[13px] mc-md-ref-chip transition-colors hover:border-border hover:bg-accent/20 focus-within:border-border">
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={href}
          className="inline-flex min-w-0 items-center gap-1.5 text-text no-underline focus-ring"
        >
          {forgeMeta.logo === 'github'
            ? <GithubLogo size={12} className="shrink-0" />
            : forgeMeta.logo === 'gitlab'
              ? <GitlabLogo size={12} className="shrink-0" />
              : ForgeIcon
                // A registered provider's own mark, when its descriptor ships one.
                ? <ForgeIcon size={12} className="shrink-0" />
                // A registered provider with no bundled logo uses the neutral
                // glyph rather than borrowing GitLab's mark.
                : <GitPullRequest className="lucide-inline shrink-0" />}
          <span className="truncate max-w-[32ch]">{forgeLabel}</span>
        </a>
      </span>
    )
  }
  if (target && meta) {
    return (
      <LinkChip meta={meta} href={target}>
        <InsideLinkCtx.Provider value={true}>{children}</InsideLinkCtx.Provider>
      </LinkChip>
    )
  }
  let ext = false
  try { ext = !!href && ALLOWED_PROTOCOLS.has(new URL(href, 'http://x').protocol) } catch { /* not a URL */ }
  // A confirmed session link is in-app navigation, so it keeps in-place semantics.
  // So does a `#heading` link: the renderer scrolls to it (see handleClick).
  if (sessionLink || href?.startsWith('#')) ext = true
  return (
    <>
    <a
      {...sp(node)}
      href={sessionHref ?? href}
      // A `/chat?sid=` href is never a path, so the session branch wins outright.
      // One predicate gates the handler, because a link that RESOLVES necessarily
      // names a session by shape too — so the two branches inside the handler
      // split the same population rather than needing different gates: resolvable
      // switches in place, and one that names a session but does not resolve (a
      // closed or unknown key, a short name no open session answers to, or the
      // active session's own key) is intercepted and declined rather than left to
      // navigate the browser to a dead `?sid=` view (#9914) or a duplicate tab.
      onClick={sessionHrefNamesSession ? onSessionClick : (pathResolution.candidate ? onPathClick : undefined)}
      title={sessionLink && !sessionLink.failure
        ? `${sessionLink.title}\n${i18nT('components.markdownRenderer.click_to_switch_to_this_session')}`
        : undefined}
      {...(ext ? {} : { target: '_blank', rel: 'noopener noreferrer' })}
      // A session link that names a session but cannot open one drops the live-link
      // affordance rather than keeping it and doing nothing. The click is swallowed
      // deliberately (#9914: navigating a dead `?sid=` is worse than not moving), so
      // the accent underline was promising an action that never came — every time,
      // for every old transcript naming a session that has since closed. Muted and
      // unadorned is the same answer the backtick chip gives an unresolved key: no
      // affordance, and the href stays intact so a modified click still works.
      className={sessionHrefNamesSession && !sessionLink && sessionRouting
        ? 'text-muted'
        : 'text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent'}
    >
      <InsideLinkCtx.Provider value={true}>{children}</InsideLinkCtx.Provider>
    </a>
    <ClosedSessionNotice failure={sessionLink?.failure} name={sessionLink?.key ?? ''} />
    {reveal.error && (
      <ErrorNotice variant="inline" className="ml-1.5 align-baseline" message={reveal.error} askAgent onDismiss={reveal.clear} testId="md-link-reveal-error" />
    )}
    </>
  )
}

/**
 * Default markdown paragraph — except when the paragraph IS a single link, in
 * which case the resolved link renders as a block card instead.
 *
 * Position is the whole selection rule: a link surrounded by prose is a chip
 * (see `MdAnchor`), a link standing alone is a card. `LinkCard` replaces the
 * `<p>` rather than nesting inside it, so the card is a block-level sibling of
 * the surrounding paragraphs.
 *
 * Jira issue URLs take a synchronous branch of the same rule, mirroring
 * `MdAnchor`'s chip: Jira instances sit behind auth, so the unfurl fetch can
 * never be relied on to produce a preview for them. The card is built from the
 * URL alone (provider mark, issue key, instance host) with NO request, and
 * recognition is the same allowlist-gated parse as the chip (`JiraHostsCtx`).
 * It obeys the same `enabled`/`live` gate as the fetched card, so ungated
 * surfaces (file previews, artifact pages, sourcePos mode) and streaming tails
 * keep today's inline chip.
 */
function MdParagraph({ node, children }: React.HTMLAttributes<HTMLParagraphElement> & ExtraProps) {
  const override = useContext(LinkOverrideCtx)
  const { enabled: cardsOn, live } = useContext(LinkUnfurlCtx)
  const jiraHosts = useContext(JiraHostsCtx)
  const sole = soleLinkInParagraph(node)
  const jira = useMemo(() => {
    if (!sole?.href || !cardsOn || live) return null
    const link = parseSourceLinkUrl(sole.href, [], jiraHosts)
    return link?.provider === 'jira' ? link : null
  }, [sole?.href, cardsOn, live, jiraHosts])
  // A recognized Jira link never reaches the unfurl machinery: its card is
  // synchronous, so handing the href on would only add a fetch whose result
  // is discarded.
  const target = useUnfurlHref(jira ? null : sole?.href)
  // Same priority rule as MdAnchor: a link the override owns stays an in-app
  // affordance inside an ordinary paragraph, never a card. The provider is a
  // pure render prop (Issue Radar's returns a RefLink element), and the probe
  // only runs when a card is otherwise on the table.
  const cardHref = jira ? sole?.href ?? null : target
  const claimed = !!(cardHref && override && override({ href: cardHref, children: sole?.text }))
  const unfurl = claimed ? null : target
  const meta = useLinkMeta(unfurl ?? undefined, unfurl !== null)
  if (jira && !claimed) {
    // `jira.url` (the parser's canonical form), NEVER `sole.href`: this branch
    // sits before the `safeHttpUrl()` rejection the unfurl path gets, so the
    // raw href could still carry Basic-auth userinfo. The canonical URL is
    // rebuilt from hostname+port alone — credentials cannot survive into it —
    // and it is the same target the inline chip's anchor already uses.
    return (
      <LinkCard
        meta={jiraCardMeta(jira)}
        href={jira.url}
        icon={<JiraLogo size={18} className="shrink-0" />}
      />
    )
  }
  if (unfurl && meta) return <LinkCard meta={meta} href={unfurl} />
  return <p {...sp(node)} className="my-1 leading-6">{children}</p>
}

/**
 * Synthetic `LinkMeta` for the Jira card, from the parsed URL alone: the issue
 * key is the title and the instance host is the domain — the same information
 * the inline chip carries, in card layout. No description on purpose: main has
 * no Jira issue fetch, and inventing one here would put this card behind auth.
 */
function jiraCardMeta(link: PullRequestLink): LinkMeta {
  let domain = ''
  try { domain = new URL(link.url).host } catch { /* unreachable: link.url came out of the parser */ }
  return {
    url: link.url,
    title: `${link.repo}-${link.number}`,
    description: '',
    siteName: '',
    domain,
    icon: '',
    iconDark: '',
    fetchedAt: 0,
  }
}

const MD_COMPONENTS = {
  code({ className, children, ...props }) {
    // Only a <code> inside a <pre> may render a block-level component here
    // (CodeBlock / MermaidBlock / ExcalidrawBlock are each rooted in a <div>).
    // rehypeMarkFencedCode stamps those with `data-fenced`; a bare <code> in
    // prose stays inline whatever class it carries, because a <div> inside the
    // enclosing <p> crashes React's reconciler. That comment carries the full
    // reasoning. `data-fenced` is destructured out so it never reaches the DOM.
    const { 'data-fenced': fenced, 'data-cred-base': credBase, ...rest } = props as Record<string, unknown>
    if (fenced === undefined) return <InlineCode {...rest}>{children}</InlineCode>

    // remark-rehype stamps `language-<first word of the info string>`; keep the
    // whole tag (`error-report`, `c++`, `asp.net`), not just its leading `\w+`
    // run, so the header label and highlighter hint match what the author
    // wrote. A class token has no whitespace, so `\S+` is the whole tag. Same
    // rule as FENCE_OPEN (useBlockAssembler) / fixCodeFences.
    const match = /language-(\S+)/.exec(className || '')
    const lang = match?.[1]
    const codeStr = String(children).replace(/\n$/, '')

    // `rehypeRedactionMarkers` stamps the ordinal of this block's first
    // credential tag when it holds any this reply has a record for. It is
    // checked before the diagram and special-fence renderers, which would
    // otherwise show the placeholder without its lock tag and card.
    if (typeof credBase === 'string') return <RedactedCodeBlock code={codeStr} lang={lang} base={Number(credBase)} />
    if (lang === 'mermaid') return <MermaidBlock code={codeStr} />
    if (lang === 'excalidraw') return <ExcalidrawBlock code={codeStr} />

    return <CodeBlock code={codeStr} lang={lang} complete={true} />
  },
  pre({ children }) { return <>{children}</> },
  // The message bubble sets `overflow-wrap:anywhere; word-break:break-word`
  // (AssistantMessage.tsx / UserMessage.tsx) so an unbreakable token can never
  // widen a message past the viewport. Table cells must NOT inherit either one.
  // `anywhere` participates in MIN-CONTENT sizing, so every cell's min-content
  // collapsed to a single character — removing the one guarantee that keeps a
  // table readable (a table is never squeezed below min-content). On a phone a
  // wide table then compressed until each cell wrapped one CHARACTER per line,
  // vertically. Verified: resetting `overflow-wrap` alone is NOT enough, because
  // Chrome still shrinks columns on the inherited `word-break:break-word`, which
  // splits `$765.72` into `$76 / 5.72`. Both are reset here.
  //
  // With word-based column widths restored, `min-w-full` (NOT `w-full`) lets a
  // table wider than the viewport overflow to its real width and scroll inside
  // the wrapper, while a narrow table still fills the container. A genuinely
  // oversized token now widens its column instead of breaking, which the
  // horizontal scroll already handles. Those classes now live on
  // `MarkdownTable`, which also adds the copy row beneath the table.
  table({ node, children }) { return <MarkdownTable node={node}>{children}</MarkdownTable> },
  ...ELEMENT_OVERRIDES,
  a: MdAnchor,
  p: MdParagraph,
  img: ImgWithFallback,
  video({ node, children }) { return <DeferredMedia tag="video" node={node}>{children}</DeferredMedia> },
  audio({ node, children }) { return <DeferredMedia tag="audio" node={node}>{children}</DeferredMedia> },
  source: MdSourceEl,
  // Custom element names the `rehypeRedactionMarkers` pass injects after
  // sanitize. `Components` is keyed by the intrinsic HTML tags, so the custom
  // keys are added through the assertion below rather than inline — react-markdown
  // resolves the component by tag name at runtime regardless of the static type.
  'blocked-link'({ node }: { node?: HastElement }) {
    return <BlockedLinkChip domain={String(node?.properties?.domain ?? '')} placeholder={String(node?.properties?.placeholder ?? '')} />
  },
  'cred-tag'({ node }: { node?: HastElement }) {
    return <CredentialTag ordinal={Number(node?.properties?.ordinal)} placeholder={String(node?.properties?.placeholder ?? '')} />
  },
  'redaction-card-slot'({ node }: { node?: HastElement }) {
    return <RedactionCardSlot ids={String(node?.properties?.ids ?? '')} />
  },
} as Components

// Disable single-$ inline math so currency strings like `$9.99` don't
// accidentally trigger KaTeX math parsing. With singleDollarTextMath=true (the
// default in remark-math v6), chat messages containing multiple dollar amounts
// get parsed as one giant math expression spanning the first $ to the last,
// which KaTeX then fails to render -- producing HTML that React cannot commit
// and crashing the whole dashboard with "DOMException: String contains an
// invalid character" during completeWork. Only $$...$$ display-math blocks
// are treated as math now; single $ is plain text.

// CommonMark has a known emphasis defect (commonmark/commonmark-spec#650): a
// closing `**` is only right-flanking when it is NOT preceded by punctuation, or
// IS followed by whitespace/punctuation. `**中文（带括号）。**这句` fails both —
// preceded by `。`, followed by the letter `这` — so it renders as literal
// asterisks. English prose sidesteps this by putting a space after the `**`; CJK
// cannot, because a space there is visibly wrong.
//
// `remark-cjk-friendly` implements the CJK-friendly flanking amendment. ORDER IS
// LOAD-BEARING: it must run BEFORE remark-gfm (it changes how emphasis
// delimiters are classified), and the strikethrough companion AFTER, because it
// extends gfm's own `~~` construct.
const REMARK_PLUGINS: PluggableList = [
  // FIRST: bounds the parsed tree's depth as part of parse(), ahead of
  // remark-gfm's own post-parse transform, which recurses over the tree.
  // Input-controlled nesting otherwise overflows the call stack there.
  remarkBoundDepth,
  remarkCjkFriendly,
  remarkGfm,
  remarkCjkFriendlyGfmStrikethrough,
  [remarkMath, { singleDollarTextMath: false }],
  // LaTeX-native `\( … \)` / `\[ … \]` → the same math nodes remark-math emits,
  // from ELIGIBLE TEXT NODES only (code, html, link destinations and reference
  // definitions are other node types and are never touched). After remark-math
  // so `$$` math is already tokenized; before the verbatim pass -- and told
  // which paired tags that pass will show as literal source, so text inside
  // them is never converted (a `<customBlock>` shown verbatim must not carry
  // a rendered KaTeX span in the middle of its source).
  [remarkLatexDelimiters, { verbatimTag: (tag: string) => VERBATIM_CONTENT_TAGS.has(tag) || !ALLOWED_TAGS.has(tag) }],
  // After gfm so an autolink literal is already a `link` node, but BEFORE the
  // verbatim pass, which retypes an unknown tag to text and hides it.
  remarkAutolinkRules,
  remarkVerbatimUnknownTags,
]

// `rehypeBoundRawDepth` sits ahead of `rehypeRaw`: raw HTML that would nest
// past the depth bound is downgraded to text before rehype-raw's recursive
// hast conversion can overflow on it.
const REHYPE_PLUGINS: PluggableList = [rehypeBoundRawDepth, [rehypeRaw, { passThrough: ['math', 'inlineMath'] }], rehypeMarkFencedCode, rehypeUnwrapBlocks, rehypeSanitize, rehypeKatex]

// User-message variant: base remark chain plus soft-break → hard-break.
const REMARK_PLUGINS_WITH_BREAKS: PluggableList = [...REMARK_PLUGINS, remarkSoftBreaks]

const REHYPE_PLUGINS_WITH_SOURCEPOS: PluggableList = [rehypeBoundRawDepth, [rehypeRaw, { passThrough: ['math', 'inlineMath'] }], rehypeMarkFencedCode, rehypeUnwrapBlocks, rehypeSanitize, rehypeKatex, rehypeSourcepos]
// NOTE: remark plugin config is shared via REMARK_PLUGINS above (singleDollarTextMath:
// false). The sourcepos variant only differs in the rehype chain.

// Which regions are off-limits (code, existing links, raw HTML, math) is NOT
// decided by hand-rolled scanning — it is read off remark's own parse of the
// source. Anything inside a fence, an indented block, an inline-code span
// (including a multi-line one), an existing link/image, an angle autolink, a raw
// HTML tag, or a math span simply never becomes an autolink-literal node, so it
// is unreachable here by construction. Built from the SAME `REMARK_PLUGINS` the
// render pipeline uses, so a future plugin addition cannot make the span-finder
// and the renderer disagree about what a link is.
const AUTOLINK_PARSER = unified().use(remarkParse).use(REMARK_PLUGINS).freeze()

/** `closeCjkAutolinkBoundaries` over the renderer's own grammar. NOT safe while
 *  `data-sourcepos` is in play: it shifts later columns (see MarkdownBlock). */
export function fixCjkAutolinkBoundaries(content: string): string {
  return closeCjkAutolinkBoundaries(content, AUTOLINK_PARSER)
}

/** `encodeRefusedLinkDestinations` over the renderer's own grammar. NOT safe
 *  while `data-sourcepos` is in play, for the same reason. */
export function fixUnencodedLinkDestinations(content: string): string {
  return encodeRefusedLinkDestinations(content, AUTOLINK_PARSER)
}

export function fixCodeFences(s: string): string {
  // Escape bare "N." lines so markdown doesn't render them as ordered lists.
  // CommonMark: 0-3 leading spaces = list item, 4+ = indented code block.
  // Tracks backtick and tilde fences with length matching per CommonMark spec.
  let inFence = false
  let fenceMarker = ''
  s = s.replace(/^( {0,3}(```+|~~~+)[\w+#-]*.*|( {0,3}\d+)\.([ \t\r]*))$/gm, (match, _, fence, num, trail) => {
    if (fence) {
      if (!inFence) { inFence = true; fenceMarker = fence }
      else if (
        fence[0] === fenceMarker[0] &&
        fence.length >= fenceMarker.length &&
        /^[ \t\r]*$/.test(match.slice(match.indexOf(fence) + fence.length))
      ) { inFence = false }
      return match
    }
    if (inFence || num === undefined) return match
    return num + '\\.' + trail
  })
  // Ensure blank line before opening fences that are glued to preceding text.
  // The info string is the whole backtick-free line, including attributes and
  // a leading space, matching FENCE_OPEN in useBlockAssembler.
  s = s.replace(/([^\n])(\n?)(```[^`\n]*\n)/g, (_, pre, nl, fence) =>
    nl ? pre + nl + fence : pre + '\n\n' + fence
  )
  // Split closing fences glued to trailing text: ```358KB → ```\n358KB
  // Preserves valid opening fences (```diff, ``` python, ```c++, ```asp.net)
  // via negative lookahead: optional info-string whitespace may precede a tag
  // that starts with a letter and continues as a backtick-free info string,
  // while a size like ```358KB still splits.
  s = s.replace(/^(```)(?!\s*[a-zA-Z][^`]*$)(.+)$/gm, '$1\n$2')
  // Split opening fences glued to uppercase text
  s = s.replace(/```([A-Z])/g, '```\n$1')
  return s
}

const MCWIDGET_STRIP_RE = /<mcwidget[\s\S]*?<\/mcwidget>|<mcwidget[\s\S]*$/g

// Anthropic tool-use protocol markup occasionally leaks into the visible
// text stream (model emits a literal `<tool_use>...</tool_use>` block alongside
// the real ACP tool call). The wrapper element is unknown to the markdown
// renderer, so the JSON body — including its escaped `\n` literals — collapses
// into a single unbroken paragraph, fragmenting the surrounding markdown.
// Mirror MCWIDGET_STRIP_RE: catch complete tag pairs and unclosed openers
// (mid-stream).
const TOOL_USE_STRIP_RE = /<tool_use[\s\S]*?<\/tool_use>|<tool_use[\s\S]*$/g

/**
 * Strip stray protocol tags (`<mcwidget>`, `<tool_use>`) that leak through to
 * a markdown block during streaming transitions, while preserving any tag
 * mentions that appear inside inline-code spans (e.g. when the agent is
 * documenting the syntax).
 *
 * Builds a per-line inline-code mask, runs the strip regex against the masked
 * text to find ranges, then splices those ranges out of the original content.
 * Mask preserves offsets so match indices are valid against the original.
 *
 * `openMarker` is a fast-path substring check to skip work when the tag is
 * not present at all. `stripRe` is the actual matcher; it must be a global
 * regex with sticky-safe semantics (advance lastIndex on zero-length match).
 */
function stripStrayTags(content: string, openMarker: string, stripRe: RegExp): string {
  if (!content.includes(openMarker)) return content
  const masked = content.split('\n').map(l => maskInlineCode(l)).join('\n')
  if (!masked.includes(openMarker)) return content
  const ranges: Array<[number, number]> = []
  stripRe.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = stripRe.exec(masked)) !== null) {
    ranges.push([m.index, m.index + m[0].length])
    if (m[0].length === 0) stripRe.lastIndex++
  }
  if (ranges.length === 0) return content
  let out = ''
  let pos = 0
  for (const [start, end] of ranges) {
    out += content.slice(pos, start)
    pos = end
  }
  out += content.slice(pos)
  return out
}

const stripStrayWidgetTags = (content: string) => stripStrayTags(content, '<mcwidget', MCWIDGET_STRIP_RE)
const stripStrayToolUseTags = (content: string) => stripStrayTags(content, '<tool_use', TOOL_USE_STRIP_RE)

// A GFM table delimiter row, e.g. `| --- | :--: |` or `---|---`. remark-gfm
// only promotes the preceding header line to a <table> once this row is present.
const TABLE_DELIM_RE = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/

/**
 * While STREAMING, withhold an incomplete trailing table so it never paints as
 * literal pipe text that later reflows into a <table>.
 *
 * remark-gfm needs BOTH a header row and a `|---|` delimiter row to recognize a
 * table. Mid-stream the header arrives first and renders as a <p> containing
 * literal "| A | B |"; when the delimiter row streams in, that paragraph
 * RESTRUCTURES into a bordered table — a visible structural snap of
 * already-shown content (see MarkdownRenderer.streamingTableSnap.test.tsx).
 * This defers the trailing header run (mirroring how an incomplete fenced code
 * block is held) until the delimiter arrives, so the transition the user sees
 * is the standard "content appears", not "paragraph morphs into a table".
 *
 * Scoped narrowly to avoid hiding ordinary prose: only a run of trailing
 * non-blank lines whose FIRST line is a bordered table header (starts with `|`)
 * is a candidate, and only when that run does NOT yet contain a delimiter row
 * (a `---` row that actually carries a `|`). A run that already has such a
 * delimiter is a real (possibly still growing) table and is left to render.
 *
 * Scoping choices (both close known edge cases):
 *  - Require the first line to START with `|`. A looser "≥2 pipes" test also
 *    matched ordinary prose (e.g. a line with an inline `` `cmd | grep | wc` ``)
 *    and would withhold that whole paragraph for the rest of the stream. Models
 *    emit bordered tables (`| a | b |`), so start-with-`|` keeps the real case
 *    while excluding prose; a borderless table simply isn't deferred (it never
 *    regressed anything — it just renders as before).
 *  - The delimiter must contain a `|`. A bare `---` is a thematic break / setext
 *    underline, NOT a GFM table delimiter (which needs matching pipe-separated
 *    cells), so counting it as "already a table" would wrongly skip deferral and
 *    let the snap happen.
 */
function deferIncompleteStreamingTable(content: string): string {
  const lines = content.split('\n')
  let start = lines.length
  while (start > 0 && lines[start - 1].trim() !== '') start--
  if (start >= lines.length) return content // trailing blank line / nothing to defer
  const run = lines.slice(start)
  if (!/^\s*\|/.test(run[0])) return content // not a bordered table header
  // A real GFM delimiter row carries at least one pipe; a bare `---` does not.
  if (run.some((l) => l.includes('|') && TABLE_DELIM_RE.test(l))) return content
  return lines.slice(0, start).join('\n')
}

const MarkdownBlock = memo(function MarkdownBlock({ content, sourcePos, startLine, glow, smooth, softBreaks, live, unfurl, markers }: { content: string; sourcePos?: boolean; startLine?: number; glow?: boolean; smooth?: boolean; softBreaks?: boolean; live?: boolean; unfurl?: boolean; markers?: RedactionMarkers }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Declared before the early return below — Rules of Hooks.
  //
  // `sourcePos` force-disables unfurl: the inline-commenting flow maps a DOM
  // selection back to source coordinates through `data-sourcepos`, and a card
  // REPLACES the `<p>` that carries it, so a standalone link would become an
  // uncommentable hole. The two are mutually exclusive in practice today (only
  // the chat transcript enables previews, and it renders without sourcepos) —
  // this makes that a guarantee instead of a coincidence.
  const unfurlCtx = useMemo<LinkUnfurl>(
    () => ({ enabled: !!unfurl && !sourcePos, live: !!live }),
    [unfurl, sourcePos, live],
  )
  // Strip any <mcwidget> or <tool_use> tags that leak through during
  // streaming transitions or when the agent emits protocol markup as text.
  // Both passes preserve mentions inside inline-code spans.
  let clean = stripStrayToolUseTags(stripStrayWidgetTags(content))
  // Cap whitespace runs before parsing. Tree depth is bounded on the parsed
  // tree (see markdownDepthBound), but the parser's own per-line container
  // scan is O(depth), so a list indented to hundreds of levels costs seconds
  // before any tree exists. Lexical, construct-agnostic, and an identity on
  // any message without a whitespace run wider than 256 columns.
  //
  // Gated off in sourcePos mode, like every other column-shifting pass in
  // this function: `data-sourcepos` maps a DOM selection back to source
  // coordinates, and a shortened run would shift every later column on that
  // line and anchor a comment to the wrong occurrence. The crash bound does
  // not depend on the cap (the tree bounds hold either way); only the
  // parser-time bound is given up on that surface.
  if (!sourcePos) clean = capWhitespaceRuns(clean)
  // `glow` marks the live streaming tail block: while streaming, hold back an
  // incomplete trailing table so it doesn't paint as pipe text then snap into a
  // <table> when the delimiter row arrives.
  if (glow) clean = deferIncompleteStreamingTable(clean)
  if (!clean.trim()) return null
  const baseRehype = sourcePos ? REHYPE_PLUGINS_WITH_SOURCEPOS : REHYPE_PLUGINS
  // Streaming tail block only (see MarkdownRenderer's `glow` prop):
  //   - in immediate mode: append the glow plugin for trailing-word shimmer;
  //   - in smooth mode: append the reveal plugin for per-char fade entrance.
  let rehypePlugins: PluggableList = baseRehype
  if (glow) {
    const tail: PluggableList = []
    // Inline caret first, so the glow/reveal plugins still see (and animate)
    // the trailing text node that the caret is inserted after.
    tail.push(rehypeStreamingCaret)
    if (!smooth) tail.push([rehypeStreamingGlow, { tailChars: GLOW_TAIL_CHARS }])
    if (smooth) tail.push(rehypeStreamingReveal)
    rehypePlugins = [...baseRehype, ...tail]
  }
  // After sanitize (baseRehype ends with it) and after the streaming tail, so
  // the injected marker elements are not stripped and the glow/reveal passes
  // have already claimed the trailing text. Only added when the message
  // carries records, so every other surface is untouched.
  if (markers && (markers.ordinals.size > 0 || markers.domains.size > 0)) {
    rehypePlugins = [...rehypePlugins, [rehypeRedactionMarkers, markers]]
  }
  // Last, so it wraps the root shape every other plugin has finished producing:
  // an earlier position would let a later plugin read `div` where it expects the
  // block itself.
  rehypePlugins = [...rehypePlugins, rehypeStableRootKeys]
  // `fixCodeFences` runs FIRST: its later passes CREATE code blocks the raw
  // source did not have (blank line before a fence glued to preceding text,
  // splitting a closing fence glued to trailing text). Rewriting boundaries
  // before that would judge such a region as prose and leave a literal `<…>`
  // inside what ends up displayed as code.
  //
  // `sourcePos` mode maps a DOM selection back to source coordinates through
  // `data-sourcepos` for inline commenting. fixCjkAutolinkBoundaries inserts two
  // characters per fixed URL, which shifts every later column on that line and
  // would anchor a comment to the wrong occurrence — so that surface keeps the
  // unfixed (but coordinate-accurate) render.
  const fenced = fixCodeFences(clean)
  // `fixUnencodedLinkDestinations` runs BEFORE the CJK pass: repairing a
  // refused `[text](url)` turns the URL's autolinked head back into a real
  // link node, so the CJK boundary pass must judge the repaired shape, not
  // the broken one. Both passes shift columns, so both are gated off in
  // sourcePos mode together.
  const prepared = sourcePos ? fenced : fixCjkAutolinkBoundaries(fixUnencodedLinkDestinations(fenced))
  const md = (
    <MdSourceCtx.Provider value={prepared}>
      <ReactMarkdown remarkPlugins={softBreaks ? REMARK_PLUGINS_WITH_BREAKS : REMARK_PLUGINS} rehypePlugins={rehypePlugins} urlTransform={urlTransform} components={MD_COMPONENTS}>
        {prepared}
      </ReactMarkdown>
    </MdSourceCtx.Provider>
  )
  const body = sourcePos ? <div data-block-start={startLine ?? 1}>{md}</div> : md
  // The provider carries no DOM node, so sourcepos / lightbox scoping upstream
  // is unaffected. It is the only way MdAnchor / MdParagraph — which react-markdown
  // instantiates deep inside its own tree — can see the gate.
  return <LinkUnfurlCtx.Provider value={unfurlCtx}>{body}</LinkUnfurlCtx.Provider>
})

/** Languages whose fenced content IS markdown, so a rendered view is
 *  meaningful. Kept in sync with `NESTABLE_LANGS` in useBlockAssembler for the
 *  markup/doc subset a reader would want rendered — mdx is included because its
 *  markdown structure still renders, its JSX just passes through as text. */
const MARKDOWN_LANGS = new Set(['markdown', 'md', 'mdx'])
function isMarkdownLang(lang?: string): boolean {
  return lang != null && MARKDOWN_LANGS.has(lang.toLowerCase())
}

/** A ```markdown fence, or an untagged / generic one whose content is clearly
 *  markdown. Any real language (```python, ```bash) is never sniffed. */
function isMarkdownBlock(lang: string | undefined, content: string): boolean {
  if (isMarkdownLang(lang)) return true
  return isGenericLang(lang) && looksLikeMarkdown(content)
}

/** A markdown content card in the chat transcript: a ```markdown fence with a
 *  Formatted | Raw view toggle in the upper right, matching the segmented
 *  control tool detail cards carry (see pages/chat/ToolDetails.tsx). Formatted
 *  renders through the same pipeline as agent prose; Raw is the verbatim source
 *  with the edit affordance, which keeps editing Raw-only. Opens Formatted; the
 *  control overrides per card. Only mounted for a COMPLETE fence — see the
 *  caller in BlockRenderer. */
const MarkdownContentCard = memo(function MarkdownContentCard(
  { content, lang }: { content: string; lang?: string },
) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [view, setView] = useState<'formatted' | 'raw'>('formatted')

  return (
    <div className="my-2">
      <div className="flex items-center justify-end mb-1">
        <SegmentedControl<'formatted' | 'raw'>
          segments={[
            {
              key: 'formatted',
              label: i18nT('components.markdownCard.formatted'),
              tooltip: i18nT('components.markdownCard.render_the_markdown_headings_lists_tables_links'),
            },
            {
              key: 'raw',
              label: i18nT('components.markdownCard.raw'),
              tooltip: i18nT('components.markdownCard.show_the_exact_markdown_source'),
            },
          ]}
          value={view}
          onChange={setView}
          layoutId="md-card-view"
          collapse={false}
        />
      </div>
      {/* Both views stay MOUNTED; the inactive one is hidden with `hidden`
          rather than unmounted. EditableCodeBlock's Raw scratch editor holds
          unsaved local edits in its own state, so unmounting it on a toggle to
          Formatted would silently discard them. Keeping it mounted preserves
          that state across any number of view switches. */}
      <div className={view === 'formatted' ? undefined : 'hidden'}>
        <MarkdownBlock content={content} />
      </div>
      <div className={view === 'raw' ? undefined : 'hidden'}>
        <EditableCodeBlock code={content} lang={lang} complete={true} />
      </div>
    </div>
  )
})

/** Try to extract a file path from chat text immediately preceding a diff
 * block. Tools sometimes emit "Created /path/to/file:" or "Modified ..."
 * before a bare diff with no +++/--- headers; this hint lets DiffBlock's
 * Open file button work in those cases.
 */
function extractPathHintFromText(text: string | undefined): string | undefined {
  if (!text) return undefined
  // Last non-empty line before the diff is the most likely carrier of
  // "Created /path:" or "Edited /path:" — scan a few lines back rather
  // than the whole block, to keep this cheap and avoid false positives.
  const lines = text.trimEnd().split('\n').slice(-5)
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim().replace(/[:.,]+$/, '')
    if (!line) continue
    // Patterns we accept:
    //   Created /abs/path
    //   Modified /abs/path
    //   Wrote /abs/path
    //   Updated /abs/path
    //   /abs/path        (bare absolute path)
    //   ~/relative/path  (home-relative)
    //   `/abs/path`      (backtick-wrapped)
    const stripped = line.replace(/^`|`$/g, '')
    const m = /(?:Created|Modified|Wrote|Updated|Edited|Saved|File|Path)?\s*[:\s]?\s*`?(\/[^\s`]+|~\/[^\s`]+)`?/i.exec(stripped)
    if (m && m[1]) return m[1]
  }
  return undefined
}

function BlockRenderer({ block, prevBlock, onFileOpen, sourcePos, messageTs, slotKey, glow, smooth, softBreaks, live, unfurl, collapseDiffs, mdCardToggle, readOnlyCode, markers }: { block: ContentBlock; prevBlock?: ContentBlock; onFileOpen?: (path: string) => void; sourcePos?: boolean; messageTs?: string; slotKey?: string; glow?: boolean; smooth?: boolean; softBreaks?: boolean; live?: boolean; unfurl?: boolean; collapseDiffs?: boolean; mdCardToggle?: boolean; readOnlyCode?: boolean; markers?: RedactionMarkers }) {
  const readOnlyCodeCtx = useContext(ReadOnlyCodeCtx)
  // A fence holding lock tags renders them in place whatever its language:
  // the diagram, diff and formatted-markdown renderers would show the
  // placeholder without its tag and card (see RedactedCodeBlock).
  if (
    (block.type === 'code' || block.type === 'diff' || block.type === 'mermaid' || block.type === 'excalidraw')
    && block.complete && markers && markers.ordinals.size > 0
    && codeHasRedactionMarkers(block.content, markers.base, markers.credentials)
  ) {
    const lang = block.type === 'code' ? block.language : block.type
    return <RedactedCodeBlock code={block.content} lang={lang} base={markers.base} />
  }
  switch (block.type) {
    case 'diff': {
      const pathHint = prevBlock?.type === 'markdown'
        ? extractPathHintFromText(prevBlock.content)
        : undefined
      // `collapseDiffs` is the CHAT TRANSCRIPT's opt-in, and only its opt-in.
      // A fence in an assistant message is the model's own retelling of a
      // change, and several of them bury the prose. Everywhere else this
      // renderer is used — artifacts, specs, knowledge documents, the
      // changelog, review reports — the patch IS the content, and collapsing
      // it would take the text out of the DOM for find-in-page, whole-surface
      // selection and printing.
      //
      // `foldKey` is slot + message + the fence's line, which is the identity
      // the block list already keys on: stable across streaming, so an opened
      // patch survives a re-mount. All THREE parts are required. Keyed on the
      // line alone, two messages whose fences start on the same line would
      // share one entry and open together; without the slot, a fork — which
      // preserves the parent's message timestamps — would collide with the
      // session it was forked from. Without a key the state is local, which
      // only costs the re-mount memory.
      const foldKey = slotKey != null && messageTs != null && block.startLine != null
        ? `${slotKey}:${messageTs}:${block.startLine}`
        : undefined
      const node = collapseDiffs
        ? <FoldableDiffBlock code={block.content} complete={block.complete} onFileOpen={onFileOpen} pathHint={pathHint} foldKey={foldKey} />
        : <DiffBlock code={block.content} complete={block.complete} onFileOpen={onFileOpen} pathHint={pathHint} />
      // Smooth mode: wrap so the block height eases as lines arrive. The wrapper
      // is mounted for the whole message lifecycle (smooth is constant) so the
      // child never remounts when streaming flips to complete.
      return smooth ? <SmoothResize enabled={!block.complete}>{node}</SmoothResize> : node
    }
    case 'mermaid':
      return block.complete ? <MermaidBlock code={block.content} /> : (
        <div className="my-2 p-3 bg-bg-elevated border border-border rounded-md text-muted text-[12px] italic animate-pulse">{i18nT('components.markdownRenderer.generating_diagram')}</div>
      )
    case 'excalidraw':
      // Held back until the fence closes: a half-streamed scene is invalid JSON,
      // so attempting to draw it would only flash the raw-source fallback.
      return block.complete ? <ExcalidrawBlock code={block.content} /> : (
        <div className="my-2 p-3 bg-bg-elevated border border-border rounded-md text-muted text-[12px] italic animate-pulse">{i18nT('components.markdownRenderer.generating_diagram')}</div>
      )
    case 'code': {
      // A ```markdown / ```md / ```mdx fence (or an untagged one that is
      // clearly markdown, see isMarkdownBlock) is the "markdown content card":
      // today it renders verbatim source with an edit affordance. In the chat
      // transcript (`mdCardToggle`) give it a Formatted | Raw segmented control
      // like tool detail cards carry, so long docs can be read rendered. Raw is
      // the pre-toggle EditableCodeBlock, so the edit affordance stays Raw-only.
      // Only fenced content whose CLOSE has arrived is offered a rendered view:
      // a half-streamed markdown source would flip structure as delimiters land.
      if (mdCardToggle && block.complete && isMarkdownBlock(block.language, block.content)) {
        const mdNode = <MarkdownContentCard content={block.content} lang={block.language} />
        return smooth ? <SmoothResize enabled={!block.complete}>{mdNode}</SmoothResize> : mdNode
      }
      // `readOnlyCode`: the content is a record the reader must not be able to
      // touch -- an approval's command awaiting authorization. EditableCodeBlock's
      // Raw scratch editor edits a local copy that is never written back, so a
      // pencil there lets someone edit the block and then Approve the ORIGINAL
      // command while looking at their edit. Plain CodeBlock keeps copy only.
      const node = readOnlyCode || readOnlyCodeCtx
        ? <CodeBlock code={block.content} lang={block.language} complete={block.complete} />
        : <EditableCodeBlock code={block.content} lang={block.language} complete={block.complete} />
      // Height-grow only — streaming code renders as one plain <pre> text node
      // so per-line content animation isn't applied here.
      return smooth ? <SmoothResize enabled={!block.complete}>{node}</SmoothResize> : node
    }
    case 'widget':
      return block.complete
        ? <WidgetFrame html={block.content} title={block.language} slug={block.slug} messageTs={messageTs} slotKey={slotKey} />
        : <WidgetPlaceholder title={block.language} />
    case 'markdown':
      // `live` = this block is the streaming tail (see MarkdownRenderer). ORed
      // with the block's own `complete` flag so a provisional block is treated
      // as live too, whatever produced it.
      return <MarkdownBlock content={block.content} sourcePos={sourcePos} startLine={block.startLine} glow={glow} smooth={smooth} softBreaks={softBreaks} live={!block.complete || !!live} unfurl={unfurl} markers={markers} />
  }
}

export default memo(function MarkdownRenderer({ content, streaming = false, onFileOpen, onFolderOpen, onArtifactOpen, onSessionOpen, sessions, activeSession, rawMode = false, sourcePos = false, messageTs, slotKey, glow = false, smooth, softBreaks = false, compactImages = false, linkPreviews = false, collapseDiffs = false, mdCardToggle = false, readOnlyCode = false, blockedLinks, redactions, redactionCoach = false }: { content: string; streaming?: boolean; onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void; onFolderOpen?: (path: string) => void; onArtifactOpen?: (slug: string) => void; onSessionOpen?: (key: string) => void; sessions?: ReadonlyMap<string, string>; activeSession?: string; rawMode?: boolean; sourcePos?: boolean; messageTs?: string; slotKey?: string; glow?: boolean; smooth?: boolean; softBreaks?: boolean; compactImages?: boolean; linkPreviews?: boolean; /** Chat transcript only: render a ```diff fence collapsed to a chip. Off everywhere else, where the patch IS the content rather than a retelling of it. */ collapseDiffs?: boolean; /** Chat transcript only: give a ```markdown content card a Formatted | Raw view toggle. Off everywhere else, where the fence IS the source being shown. */ mdCardToggle?: boolean; /** Render fenced code with the plain CodeBlock (copy only) instead of EditableCodeBlock. For content the reader must not be able to alter in place -- an approval's command beside its Approve control. */ readOnlyCode?: boolean; /** Raw `meta.blocked_links` off the assistant message — the step-3 suspicious-URL records this message's redaction placeholders render from. Validated here; absent/malformed leaves every placeholder as plain text. */ blockedLinks?: unknown; /** Raw `meta.redactions` off the assistant message: one record per credential placeholder, validated here. */ redactions?: unknown; /** This reply is the session's first with removed values: show the one-time coach after its first such block. */ redactionCoach?: boolean }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const blocks = useBlockAssembler(content, streaming)
  // One message = one config-rule scan pool. The blocks below each mount their
  // OWN remark tree, so the rearm cannot live at the plugin's tree entry — a
  // fence-heavy message would restore the pool once per block and multiply the
  // 50ms ceiling by the block count. Render-phase on purpose (same discipline
  // as ChatPage's registry write): the pool must be full before the first
  // block's synchronous remark pass, and parent-then-children render order
  // guarantees exactly that. Double-invoke under StrictMode is harmless — the
  // pool is refilled before any drain either way.
  rearmConfigScanBudget()

  /** Chip activation lives on the chip itself (see InlineCode); this handler is
   *  only link delegation: `#heading` jumps, `/artifacts/<slug>` links and
   *  staged-dashboard links. */
  const handleClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const el = e.target as HTMLElement
    // `#heading` links scroll within THIS message, never the whole document:
    // several renderers coexist and may repeat one heading.
    const frag = e.defaultPrevented ? null : el.closest('a[href^="#"]')
    if (frag) {
      e.preventDefault()
      let id = frag.getAttribute('href')!.slice(1)
      try { id = decodeURIComponent(id) } catch { /* keep it raw */ }
      Array.from(e.currentTarget.querySelectorAll('[id]')).find(n => n.id === id)?.scrollIntoView({ block: 'start' })
      return
    }
    // e.target may be an inline child of the `/artifacts/<slug>` anchor (e.g.
    // <em>/<code>), so walk up with closest(). preventDefault stops the
    // relative href from navigating full-page instead of opening the panel.
    if (onArtifactOpen && !e.shiftKey) {
      const anchor = el.closest('a[href^="/artifacts/"]') as HTMLAnchorElement | null
      if (anchor) {
        const slug = artifactSlugFromHref(anchor.getAttribute('href'))
        if (slug) {
          e.preventDefault()
          onArtifactOpen(slug)
          return
        }
      }
    }
    // A staged dashboard's link opens beside the chat, through the same host
    // action. Only a PLAIN click: Cmd/Ctrl/Alt/Shift-click and middle-click keep
    // the real href, so the browser can still open the URL itself.
    if (onArtifactOpen && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
      const anchor = el.closest('a[href]') as HTMLAnchorElement | null
      const previewSlug = dashboardPreviewSlugFromHref(anchor?.getAttribute('href'))
      if (previewSlug) {
        e.preventDefault()
        onArtifactOpen(dashboardPreviewRef(previewSlug))
      }
    }
  }, [onArtifactOpen])

  // Repeated headings get GitHub's -1/-2 suffixes so each `#` link has one target.
  // `ids` remembers each heading's [own slug, id written here]: an id that still
  // equals what we wrote keeps its slug; anything else is React's fresh slug.
  const rootRef = useRef<HTMLDivElement>(null)
  const ids = useRef(new WeakMap<Element, [string, string]>())
  useLayoutEffect(() => {
    const seen = new Map<string, number>()
    rootRef.current?.querySelectorAll('h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]').forEach(h => {
      const prev = ids.current.get(h)
      const slug = prev && prev[1] === h.id ? prev[0] : h.id
      const n = seen.get(slug) ?? 0
      seen.set(slug, n + 1)
      h.id = n ? `${slug}-${n}` : slug
      ids.current.set(h, [slug, h.id])
    })
  })

  /** Stable identity so every chip in a long transcript doesn't re-render when
   *  this component does. */
  const pathActions = useMemo<PathActions>(() => ({ onFileOpen, onFolderOpen }), [onFileOpen, onFolderOpen])
  // The message's redaction records, validated once. The sets are what the
  // injection pass gates on; the full records ride down through context.
  // A host allowed from a card still open keeps that card's records here:
  // the reloaded reply shows the host's links as plain links and no longer
  // carries them.
  const replyKey = slotKey && messageTs ? `${slotKey}|${messageTs}` : undefined
  const holds = useAllowHolds(replyKey)
  const ownBlocked = useMemo(() => normalizeBlockedLinks(blockedLinks), [blockedLinks])
  const heldDomains = useMemo(() => {
    const own = new Set(ownBlocked.map(r => r.domain))
    return new Set([...holds.keys()].filter(d => !own.has(d)))
  }, [ownBlocked, holds])
  const blockedRecords = useMemo(
    () => heldDomains.size ? [...ownBlocked, ...[...heldDomains].flatMap(d => holds.get(d)?.records ?? [])] : ownBlocked,
    [ownBlocked, heldDomains, holds],
  )
  const credentialRecords = useMemo(() => normalizeCredentialRecords(redactions), [redactions])
  const blockedDomains = useMemo(() => new Set(blockedRecords.map(r => r.domain)), [blockedRecords])
  const credentialMap = useMemo(() => new Map(credentialRecords.map(r => [r.ordinal, r])), [credentialRecords])
  const credentialOrdinals = useMemo(() => new Set(credentialMap.keys()), [credentialMap])
  const sessionActions = useMemo<SessionActions>(
    // The write time the SHORT-name chip needs. Absent, non-absolute, or
    // unparseable yields undefined, and a short name then resolves to NOTHING —
    // fail closed, because this is compared against server-clock slot mint epochs
    // and a wrong comparison opens the wrong conversation silently.
    //
    // Validated, not trusted: `messageTs` is DECLARED `string` but crosses an API
    // boundary that does not enforce it, and the transcript endpoint really does
    // send epoch NUMBERS (see the fixture in `playwright/voice-recovery.spec.ts`).
    // Calling a string method on that value threw and blanked the transcript, so
    // the shape is checked here rather than assumed.
    //
    // A number is an epoch and already absolute; `toDate` owns the
    // seconds-vs-milliseconds rule for the whole app, so it is not re-guessed here.
    // A STRING has to carry `Z` or an explicit `±HH:MM`: `Date.parse('2026-09-11T23:39:00')`
    // reads a bare local time, so the same row would mean a different instant per
    // viewer timezone, and a viewer behind UTC shifts it forward far enough to let a
    // slot minted after the row pass the check.
    () => {
      const raw: unknown = messageTs
      const absolute = typeof raw === 'number'
        || (typeof raw === 'string' && /(?:Z|[+-]\d{2}:?\d{2})$/.test(raw.trim()))
      const when = absolute ? toDate(raw as string | number) : null
      return {
        onSessionOpen,
        sessions,
        activeSession,
        writtenAtEpoch: when ? Math.floor(when.getTime() / 1000) : undefined,
      }
    },
    [onSessionOpen, sessions, activeSession, messageTs],
  )

  // Index of the last markdown block — the streaming tail that gets the glow
  // (only when `glow` is set). -1 if the message ends in a non-markdown block.
  const lastMarkdownIdx = useMemo(() => {
    for (let i = blocks.length - 1; i >= 0; i--) if (blocks[i].type === 'markdown') return i
    return -1
  }, [blocks])
  // Each block renders its own sub-document, so the credential ordinal its
  // first tag carries is the count of tags in every block before it.
  const blockMarkers = useMemo<Array<RedactionMarkers | undefined>>(() => {
    if (credentialOrdinals.size === 0 && blockedDomains.size === 0) return blocks.map(() => undefined)
    let base = 0
    return blocks.map(b => {
      const m: RedactionMarkers = { ordinals: credentialOrdinals, domains: blockedDomains, held: heldDomains, credentials: credentialMap, base }
      base += countCredentialTags(b.content)
      return m
    })
  }, [blocks, credentialOrdinals, blockedDomains, heldDomains, credentialMap])

  // Settle the reveal edge once the content stops changing, and NEVER un-settle
  // it. One-way is the whole point: `.ft-word` spans persist across chunks
  // (hast-util-to-jsx-runtime keys element children by per-parent ordinal, and
  // `--ft-o` is a function of the span's slot, not of the character), so
  // REMOVING `.ft-idle` would transition the entire 32-character edge from the
  // settled 1 back down to `--ft-o` — an inverse of the fade-in #697 built, in
  // the same pixels. Pre-paint clearing cannot avoid that either, because a
  // transition starts from the previously COMPUTED style, not the last painted
  // frame. Making the settle one-way removes the downward transition by
  // construction: a character's opacity only ever rises.
  //
  // The cost is deliberate: after the first stall the rest of that row renders
  // at full opacity with no reveal. A latched class is harmless once streaming
  // ends — the spans only exist while `glow` is set, and the class's effect is
  // full opacity, which is the correct end state anyway.
  //
  // Skipped entirely when `smooth` is off: `.ft-idle` is inert there, and this
  // component has ~15 non-streaming call sites.
  const [revealIdle, setRevealIdle] = useState(false)
  useEffect(() => {
    if (!smooth || revealIdle) return
    const t = setTimeout(() => setRevealIdle(true), REVEAL_IDLE_SETTLE_MS)
    return () => clearTimeout(t)
  }, [content, smooth, revealIdle])

  if (rawMode) {
    return <pre className="text-[13px] font-mono whitespace-pre-wrap break-words leading-relaxed text-muted">{content}</pre>
  }

  // Root class drives the per-char entrance keyframe (.ft-word descendants
  // only exist in the streaming tail block, so this is inert otherwise).
  // ft-streaming scopes the animation to live streaming so history/scroll
  // re-mounts don't re-fade.
  const animOn = !!smooth
  const animClass = animOn ? ' ft-anim-smooth' : ''
  // The root tag below is laid out one attribute per line: the repo's
  // accessible-interactive-elements rule greps ADDED lines for a non-role div
  // or span carrying a click handler (check-added: true), so an attribute can
  // join the tag only if no single added line holds both the element and its
  // handler. The element, its class list and its delegating handler are the
  // base's; `data-tip-flow` is the one addition.
  const streamClass =
    (animOn && streaming ? ' ft-streaming' : '') +
    (animOn && revealIdle ? ' ft-idle' : '')

  return (
    // Presentational content wrapper for rendered markdown blocks. The onClick is
    // pure event delegation for `#heading` and `/artifacts/<slug>` links — path chips bind
    // their own handlers (see InlineCode), so this wrapper is not an interactive
    // control and carries no role. `data-image-scope` is the lightbox's grouping
    // root; `data-tip-flow` is the InstantTip flow container (one rendered
    // message, so a chip's bubble opens off the words it reports on) — two
    // attributes on the one per-message root, each owned by its feature.
    // `data-guide-untrusted`: authored content is never a guide target.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
    <div
      ref={rootRef}
      className={`group${animClass}${streamClass}`}
      onClick={handleClick}
      data-image-scope=""
      data-tip-flow=""
      // Authored content: nothing in it is a control a guide points at.
      {...guideUntrusted}
    >
      {/* PathProbeCtx: suppress path stat probes while the message is still
          streaming, so partial paths ('/Users' en route to '/Users/me/x.ts')
          neither burn requests nor flash the wrong affordance.
          PathActionCtx: where a confirmed chip sends its click — MD_COMPONENTS is
          module-level, so the renderer cannot pass these down as props. */}
      <PathProbeCtx.Provider value={!streaming}>
      <PathActionCtx.Provider value={pathActions}>
      <SessionActionCtx.Provider value={sessionActions}>
      {/* CompactImagesCtx: user-message ("sent prompt") callers pass compactImages
          so their attached images render as small previews. The provider wraps the
          blocks here (a context Provider renders no DOM node, so data-image-scope /
          lightbox scoping on the div above is unaffected) and lives in this module
          so a caller that mocks it in tests never needs to re-export the context. */}
      <CompactImagesCtx.Provider value={compactImages}>
      {/* Remote media is always click-to-load; deferral is unconditional inline. */}
      {/* ImageVersionCtx: scopes local image URLs to this message so an agent
          rewriting one file across turns is not served the previous bytes from
          the in-document resource cache. */}
      <ImageVersionCtx.Provider value={messageTs ?? null}>
      {/* RedactionProvider: this reply's credential and blocked-link records,
          and which of their cards is open. A Provider renders no DOM node, so
          the scoping on the wrapper div above is unaffected. */}
      <RedactionProvider credentials={credentialRecords} blockedLinks={blockedRecords} slotKey={slotKey} replyKey={replyKey} messageTs={messageTs} coached={redactionCoach && credentialRecords.length > 0}>
        {blocks.map((block, i) => (
          // Key on startLine (stable across streaming) instead of block.type, so
          // a code -> diff reclassification mid-stream doesn't unmount the
          // in-progress component. Falls back to index for blocks without a
          // startLine (e.g. extracted widgets). The "idx-" prefix avoids
          // collision with real startLine numbers.
          <BlockRenderer
            key={block.startLine != null ? `line-${block.startLine}` : `idx-${i}`}
            block={block} prevBlock={blocks[i - 1]} onFileOpen={onFileOpen} sourcePos={sourcePos}
            messageTs={messageTs}
            slotKey={slotKey}
            glow={glow && i === lastMarkdownIdx}
            // Same gate `glow` uses — the last markdown block of a streaming
            // message IS the live tail. Reusing it means the unfurl suppression
            // and the shimmer can never disagree about which block is still
            // being typed. `streaming` rather than `glow` because a caller may
            // render a streaming transcript without asking for the shimmer.
            live={streaming && i === lastMarkdownIdx}
            unfurl={linkPreviews}
            smooth={smooth}
            softBreaks={softBreaks}
            collapseDiffs={collapseDiffs}
            mdCardToggle={mdCardToggle}
            readOnlyCode={readOnlyCode}
            markers={blockMarkers[i]}
          />
        ))}
      </RedactionProvider>
      </ImageVersionCtx.Provider>
      </CompactImagesCtx.Provider>
      </SessionActionCtx.Provider>
      </PathActionCtx.Provider>
      </PathProbeCtx.Provider>
    </div>
  )
})
