import type { ActiveRepo } from './lib/types'
// Welcome carousel — a self-contained onboarding wizard: card shell, Back/Next
// nav, progress dots, per-page fade-in, floating/pulsing icon animation, and
// the FULL orbiting solar-system background decoration (8 rings, moons on
// rocky/gas giants, hover tooltips, Carl Sagan quote). Themed onto this
// project's --accent/--bg/--text tokens.
//
// The connect step is a SLIDE OF THIS CAROUSEL, not a separate component
// rendered after it — putting it outside the carousel meant Back stopped
// working once you reached it (the parent swapped WelcomeCarousel out
// entirely). Keeping it as the last slide means Back always has somewhere to
// go, all the way back to slide 0.
//
// That last slide's BODY is the shared <ConnectPanel> (provider rows +
// recent-repo multi-select), and its state lives in the shared
// `useConnectFlow` hook — shared with ConnectRepoModal so the first-run flow
// and the "connect another repo" overlay can never drift apart. The hook is
// called HERE rather than inside the slide because this component renders the
// Connect button in its nav row (the slot Next occupies on content slides) and
// its Back button needs to see the provider selection: Back on the connect
// slide first clears the chosen provider (collapsing the card back down)
// before it pops the page, otherwise a user who opened GitHub and changed
// their mind gets yanked all the way to the previous content slide instead of
// a single step back.
import { useState } from 'react'
import { Radar, Search, GitPullRequest, BookOpen, RefreshCw, ArrowLeft, ArrowRight } from 'lucide-react'
import ConnectPanel, { COLLAPSED_CARD, EXPANDED_CARD, expandsCard, useConnectFlow } from './ConnectPanel'

import { i18nT } from '../../i18n/t'
interface Slide {
  /** Stable slide identifier — indexes the copy tables below and keys nothing else. */
  id: string
  icon: React.ReactNode
  /** CSS animation class applied to the icon wrapper — varies per slide so the
   * carousel doesn't feel like the exact same motion five times in a row. */
  animClass: string
}

/**
 * Catalog KEYS for the slide headings and body copy.
 *
 * Keys rather than the strings themselves: `SLIDES` is module-level data,
 * evaluated once at import, so an `i18nT()` call in it would freeze whatever
 * language was active at boot and never re-resolve on a language switch. The
 * lookups happen in the component body, which runs per render.
 *
 * Two flat `Record`s of full literal keys, indexed inline at the `i18nT()` call
 * (`i18nT(SLIDE_TITLE_KEY[slide.id])`) — the only shape
 * `scripts/check-i18n-keys.mjs` can resolve statically. Nesting them per slide
 * (`SLIDES[page].titleKey`) would not resolve, and would land this file in
 * `dynamic-keys-baseline.json`.
 */
export const SLIDE_TITLE_KEY: Record<string, string> = {
  welcome: 'apps.issueRadar.welcomeCarousel.slide_welcome_title',
  local: 'apps.issueRadar.welcomeCarousel.slide_local_title',
  bots: 'apps.issueRadar.welcomeCarousel.slide_bots_title',
  linked_prs: 'apps.issueRadar.welcomeCarousel.slide_linked_prs_title',
  decide: 'apps.issueRadar.welcomeCarousel.slide_decide_title',
}

export const SLIDE_SUBTITLE_KEY: Record<string, string> = {
  welcome: 'apps.issueRadar.welcomeCarousel.slide_welcome_subtitle',
  local: 'apps.issueRadar.welcomeCarousel.slide_local_subtitle',
  bots: 'apps.issueRadar.welcomeCarousel.slide_bots_subtitle',
  linked_prs: 'apps.issueRadar.welcomeCarousel.slide_linked_prs_subtitle',
  decide: 'apps.issueRadar.welcomeCarousel.slide_decide_subtitle',
}

const SLIDES: Slide[] = [
  {
    id: 'welcome',
    icon: <Radar size={48} strokeWidth={1.5} />,
    animClass: 'wc-spin',
  },
  {
    id: 'local',
    icon: <Search size={48} strokeWidth={1.5} />,
    animClass: 'wc-float',
  },
  {
    id: 'bots',
    icon: <BookOpen size={48} strokeWidth={1.5} />,
    animClass: 'wc-pulse',
  },
  {
    id: 'linked_prs',
    icon: <GitPullRequest size={48} strokeWidth={1.5} />,
    animClass: 'wc-float',
  },
  {
    id: 'decide',
    icon: <Radar size={48} strokeWidth={1.5} />,
    animClass: 'wc-spin',
  },
]

// One extra "slide" beyond SLIDES: the connect slide (provider rows ->
// expands in place into the repo picker, see ConnectPanel).
const CONNECT_PAGE = SLIDES.length

export default function WelcomeCarousel({ onConnected }: { onConnected: (repo: ActiveRepo) => void }) {
  const [page, setPage] = useState(0)
  const flow = useConnectFlow(onConnected)

  const isContentSlide = page < SLIDES.length
  const isConnectSlide = page === CONNECT_PAGE
  const expanded = isConnectSlide && expandsCard(flow.provider)
  const targetCount = flow.targets.length

  const handleBack = () => {
    // Blocked while a connect is in flight, for the same reason the modal
    // blocks dismissal: navigating away only changes the UI, it does not
    // cancel the sequential fetch loop, so repos would keep connecting behind
    // a screen that looks like the user backed out.
    if (flow.pending) return
    // Two-level pop on the connect slide: first collapse the provider
    // selection (shrinking the card), only then leave the slide.
    if (isConnectSlide && flow.provider) {
      flow.clearProvider()
      return
    }
    setPage((p) => p - 1)
  }

  return (
    <div className="relative flex h-full items-center justify-center bg-bg overflow-hidden p-3">
      <SolarSystemBackground />
      <div
        className={`relative z-10 border border-border rounded-[14px] bg-card flex flex-col items-center justify-between p-10 text-center transition-[width,min-height] duration-200 ease-out ${
          expanded ? EXPANDED_CARD : COLLAPSED_CARD
        }`}
      >
        {/* min-h-0 + overflow-hidden: the nav row below is a sibling, so
         * without clipping here a taller-than-expected slide would push
         * Back/Next past the card's fixed height and out of the window.
         * Only the EXPANDED connect slide is top-anchored (justify-start):
         * centring a two-column form in a 540px card left a large, lopsided gap
         * above the title. Its collapsed state is a short heading plus two rows,
         * which centres like the five content slides do — top-anchored it sat
         * high with ~140px of dead space between the rows and the nav row. */}
        <div
          className={`flex-1 min-h-0 overflow-y-auto flex flex-col items-center gap-3.5 w-full ${
            isConnectSlide && expanded ? 'justify-start' : 'justify-center'
          }`}
        >
          {isContentSlide && (
            <div key={page} className="animate-[wc-fade_.2s_ease] flex flex-col items-center gap-3.5">
              <div className={`flex items-center justify-center h-20 text-accent ${SLIDES[page].animClass}`}>
                {SLIDES[page].icon}
              </div>
              <div className="text-[20px] font-bold text-text tracking-[-0.2px]">
                {i18nT(SLIDE_TITLE_KEY[SLIDES[page].id])}
              </div>
              <div className="text-[13.5px] text-muted leading-[1.7] max-w-[380px]">
                {i18nT(SLIDE_SUBTITLE_KEY[SLIDES[page].id])}
              </div>
            </div>
          )}
          {isConnectSlide && <ConnectPanel flow={flow} />}
        </div>

        <div className="flex items-center justify-between w-full pt-3 flex-shrink-0">
          <button
            onClick={handleBack}
            disabled={page === 0 || flow.pending}
            className="min-w-[84px] px-4 py-1.5 rounded-md border border-border bg-bg text-text text-xs cursor-pointer disabled:opacity-30 disabled:cursor-default hover:bg-bg-hover"
          >
            <ArrowLeft size={12} className="lucide-inline" /> {i18nT('apps.issueRadar.welcomeCarousel.back')}
          </button>
          <div className="flex gap-1">
            {Array.from({ length: CONNECT_PAGE + 1 }, (_, i) => (
              <div
                key={i}
                className={`h-1.5 rounded-full transition-all ${i === page ? 'w-3.5 bg-accent' : 'w-1.5 bg-border'}`}
              />
            ))}
          </div>
          {/* This slot is Next on content slides, Connect on the connect slide
           * once a provider is chosen (per product decision — "same position
           * Next used to occupy"), and an empty spacer on the connect slide's
           * collapsed state, where the provider rows are the only action. The
           * shared min-width keeps Back/dots aligned across all three. */}
          {isContentSlide && (
            <button
              onClick={() => setPage((p) => p + 1)}
              className="min-w-[84px] px-4 py-1.5 rounded-md border border-accent bg-accent text-bg text-xs font-semibold cursor-pointer hover:opacity-90"
            >
              {i18nT('apps.issueRadar.welcomeCarousel.next')} <ArrowRight size={12} className="lucide-inline" />
            </button>
          )}
          {isConnectSlide && flow.provider && (
            <button
              onClick={flow.submit}
              disabled={targetCount === 0 || flow.pending}
              className="min-w-[84px] inline-flex items-center justify-center gap-1 px-4 py-1.5 rounded-md border border-accent text-accent bg-transparent text-xs font-semibold cursor-pointer hover:bg-accent-subtle disabled:opacity-30"
            >
              <RefreshCw size={12} className={flow.pending ? 'animate-spin' : ''} />
              {flow.progress
                ? i18nT('apps.issueRadar.welcomeCarousel.connecting_n_of_total', { n: flow.progress.done + 1, total: flow.progress.total })
                : targetCount > 1 ? `Connect ${targetCount}` : 'Connect'}
            </button>
          )}
          {isConnectSlide && !flow.provider && <div className="min-w-[84px]" />}
        </div>
      </div>

      {/* Bottom-right quote. */}
      <div className="absolute bottom-6 right-8 z-0 whitespace-nowrap">
        <span className="text-[11px] italic text-muted opacity-50">
          {i18nT('apps.issueRadar.welcomeCarousel.somewhere_something_incredible_is_waiting_to_be')}
        </span>
        <span className="text-[10px] text-muted opacity-35 ml-2">{i18nT('apps.issueRadar.welcomeCarousel.carl_sagan')}</span>
      </div>

      <style>{`
        @keyframes wc-fade{from{opacity:0;transform:translateX(8px)}to{opacity:1;transform:translateX(0)}}
        @keyframes wc-float-kf{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
        @keyframes wc-pulse-kf{0%,100%{opacity:1}50%{opacity:0.4}}
        @keyframes wc-spin-kf{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
        .wc-float{animation:wc-float-kf 3s ease-in-out infinite}
        .wc-pulse{animation:wc-pulse-kf 2.2s ease-in-out infinite}
        .wc-spin{animation:wc-spin-kf 6s linear infinite}

        /* Solar system — full 8-ring layout (Mercury..Neptune),
         * including Earth/Mars/Jupiter/Saturn/Uranus/Neptune's moon rings,
         * hover-to-reveal planet-name tooltips, and orbit-ring guides. */
        @keyframes wc-orbit{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
        .wc-solar{position:absolute;top:78%;left:15%;width:0;height:0;pointer-events:none}
        .wc-orbit-ring{position:absolute;border-radius:50%;border:1.5px solid var(--accent);opacity:0.12;top:50%;left:50%;transform:translate(-50%,-50%)}
        .wc-orbit-ring:nth-child(1){width:73px;height:73px}
        .wc-orbit-ring:nth-child(2){width:134px;height:134px}
        .wc-orbit-ring:nth-child(3){width:188px;height:188px}
        .wc-orbit-ring:nth-child(4){width:286px;height:286px}
        .wc-orbit-ring:nth-child(5){width:483px;height:483px}
        .wc-orbit-ring:nth-child(6){width:883px;height:883px}
        .wc-orbit-ring:nth-child(7){width:1786px;height:1786px}
        .wc-orbit-ring:nth-child(8){width:2800px;height:2800px}
        .wc-sun{position:absolute;width:20px;height:20px;margin:-10px 0 0 -10px;border-radius:50%;background:color-mix(in srgb,var(--accent) 20%,var(--bg))}
        .wc-orb-ring{position:absolute;width:0;height:0;pointer-events:auto}
        .wc-planet{position:absolute;border-radius:50%;background:color-mix(in srgb,var(--accent) 25%,var(--bg));box-shadow:0 0 0 4px var(--bg);cursor:default}
        .wc-r1{animation:wc-orbit 48s linear infinite;animation-delay:-14s}
        .wc-r1 .wc-planet{width:5px;height:5px;top:-2.5px;left:34px}
        .wc-r1 .wc-planet::after{content:'Mercury'}
        .wc-r2{animation:wc-orbit 96s linear infinite;animation-delay:-62s}
        .wc-r2 .wc-planet{width:7px;height:7px;top:-3.5px;left:63.5px}
        .wc-r2 .wc-planet::after{content:'Venus'}
        .wc-r3{animation:wc-orbit 144s linear infinite;animation-delay:-104s}
        .wc-r3 .wc-planet{width:7px;height:7px;top:-3.5px;left:90.5px}
        .wc-r3 .wc-planet::after{content:'Earth'}
        .wc-r3 .wc-moon-ring{position:absolute;top:0;left:94px;width:0;height:0;animation:wc-orbit 10s linear infinite}
        .wc-r3 .wc-moon{position:absolute;width:3px;height:3px;border-radius:50%;background:var(--accent);opacity:0.3;top:-1.5px;left:9px}
        .wc-r4{animation:wc-orbit 216s linear infinite;animation-delay:-170s}
        .wc-r4 .wc-planet{width:6px;height:6px;top:-3px;left:140px}
        .wc-r4 .wc-planet::after{content:'Mars'}
        .wc-r4 .wc-moon-ring{position:absolute;top:0;left:143px;width:0;height:0;animation:wc-orbit 3s linear infinite}
        .wc-r4 .wc-moon{position:absolute;width:2px;height:2px;border-radius:50%;background:var(--accent);opacity:0.3;top:-1px;left:6px}
        .wc-r5{animation:wc-orbit 600s linear infinite;animation-delay:-380s}
        .wc-r5 .wc-planet{width:14px;height:14px;top:-7px;left:234.5px}
        .wc-r5 .wc-planet::after{content:'Jupiter'}
        .wc-r5 .wc-moon-ring{position:absolute;top:0;left:241.5px;width:0;height:0;animation:wc-orbit 4s linear infinite}
        .wc-r5 .wc-moon1{position:absolute;width:3px;height:3px;border-radius:50%;background:var(--accent);opacity:0.3;top:-1.5px;left:12px}
        .wc-r5 .wc-moon-ring2{position:absolute;top:0;left:241.5px;width:0;height:0;animation:wc-orbit 7s linear infinite;animation-delay:-2s}
        .wc-r5 .wc-moon2{position:absolute;width:3px;height:3px;border-radius:50%;background:var(--accent);opacity:0.25;top:-1.5px;left:16px}
        .wc-r5 .wc-moon-ring3{position:absolute;top:0;left:241.5px;width:0;height:0;animation:wc-orbit 11s linear infinite;animation-delay:-5s}
        .wc-r5 .wc-moon3{position:absolute;width:4px;height:4px;border-radius:50%;background:var(--accent);opacity:0.25;top:-2px;left:21px}
        .wc-r5 .wc-moon-ring4{position:absolute;top:0;left:241.5px;width:0;height:0;animation:wc-orbit 16s linear infinite;animation-delay:-9s}
        .wc-r5 .wc-moon4{position:absolute;width:3px;height:3px;border-radius:50%;background:var(--accent);opacity:0.2;top:-1.5px;left:26px}
        .wc-r6{animation:wc-orbit 1080s linear infinite;animation-delay:-740s}
        .wc-r6 .wc-planet{width:12px;height:12px;top:-6px;left:435.5px}
        .wc-r6 .wc-planet::after{content:'Saturn'}
        .wc-r6 .wc-moon-ring{position:absolute;top:0;left:441.5px;width:0;height:0;animation:wc-orbit 8s linear infinite}
        .wc-r6 .wc-moon{position:absolute;width:4px;height:4px;border-radius:50%;background:var(--accent);opacity:0.25;top:-2px;left:14px}
        .wc-r7{animation:wc-orbit 1920s linear infinite;animation-delay:-1680s}
        .wc-r7 .wc-planet{width:9px;height:9px;top:-4.5px;left:888px}
        .wc-r7 .wc-planet::after{content:'Uranus'}
        .wc-r7 .wc-moon-ring{position:absolute;top:0;left:892.5px;width:0;height:0;animation:wc-orbit 6s linear infinite}
        .wc-r7 .wc-moon{position:absolute;width:2.5px;height:2.5px;border-radius:50%;background:var(--accent);opacity:0.25;top:-1.25px;left:10px}
        .wc-r8{animation:wc-orbit 3120s linear infinite;animation-delay:-2860s}
        .wc-r8 .wc-planet{width:9px;height:9px;top:-4.5px;left:1396px}
        .wc-r8 .wc-planet::after{content:'Neptune'}
        .wc-r8 .wc-moon-ring{position:absolute;top:0;left:1400px;width:0;height:0;animation:wc-orbit 7s linear infinite reverse}
        .wc-r8 .wc-moon{position:absolute;width:3px;height:3px;border-radius:50%;background:var(--accent);opacity:0.25;top:-1.5px;left:11px}
        .wc-planet::after{position:absolute;top:-20px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--accent);opacity:0;transition:opacity .2s;white-space:nowrap;pointer-events:none}
        .wc-planet:hover{transform:scale(1.8);transition:transform .15s}
        .wc-planet:hover::after{opacity:0.5}
        .wc-star{position:absolute;width:4px;height:4px;border-radius:50%;background:var(--text);opacity:0.12}
      `}</style>
    </div>
  )
}

/** Decorative orbiting background (8 rings; Earth/Mars/Jupiter/Saturn/Uranus/
 * Neptune each carry moon rings, Jupiter alone has 4). Purely cosmetic — no
 * interaction beyond the hover tooltip — so it's split out to keep the main
 * component's render focused on carousel state. */
function SolarSystemBackground() {
  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none z-0">
      <div className="wc-solar">
        <div className="wc-orbit-ring" /><div className="wc-orbit-ring" /><div className="wc-orbit-ring" />
        <div className="wc-orbit-ring" /><div className="wc-orbit-ring" /><div className="wc-orbit-ring" />
        <div className="wc-orbit-ring" /><div className="wc-orbit-ring" />
        <div className="wc-sun" />
        <div className="wc-orb-ring wc-r1"><div className="wc-planet" /></div>
        <div className="wc-orb-ring wc-r2"><div className="wc-planet" /></div>
        <div className="wc-orb-ring wc-r3">
          <div className="wc-planet" />
          <div className="wc-moon-ring"><div className="wc-moon" /></div>
        </div>
        <div className="wc-orb-ring wc-r4">
          <div className="wc-planet" />
          <div className="wc-moon-ring"><div className="wc-moon" /></div>
        </div>
        <div className="wc-orb-ring wc-r5">
          <div className="wc-planet" />
          <div className="wc-moon-ring"><div className="wc-moon1" /></div>
          <div className="wc-moon-ring2"><div className="wc-moon2" /></div>
          <div className="wc-moon-ring3"><div className="wc-moon3" /></div>
          <div className="wc-moon-ring4"><div className="wc-moon4" /></div>
        </div>
        <div className="wc-orb-ring wc-r6">
          <div className="wc-planet" />
          <div className="wc-moon-ring"><div className="wc-moon" /></div>
        </div>
        <div className="wc-orb-ring wc-r7">
          <div className="wc-planet" />
          <div className="wc-moon-ring"><div className="wc-moon" /></div>
        </div>
        <div className="wc-orb-ring wc-r8">
          <div className="wc-planet" />
          <div className="wc-moon-ring"><div className="wc-moon" /></div>
        </div>
      </div>
      <div className="wc-star" style={{ top: '8%', left: '50%' }} />
      <div className="wc-star" style={{ top: '15%', right: '10%' }} />
      <div className="wc-star" style={{ top: '25%', right: '22%' }} />
      <div className="wc-star" style={{ top: '4%', right: '35%' }} />
      <div className="wc-star" style={{ top: '50%', right: '5%' }} />
      <div className="wc-star" style={{ top: '72%', right: '15%' }} />
      <div className="wc-star" style={{ top: '3%', left: '65%' }} />
      <div className="wc-star" style={{ top: '38%', right: '3%' }} />
      <div className="wc-star" style={{ top: '88%', right: '30%' }} />
      <div className="wc-star" style={{ top: '60%', right: '25%' }} />
    </div>
  )
}
