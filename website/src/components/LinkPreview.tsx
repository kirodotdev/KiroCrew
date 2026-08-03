/**
 * Link-unfurl presentation: the two forms a resolved link can take.
 *
 * `LinkChip` for a link sitting inside a sentence, `LinkCard` for a link that
 * is the whole paragraph. `MarkdownRenderer` picks between them by position;
 * neither component decides anything about whether to unfurl.
 *
 * Accessibility: the accessible name is the PAGE TITLE (the anchor's own text),
 * never the raw URL — an unfurled link read aloud as
 * "h-t-t-p-s-colon-slash-slash…" is worse than the bare markdown it replaced.
 * The favicon is decorative and carries `alt=""`, so it is not announced.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Check, Copy, Globe } from 'lucide-react'
import type { LinkMeta } from '../lib/linkMeta'
import { copyToClipboard } from '../utils/clipboard'

/** How long the copied checkmark stays before reverting to the copy icon. */
const COPIED_FEEDBACK_MS = 1600

/**
 * Fixed-size favicon box.
 *
 * The box is sized by class, not by the image, so a missing, slow or broken
 * icon never reflows the surrounding text — the space is reserved before the
 * bytes arrive. `onError` swaps the `<img>` for the placeholder rather than
 * leaving the browser's broken-image glyph in a 14px slot.
 *
 * The box paints NO background of its own. Favicons are routinely transparent
 * PNG/ICO, so a tinted fill here shows through as a themed plate around the
 * logo — on a light theme it reads as a grey square the site did not ship.
 * Letting the surface behind it (the card, the chip's own tint, the paragraph)
 * show through is what makes a transparent icon look like the site's icon.
 */
function Favicon({ icon, className, iconClassName }: {
  icon: string
  className: string
  iconClassName: string
}) {
  const [broken, setBroken] = useState(false)
  const show = icon && !broken
  return (
    <span
      aria-hidden="true"
      className={`shrink-0 grid place-items-center overflow-hidden rounded-sm ${className}`}
    >
      {show ? (
        <img
          src={icon}
          alt=""
          className="w-full h-full object-contain"
          onError={() => setBroken(true)}
        />
      ) : (
        <Globe className={`${iconClassName} text-muted`} />
      )}
    </span>
  )
}

/**
 * Inline pill: favicon + page title on a single line, truncated with an
 * ellipsis, plus a copy-the-URL button. `children` is the anchor's original
 * markdown content, kept as a last-resort label for a meta object with neither
 * title nor domain.
 *
 * The chip carries its own copy button because unfurling REMOVES a capability
 * the reader already had: before this feature a link was raw URL text, so
 * selecting it and copying gave you the URL. Rendering the page title in its
 * place takes that away, and restoring it only through the selection toolbar
 * would make the inline form strictly worse than the plain text it replaced.
 *
 * Same shape as the card: a container with the anchor and the button as
 * SIBLINGS. A `<button>` nested inside an `<a>` is interactive content inside a
 * link — invalid, and one click would fire both.
 *
 * The button is always present rather than revealed on hover. A hover-reveal
 * either reflows the sentence when the button appears, or (if it merely fades)
 * leaves an invisible click target sitting in the text — and neither works on
 * touch, where there is no hover at all. It rests at reduced opacity instead:
 * no layout shift, always hittable, quiet enough to sit mid-paragraph.
 */
export function LinkChip({ meta, href, children }: {
  meta: LinkMeta
  href: string
  children?: React.ReactNode
}) {
  const label = meta.title || meta.domain
  return (
    <span className="group inline-flex max-w-full items-center gap-1 rounded-md border border-border/60 bg-accent/10 px-1.5 py-px align-baseline text-[13px] transition-colors hover:border-border hover:bg-accent/20 focus-within:border-border">
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        title={meta.title || meta.domain || href}
        data-unfurl-url={href}
        className="inline-flex min-w-0 items-center gap-1.5 text-text no-underline focus-ring"
      >
        <Favicon icon={meta.icon} className="w-[14px] h-[14px]" iconClassName="w-[11px] h-[11px]" />
        {/* Capped in `ch`, not just at the container edge: an og:title is written
         *  for a browser tab, so a retailer's "<Brand>. Spend less. Smile more."
         *  runs 35+ characters, and a container-width cap lets one swallow most of
         *  a line, shunting the rest of the sentence to the right. The full title
         *  stays available through the anchor's `title` attribute. */}
        <span className="truncate max-w-[24ch]">{label || children}</span>
      </a>
      <CopyUrlButton href={href} name={label || href} compact />
    </span>
  )
}

/**
 * Block card: favicon square, bold title, 2-line-clamped description, domain,
 * plus a copy-the-URL button.
 *
 * The card is a container with the anchor and the button as SIBLINGS, not a
 * single `<a>` wrapping both: a `<button>` inside an `<a>` is interactive
 * content nested in a link, which is invalid and makes one click do two things.
 * The anchor still covers the whole content area, so the card as a whole stays
 * one click target.
 *
 * Built from `<span>`s with `block` rather than `<div>`s so the card stays
 * valid markup wherever the renderer places it, including inside a `<p>` —
 * `<button>` is phrasing content, so it is legal there too.
 */
export function LinkCard({ meta, href }: { meta: LinkMeta; href: string }) {
  return (
    <span className="relative flex items-start gap-3 my-2 rounded-lg border border-border bg-card p-3 transition-colors hover:border-border-strong focus-within:border-border-strong">
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        data-unfurl-url={href}
        className="flex min-w-0 flex-1 items-start gap-3 no-underline focus-ring"
      >
        <Favicon icon={meta.icon} className="w-8 h-8 rounded-md" iconClassName="w-4 h-4" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-text font-semibold">
            {meta.title || meta.domain}
          </span>
          {meta.description && (
            <span className="mt-0.5 block text-[13px] leading-snug text-muted line-clamp-2">
              {meta.description}
            </span>
          )}
          {meta.domain && (
            <span className="mt-1 block text-[12px] text-muted-strong">{meta.domain}</span>
          )}
        </span>
      </a>
      <CopyUrlButton href={href} name={meta.title || meta.domain} />
    </span>
  )
}

/**
 * Copies the link's URL — the ORIGINAL href, not the rendered title.
 *
 * Unfurling replaces the visible URL text with the page title, so this button
 * is what keeps the raw URL reachable in one click. `name` is the link's visible
 * label, used only for the accessible name: a paragraph can hold several chips,
 * and a screen reader listing three buttons all called "Copy URL" gives the user
 * no way to tell which link each one belongs to. The tooltip stays short,
 * because it sits next to the title it would otherwise repeat.
 *
 * `compact` is the inline-chip size — a 16px box against the card's 28px, so the
 * button matches the favicon's scale and the chip stays on one line.
 */
function CopyUrlButton({ href, name, compact = false }: {
  href: string
  name?: string
  compact?: boolean
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // The timeout is cleared on unmount so a card that scrolls out of the
  // transcript mid-confirmation cannot set state on a dead component.
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  const copy = async (e: React.MouseEvent) => {
    // The chip renders inside message prose, where the container delegates
    // clicks for artifact links and path chips. Without this the copy also
    // reaches that handler, so one click both copies and navigates.
    e.preventDefault()
    e.stopPropagation()
    await copyToClipboard(href)
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), COPIED_FEEDBACK_MS)
  }

  const tooltip = copied
    ? t('components.linkPreview.copied')
    : t('components.linkPreview.copy_url')
  const spoken = copied
    ? tooltip
    : name
      ? t('components.linkPreview.copy_url_of', { name })
      : tooltip
  return (
    <button
      type="button"
      onClick={copy}
      title={tooltip}
      aria-label={spoken}
      className={`shrink-0 grid place-items-center rounded-md text-muted transition-colors hover:bg-bg-hover hover:text-text focus-ring ${
        compact
          ? 'w-4 h-4 opacity-60 group-hover:opacity-100 focus-visible:opacity-100'
          : 'w-7 h-7'
      }`}
    >
      {copied ? (
        <Check className={`${compact ? 'w-3 h-3' : 'w-4 h-4'} text-ok`} aria-hidden="true" />
      ) : (
        <Copy className={compact ? 'w-3 h-3' : 'w-4 h-4'} aria-hidden="true" />
      )}
    </button>
  )
}
