/**
 * Capture entry for the top-bar's THREE-TRACK layout at a range of window widths.
 *
 * All of the responsive behaviour now lives in CSS (`.topbar`, `.tb-left`,
 * `.tb-right`, `.tb-drop-*` in index.css), so this harness renders the real class
 * names against reproduced content and lets the real stylesheet do the layout.
 * That is deliberate: booting <App/> needs a live gateway session, and the thing
 * under test is the stylesheet, not the data flow. The content mirrors the
 * shipped header (home + crew chip · search · readout capsule + feedback + bell)
 * so the container-query rungs trip at realistic group widths.
 *
 * The header must span the WINDOW, because the centre track is a vw function —
 * so drive width through the browser viewport, one screenshot per width.
 *
 * It also hosts the unread-badge overhang scene, because that defect is a
 * property of the same `.tb-right` group: the badge is offset 4px past the bell
 * button's top-right corner, and the group's clip box decides whether that
 * overhang paints. Adding it here rather than in a second entry keeps ONE
 * reproduction of the shipped header — two copies drift, and the bell markup in
 * this file had already drifted from `App.tsx` before it was made verbatim.
 *
 * ?theme=dark   ?form=mobile|desktop
 * ?count=11     the unread count to render in the badge
 * ?update=on    render the top-bar update pill (its presence is conditional in
 *               the shipped header, so the rung budget has TWO bases: with it
 *               and without it) and put `tb-has-update` on the actions group,
 *               exactly as App.tsx does
 * ?updatelabel=…  override the pill's label, to measure locale variants. The
 *               default is the zh-CN label, NOT the widest shipped form — the
 *               rung budget in index.css is derived from de downloading_percent
 *               ("Wird heruntergeladen 100 %"), so pass that to reproduce the
 *               measurement
 * ?budget=off|norungs|nonowrap  before states for the update-pill budget fix
 *               (documented at the parse site below)
 * ?fix=off      strip the gutter that admits the badge's overhang (before state)
 * ?metrics=loaded|pending  which metrics-readout state to render. `pending` is the
 *               open-but-no-frame state: the query has produced neither a frame
 *               nor an error, which is the whole of the first fetch and the retry
 *               window of a failing one
 * ?metricsfix=off  reproduce the defect for `?metrics=pending` by pushing NO
 *               segment at all, which is what the open branch did before #7967 --
 *               the readout is logically open and its toggle is off screen
 * ?pins=N       render N pinned-crew chips in the identity group's chip row
 *               (default 0 — the group renders exactly as it did without them)
 * ?unread=N     unread count carried by the LAST pinned chip, the one a cut reaches
 * ?roww=N       pin the chip row's clip width to N px, to photograph one cut
 * ?fade=on      re-inject the retired alpha mask across the row's last 18px
 *               (before state for the cut-edge cue)
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Home, Search, Bell, Lightbulb, Bug, Layers, Coins, AudioWaveform, ChevronDown, Download } from 'lucide-react'

import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const count = params.get('count') || '99+'
const pins = Number(params.get('pins') || '0')
const unread = Number(params.get('unread') || '0')
const rowW = params.get('roww')
const update = params.get('update') === 'on'
// The metrics readout's state, and the before/after switch for the state that had
// no segment at all. Separate params for the same reason as `?fix` above: the
// scene and its regression have to be selectable independently.
const metricsState = params.get('metrics') === 'pending' ? 'pending' : 'loaded'
const metricsFix = params.get('metricsfix') !== 'off'
const updateLabel = params.get('updatelabel') || '有可用更新'
// The before states for the update-pill budget fix, separable because the fix
// has two independent halves and evidence must attribute the effect to the
// right one (same convention as ?fix=off above):
//   budget=off       both halves off — the shipped-before header
//   budget=norungs   no `tb-has-update` class (base rungs only), nowrap kept
//   budget=nonowrap  class kept, segment nowrap backstop stripped
// The nowrap strip targets only the capsule's own segment buttons — the
// shipped-before UpdatePill already carried its own whitespace-nowrap.
const budget = params.get('budget')
if (budget === 'off' || budget === 'nonowrap') {
  const s = document.createElement('style')
  // The shipped-before state: the seg-derived nowrap did not exist on ANY
  // capsule segment (buttons and the mobile metrics span alike), while the
  // usage reading span and the update pill carried their own literal
  // whitespace-nowrap and keep it. `data-seg` marks exactly the elements that
  // take the shared seg class string, so the strip cannot be told apart from
  // the shipped-before build by anything inside the capsule.
  s.textContent = '[data-seg]{white-space:normal}'
  document.head.appendChild(s)
}
const hasUpdateClass = update && budget !== 'off' && budget !== 'norungs'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('zh-CN')

// The retired cue: an alpha mask over the row's last 18px. Injected verbatim so
// the before state is the shipped one rather than a paraphrase of it.
if (params.get('fade') === 'on') {
  const s = document.createElement('style')
  s.textContent =
    '.crew-chip-row{-webkit-mask-image:linear-gradient(to right,#000 calc(100% - 18px),transparent 100%);' +
    'mask-image:linear-gradient(to right,#000 calc(100% - 18px),transparent 100%)}' +
    ".crew-chip-row[data-cut='true']::after{content:none}"
  document.head.appendChild(s)
}

// The pre-fix state for the badge-overhang scene. `.tb-right` reserves the
// badge's 4px overhang with padding and puts its outer box back with an equal
// negative margin; stripping both reproduces the shipped-before computed values
// exactly, because that rule previously carried neither. Same specificity,
// injected later, so it wins.
if (params.get('fix') === 'off') {
  const s = document.createElement('style')
  s.textContent = '.tb-right{padding:0;margin:0}'
  document.head.appendChild(s)
}

const seg = 'flex items-center gap-1 -my-0.5 px-1.5 py-0.5 rounded-md text-muted whitespace-nowrap'

/** The pinned-crew chip row, verbatim from InstanceTabBar's `CrewChipRow` +
 *  `SwitcherChip` + `UnreadBadge` class strings, so the real stylesheet decides
 *  what a cut looks like.
 *
 *  `?roww` pins the CLIP width. In production that width is whatever flex-shrink
 *  leaves the row after the active chip and the trailing dropdown, i.e. a
 *  continuous value — pinning it is how one specific cut gets photographed twice
 *  under identical geometry. `data-cut` is forced on for the same reason: the
 *  shipped attribute comes from a ResizeObserver measurement this harness does
 *  not run. */
function PinnedChipRow() {
  const names = ['prod-us-east-1', 'staging-eu-west-1', 'sandbox'].slice(0, pins)
  return (
    <div
      data-testid="crew-chip-row"
      data-cut="true"
      className="crew-chip-row relative flex flex-nowrap items-center gap-1 min-w-0 overflow-hidden"
      style={rowW ? { width: Number(rowW), flex: 'none' } : undefined}
    >
      {names.map((name, i) => (
        <button
          key={name}
          type="button"
          data-chip={i === names.length - 1 ? 'last' : undefined}
          aria-label={name}
          className="flex items-center gap-1.5 h-6 px-2 rounded-md text-[12px] whitespace-nowrap transition-colors shrink-0 border focus-ring border-border text-text"
        >
          <span className="w-1.5 h-1.5 rounded-full shrink-0 bg-[var(--ok)]" aria-hidden />
          <span className="tb-drop-crew-name truncate max-w-[140px]">{name}</span>
          {i === names.length - 1 && unread > 0 ? (
            <span
              data-badge-chip
              className="ml-0.5 min-w-[16px] h-4 px-1 rounded-full text-[10px] leading-4 text-center font-bold shrink-0 bg-accent text-accent-fg"
            >
              {unread}
            </span>
          ) : null}
        </button>
      ))}
    </div>
  )
}

/** Verbatim from App.tsx's NotificationsBellButton -- including the wrapper,
 *  whose `relative` is the badge's containing block. Kept byte-faithful on
 *  purpose: this harness exists to let the REAL stylesheet lay out the REAL
 *  class strings, and a paraphrased bell silently stops measuring the shipped
 *  one (this markup replaced a drifted copy that had gone to a `<span>` with
 *  `Bell size={17}` and a badge with no `min-w`). */
function BellButton() {
  return (
    <div className="relative" data-bell-wrap>
      <button
        className="flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 relative text-muted hover:text-text"
        aria-label="Notifications"
      >
        <Bell size={15} />
        <span
          className="absolute -top-1 -right-1 min-w-[16px] h-[16px] px-1 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center shadow-[0_0_8px_var(--accent-glow)]"
          data-badge
          aria-hidden="true"
        >
          {count}
        </span>
      </button>
    </div>
  )
}

/** Verbatim from UpdatePill.tsx's button class string, so the rung budget is
 *  measured against the shipped pill rather than a paraphrase. The label comes
 *  from `?updatelabel` because it is locale- and state-dependent ("Update
 *  available", "下载中 45%…"): the budget has to clear the widest form. */
function UpdatePillLookalike() {
  return (
    <button
      type="button"
      data-update-pill
      className="flex items-center gap-1.5 h-7 px-2.5 rounded-xl shrink-0 cursor-pointer text-[12px] whitespace-nowrap border border-accent/30 bg-accent-subtle text-accent hover:opacity-90 transition-opacity"
    >
      <Download size={13} className="lucide-inline" />
      <span className="hidden sm:inline">{updateLabel}</span>
    </button>
  )
}

function TopBar() {
  return (
    <header className="topbar topbar-glass relative pl-3 pr-3" data-topbar style={{ height: 42 }}>
      <div className="tb-left relative h-full">
        <span className="flex items-center gap-1.5 text-[13px] text-muted shrink-0">
          <Home size={15} className="lucide-inline" /> 本地
        </span>
        <span className="flex items-center gap-1.5 rounded-md bg-accent-subtle px-2 py-1 text-[13px] font-medium text-accent shrink-0">
          <span className="w-1.5 h-1.5 rounded-full bg-ok" />
          <Layers size={14} className="lucide-inline" /> devdesk
          <span className="rounded bg-accent px-1.5 text-[11px] text-accent-fg">3</span>
        </span>
      </div>

      <button
        type="button"
        className="h-7 w-full px-3 rounded-md border border-border bg-card text-muted flex items-center justify-center gap-2 cursor-pointer shadow-none"
      >
        <span className="text-[13px] truncate min-w-0">⌘K — 搜索任何内容…</span>
      </button>

      <div className={`tb-right relative${hasUpdateClass ? ' tb-has-update' : ''}`}>
        <div className="tb-capsule flex items-center gap-2 h-7 px-2.5 rounded-xl bg-card">
          <span className="w-1.5 h-1.5 rounded-full bg-ok shrink-0" />
          <span className="w-px h-3.5 bg-border shrink-0" />
          {/* Verbatim from App.tsx's two open states. `pending` is dimmed and
              carries an em dash per metric rather than a spinner, which holds the
              capsule at the loaded width -- so the frame's arrival does not
              reflow the group, and the container-query rungs are calibrated
              against ONE width for both states. `metricsfix=off` pushes nothing,
              which is the defect: an open readout whose toggle is not on screen. */}
          {metricsState === 'pending' && !metricsFix ? null : (
            <button data-seg data-metrics className={`${seg} gap-2 text-[11px] font-mono${metricsState === 'pending' ? ' opacity-60' : ''}`}>
              <AudioWaveform size={12} className="tb-narrow-only text-accent" />
              <span className={`tb-drop-metrics flex items-center gap-2${metricsState === 'pending' ? ' text-muted' : ''}`}>
                {metricsState === 'pending'
                  ? <><span>CPU —</span><span>MEM —</span><span>DSK —</span></>
                  : <><span>CPU 1%</span><span>MEM 42%</span><span>DSK 20%</span></>}
              </span>
            </button>
          )}
          {metricsState === 'pending' && !metricsFix ? null : <span className="w-px h-3.5 bg-border shrink-0" />}
          <button data-seg className={seg}>
            <Coins size={12} />
            <span className="tb-drop-usage font-mono text-[11px] whitespace-nowrap tabular-nums">12.2万<span className="text-muted">/1万</span></span>
          </button>
        </div>
        {update ? <UpdatePillLookalike /> : null}
        <span className="tb-drop-feedback flex items-center">
          <span className="flex items-center gap-2 h-7 rounded-xl border border-border bg-card px-3 text-[12px] text-muted">
            <span className="flex items-center gap-1"><Lightbulb size={13} className="lucide-inline" /> 申请功能</span>
            <span className="border-l border-border pl-2 flex items-center gap-1"><Bug size={13} className="lucide-inline" /> 反馈问题</span>
          </span>
        </span>
        <BellButton />
      </div>
    </header>
  )
}

/** Mobile form: the icon-only search is its OWN grid child in the window-centred
 *  centre track, exactly as App.tsx renders it -- not a member of the actions
 *  group, which would put three action controls in one horizontal row.
 *
 *  The identity group carries the nav button AND the crew switcher, which is what
 *  its own collapse ladder acts on (`tb-drop-crew-name`, `tb-crew-active-chip` in
 *  index.css): the chip's name goes first, then the chip, so the trailing
 *  dropdown -- the only route to another crew -- never leaves the clip box. Chip
 *  and trigger classes are verbatim from InstanceTabBar's SwitcherChip and
 *  SwitcherMenu, so the rungs trip at the real content widths. */
function TopBarMobile() {
  return (
    <header className="topbar topbar-glass relative pl-3 pr-3" data-topbar style={{ height: 42 }}>
      <div className="tb-left relative h-full px-2">
        {/* The nav button, verbatim from App.tsx. `/logo.png` is a GATEWAY route, so
            the harness serves nothing for it and the shot shows an empty rounded box:
            the layout under test is the 24px box the classes fix, which `object-contain`
            cannot change, so the missing bitmap costs the rungs nothing. */}
        <button className="group p-2 rounded-md bg-transparent border-none text-muted shrink-0" aria-label="nav">
          <img src="/logo.png" alt="" aria-hidden="true" className="w-6 h-6 rounded-md shrink-0 object-contain transition-transform duration-300 group-hover:rotate-[-8deg]" />
        </button>
        <div className="instance-tab-bar-inline flex items-center h-full gap-1 min-w-0">
          <div className="flex items-center gap-1 min-w-0">
            <button
              type="button"
              aria-current="true"
              aria-label="本地"
              className="tb-crew-active-chip flex items-center gap-1.5 h-6 px-2 rounded-md text-[12px] whitespace-nowrap shrink-0 border bg-accent-subtle text-accent font-bold border-transparent"
            >
              <Home className="lucide-inline shrink-0" />
              <span className="tb-drop-crew-name truncate max-w-[140px]">本地</span>
            </button>
            {pins > 0 ? <PinnedChipRow /> : null}
            <button
              type="button"
              aria-label="切换 crew"
              className="relative flex items-center justify-center h-6 w-6 shrink-0 rounded-md border border-transparent text-muted"
            >
              <ChevronDown className="lucide-inline shrink-0" />
              {pins > 0 && unread > 0 ? (
                <span
                  data-badge-trigger
                  aria-hidden
                  className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-[3px] rounded-full bg-accent text-accent-fg text-[10px] font-semibold leading-[14px] text-center pointer-events-none"
                >
                  {unread}
                </span>
              ) : null}
            </button>
          </div>
        </div>
      </div>
      <button className="h-7 w-7 rounded-md border border-border bg-card text-muted flex items-center justify-center shrink-0">
        <Search size={14} />
      </button>
      <div className={`tb-right relative${hasUpdateClass ? ' tb-has-update' : ''}`}>
        <div className="tb-capsule flex items-center gap-2 h-7 px-2.5 rounded-xl bg-card">
          <span className="w-1.5 h-1.5 rounded-full bg-ok shrink-0" />
          <span className="w-px h-3.5 bg-border shrink-0" />
          {/* The mobile metrics readout is a passive SPAN, not a button — the
              nowrap backstop must hold on it too, so the harness renders it
              verbatim (App.tsx's isMobile && sysMetrics branch). The rarer
              resource-posture segment shares the same seg class recipe, so
              this span stands in for every span-shaped segment. */}
          <span data-seg className={`${seg} gap-2 text-[11px] font-mono tabular-nums`}>
            <span>CPU 1%</span><span>MEM 42%</span><span>DSK 20%</span>
          </span>
          <span className="w-px h-3.5 bg-border shrink-0" />
          <button data-seg className={seg}><Coins size={12} /></button>
        </div>
        {update ? <UpdatePillLookalike /> : null}
        <BellButton />
      </div>
    </header>
  )
}

/** Which form to render. The real shell branches on `useIsMobile()` (viewport
 *  < 768px); mirror that with the same query so an animated width shows the
 *  actual switch rather than a desktop DOM under a mobile grid template. An
 *  explicit ?form= override wins, for stills. */
function Harness() {
  const forced = params.get('form')
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width:767px)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(max-width:767px)')
    const on = () => setMobile(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  const isMobile = forced ? forced === 'mobile' : mobile
  return (
    <div style={{ background: 'var(--bg)', minHeight: '100vh' }}>
      {isMobile ? <TopBarMobile /> : <TopBar />}
      <div style={{ height: 30 }} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Harness />)
