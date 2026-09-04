/**
 * FeaturedSpotlight — one editorial card in the featured list at the top of Discover.
 *
 * Layout: artwork across the top, then editorial copy, then one row per app.
 * Art and text occupy SEPARATE planes rather than being composited, because
 * artwork arrives from a curator with no guarantee about where its bright or
 * busy region falls — title-over-image would be unreadable on some pictures and
 * nothing in the pipeline could catch it.
 *
 * TWO TYPES, ONE SHELL.
 *  - `app`: one featured app. Its own name is the heading, and the whole card
 *    opens it.
 *  - `collection`: several apps under a curator's theme. The theme is the
 *    heading, and the card itself is NOT clickable — there is no collection
 *    detail page to open, so the rows are the only targets. A card that looked
 *    clickable and did nothing would be worse than one that does not.
 *
 * Every app renders a row with its OWN install control. A member the reader
 * cannot act on is just a picture of an app, and the collection card exists to
 * be acted on.
 */
import { useState } from 'react'
import { BadgeCheck, Check, ChevronRight, Download, Monitor, Package, Power } from 'lucide-react'
import { Btn } from '../ui'
import { Dialog, DialogBody, DialogContent, DialogTitle } from '../ui/dialog'
import Clickable from '../Clickable'
import AppIcon from '../AppIcon'
import { gradientFor } from './gradient'
import { categoryFor } from './categories'
import { useHeroArt } from './useHeroArt'
import { useEditorialArt, type EditorialArtwork } from './useEditorialArt'
import { sourceLabel, isVerified, type RegistryApp } from './types'
import { appDisplayName, appDescription } from './appManifest'
import { needsDesktopApp } from '../../lib/electron'

import { i18nT } from '../../i18n/t'

/**
 * One app inside a featured card: icon, name, a secondary line, and the control
 * that acts on it.
 *
 * The control is per-row rather than per-card. A collection whose card carried a
 * single Get button could only ever install one member, and which one would be
 * an accident of ordering.
 *
 * `onOpen` is OPTIONAL, and that is what keeps exactly ONE interactive layer per
 * card. On a single-app card the card itself is the click target, so the row body
 * must stay inert: a nested clickable would bubble into the card's handler and
 * open the app twice -- two history entries on a plain click, two browser tabs on
 * a modified one -- besides nesting `role="button"` inside `role="button"` with a
 * duplicate label. A collection card is not clickable, so there the rows are the
 * only targets and each one gets a handler.
 */
function FeaturedAppRow({
  app,
  secondary,
  busy,
  onOpen,
  onGet,
  onEnable,
}: {
  app: RegistryApp
  /** The line under the name. The app's description, or its provenance meta. */
  secondary: string
  busy?: boolean
  /** Omit when an ancestor already opens this app, so the row stays inert. */
  onOpen?: (e?: React.MouseEvent | React.KeyboardEvent) => void
  onGet: () => void
  onEnable: () => void
}) {
  // A built-in that is installed but disabled needs enabling, not installing:
  // the bytes are already present, so offering "Get" would be a no-op.
  const hiddenBuiltin = app.origin === 'builtin' && app.installed && !app.enabled

  const identity = (
    <>
      <AppIcon icon={app.icon} iconUrl={app.iconUrl} iconUrlDark={app.iconUrlDark} size={38} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 min-w-0">
          {isVerified(app) && (
            <BadgeCheck size={13} className="text-accent shrink-0" aria-label={i18nT('components.appstore.featuredSpotlight.verified_publisher')}>
              <title>{i18nT('components.appstore.featuredSpotlight.verified_publisher_first_party')}</title>
            </BadgeCheck>
          )}
          {/* An app's name comes from its own manifest, so it is the same Latin
              text in every locale and must never be translated. */}
          <span data-i18n-opaque className="text-[13.5px] font-semibold text-text-strong truncate">{appDisplayName(app)}</span>
        </div>
        {/* Opaque as a WHOLE line, not per segment. The render scanner joins
            adjacent inline nodes into one run before grading it, so an opaque
            span inside a transparent paragraph is graded together with its
            neighbours and the marker has no effect.

            Both variants of this line are predominantly app-supplied: a
            description, or `author · category · version · source`. The two
            catalog strings in the provenance variant lose no coverage by being
            skipped here -- `AppListRow` renders `categoryFor` and `sourceLabel`
            on this same surface, so the gate still asserts both on every row of
            the list below. */}
        <p data-i18n-opaque className="text-[12px] text-muted truncate" title={secondary}>{secondary}</p>
      </div>
    </>
  )

  return (
    <div className="flex items-center gap-3 py-2.5 min-w-0">
      {onOpen ? (
        <Clickable
          aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: appDisplayName(app) })}
          className="flex items-center gap-3 flex-1 min-w-0 rounded-[10px] focus-ring cursor-pointer"
          onClick={onOpen}
        >
          {identity}
        </Clickable>
      ) : (
        <div className="flex items-center gap-3 flex-1 min-w-0">{identity}</div>
      )}
      <div
        className="shrink-0"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}
        role="presentation"
      >
        {hiddenBuiltin ? (
          /* A desktop-only builtin is still ENABLE-able from a browser (the
             state change is server-side; only its UI needs the desktop shell).
             Same contract as AppListRow: the Monitor badge's hint lives in a
             hover title, so the button's accessible name carries the same hint
             for keyboard / screen-reader users. Stacked vertically like
             AppListRow's action column -- side by side, the pair overflows a
             narrow card in locales with long translations. */
          <span className="flex flex-col items-end gap-1">
            <Btn
              primary
              className="rounded-full px-3.5 py-1 font-semibold"
              disabled={busy}
              onClick={onEnable}
              aria-label={needsDesktopApp(app)
                ? `${i18nT('components.appstore.featuredSpotlight.enable')}. ${i18nT('components.appstore.featuredSpotlight.desktop_app_hint')}`
                : undefined}
            >
              <Power size={13} /> {i18nT('components.appstore.featuredSpotlight.enable')}
            </Btn>
            {needsDesktopApp(app) && (
              <span className="inline-flex items-center gap-1 text-[12px] text-muted" title={i18nT('components.appstore.featuredSpotlight.desktop_app_hint')}>
                <Monitor size={12} /> {i18nT('components.appstore.featuredSpotlight.desktop_app')}
              </span>
            )}
          </span>
        ) : app.installed ? (
          <span className="inline-flex items-center gap-1.5 text-[12.5px] text-muted whitespace-nowrap"><Check size={13} /> {i18nT('components.appstore.featuredSpotlight.installed')}</span>
        ) : (
          <Btn primary className="rounded-full px-3.5 py-1 font-semibold" disabled={busy} onClick={onGet}>
            <Download size={13} /> {i18nT('components.appstore.featuredSpotlight.get')}
          </Btn>
        )}
      </div>
    </div>
  )
}

export default function FeaturedSpotlight({
  type,
  apps,
  title,
  blurb,
  artwork,
  curated = false,
  layout = 'stacked',
  compact = false,
  onOpenApp,
  onGet,
  onEnable,
  busyName,
}: {
  /** Which shape this is. `app` carries one entry in `apps`; `collection` two or more. */
  type: 'app' | 'collection'
  /** Every app in the placement, in the curator's order. Never empty. */
  apps: RegistryApp[]
  /** The curator's theme. Present for a collection, absent for a single app. */
  title?: string
  /** Curator copy, preferred over the app's own description when present. */
  blurb?: string
  artwork?: EditorialArtwork | null
  /**
   * True when this card renders a PUBLISHED editorial section, false when it
   * renders a derived pick.
   *
   * It gates one thing: whether the lead app's own hero image may fill the art
   * band. A curator's placement may not borrow it -- that art argues for one app,
   * and illustrating a curator's claim with the author's picture attributes the
   * claim to the wrong party; on a collection it is worse, since the art would
   * come from whichever member is first and would silently promote it. A derived
   * pick makes no such claim: it is "here is an app, here is its art", so the
   * app's own image is exactly the right picture and stays.
   */
  curated?: boolean
  /**
   * How the art and the copy sit relative to each other.
   *
   * `stacked` (the default) is a band across the top, then the copy, then the
   * rows -- the shape a half-width card needs, where a side-by-side split would
   * leave neither half wide enough to read.
   *
   * `side` puts the art in a left panel beside the copy. For the LEAD placement
   * at full width, where a band deep enough to read as art (220px) plus copy plus
   * rows is most of a screen: the same picture costs no extra height beside the
   * text instead of above it.
   */
  layout?: 'stacked' | 'side'
  /**
   * Collapse a COLLECTION to a short art-beside-copy card that opens its app
   * rows in a dialog. For the cards of a `row` block: three inline install rows
   * per card made the row taller than the full-width lead above it, inverting
   * the page's hierarchy. The cost is one click between the reader and the
   * Get button, paid only on the compact variant.
   *
   * Ignored for `app` placements when the card is CURATED -- a single app has
   * no rows to fold away, and the curator's artwork is the placement. On a
   * DERIVED app card it suppresses the stacked art band instead, so a
   * fallback row reads as secondary beside the lead.
   */
  compact?: boolean
  onOpenApp: (name: string, e?: React.MouseEvent | React.KeyboardEvent) => void
  onGet: (name: string) => void
  onEnable: (name: string) => void
  /** Name of the app with an action in flight, so only its own control disables. */
  busyName?: string | null
}) {
  // The lead app anchors the fallback art and the single-app heading. For a
  // collection it is only the art fallback: the heading is the curator's theme,
  // and no member is promoted above the others in the rows.
  const lead = apps[0]
  const isCollection = type === 'collection'
  // `lead` may be absent, so the hooks run unconditionally against an optional
  // app rather than being skipped -- React forbids the skip, and `useHeroArt`
  // answers "no art" for no app, which is the same answer it gives for an app
  // shipping none.
  const hero = useHeroArt(lead)
  const editorial = useEditorialArt(artwork)
  // Unconditional like the art hooks above: the early return below sits between
  // this and the compact branch that reads it, and React forbids the skip.
  const [expanded, setExpanded] = useState(false)
  // Editorial artwork wins: this is an editorial placement, so the curator's
  // picture is the point of it. The app's own hero remains the fallback for a
  // DERIVED pick only -- see `curated` for why a published section may not
  // borrow it, and renders no band at all instead.
  const artSrc = editorial.src || (curated ? '' : hero.src)
  const onArtError = editorial.src ? editorial.onError : hero.onError

  // Nothing read from the document may cost the page its render, and everything
  // below dereferences `lead`. The caller drops a section with no resolvable app,
  // so this is the belt to that braces -- a future caller gets an empty render
  // instead of a thrown one.
  if (!lead) return null

  const heading = isCollection ? title : appDisplayName(lead)
  const sub = blurb || (isCollection ? '' : appDescription(lead))

  /**
   * The dissolve at the foot of the art, and how far the copy climbs into it.
   *
   * FIXED pixels, not a percentage of the art: the dissolve is an optical
   * distance, so a proportional one gets thicker on a taller card and the two
   * cards in a row stop matching. It also has to be a real number here because
   * `OVERLAP` is measured against it -- a percentage cannot be pulled up by a
   * margin that tracks it.
   *
   * OVERLAP is what pays for the 16:9 art. The copy starts inside the dissolve
   * rather than below it, so the band's lower quarter carries the eyebrow and the
   * heading instead of being spent twice -- once fading out, once as blank card.
   * It stops short of FADE so the first text line lands where the tint is already
   * most of the way to the card colour, never on sharp picture.
   */
  const FADE = 140
  const OVERLAP = 104

  /* A row-block APP card: the stacked art band is what makes the card tall,
     and beside a side-layout lead the row must read as SECONDARY -- so
     `compact` suppresses the band on a DERIVED app card (the icon row still
     identifies the app). A curated card keeps its band: the curator chose
     that artwork for this placement, and the published path's rendering is
     this component's contract. Collections take their own compact branch. */
  const artSuppressed = compact && !isCollection && !curated

  const art = artSuppressed || (curated && !artSrc) ? null : (
    <div
      /* 16:9, matching what the schema tells a curator to author (1600x900). A
         fixed band height cropped that source to whatever the band happened to
         be, so the curator's framing survived only by luck -- the safe area
         moved with the card's width. Honouring the authored ratio is what makes
         "compose for 16:9" a promise rather than a suggestion. */
      className={
        layout === 'side'
          // 16:9 here too, but it has to opt OUT of the row stretch to get it: a
          // stretched grid item takes the row's height and ignores aspect-ratio
          // entirely. `self-start` lets the ratio size the item, which then sizes
          // the row -- and `min-h-full` is the guard for the opposite case, a copy
          // column taller than the picture, where without it the art would stop
          // short and leave a hole in the card. So: exactly 16:9 whenever the art
          // is the taller side, cropped taller only when the copy outgrows it.
          ? 'relative aspect-[16/9] self-start min-h-full overflow-hidden grid place-items-center'
          : 'relative aspect-[16/9] overflow-hidden grid place-items-center'
      }
      style={artSrc ? { background: 'var(--bg-elevated)' } : { background: gradientFor(lead.name) }}
    >
      {artSrc ? (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle hook (it retires unreachable art in favour of the gradient plate), not a user interaction; the card's activation lives on the Clickable that wraps it
        <img
          src={artSrc}
          alt={editorial.src ? editorial.alt : ''}
          className="absolute inset-0 w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
          onError={onArtError}
        />
      ) : (
        /* `relative overflow-hidden` are both load-bearing here. `rasterFill`
           absolutely insets the image, and this plate's own wrapper is the
           `relative aspect-[16/9]` art panel — so without `relative` on THIS box
           the icon would fill the whole 16:9 panel and read as hero art rather
           than as an icon. `overflow-hidden` is what makes it take the
           `rounded-3xl`, which this plate did not need while the icon was inset. */
        <div className="w-[92px] h-[92px] rounded-3xl bg-white/15 border border-white/25 backdrop-blur-sm grid place-items-center text-white relative overflow-hidden">
          {(lead.iconUrl || lead.iconUrlDark || lead.icon) ? <AppIcon icon={lead.icon} iconUrl={lead.iconUrl} iconUrlDark={lead.iconUrlDark} size={56} rasterFill /> : <Package size={44} />}
        </div>
      )}

      {/* Progressive dissolve into the copy below, on the STACKED card only: its
          art meets the text along a horizontal edge, so the picture can hand off
          to the words. The `side` card's boundary is vertical and its copy sits
          beside the art, where a bottom fade would blur nothing and hide part of
          the picture.

          Blur RAMPS across three stacked layers rather than one: a single
          backdrop-blur has one radius, so its own top edge is a visible line
          where sharp meets blurred -- the artefact this is supposed to remove.
          Each layer masks itself in later and blurs harder, and every mask is
          paired with `WebkitMaskImage` because Safari still needs the prefix.

          The tint is `color-mix`, not `bg-card/NN`: these theme tokens carry no
          alpha channel, so a Tailwind opacity modifier on them compiles to
          nothing at all. Its curve is front-loaded because the copy climbs into
          this band -- by the line where text begins the tint already has to be
          most of the way to the card colour. */}
      {layout === 'stacked' && artSrc && (
        <div className="absolute inset-x-0 bottom-0 pointer-events-none" style={{ height: FADE }}>
          <div
            className="absolute inset-0 backdrop-blur-[2px]"
            style={{
              maskImage: 'linear-gradient(to bottom, transparent 0%, black 40%)',
              WebkitMaskImage: 'linear-gradient(to bottom, transparent 0%, black 40%)',
            }}
          />
          <div
            className="absolute inset-0 backdrop-blur-[10px]"
            style={{
              maskImage: 'linear-gradient(to bottom, transparent 18%, black 55%)',
              WebkitMaskImage: 'linear-gradient(to bottom, transparent 18%, black 55%)',
            }}
          />
          <div
            className="absolute inset-0 backdrop-blur-[24px]"
            style={{
              maskImage: 'linear-gradient(to bottom, transparent 45%, black 85%)',
              WebkitMaskImage: 'linear-gradient(to bottom, transparent 45%, black 85%)',
            }}
          />
          <div
            className="absolute inset-0"
            style={{
              background:
                'linear-gradient(to bottom,' +
                ' color-mix(in srgb, var(--card) 0%, transparent) 0%,' +
                ' color-mix(in srgb, var(--card) 62%, transparent) 26%,' +
                ' color-mix(in srgb, var(--card) 90%, transparent) 55%,' +
                ' var(--card) 82%)',
            }}
          />
        </div>
      )}
    </div>
  )

  const body = (
    <div
      className="px-5 pt-4 pb-2 relative"
      /* Climb into the dissolve on a stacked card. `relative` (a paint order,
         not a position) keeps the copy above the art's own overlay: both are in
         normal flow, so without a stacking context the negative margin would
         slide the text UNDER the tint that is supposed to be behind it. */
      style={layout === 'stacked' && artSrc && !artSuppressed ? { marginTop: -OVERLAP } : undefined}
    >
      <span className="text-[11px] font-bold tracking-[.14em] text-accent">
        {isCollection
          ? i18nT('components.appstore.featuredSpotlight.collection')
          : i18nT('components.appstore.featuredSpotlight.featured')}
      </span>
      {/* Both the curator's theme and an app's own name are Latin in every
          locale: one is published English-only in the editorial document, the
          other comes from a third-party manifest. Neither is catalog copy, so
          neither may be translated -- and the gate needs telling, or every
          featured card reads as a fresh render-time i18n defect. */}
      <h2 data-i18n-opaque className="text-[23px] leading-[1.2] font-bold text-text-strong tracking-tight mt-1.5">{heading}</h2>
      {sub && <p data-i18n-opaque className="text-[14px] text-muted line-clamp-2 mt-1" title={sub}>{sub}</p>}
      <div className="mt-2.5 divide-y divide-border">
        {apps.map(a => (
          <FeaturedAppRow
            key={a.name}
            app={a}
            /* A collection row describes the app, since the card's copy already
               carries the theme. A single-app card has already shown the
               description above, so its row carries provenance instead. */
            secondary={
              isCollection
                ? appDescription(a)
                : `${a.author} · ${categoryFor(a.tags)} · ${i18nT('components.appstore.featuredSpotlight.v')}${a.installedVersion || a.version} · ${sourceLabel(a)}`
            }
            busy={busyName === a.name}
            /* Only a collection's rows are interactive; on a single-app card the
               card itself is the target, so passing a handler here would open
               the app twice. */
            onOpen={isCollection ? e => onOpenApp(a.name, e) : undefined}
            onGet={() => onGet(a.name)}
            onEnable={() => onEnable(a.name)}
          />
        ))}
      </div>
    </div>
  )

  const shell = 'border border-border rounded-[20px] overflow-hidden bg-card mb-3.5'

  // A collection has nothing to open, so the card is a plain container and its
  // rows carry every interaction. A single-app card opens that app, which is
  // what a reader expects from tapping a picture of one app.
  //
  // `group` goes on the clickable variant ONLY: the art's hover zoom hangs off
  // it, and animating a collection card under the cursor would signal exactly
  // the interactivity this variant refuses to fake.
  // `side` is a two-column split; `stacked` is the original vertical order. The
  // art is FIRST in the DOM either way, so a reader meets the picture before the
  // heading in both, and `side` needs no order override to stay in reading order.
  const inner =
    layout === 'side' && art ? (
      <div className="grid grid-cols-1 md:grid-cols-[minmax(0,44%)_1fr] items-stretch">
        {art}
        {/* Centred, not top-aligned: the 16:9 art now sizes the row, so on a
            one-row card the copy is the shorter side and pinning it to the top
            would strand it against a picture half again its height. */}
        <div className="min-w-0 flex flex-col justify-center">{body}</div>
      </div>
    ) : (
      <>{art}{body}</>
    )

  if (isCollection && compact) {
    /* The short card: art beside copy, app rows folded into a dialog. ONE
       interactive layer, same rule as the single-app card -- the rows moved
       into the dialog, so the card has no inner control left to nest and the
       whole surface can be the trigger without a double-open hazard.

       Deliberately NO member preview on the face: a strip of app icons was
       tried and cut, because member icons are third-party assets of uneven
       quality and the compact face is curated surface -- the curator's art and
       copy carry it, and what is inside is the dialog's job to answer. */
    return (
      <>
        <Clickable
          aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: title || '' })}
          aria-haspopup="dialog"
          aria-expanded={expanded}
          className={`${shell} group cursor-pointer hover:border-border-strong transition-colors focus-ring mb-0`}
          onClick={() => setExpanded(true)}
        >
          <div className={artSrc ? 'grid grid-cols-1 md:grid-cols-[minmax(0,40%)_1fr] items-stretch' : ''}>
            {artSrc && (
              <div
                /* Stacked on a narrow viewport, so the art needs its own height
                   there: as a grid COLUMN it is stretched by the copy beside it,
                   but stacked above the copy nothing sizes it. 16:9 provides
                   that; `md:aspect-auto` hands sizing back to the row once the
                   split kicks in, mirroring the `side` layout above. */
                className="relative aspect-[16/9] md:aspect-auto overflow-hidden"
                style={{ background: 'var(--bg-elevated)' }}
              >
                {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle hook (retire unreachable art), not a user interaction; the collection face's activation is the Clickable above */}
                <img
                  src={artSrc}
                  alt={editorial.src ? editorial.alt : ''}
                  className="absolute inset-0 w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
                  onError={onArtError}
                />
              </div>
            )}
            <div className="min-w-0 px-5 py-4">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] font-bold tracking-[.14em] text-accent">
                  {i18nT('components.appstore.featuredSpotlight.collection')}
                </span>
                {/* The face folded its rows into a dialog, so it needs to SAY it
                    opens: without an affordance the card reads as a static
                    banner and the apps inside are never discovered. Decorative
                    (aria-hidden) because the Clickable already carries the
                    accessible name and role. */}
                <ChevronRight
                  size={16}
                  aria-hidden
                  className="shrink-0 text-muted group-hover:text-text-strong group-hover:translate-x-0.5 transition-all"
                />
              </div>
              <h2 data-i18n-opaque className="text-[18px] leading-[1.25] font-bold text-text-strong tracking-tight mt-1 truncate">{heading}</h2>
              {sub && <p data-i18n-opaque className="text-[13px] text-muted line-clamp-2 mt-1" title={sub}>{sub}</p>}
            </div>
          </div>
        </Clickable>
        <Dialog open={expanded} onOpenChange={setExpanded}>
          <DialogContent maxWidth={560} aria-label={title}>
            {artSrc && (
              <div className="relative aspect-[16/9] shrink-0 overflow-hidden" style={{ background: 'var(--bg-elevated)' }}>
                {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle hook (retire unreachable art), not a user interaction; this art is decoration inside the dialog and offers nothing to activate */}
                <img
                  src={artSrc}
                  alt={editorial.src ? editorial.alt : ''}
                  className="absolute inset-0 w-full h-full object-cover"
                  onError={onArtError}
                />
              </div>
            )}
            <DialogBody className="pt-4">
              <span className="text-[11px] font-bold tracking-[.14em] text-accent">
                {i18nT('components.appstore.featuredSpotlight.collection')}
              </span>
              <DialogTitle asChild>
                <h2 data-i18n-opaque className="text-[21px] leading-[1.2] font-bold text-text-strong tracking-tight mt-1">{heading}</h2>
              </DialogTitle>
              {sub && <p data-i18n-opaque className="text-[13.5px] text-muted mt-1">{sub}</p>}
              <div className="mt-2.5 divide-y divide-border">
                {apps.map(a => (
                  <FeaturedAppRow
                    key={a.name}
                    app={a}
                    secondary={appDescription(a)}
                    busy={busyName === a.name}
                    onOpen={e => onOpenApp(a.name, e)}
                    onGet={() => onGet(a.name)}
                    onEnable={() => onEnable(a.name)}
                  />
                ))}
              </div>
            </DialogBody>
          </DialogContent>
        </Dialog>
      </>
    )
  }

  if (isCollection) {
    return <div className={shell}>{inner}</div>
  }
  return (
    <Clickable
      aria-label={i18nT('components.appstore.featuredSpotlight.view_details_for', { name: appDisplayName(lead) })}
      className={`${shell} group cursor-pointer hover:border-border-strong transition-colors focus-ring`}
      onClick={e => onOpenApp(lead.name, e)}
    >
      {inner}
    </Clickable>
  )
}
