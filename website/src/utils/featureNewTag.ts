/**
 * The "New" tag a feature intro hands to its rail item when the user picks
 * "Not now", plus the ghost flight that carries it there.
 *
 * The intro modal unmounts the moment it closes, so the flight cannot live in
 * React state inside it: it is plain DOM on `document.body`, driven by the Web
 * Animations API, and it outlives the modal by design. The rail reads the tag
 * through `useFeatureNewTag`, which is the only React-facing part.
 *
 * Stored per browser in localStorage. A tag clears the first time its route is
 * opened (see `NavBadge`), so a stale entry costs one pill at most.
 */
import { useSyncExternalStore } from 'react'

import { safeGetItem, safeSetItem } from './safeStorage'

const STORAGE_KEY = 'mc-feature-new-tags'
// The intro's "Try it" route per tag, so opening the page from the tag lands
// where "Try it" would have.
const ROUTE_KEY = 'mc-feature-new-tag-routes'
const CHANGE_EVENT = 'mc-feature-new-tags-change'

/** `arriving` renders the pill invisible so the flight can measure where it lands. */
export type FeatureNewTagState = 'arriving' | 'shown'

function readTags(): Record<string, FeatureNewTagState> {
  try {
    const raw = JSON.parse(safeGetItem(STORAGE_KEY) ?? '{}') as unknown
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, FeatureNewTagState> = {}
    for (const [id, v] of Object.entries(raw as Record<string, unknown>)) {
      // A reload in the middle of a flight leaves `arriving` behind; the tag was
      // still given, so it reads as shown.
      if (v === 'arriving' || v === 'shown') out[id] = 'shown'
    }
    return out
  } catch {
    return {}
  }
}

let cache: Record<string, FeatureNewTagState> | null = null

function writeTag(navId: string, state: FeatureNewTagState | null) {
  const next = { ...(cache ?? readTags()) }
  if (state) next[navId] = state
  else delete next[navId]
  cache = next
  safeSetItem(STORAGE_KEY, JSON.stringify(next))
  window.dispatchEvent(new Event(CHANGE_EVENT))
}

function subscribe(onChange: () => void) {
  const onStorage = (e: StorageEvent) => {
    if (e.key !== STORAGE_KEY) return
    cache = null
    onChange()
  }
  window.addEventListener(CHANGE_EVENT, onChange)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange)
    window.removeEventListener('storage', onStorage)
  }
}

export function useFeatureNewTag(navId: string): FeatureNewTagState | null {
  return useSyncExternalStore(subscribe, () => {
    cache ??= readTags()
    return cache[navId] ?? null
  })
}

function readRoutes(): Record<string, string> {
  try {
    const raw = JSON.parse(safeGetItem(ROUTE_KEY) ?? '{}') as unknown
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, string> : {}
  } catch {
    return {}
  }
}

/** The route a tag's page should open on, when the intro named one. */
export function featureNewTagRoute(navId: string): string | null {
  const route = readRoutes()[navId]
  return typeof route === 'string' && route.startsWith('/') ? route : null
}

export function clearFeatureNewTag(navId: string) {
  const routes = readRoutes()
  if (navId in routes) {
    delete routes[navId]
    safeSetItem(ROUTE_KEY, JSON.stringify(routes))
  }
  if ((cache ?? readTags())[navId]) writeTag(navId, null)
}

/** Where the ghost starts: the horizontal centre and top of its figure on screen. */
export interface GhostOrigin {
  cx: number
  top: number
  height: number
  /** The figure is not on screen yet (the clip is before its arrival), so it pops in first. */
  popIn: boolean
}

const GHOST_BODY = 'M398.554 818.914C316.315 1001.03 491.477 1046.74 620.672 940.156C658.687 1059.66 801.052 970.473 852.234 877.795C964.787 673.567 919.318 465.357 907.64 422.374C827.637 129.443 427.623 128.946 358.8 423.865C342.651 475.544 342.402 534.18 333.458 595.051C328.986 625.86 325.507 645.488 313.83 677.785C306.873 696.424 297.68 712.819 282.773 740.645C259.915 783.881 269.604 867.113 387.87 823.883L399.051 818.914H398.554Z'
const GHOST_EYES = 'M636.123 549.353C603.328 549.353 598.359 510.097 598.359 486.742C598.359 465.623 602.086 448.977 609.293 438.293C615.504 428.852 624.697 424.131 636.123 424.131C647.555 424.131 657.492 428.852 664.447 438.541C672.398 449.474 676.623 466.12 676.623 486.742C676.623 525.998 661.471 549.353 636.375 549.353H636.123ZM771.24 549.353C738.445 549.353 733.477 510.097 733.477 486.742C733.477 465.623 737.203 448.977 744.41 438.293C750.621 428.852 759.814 424.131 771.24 424.131C782.672 424.131 792.609 428.852 799.564 438.541C807.516 449.474 811.74 466.12 811.74 486.742C811.74 525.998 796.588 549.353 771.492 549.353H771.24Z'

/** Ghost size once it has shrunk to the rail, in CSS px. */
const GHOST_W = 28
const SVG_NS = 'http://www.w3.org/2000/svg'

/** The standing ghost as DOM nodes: body plus an eyes group the flights move. */
function standingGhost(style: string): { svg: SVGSVGElement; eyes: SVGGElement } {
  const svg = document.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('viewBox', '255 110 745 980')
  svg.setAttribute('style', style)
  const body = document.createElementNS(SVG_NS, 'path')
  body.setAttribute('fill', '#fff')
  body.setAttribute('d', GHOST_BODY)
  const eyes = document.createElementNS(SVG_NS, 'g')
  const pupils = document.createElementNS(SVG_NS, 'path')
  pupils.setAttribute('fill', '#000')
  pupils.setAttribute('d', GHOST_EYES)
  eyes.appendChild(pupils)
  svg.append(body, eyes)
  return { svg, eyes }
}
const GHOST_H = 36
const FLIGHT_MS = 1100
const EXIT_MS = 400

/**
 * Give `navId` its "New" tag, carried there by the ghost from `from`.
 *
 * Falls back to showing the tag with no flight when motion is reduced, or when
 * the rail row is not on screen (a closed mobile drawer, a hidden preview
 * surface): there is nowhere visible to fly to.
 */
export function deliverFeatureNewTag(navId: string, label: string, from: GhostOrigin | null, route?: string) {
  if (route) safeSetItem(ROUTE_KEY, JSON.stringify({ ...readRoutes(), [navId]: route }))
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const row = document.querySelector<HTMLElement>(`[data-onboarding-nav="${CSS.escape(navId)}"]`)
  if (reduce || !from || !row || row.getClientRects().length === 0) {
    writeTag(navId, 'shown')
    return
  }
  writeTag(navId, 'arriving')
  // The pill renders on the next frame; measure it there, invisible, so the
  // flight lands exactly where the real pill sits in either rail width.
  requestAnimationFrame(() => {
    const pill = document.querySelector<HTMLElement>(`[data-feature-new-tag="${CSS.escape(navId)}"]`)
    if (!pill) {
      // The route may have opened (and cleared the tag) since the flight began.
      if (readTags()[navId]) writeTag(navId, 'shown')
      return
    }
    const b = pill.getBoundingClientRect()
    const cw = Math.max(GHOST_W, b.width)
    const courier = document.createElement('div')
    courier.setAttribute('aria-hidden', 'true')
    Object.assign(courier.style, {
      // Placed by transform at measured viewport coordinates, never pinned to
      // an edge, so the safe-area insets do not apply.
      position: 'fixed', inset: '0px auto auto 0px', width: `${cw}px`, height: `${GHOST_H + b.height - 6}px`,
      zIndex: '60', pointerEvents: 'none', transformOrigin: '50% 0',
    })
    const { svg, eyes } = standingGhost(
      `position:absolute;top:0;left:${(cw - GHOST_W) / 2}px;width:${GHOST_W}px;height:${GHOST_H}px;overflow:visible;transform-origin:50% 60%;filter:drop-shadow(0 1px 2px rgba(0,0,0,.25))`)
    const tag = document.createElement('span')
    tag.style.cssText = `position:absolute;bottom:0;left:${(cw - b.width) / 2}px;width:${b.width}px;height:${b.height}px;box-sizing:border-box;display:flex;align-items:center;justify-content:center;border-radius:999px;background:var(--accent);color:var(--accent-fg);font:700 ${getComputedStyle(pill).fontSize}/1 system-ui,sans-serif;opacity:0`
    tag.textContent = label
    courier.append(svg, tag)
    document.body.appendChild(courier)

    const k0 = from.height / GHOST_H
    const S = { x: from.cx, y: from.top }
    const E = { x: b.left + b.width / 2, y: b.top - (GHOST_H - 6) }
    const S2 = { x: S.x, y: S.y - 10 * k0 }
    const C = { x: E.x + (S.x - E.x) * 0.45, y: Math.max(-20, Math.min(S.y, E.y) - 60) }
    const lean = E.x < S.x ? -1 : 1
    const at = (x: number, y: number, k: number, sx = 1, sy = 1, opacity = 1): Keyframe => ({
      transform: `translate(${x - cw / 2}px, ${y}px) scale(${k * sx}, ${k * sy})`, opacity,
    })
    const ease = (u: number) => (u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2)
    const frames: Keyframe[] = from.popIn
      ? [{ offset: 0, ...at(S.x, S.y, k0 * 0.4, 1, 1, 0) }, { offset: 0.08, ...at(S.x, S.y, k0, 1.2, 0.8) }, { offset: 0.12, ...at(S.x, S.y, k0) }]
      : [{ offset: 0, ...at(S.x, S.y, k0) }, { offset: 0.12, ...at(S.x, S.y, k0) }]
    frames.push({ offset: 0.19, ...at(S.x, S.y, k0, 1.15, 0.85) }, { offset: 0.27, ...at(S2.x, S2.y, k0, 0.94, 1.08) })
    for (let i = 1; i <= 20; i++) {
      const u = i / 20
      const t = ease(u)
      const x = (1 - t) * (1 - t) * S2.x + 2 * (1 - t) * t * C.x + t * t * E.x
      const y = (1 - t) * (1 - t) * S2.y + 2 * (1 - t) * t * C.y + t * t * E.y
      frames.push({ offset: 0.27 + 0.63 * u, ...at(x, y - 3 * Math.sin(u * Math.PI * 2), 1 + (k0 - 1) * Math.pow(1 - u, 3)) })
    }
    frames.push({ offset: 0.95, ...at(E.x, E.y + 2, 1, 1.07, 0.93) }, { offset: 1, ...at(E.x, E.y, 1) })

    // Eyes glance at the rail before take-off, then look down at the tag on landing.
    eyes.animate([
      { transform: 'none' }, { transform: `translate(${40 * lean}px,-6px)`, offset: 0.1 },
      { transform: `translate(${40 * lean}px,-6px)`, offset: 0.8 }, { transform: 'translate(0,16px)', offset: 0.95 },
      { transform: 'translate(0,16px)' },
    ], { duration: FLIGHT_MS, fill: 'forwards', easing: 'ease-out' })
    tag.animate([
      // Picked up mid-flight, once the ghost has shrunk most of the way, so the
      // tag never reads as a giant pill over the page.
      { opacity: 0, transform: 'scale(.4)' }, { opacity: 0, transform: 'scale(.4)', offset: 0.5 },
      { opacity: 1, transform: 'scale(1.15)', offset: 0.58 }, { opacity: 1, transform: 'none', offset: 0.64 },
      { opacity: 1, transform: 'none' },
    ], { duration: FLIGHT_MS, fill: 'forwards' })
    svg.animate([
      { transform: 'none' }, { transform: 'none', offset: 0.27 }, { transform: `rotate(${10 * lean}deg)`, offset: 0.42 },
      { transform: `rotate(${6 * lean}deg)`, offset: 0.75 }, { transform: 'none', offset: 0.9 }, { transform: 'none' },
    ], { duration: FLIGHT_MS, fill: 'forwards' })
    courier.animate(frames, { duration: FLIGHT_MS, fill: 'forwards' }).onfinish = () => {
      tag.style.visibility = 'hidden'
      if (readTags()[navId]) writeTag(navId, 'shown')
      requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(`[data-feature-new-tag="${CSS.escape(navId)}"]`)?.animate(
          [{ transform: 'translateY(-3px)' }, { transform: 'scale(1.15,.82)', offset: 0.4 }, { transform: 'none' }],
          { duration: 400, easing: 'cubic-bezier(.34,1.4,.64,1)' },
        )
        row.animate(
          [{ backgroundColor: 'color-mix(in srgb, var(--accent) 22%, transparent)' }, { backgroundColor: 'transparent' }],
          { duration: 800, easing: 'ease-out' },
        )
      })
      svg.animate([
        { transform: 'none', opacity: 1 }, { transform: `rotate(${-6 * lean}deg) translateY(1px)`, opacity: 1, offset: 0.2 },
        { transform: 'none', opacity: 1, offset: 0.4 }, { transform: 'translateY(-16px)', opacity: 0 },
      ], { duration: EXIT_MS, easing: 'ease-in', fill: 'forwards' }).onfinish = () => courier.remove()
    }
  })
}

/** Fired on `window` once the ghost has landed, so a page's own walkthrough can start. */
export const FEATURE_LANDED_EVENT = 'mc-feature-landed'
const LANDING_WAIT_MS = 2500
const SQUASH_MS = 120
const POP_IN_MS = 260
const DIVE_MS = 550
/** The flying pose's own box, in CSS px at scale 1. */
const FLY_W = 84
const FLY_H = 64
// Traced from the designer's KIRO_Ghost_Onboarding_01_Fly-Across-Smear Lottie (viewBox 0 26 86 66); it faces left.
const FLY_GHOST = `<g><g transform="matrix(0.137,0,0,0.137,-11.491,20.130)"><g transform="matrix(-1.497,0,0,1.497,401.872,277.026)"><g><g><g transform="matrix(-0.999,-0.039,-0.039,0.999,0.001,0)"><path fill="#fff" d="M-156.639,84.834 C-97.625,143.302 -9.781,163.899 41.889,116.207 C59.848,99.630 45.328,72.510 62.228,39.008 C114.245,-64.108 -52.054,-188.091 -139.121,-127.866 C-226.188,-67.642 -205.139,36.783 -156.639,84.834z"></path></g></g><g transform="matrix(0.996,-0.079,0.079,0.996,99.202,-33.458)"><path fill="#fff" d="M-15.972,-0.001 C-15.972,9.569 -13.965,25.624 -0.532,25.624 C-0.532,25.624 -0.529,25.624 -0.529,25.624 C9.803,25.624 15.972,16.045 15.972,-0.001 C15.972,-8.493 14.262,-15.324 11.027,-19.759 C8.187,-23.650 4.160,-25.624 -0.532,-25.624 C-5.224,-25.624 -8.914,-23.681 -11.494,-19.854 C-14.425,-15.503 -15.972,-8.638 -15.972,-0.001z"></path></g><g transform="matrix(0.996,-0.079,0.079,0.996,154.268,-37.875)"><path fill="#fff" d="M-15.972,-0.001 C-15.972,9.569 -13.965,25.624 -0.532,25.624 C-0.532,25.624 -0.529,25.624 -0.529,25.624 C9.803,25.624 15.972,16.045 15.972,-0.001 C15.972,-8.493 14.262,-15.324 11.027,-19.759 C8.187,-23.650 4.160,-25.624 -0.532,-25.624 C-5.224,-25.624 -8.914,-23.681 -11.494,-19.854 C-14.425,-15.503 -15.972,-8.638 -15.972,-0.001z"></path></g></g></g><g transform="matrix(-1.497,0,0,1.497,401.872,277.026)"><g><g><g transform="matrix(-0.999,-0.039,-0.039,0.999,0.001,0)"><path fill="#fff" d="M-156.639,84.834 C-77.114,163.621 61.653,172.707 165.401,73.196 C194.139,45.774 144.354,43.527 132.757,44.121 C60.518,53.438 -205.139,36.783 -156.639,84.834z"></path></g></g></g></g><g transform="matrix(-1.497,0,0,1.497,401.872,277.026)"><g><g><g transform="matrix(-0.999,-0.039,-0.039,0.999,0.001,0)"><path fill="#fff" d="M58.077,66.573 C129.320,70.203 129.460,37.649 163.748,10.826 C184.782,-5.196 204.740,-20.644 195.357,-39.117 C187.922,-53.713 132.157,-34.643 79.441,-56.047 C-89.262,-95.940 -111.749,87.344 58.077,66.573z"></path></g></g></g></g><g transform="matrix(-1.497,0,0,1.497,401.872,277.026)"><g><g><g transform="matrix(-0.999,-0.039,-0.039,0.999,0.001,0)"><path fill="#fff" d="M114.863,-47.666 C154.887,-77.688 115.371,-85.613 99.567,-84.284 C5.339,-75.942 -29.256,-181.021 -116.323,-120.797 C-203.390,-60.573 44.725,4.585 114.863,-47.666z"></path></g></g></g></g><g transform="matrix(-1.497,0,0,1.497,401.872,270.718)"><g><g transform="matrix(0.996,-0.079,0.079,0.996,154.268,-37.875)"><path fill="#000" d="M-15.972,-0.001 C-15.972,9.569 -13.965,25.624 -0.532,25.624 C-0.532,25.624 -0.529,25.624 -0.529,25.624 C9.803,25.624 15.972,16.045 15.972,-0.001 C15.972,-8.493 14.262,-15.324 11.027,-19.759 C8.187,-23.650 4.160,-25.624 -0.532,-25.624 C-5.224,-25.624 -8.914,-23.681 -11.494,-19.854 C-14.425,-15.503 -15.972,-8.638 -15.972,-0.001z"></path></g></g></g><g transform="matrix(-1.497,0,0,1.497,401.872,270.719)"><g><g transform="matrix(0.996,-0.079,0.079,0.996,99.202,-33.458)"><path fill="#000" d="M-15.972,-0.001 C-15.972,9.569 -13.965,25.624 -0.532,25.624 C-0.532,25.624 -0.529,25.624 -0.529,25.624 C9.803,25.624 15.972,16.045 15.972,-0.001 C15.972,-8.493 14.262,-15.324 11.027,-19.759 C8.187,-23.650 4.160,-25.624 -0.532,-25.624 C-5.224,-25.624 -8.914,-23.681 -11.494,-19.854 C-14.425,-15.503 -15.972,-8.638 -15.972,-0.001z"></path></g></g></g></g></g>`

/**
 * Carry the ghost from `from` into the page's `[data-feature-landing="<navId>"]`
 * anchor, then fire FEATURE_LANDED_EVENT. The ghost squashes, then smear-dives
 * nose first into the anchor and brakes hard onto it, like the designer's
 * Fly-Across-Smear animation. A pointer press skips to the landing. The route
 * is lazy and the roster loads after it, so the anchor is polled for a short
 * while; a page with no anchor gets the event and no flight, and so does
 * reduced motion.
 */
export function landFeatureGhost(navId: string, from: GhostOrigin | null) {
  const landed = () => window.dispatchEvent(new CustomEvent(FEATURE_LANDED_EVENT, { detail: { navId } }))
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const started = performance.now()
  const find = () => {
    // A page can render the anchor twice (a narrow-screen copy hidden on wide
    // screens), so take the first one that is actually laid out.
    const anchor = [...document.querySelectorAll<HTMLElement>(`[data-feature-landing="${CSS.escape(navId)}"]`)]
      .find(el => el.getClientRects().length > 0)
    if (!anchor) {
      if (performance.now() - started < LANDING_WAIT_MS) requestAnimationFrame(find)
      else landed()
      return
    }
    if (reduce || !from) {
      landed()
      return
    }
    const a = anchor.getBoundingClientRect()
    const layer = (w: number, h: number, child?: Node) => {
      const el = document.createElement('div')
      el.setAttribute('aria-hidden', 'true')
      Object.assign(el.style, {
        // Same as the courier: positioned by transform, not edge-pinned.
        position: 'fixed', inset: '0px auto auto 0px', width: `${w}px`, height: `${h}px`, zIndex: '60', pointerEvents: 'none',
      })
      if (child) el.appendChild(child)
      document.body.appendChild(el)
      return el
    }
    const shadow = 'filter:drop-shadow(0 2px 1.5px rgba(0,0,0,.18))'
    const stand = layer(GHOST_W, GHOST_H, standingGhost(`width:100%;height:100%;display:block;overflow:visible;${shadow}`).svg)

    // The avatar's own ghost waits for the flying one: an empty tile until impact.
    const face = anchor.querySelector<HTMLElement>('img, svg') ?? anchor
    const f = face.getBoundingClientRect()
    const tile = anchor.dataset.featureLandingTile
    const cover = tile ? layer(f.width, f.height) : null
    if (cover) {
      Object.assign(cover.style, {
        left: `${f.left}px`, top: `${f.top}px`, background: tile, borderRadius: getComputedStyle(face).borderRadius, zIndex: '59',
      })
    } else {
      anchor.style.visibility = 'hidden'
    }

    const k0 = from.height / GHOST_H
    const S = { x: from.cx, y: from.top + from.height / 2 }
    const E = { x: a.left + a.width / 2, y: a.top + a.height / 2 }
    const ux = E.x - S.x
    const uy = E.y - S.y
    const dist = Math.hypot(ux, uy) || 1
    const dirx = ux < 0 ? -1 : 1
    // Point the nose along the dive; a rightward dive mirrors the left-facing pose.
    let ang = (Math.atan2(uy, ux) * 180) / Math.PI
    if (dirx < 0) ang = ang - 180 < -180 ? ang + 180 : ang - 180
    ang = Math.max(-75, Math.min(75, ang))
    const lead = from.popIn ? POP_IN_MS : SQUASH_MS
    const anims: Animation[] = []

    const at = (k: number, sx = 1, sy = 1, opacity = 1): Keyframe => ({
      transform: `translate(${S.x - GHOST_W / 2}px, ${S.y - GHOST_H / 2}px) scale(${k * sx}, ${k * sy})`, opacity,
    })
    anims.push(stand.animate(from.popIn
      ? [{ offset: 0, ...at(k0 * 0.4, 1, 1, 0) }, { offset: 0.4, ...at(k0, 1.2, 0.8) }, { offset: 0.6, ...at(k0) }, { offset: 0.85, ...at(k0, 1.2, 0.8) }, { offset: 1, ...at(k0, 0.7, 1.3, 0) }]
      : [{ offset: 0, ...at(k0) }, { offset: 0.9, ...at(k0, 1.2, 0.8) }, { offset: 1, ...at(k0, 0.7, 1.3, 0) }],
    { duration: lead, fill: 'forwards' }))

    // Thickness from the approved mock: a heavy smear off the clip's big ghost, a thin one off the pill.
    const sh = from.height > 40 ? 22 : 9
    const smear = layer(dist, sh)
    Object.assign(smear.style, {
      borderRadius: '999px', background: 'var(--accent)', opacity: '0.5', transformOrigin: `0 ${sh / 2}px`,
      transform: `translate(${S.x}px, ${S.y - sh / 2}px) rotate(${Math.atan2(uy, ux)}rad)`,
    })
    anims.push(smear.animate(
      [{ clipPath: 'inset(0 100% 0 0 round 999px)' }, { clipPath: 'inset(0 20% 0 0 round 999px)', offset: 0.45 }, { clipPath: 'inset(0 0 0 100% round 999px)' }],
      { delay: lead, duration: 300, fill: 'both' }))

    // Created after the smear so the ghost flies over its own streak.
    // The flying pose is static vector art, parsed as SVG rather than assigned as HTML.
    const flySvg = new DOMParser().parseFromString(
      `<svg xmlns="${SVG_NS}" viewBox="0 26 86 66">${FLY_GHOST}</svg>`, 'image/svg+xml').documentElement
    flySvg.setAttribute('style', `width:100%;height:100%;display:block;overflow:visible;${shadow}`)
    const fly = layer(FLY_W, FLY_H, document.importNode(flySvg, true))
    const f0 = (from.height * (22 / 29) * 1.3) / FLY_W
    const f1 = (a.width * (20 / 26)) / FLY_W
    const flyAt = (t: number, opacity = 1): Keyframe => {
      // Quartic ease-out: fast launch, then a hard brake onto the avatar, as in the Lottie.
      const e = 1 - Math.pow(1 - t, 4)
      const sc = f0 + (f1 - f0) * e + 0.3 * Math.sin(e * Math.PI) * (1 - e * 0.5)
      const rot = ang * (1 - Math.max(0, Math.min(1, (e - 0.7) / 0.3)))
      return {
        offset: t, opacity,
        transform: `translate(${S.x + ux * e - FLY_W / 2}px, ${S.y + uy * e - FLY_H / 2}px) rotate(${rot}deg) scale(${-dirx * sc * (1 + 0.35 * (1 - e))}, ${sc * (1 - 0.12 * (1 - e))})`,
      }
    }
    const flyFrames: Keyframe[] = [flyAt(0, 0)]
    for (let i = 1; i <= 44; i++) flyFrames.push(flyAt(i / 44))
    const dive = fly.animate(flyFrames, { delay: lead, duration: DIVE_MS, fill: 'both' })
    anims.push(dive)

    const skip = () => anims.forEach((x) => x.finish())
    window.addEventListener('pointerdown', skip, { capture: true, once: true })
    dive.onfinish = () => {
      window.removeEventListener('pointerdown', skip, { capture: true })
      stand.remove()
      smear.remove()
      fly.remove()
      cover?.remove()
      anchor.style.visibility = ''
      anchor.animate(
        [{ transform: 'none' }, { transform: 'translateY(4px) scale(1.38,.66)', offset: 0.22 }, { transform: 'translateY(-3px) scale(.86,1.18)', offset: 0.48 }, { transform: 'scale(1.07,.95)', offset: 0.72 }, { transform: 'none' }],
        { duration: 650, easing: 'ease-out' },
      )
      anchor.animate(
        [{ boxShadow: '0 0 0 0 color-mix(in srgb, var(--accent) 75%, transparent)' }, { boxShadow: '0 0 0 22px transparent' }],
        { duration: 700, easing: 'ease-out' },
      )
      for (let i = 0; i < 8; i++) {
        const b = (i / 8) * 2 * Math.PI
        const puff = layer(6, 6)
        Object.assign(puff.style, { borderRadius: '50%', background: 'var(--accent)', left: `${E.x - 3}px`, top: `${E.y + 3}px` })
        puff.animate(
          [{ transform: 'none', opacity: 1 }, { transform: `translate(${24 * Math.cos(b)}px,${20 * Math.sin(b)}px) scale(.3)`, opacity: 0 }],
          { duration: 450, easing: 'ease-out' },
        ).onfinish = () => puff.remove()
      }
      landed()
    }
  }
  requestAnimationFrame(find)
}
