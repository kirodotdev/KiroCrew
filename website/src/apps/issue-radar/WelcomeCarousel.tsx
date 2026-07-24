// Welcome carousel — a self-contained onboarding wizard: card shell, Back/Next
// nav, progress dots, per-page fade-in, floating/pulsing icon animation, and
// the FULL orbiting solar-system background decoration (8 rings, moons on
// rocky/gas giants, hover tooltips, Carl Sagan quote). Themed onto this
// project's --accent/--bg/--text tokens.
//
// The GitHub connect step is a SLIDE OF THIS CAROUSEL, not a separate
// component rendered after it — putting it outside the carousel meant Back
// stopped working once you reached it (the parent swapped WelcomeCarousel
// out entirely). Keeping it as the last slide means Back always has
// somewhere to go, all the way back to slide 0.
//
// Choosing GitHub and filling in the repo URL happen on the SAME slide (not
// a slide transition): the button morphs in place into logo-left +
// input-field-right, per product decision. That sub-state (showConnectForm)
// is lifted to THIS component rather than kept private inside the slide
// component, because the parent's Back button needs to know about it — Back
// on this slide must first collapse the form back to the button before it
// pops the page, otherwise a user who opened the form and changed their mind
// gets yanked all the way to the previous content slide instead of a single
// step back.
import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Radar, Search, GitPullRequest, BookOpen, RefreshCw, ArrowLeft, ArrowRight } from 'lucide-react'
import { issueRadarApi } from './api'
import GithubLogo from '../../components/icons/GithubLogo'

interface Slide {
  title: string
  subtitle: string
  icon: React.ReactNode
  /** CSS animation class applied to the icon wrapper — varies per slide so the
   * carousel doesn't feel like the exact same motion five times in a row. */
  animClass: string
}

const SLIDES: Slide[] = [
  {
    title: 'Welcome to Issue Radar',
    subtitle:
      'AI duplicate detection and labeling for your issues — running locally, and remembering every investigation.',
    icon: <Radar size={48} strokeWidth={1.5} />,
    animClass: 'wc-spin',
  },
  {
    title: 'No Cloud Account, No API Keys',
    subtitle:
      'No AWS account, no keys, no per-issue bill. Just your existing gh CLI, on your machine.',
    icon: <Search size={48} strokeWidth={1.5} />,
    animClass: 'wc-float',
  },
  {
    title: 'Reads What Your Bots Already Told You',
    subtitle:
      'Renovate, Dependabot, CodeRabbit — Issue Radar reads what your bots already found instead of starting over.',
    icon: <BookOpen size={48} strokeWidth={1.5} />,
    animClass: 'wc-pulse',
  },
  {
    title: 'Linked PRs, at a Glance',
    subtitle:
      "See the PR meant to fix each issue and whether it's open, draft, or merged. Code review stays with your tools.",
    icon: <GitPullRequest size={48} strokeWidth={1.5} />,
    animClass: 'wc-float',
  },
  {
    title: 'You Decide — and It Remembers',
    subtitle:
      'It suggests; you approve. Every conclusion stays local, ready when you come back.',
    icon: <Radar size={48} strokeWidth={1.5} />,
    animClass: 'wc-spin',
  },
]

// One extra "slide" beyond SLIDES: the GitHub connect slide (button ->
// expands in place into the connect form, see GithubConnectSlide below).
const CONNECT_PAGE = SLIDES.length

export default function WelcomeCarousel({ onConnected }: { onConnected: (repo: { owner: string; repo: string }) => void }) {
  const [page, setPage] = useState(0)
  // Lifted out of GithubConnectSlide (see file-header comment) so Back's
  // two-level pop (form -> button, then button -> previous slide) can see it.
  const [showConnectForm, setShowConnectForm] = useState(false)
  // Also lifted (form field + mutation, not just the open/closed flag): the
  // Connect action now renders in the NAV ROW's Next-button slot (per
  // product decision — "same position Next used to occupy"), not inside the
  // slide's own content area, so the button that triggers submission has to
  // live where the parent renders it.
  const [repoUrl, setRepoUrl] = useState('')
  const connectMutation = useMutation({
    mutationFn: (url: string) => issueRadarApi.connect(url),
    onSuccess: (data) => onConnected({ owner: data.owner, repo: data.repo }),
  })
  const submitConnect = () => {
    if (!repoUrl.trim() || connectMutation.isPending) return
    connectMutation.mutate(repoUrl.trim())
  }

  const isContentSlide = page < SLIDES.length
  const isConnectSlide = page === CONNECT_PAGE

  const handleBack = () => {
    if (isConnectSlide && showConnectForm) {
      setShowConnectForm(false)
      return
    }
    setPage((p) => p - 1)
  }

  return (
    <div className="relative flex h-full items-center justify-center bg-bg overflow-hidden">
      <SolarSystemBackground />
      <div className="relative z-10 border border-border rounded-[14px] bg-card w-[480px] min-h-[420px] flex flex-col items-center justify-between p-10 text-center">
        <div className="flex-1 flex flex-col items-center justify-center gap-3.5 w-full">
          {isContentSlide && (
            <div key={page} className="animate-[wc-fade_.2s_ease] flex flex-col items-center gap-3.5">
              <div className={`flex items-center justify-center h-20 text-accent ${SLIDES[page].animClass}`}>
                {SLIDES[page].icon}
              </div>
              <div className="text-[20px] font-bold text-text tracking-[-0.2px]">{SLIDES[page].title}</div>
              <div className="text-[13.5px] text-muted leading-[1.7] max-w-[380px]">{SLIDES[page].subtitle}</div>
            </div>
          )}
          {isConnectSlide && (
            <GithubConnectSlide
              showConnectForm={showConnectForm}
              onOpenConnectForm={() => setShowConnectForm(true)}
              url={repoUrl}
              onUrlChange={setRepoUrl}
              onSubmit={submitConnect}
              submitError={connectMutation.isError ? (connectMutation.error as Error).message : null}
            />
          )}
        </div>

        <div className="flex items-center justify-between w-full pt-3">
          <button
            onClick={handleBack}
            disabled={page === 0}
            className="min-w-[84px] px-4 py-1.5 rounded-md border border-border bg-bg text-text text-xs cursor-pointer disabled:opacity-30 disabled:cursor-default hover:bg-bg-hover"
          >
            <ArrowLeft size={12} className="lucide-inline" /> Back
          </button>
          <div className="flex gap-1">
            {Array.from({ length: CONNECT_PAGE + 1 }, (_, i) => (
              <div
                key={i}
                className={`h-1.5 rounded-full transition-all ${i === page ? 'w-3.5 bg-accent' : 'w-1.5 bg-border'}`}
              />
            ))}
          </div>
          {/* This slot is Next on content slides, Connect on the connect
           * slide once the form is open (per product decision — "same
           * position Next used to occupy"), and an empty spacer on the
           * connect slide's collapsed (button-only) state, where the
           * GitHub button itself is the only action and lives in the
           * content area instead. The shared min-width keeps Back/dots
           * aligned across all three variants. */}
          {isContentSlide && (
            <button
              onClick={() => setPage((p) => p + 1)}
              className="min-w-[84px] px-4 py-1.5 rounded-md border border-accent bg-accent text-bg text-xs font-semibold cursor-pointer hover:opacity-90"
            >
              Next <ArrowRight size={12} className="lucide-inline" />
            </button>
          )}
          {isConnectSlide && showConnectForm && (
            <button
              onClick={submitConnect}
              disabled={!repoUrl.trim() || connectMutation.isPending}
              className="min-w-[84px] inline-flex items-center justify-center gap-1 px-4 py-1.5 rounded-md border border-accent text-accent bg-transparent text-xs font-semibold cursor-pointer hover:bg-accent-subtle disabled:opacity-30"
            >
              <RefreshCw size={12} className={connectMutation.isPending ? 'animate-spin' : ''} />
              Connect
            </button>
          )}
          {isConnectSlide && !showConnectForm && <div className="min-w-[84px]" />}
        </div>
      </div>

      {/* Bottom-right quote. */}
      <div className="absolute bottom-6 right-8 z-0 whitespace-nowrap">
        <span className="text-[11px] italic text-muted opacity-50">
          "Somewhere, something incredible is waiting to be known."
        </span>
        <span className="text-[10px] text-muted opacity-35 ml-2">— Carl Sagan</span>
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

/** GitHub connect slide — a single slide with two visual states of the SAME
 * title/subtitle (their position never shifts between states — verified,
 * not assumed: they live in this component's own fixed header, above
 * whichever variant renders below):
 *
 * 1. Collapsed: a tall outlined (not filled — per product decision) button,
 *    logo above the "GitHub" label. No gh-CLI note here — that note only
 *    appears AFTER clicking GitHub (per product decision), so it reads as a
 *    confirmation of what just happened rather than a caveat shown before
 *    the user has committed to anything.
 * 2. Expanded (after clicking it): the button morphs in place into an
 *    UNBORDERED logo + input row — no wrapping box around them, per product
 *    decision. Connect itself is NOT rendered here — it moved to the parent
 *    (WelcomeCarousel)'s nav row, in the slot Next used to occupy, per
 *    product decision ("Connect goes bottom-right, where Next used to be").
 *    So this component is a pure display component now: form state (url,
 *    submit mutation) lives in the parent, which needs it to wire up that
 *    nav-row button; this component just renders the input and forwards
 *    onChange/onSubmit.
 */
function GithubConnectSlide({
  showConnectForm,
  onOpenConnectForm,
  url,
  onUrlChange,
  onSubmit,
  submitError,
}: {
  showConnectForm: boolean
  onOpenConnectForm: () => void
  url: string
  onUrlChange: (url: string) => void
  onSubmit: () => void
  submitError: string | null
}) {
  return (
    <div className="flex flex-col items-center gap-4 w-full">
      <div className="text-[20px] font-bold text-text tracking-[-0.2px]">Let's Connect a Repo</div>
      <div className="text-[13.5px] text-muted leading-[1.7] max-w-[380px]">
        Connect a repo. Nothing runs without your say.
      </div>

      {!showConnectForm && (
        <button
          onClick={onOpenConnectForm}
          className="flex flex-col items-center justify-center gap-2.5 w-[120px] h-[140px] rounded-md border border-accent text-accent bg-transparent cursor-pointer hover:bg-accent-subtle"
        >
          <GithubLogo size={40} />
          <span className="text-[13px] font-semibold">GitHub</span>
        </button>
      )}

      {showConnectForm && (
        <div className="flex items-center gap-3 w-full max-w-[380px]">
          <GithubLogo size={20} className="flex-shrink-0 text-accent" />
          <input
            value={url}
            onChange={(e) => onUrlChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') onSubmit() }}
            autoFocus
            aria-label="GitHub repository URL"
            placeholder="https://github.com/<owner>/<repo>"
            className="flex-1 box-border text-[13.5px] px-3 py-2 rounded-md bg-bg text-text border border-border font-mono"
          />
        </div>
      )}

      {submitError && <div className="text-danger text-xs">{submitError}</div>}
      {showConnectForm && (
        <p className="text-[12px] text-muted opacity-70 max-w-[380px]">
          Read access via your existing <code>gh</code> CLI session — nothing is installed or granted beyond that.
        </p>
      )}
    </div>
  )
}
