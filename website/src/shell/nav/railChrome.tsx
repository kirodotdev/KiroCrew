import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowLeftToLine, PanelLeft } from 'lucide-react'
import { GithubIcon, DiscordIcon } from '../../components/BrandIcon'
import type { getThemeBranding } from '../../themeBranding'
import { i18nT } from '../../i18n/t'

/** Glyph inside the nav-rail header's expand/collapse button — the same
 *  load-proof contract as MobileNavGlyph, with the rail's own geometry. When
 *  the rail is collapsed the logo is the button's ONLY visible content (the
 *  bot name is unmounted), so a 404 on the avatar asset, a blocked request or
 *  a hung fetch used to leave an invisible control that still toggled the
 *  rail when clicked. A PanelLeft glyph therefore fills the box by default,
 *  the swap to the logo happens only on the img's own `load` event, and
 *  `error` reverts it. `loadedSrc` records WHICH src loaded so a branding or
 *  theme swap falls back until the new asset proves itself. `boxClass` is the
 *  theme-overridable size (`branding.logoClass`, else w-10 collapsed / w-7
 *  expanded) and is applied to BOTH the fallback and the img so the swap never
 *  moves the button's geometry; the hover tilt and `transition-all` classes
 *  live on the img exactly as before. The img stays mounted (display:none)
 *  while hidden so the browser still fetches it. A sibling rather than a
 *  generalisation of MobileNavGlyph: that component's literal `w-6 h-6` box is
 *  pinned by narrowFirstBaseline.test.ts, while this box is a runtime
 *  expression. */
export function RailHeaderGlyph({ avatar, boxClass, iconSize }: { avatar: string; boxClass: string; iconSize: number }) {
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null)
  const showLogo = !!avatar && loadedSrc === avatar
  return (
    <>
      {!showLogo && (
        <span data-testid="rail-header-fallback" className={`${boxClass} flex items-center justify-center shrink-0 transition-all duration-300 group-hover:rotate-[-8deg]`} aria-hidden="true">
          <PanelLeft size={iconSize} />
        </span>
      )}
      {!!avatar && (
        <img src={avatar} alt="" aria-hidden="true" onLoad={() => setLoadedSrc(avatar)} onError={() => setLoadedSrc(null)} className={`${boxClass} rounded-md shrink-0 object-contain transition-all duration-300 group-hover:rotate-[-8deg] ${showLogo ? '' : 'hidden'}`} />
      )}
    </>
  )
}

/** The rail header: the brand mark that is also the collapse toggle. */
export function RailBrandToggle({ effectiveCollapsed, toggleNav, avatar, branding, botName }: {
  effectiveCollapsed: boolean
  toggleNav: () => void
  avatar: string
  branding: ReturnType<typeof getThemeBranding>
  botName: string
}) {
  /*
   * mb-1.5 (6px) + the container's gap-0.5 (2px) = 8px between the
   * header and the first nav item, without widening the 2px item gaps.
   */
  return (
    <div className={`relative flex items-center mb-1.5 ${effectiveCollapsed ? 'justify-center' : ''}`}>
      {/* One persistent click target that toggles the rail. The logo
          never unmounts, so there is no swap across collapse/expand; its
          size (w-7 ↔ w-9) and collapsed nudge (mt-1) both animate over
          duration-300, so the mark glides between states rather than
          jumping. Only the brand text + collapse arrow
          animate — fading in on expand and out on collapse via
          AnimatePresence. No hover tint on the row; on hover only the
          logo rotates (group-hover). */}
      {/* No overflow-hidden here: the logo's hover-rotate paints a few
          px past its box, and clipping it looked cut off. Rotation is a
          transform so it doesn't affect the header's layout height
          (row height tracks the logo, collapse-icon alignment
          unchanged); horizontal spill on collapse is still clipped by
          the rail (motion.nav) and the brand text clips itself via
          `truncate`.
          Logo is DUAL-SIZE: w-7 (28px) expanded — 1px card border +
          pt-2 + 14 puts the header row's center on the 23px shared
          control baseline — and w-9 (36px) collapsed, centered in the
          icon strip and nudged down (mt-1) so both its symmetric inset
          and its top clear the rail's rounded-xl corner, which clips
          anything that reaches it; the mt-1 animates (transition-[margin]
          duration-300) in lockstep with the glyph's size transition so
          the flip is a glide, not a jump (a branding logoClass overrides
          both). The collapse arrow pins to top-[6px] rather than centering
          in the row — so its center stays on the
          23px shared control baseline (chat title row, its sessions
          toggle, and the activity strip icons) while the two-line
          brand block makes the row taller. */}
      <button
        type="button"
        className={`group relative flex items-center gap-2 w-full p-0 bg-transparent border-none cursor-pointer text-left ${effectiveCollapsed ? 'justify-center' : ''}`}
        onClick={toggleNav}
        title={effectiveCollapsed ? i18nT('app.expand_sidebar') : i18nT('app.collapse_sidebar')}
        aria-label={effectiveCollapsed ? i18nT('app.expand_sidebar') : i18nT('app.collapse_sidebar')}
        aria-expanded={!effectiveCollapsed}
      >
        <span className={`flex items-center gap-2.5 min-w-0 transition-[margin] duration-300 ${effectiveCollapsed ? 'mt-1' : ''}`}>
          <RailHeaderGlyph avatar={avatar} boxClass={branding?.logoClass ?? (effectiveCollapsed ? 'w-9 h-9' : 'w-7 h-7')} iconSize={effectiveCollapsed ? 24 : 18} />
          <AnimatePresence initial={false}>
            {!effectiveCollapsed && (
              <motion.span
                key="brand-text"
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -6, transition: { duration: 0.12, ease: 'easeIn' } }}
                transition={{ duration: 0.2, ease: 'easeOut' }}
                className="text-[13px] font-bold tracking-[.14em] uppercase whitespace-nowrap truncate min-w-0"
              >
                {/* Last word of the bot name carries the accent (KIRO
                    CREW: muted brand, accent product); single-word names
                    render all-muted. */}
                {botName.includes(' ') ? (
                  <>
                    <span className="text-muted">{botName.slice(0, botName.lastIndexOf(' ') + 1)}</span>
                    <span className="text-accent/90">{botName.slice(botName.lastIndexOf(' ') + 1)}</span>
                  </>
                ) : (
                  <span className="text-muted">{botName}</span>
                )}
              </motion.span>
            )}
          </AnimatePresence>
        </span>
        {/* Arrow is ABSOLUTE (out of flex flow), pinned to the right.
            If it were a flex child it would reserve ~16px on the right
            from frame 1 of expand — but the rail is still at collapsed
            width (74px) for that frame, so logo + gap + arrow overflowed
            and the logo got crammed/clipped against the arrow (the
            "blink"). Absolute-positioning removes that reserved space, so
            the logo stays put and the arrow just fades in at the edge. */}
        <AnimatePresence initial={false}>
          {!effectiveCollapsed && (
            <motion.span
              key="collapse-arrow"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.18, ease: 'easeOut', delay: 0.12 } }}
              exit={{ opacity: 0, transition: { duration: 0.12, ease: 'easeIn' } }}
              className="absolute right-0 top-[6px] h-4 flex items-center text-muted pointer-events-none"
            >
              {/* Arrow-to-edge, not a hide-panel glyph: the rail
                  collapses to an icon rail rather than hiding. */}
              <ArrowLeftToLine size={15} />
            </motion.span>
          )}
        </AnimatePresence>
      </button>
    </div>
  )
}

export function RailCommunityLinks({ effectiveCollapsed, setReportProblemOpen }: {
  effectiveCollapsed: boolean
  setReportProblemOpen: (open: boolean) => void
}) {
  /*
   * Community row — a leading GitHub mark, then two links on ONE
   * line separated by a middot, then the icon-only Discord link.
   *
   * This line is tight by construction, and the numbers are
   * MEASURED against real font advance widths, not estimated.
   * The rail is 236px, which leaves a 143px text group after the
   * mark, the Discord icon and padding; the middot plus its gaps
   * costs ~10-15px depending on family.
   *
   * CRITICAL: size this against the WIDEST font the user can pick,
   * not the default. `useZoom` lets them set --font-body to sans
   * (Space Grotesk), mono (JetBrains Mono) or system (-apple-system),
   * and mono is ~20% wider. A 12px row measured only against Space
   * Grotesk truncates for every mono user.
   *
   * "Star us · Report issue" at 12px, measured:
   * Space Grotesk   114.0px against a 132.8px budget — 18.7 spare
   * JetBrains Mono  136.8px against a 127.8px budget — 9.0 OVER
   * Rather than shrink the type for everyone or drop the Discord
   * link, mono alone is tightened to -0.05em, which brings it to
   * 125.4px (+3.0 spare). That rule lives in index.css keyed on
   * html[data-font-family="mono"] via the `rail-community-links`
   * class, and its measurement table is there. Mono's margin is only
   * ~3px, so ANY copy growth here must be re-measured IN MONO first.
   *
   * The separator is a middot because " / " is wider, and the row's
   * right padding is trimmed for the same budget reason.
   *
   * The mark sits 2px from the text (ml-0.5) while the middot keeps
   * 4px gaps. That asymmetry is an OPTICAL correction, not an
   * oversight: github-mark.svg is a circle filling its whole 16x16
   * viewBox (no internal padding), and a circle beside a capital "S"
   * curves away from it, so an equal metric gap reads as a wider
   * one. Matching the middot's 4px here looked detached. Font and
   * letter-spacing are deliberately NOT overridden — the row
   * inherits --font-body and letter-spacing:normal from body, so it
   * follows the user's own font choice like everything else.
   *
   * Order of yielding under pressure is deliberate: "Star us" and
   * the middot are shrink-0, so a longer locale (Spanish's "Informar
   * de un problema") ellipsizes the TAIL of the second link rather
   * than mangling both. Both links keep a title tooltip, so a
   * clipped label is still readable on hover.
   *
   * One mark for two links is correct — both destinations ARE
   * GitHub. It is decorative (BrandGlyph is aria-hidden) and each
   * link carries its own descriptive aria-label, since "Star us"
   * alone names no target. Hidden while the rail is collapsed (folds
   * away via max-height so the collapse stays smooth).
   */
  return (
    <div {...(effectiveCollapsed ? { inert: '' } : {})} className={`overflow-hidden transition-all duration-200 ${effectiveCollapsed ? 'max-h-0 opacity-0' : 'max-h-16 opacity-100 mt-1'}`}>
      <div className="flex items-center border-t border-border pl-3 pr-0.5 pt-2.5 pb-0.5 whitespace-nowrap">
        {/* pl-3 puts the mark on the same 12px x-offset as the
            nav-item icons above. No `gap` on this row ON PURPOSE: a row
            gap applies between ALL THREE children (mark, links,
            Discord), so pairing it with ml-0.5 would silently double
            the mark-to-text distance to 6px and cost 4px the budget
            below never accounts for. Spacing is explicit per child instead. */}
        <span className="flex items-center shrink-0 text-muted"><GithubIcon size={15} /></span>
        {/* `flex-wrap`: in a locale where "Star us" and "Report issue" together
            outrun the rail (the pseudolocale does, and so will any long-word
            language), the second link drops to its own line with the full
            row width instead of truncating to a third of itself. */}
        <div className="rail-community-links flex flex-wrap items-center gap-x-[5px] gap-y-0.5 flex-1 min-w-0 ml-1.5 text-[12px]">
          <a href="https://github.com/kirodotdev/KiroCrew" target="_blank" rel="noopener noreferrer" title={i18nT('app.star_kirocrew_on_github')} aria-label={i18nT('app.star_kirocrew_on_github')} className="shrink-0 rounded text-muted hover:text-text transition-colors">{i18nT('app.star_us')}</a>
          <span aria-hidden="true" className="shrink-0 opacity-40">·</span>
          {/* "Report issue" opens the SAME diagnostics flow as Settings ›
              About › Support rather than linking to the bare issue list.
              A user who reaches for this link is reporting a failure, and
              an empty issue form loses exactly what triage needs (logs +
              crash reports); the collector scrubs secrets, zips them, and
              still ends at a pre-filled GitHub issue, so the old
              destination is reachable WITH evidence attached. A <button>
              (not an <a>) because it no longer navigates — styled to match
              its sibling link so the row's width budget above is unchanged. */}
          <button type="button" onClick={() => setReportProblemOpen(true)} title={i18nT('app.report_a_problem_with_diagnostics')} aria-label={i18nT('app.report_a_problem_with_diagnostics')} className="min-w-0 overflow-hidden text-ellipsis rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-0 p-0 text-[12px]">{i18nT('app.report_issue')}</button>
        </div>
        <a href="https://kiro.dev/discord/" target="_blank" rel="noopener noreferrer" title={i18nT('app.discord_community')} aria-label={i18nT('app.kiro_discord_community')} className="flex items-center justify-center ml-1 w-6 h-6 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors shrink-0"><DiscordIcon size={15} /></a>
      </div>
    </div>
  )
}
