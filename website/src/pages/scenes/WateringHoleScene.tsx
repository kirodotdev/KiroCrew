/**
 * Serengeti Watering Hole scene.
 *
 * Each agent is one of three savanna animals — giraffe, warthog, or elephant —
 * chosen by a stable hash of the agent id, so the savanna always shows a mix
 * regardless of how many slots vs crons vs spawns are running:
 *   giraffe   → walks to a tree to reach up and eat leaves, or drinks
 *   warthog   → forages in dirt patches, or drinks
 *   elephant  → ambles the savanna, rests in shade, or drinks at the hole
 *
 * Idle agents stand still near their home spot. Running agents wander between
 * activities at randomized intervals.
 */
import { SCENE_SCALE } from './config'
import { useEffect, useRef } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import { sceneFont, drawLabel, sceneLineHeight, drawSpeechBubble, SPEECH_BUBBLE_MS, TEXT_CANVAS_STYLE, SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'
import { i18nT } from '../../i18n/t'

const SERENGETI_THEME: SceneTooltipTheme = { active: 'On the move', idle: 'Resting' }

/* ── Types ── */
type AnimalKind = 'giraffe' | 'warthog' | 'elephant'
type Activity =
  | 'idle'      // standing at home
  | 'tree'      // giraffe reaching up to eat leaves
  | 'water'     // drinking at the watering hole
  | 'forage'    // warthog rooting in dirt
  | 'shade'     // elephant resting under a tree
  | 'prowl'     // elephant ambling across the savanna

interface Animal {
  id: string; name: string; kind: 'slot' | 'cron' | 'spawn'
  animal: AnimalKind
  homeX: number; homeY: number
  x: number; y: number; tx: number; ty: number
  running: boolean; detail: string
  lastMessage: string; msgAt: number
  facing: 'right' | 'left'
  activity: Activity
  actTimer: number; actLimit: number
  bobPhase: number
}

/* ── Layout constants ── */
const W = 480, H = 340, S = SCENE_SCALE
const MAX_ANIMALS = 8
const HORIZON_Y = 130

/** Watering hole — oval at the front-center of the savanna. */
const HOLE = { cx: 240, cy: 240, rx: 70, ry: 26 }

/** Acacia trees (umbrella-canopy). x,y is canopy center. */
const TREES = [
  { x: 70,  y: 120, size: 1.0 },
  { x: 405, y: 130, size: 1.05 },
  { x: 235, y: 95,  size: 0.7 },  // distant background tree
]

/** Home positions where animals stand when idle, indexed by slot 0-7. */
const HOME_SPOTS = [
  { x: 90,  y: 200 },
  { x: 175, y: 200 },
  { x: 320, y: 200 },
  { x: 405, y: 210 },
  { x: 60,  y: 280 },
  { x: 145, y: 295 },
  { x: 350, y: 290 },
  { x: 430, y: 280 },
]

/** Drink stations spaced around the watering hole rim. */
const DRINK_SPOTS = [
  { x: HOLE.cx - 50, y: HOLE.cy - 8 },
  { x: HOLE.cx - 20, y: HOLE.cy - 18 },
  { x: HOLE.cx + 20, y: HOLE.cy - 18 },
  { x: HOLE.cx + 50, y: HOLE.cy - 8 },
  { x: HOLE.cx - 35, y: HOLE.cy + 12 },
  { x: HOLE.cx + 35, y: HOLE.cy + 12 },
]

/** Forage spots (small dirt patches) for warthogs. */
const FORAGE_SPOTS = [
  { x: 130, y: 270 }, { x: 200, y: 305 }, { x: 295, y: 305 }, { x: 380, y: 270 },
]

/** Stable per-agent species so the savanna shows a mix of giraffes, warthogs,
 *  and elephants regardless of how many slots/crons/spawns are running. Uses a
 *  djb2 hash of the agent id so a given agent always keeps the same species. */
const ANIMAL_KINDS: AnimalKind[] = ['giraffe', 'warthog', 'elephant']
function pickAnimal(id: string): AnimalKind {
  let h = 5381
  for (let i = 0; i < id.length; i++) h = ((h << 5) + h + id.charCodeAt(i)) >>> 0
  return ANIMAL_KINDS[h % ANIMAL_KINDS.length]
}

/* ── Color palette ── */
const COL = {
  // Sky gradient stops (manually tinted for warm savanna sunrise)
  skyTop: '#f5c879', skyMid: '#f8a464', skyLow: '#fbe09e',
  sun: '#fff1b8', sunCore: '#ffe17a',
  cloud: '#fff6dc',
  mountainFar: '#a87b5c', mountainNear: '#8a5a3d',
  grassLight: '#d7b14a', grassMid: '#b88a36', grassDark: '#94682a',
  dirt: '#7a4f28', dirtDark: '#5e3a1d',
  rock: '#8a7a66', rockShade: '#5d4f3f',
  tussock: '#7a6024', tussockTip: '#c9a64b',
  water: '#4a8ec1', waterDeep: '#2f6791', waterRim: '#6fa7d2', waterShine: '#cfe6f4',
  treeTrunk: '#6b3f24', treeBark: '#4a2a16',
  treeLeaves: '#3f6e36', treeLeavesAlt: '#2e5328', treeLeavesHighlight: '#87a55b',
  giraffeBody: '#f3d27a', giraffeSpot: '#9b6320', giraffeMane: '#5e3a1d',
  warthogBody: '#7c5a3c', warthogBack: '#5d3f25', warthogTusk: '#fff0c4',
  elephantBody: '#9aa0a6', elephantShade: '#6f757b', elephantEar: '#868c92', elephantTusk: '#fff0c4',
  shadow: 'rgba(0,0,0,0.18)',
  hoof: '#3a2415', eye: '#1a1408',
}

/* ── Component ── */
interface Props { agents: AgentSource[]; visible?: boolean }

export default function WateringHoleScene({ agents, visible = true }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const animalsRef = useRef<Animal[]>([])
  const ripplesRef = useRef<{ x: number; y: number; r: number; life: number }[]>([])
  const visibleRef = useRef(visible)
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, animalsRef, W, H, SERENGETI_THEME, 10, undefined, agents)

  /* ── Sync agents → animals ── */
  useEffect(() => {
    const existing = animalsRef.current
    const next: Animal[] = []

    // First pass: retain existing animals so their home spots are reserved in
    // `next` BEFORE any new agent looks for a free spot. Without this, a new
    // agent appearing earlier in the array could claim a spot still owned by an
    // existing agent processed later, overlapping two animals at one home.
    agents.forEach((src) => {
      const prev = existing.find(a => a.id === src.id)
      if (!prev) return
      prev.name = src.name
      prev.detail = src.detail
      if ((src.lastMessage || '') !== prev.lastMessage) { prev.lastMessage = src.lastMessage || ''; prev.msgAt = Date.now() }
      // If kind flipped (rare), just reset activity to idle so we don't get
      // an animal stuck mid-activity. Species stays fixed (hashed from id).
      if (prev.kind !== src.kind) {
        prev.kind = src.kind
        prev.activity = 'idle'
        prev.tx = prev.homeX; prev.ty = prev.homeY
      }
      // If running flipped from off → on, push the animal out of idle so it
      // starts wandering. Off → on alone won't trigger an action; that's the
      // job of the random-action picker below.
      if (!prev.running && src.running && prev.activity === 'idle') {
        prev.actTimer = prev.actLimit  // expire current dwell so picker fires
      }
      prev.running = src.running
      next.push(prev)
    })

    // Second pass: assign free home spots to genuinely new agents only, now
    // that every existing agent's spot is already reserved in `next`.
    agents.forEach((src) => {
      if (next.some(a => a.id === src.id)) return
      const idx = HOME_SPOTS.findIndex((_, hi) => !next.some(a => Math.hypot(a.homeX - HOME_SPOTS[hi].x, a.homeY - HOME_SPOTS[hi].y) < 1))
      if (idx < 0) return
      const home = HOME_SPOTS[idx]
      const known = isKnownAgent('serengeti', src.id)
      const actLimit = 90 + Math.floor(Math.random() * 90)
      next.push({
        id: src.id, name: src.name, kind: src.kind, animal: pickAnimal(src.id),
        homeX: home.x, homeY: home.y,
        // New animals fade in from the edge; known ones reappear at their home.
        x: known ? home.x : -20,
        y: known ? home.y : home.y,
        tx: home.x, ty: home.y,
        running: src.running, detail: src.detail,
          lastMessage: src.lastMessage || '', msgAt: 0,
        facing: home.x < HOLE.cx ? 'right' : 'left',
        activity: 'idle',
        // A new agent that arrives already running should start wandering on
        // the next tick rather than sitting idle for a full dwell; expire its
        // timer up front (mirrors the off->on handling for existing agents).
        actTimer: src.running ? actLimit : 0,
        actLimit,
        bobPhase: Math.random() * Math.PI * 2,
      })
      markAgentsKnown('serengeti', [src.id])
    })

    animalsRef.current = next
    // Prune the known-agent cache against ALL incoming agents, not just the
    // ones that fit into a home spot this sync. Otherwise an overflow agent
    // (more than MAX_ANIMALS) loses its "known" status and slides in from the
    // edge instead of appearing at home once a spot frees up.
    pruneAgents('serengeti', agents.map(a => a.id))
  }, [agents])

  useVisibleSync(visibleRef, visible)

  /* ── Canvas render loop ── */
  useEffect(() => {
    const C = canvasRef.current!
    const TC = textRef.current!
    const { X, T, d } = initSceneCanvases(C, TC, W, H, S)
    // Capture the (stable, never-reassigned) ripples array so the cleanup
    // clears the same instance without reading ripplesRef.current post-unmount.
    const ripples = ripplesRef.current

    /* ── Background ── */
    const drawBackground = () => {
      // Sky gradient
      const g = X.createLinearGradient(0, 0, 0, HORIZON_Y * S)
      g.addColorStop(0, COL.skyTop)
      g.addColorStop(0.55, COL.skyMid)
      g.addColorStop(1, COL.skyLow)
      X.fillStyle = g
      X.fillRect(0, 0, W * S, HORIZON_Y * S)

      // Sun (top right)
      X.fillStyle = COL.sun
      X.beginPath()
      X.arc(420 * S, 50 * S, 20 * S, 0, Math.PI * 2)
      X.fill()
      X.fillStyle = COL.sunCore
      X.beginPath()
      X.arc(420 * S, 50 * S, 12 * S, 0, Math.PI * 2)
      X.fill()

      // Soft clouds
      X.fillStyle = COL.cloud
      const cloud = (cx: number, cy: number) => {
        ;[[-10, 0], [-3, -3], [4, -2], [10, 1], [2, 2]].forEach(([dx, dy]) => {
          X.beginPath(); X.arc((cx + dx) * S, (cy + dy) * S, 5 * S, 0, Math.PI * 2); X.fill()
        })
      }
      cloud(120, 35)
      cloud(280, 60)

      // Mountains (back layer)
      X.fillStyle = COL.mountainFar
      X.beginPath()
      X.moveTo(0, HORIZON_Y * S)
      ;[[40, 105], [85, 118], [130, 100], [175, 115], [220, 95], [270, 110], [310, 100], [355, 118], [400, 105], [440, 115], [W, 110]]
        .forEach(([px, py]) => X.lineTo(px * S, py * S))
      X.lineTo(W * S, HORIZON_Y * S); X.closePath(); X.fill()

      // Mountains (near layer)
      X.fillStyle = COL.mountainNear
      X.beginPath()
      X.moveTo(0, HORIZON_Y * S)
      ;[[60, 122], [120, 116], [200, 124], [280, 118], [360, 122], [440, 117], [W, 124]]
        .forEach(([px, py]) => X.lineTo(px * S, py * S))
      X.lineTo(W * S, HORIZON_Y * S); X.closePath(); X.fill()

      // Savanna ground (banded for depth)
      d(0, HORIZON_Y, W, 30, COL.grassLight)
      d(0, HORIZON_Y + 30, W, 60, COL.grassMid)
      d(0, HORIZON_Y + 90, W, H - HORIZON_Y - 90, COL.grassDark)

      // Scattered grass tufts
      const tufts: [number, number][] = [
        [40, 175], [110, 200], [200, 185], [310, 195], [410, 180], [460, 200],
        [25, 250], [180, 270], [310, 270], [440, 260],
        [15, 320], [110, 325], [220, 320], [320, 320], [430, 320],
      ]
      tufts.forEach(([tx, ty]) => {
        d(tx, ty - 1, 1, 1, COL.tussockTip)
        d(tx - 1, ty, 1, 1, COL.tussockTip)
        d(tx + 1, ty, 1, 1, COL.tussockTip)
        d(tx, ty, 1, 1, COL.tussock)
        d(tx + 2, ty - 1, 1, 1, COL.tussockTip)
        d(tx + 2, ty, 1, 1, COL.tussock)
      })

      // A few rocks near the watering hole
      const rocks: [number, number, number][] = [[170, 230, 6], [310, 232, 5], [290, 270, 4]]
      rocks.forEach(([rx, ry, sz]) => {
        d(rx, ry, sz, sz - 1, COL.rock)
        d(rx, ry, sz, 1, COL.rockShade)
      })

      // Forage dirt patches
      FORAGE_SPOTS.forEach(({ x, y }) => {
        d(x - 7, y, 14, 5, COL.dirt)
        d(x - 7, y, 14, 1, COL.dirtDark)
      })

      // Watering hole: muddy rim then water then highlight
      const drawEllipse = (cx: number, cy: number, rx: number, ry: number, color: string) => {
        X.fillStyle = color
        X.beginPath()
        X.ellipse(cx * S, cy * S, rx * S, ry * S, 0, 0, Math.PI * 2)
        X.fill()
      }
      drawEllipse(HOLE.cx, HOLE.cy, HOLE.rx + 6, HOLE.ry + 4, COL.dirt)
      drawEllipse(HOLE.cx, HOLE.cy, HOLE.rx + 2, HOLE.ry + 1, COL.waterRim)
      drawEllipse(HOLE.cx, HOLE.cy, HOLE.rx, HOLE.ry, COL.water)
      drawEllipse(HOLE.cx, HOLE.cy + 4, HOLE.rx - 14, HOLE.ry - 6, COL.waterDeep)
      // Static highlight bands (reflection)
      X.fillStyle = COL.waterShine
      X.fillRect((HOLE.cx - 30) * S, (HOLE.cy - 4) * S, 18 * S, S)
      X.fillRect((HOLE.cx + 6) * S, (HOLE.cy - 8) * S, 12 * S, S)
    }

    /** Acacia tree (umbrella canopy + trunk). */
    const drawTree = (tx: number, ty: number, size: number) => {
      const trunkH = Math.round(70 * size)
      const trunkW = Math.max(3, Math.round(5 * size))
      const canopyW = Math.round(70 * size)
      const canopyH = Math.round(20 * size)
      const trunkX = tx - Math.floor(trunkW / 2)
      // Trunk
      d(trunkX, ty, trunkW, trunkH, COL.treeTrunk)
      d(trunkX, ty, 1, trunkH, COL.treeBark)
      // Canopy: oblong umbrella, 3-tone shading
      X.fillStyle = COL.treeLeavesAlt
      X.beginPath()
      X.ellipse(tx * S, ty * S, (canopyW / 2) * S, (canopyH / 2) * S, 0, 0, Math.PI * 2)
      X.fill()
      X.fillStyle = COL.treeLeaves
      X.beginPath()
      X.ellipse(tx * S, (ty - 2) * S, (canopyW / 2 - 3) * S, (canopyH / 2 - 2) * S, 0, 0, Math.PI * 2)
      X.fill()
      X.fillStyle = COL.treeLeavesHighlight
      X.beginPath()
      X.ellipse((tx - 8) * S, (ty - 4) * S, (canopyW / 6) * S, (canopyH / 4) * S, 0, 0, Math.PI * 2)
      X.fill()
    }

    /** Animals — pixel art primitives drawn fresh each frame. */
    const drawShadow = (cx: number, by: number, w: number) => {
      X.fillStyle = COL.shadow
      X.beginPath()
      X.ellipse(cx * S, by * S, (w / 2) * S, 2 * S, 0, 0, Math.PI * 2)
      X.fill()
    }

    /**
     * Giraffe — body 18×8 with a 14-tall neck and small head. Spotted with
     * darker patches. Posture changes by activity:
     *   tree  → neck stretched up to the canopy
     *   water → forelegs splayed, head down
     *   else  → standing
     */
    const drawGiraffe = (a: Animal, t: number) => {
      const x = a.x | 0
      const y = a.y | 0
      const flip = a.facing === 'left'
      const fx = (px: number, w: number) => flip ? x - px - w + 18 : x + px
      const bob = Math.sin((t + a.bobPhase * 30) * 0.05) * 0.5
      drawShadow(x + 9, y + 28, 22)

      const drinking = a.activity === 'water'
      const eating = a.activity === 'tree'

      // Legs
      const legY = y + 18
      const legColor = COL.giraffeBody
      // Drinking: front legs splayed wider
      if (drinking) {
        d(fx(2, 2), legY, 2, 9, legColor); d(fx(2, 2), legY + 9, 2, 1, COL.hoof)
        d(fx(15, 2), legY, 2, 9, legColor); d(fx(15, 2), legY + 9, 2, 1, COL.hoof)
        d(fx(6, 2), legY, 2, 10, legColor); d(fx(6, 2), legY + 10, 2, 1, COL.hoof)
        d(fx(11, 2), legY, 2, 10, legColor); d(fx(11, 2), legY + 10, 2, 1, COL.hoof)
      } else {
        d(fx(3, 2), legY, 2, 11, legColor); d(fx(3, 2), legY + 11, 2, 1, COL.hoof)
        d(fx(6, 2), legY, 2, 11, legColor); d(fx(6, 2), legY + 11, 2, 1, COL.hoof)
        d(fx(11, 2), legY, 2, 11, legColor); d(fx(11, 2), legY + 11, 2, 1, COL.hoof)
        d(fx(14, 2), legY, 2, 11, legColor); d(fx(14, 2), legY + 11, 2, 1, COL.hoof)
      }

      // Body (main) + tail
      d(fx(2, 14), y + 12 + bob, 14, 7, COL.giraffeBody)
      d(fx(0, 2), y + 14 + bob, 2, 5, COL.giraffeBody)
      // Spots
      d(fx(5, 2), y + 13 + bob, 2, 2, COL.giraffeSpot)
      d(fx(9, 2), y + 13 + bob, 2, 2, COL.giraffeSpot)
      d(fx(12, 2), y + 16 + bob, 2, 2, COL.giraffeSpot)
      d(fx(7, 2), y + 17 + bob, 2, 1, COL.giraffeSpot)

      // Tail tuft
      d(fx(-1, 1), y + 16 + bob, 1, 3, COL.giraffeMane)

      // Neck + head
      // Drinking: neck arcs forward and down to the water.
      // Eating: neck stretches up, head reaches into canopy.
      // Else: standing tall.
      if (drinking) {
        // Forward-curved neck
        d(fx(13, 3), y + 8 + bob, 3, 5, COL.giraffeBody)
        d(fx(15, 4), y + 7 + bob, 4, 4, COL.giraffeBody)
        d(fx(18, 5), y + 8 + bob, 4, 3, COL.giraffeBody) // head
        d(fx(20, 1), y + 9 + bob, 1, 1, COL.eye)
        // Spots on neck
        d(fx(14, 1), y + 9 + bob, 1, 2, COL.giraffeSpot)
      } else if (eating) {
        // Neck fully stretched up; head pushed up into the canopy with a couple
        // of leaf pixels at the mouth to show it actively browsing the leaves.
        d(fx(11, 3), y - 2 + bob, 3, 15, COL.giraffeBody)   // long neck up
        d(fx(11, 5), y - 8 + bob, 5, 4, COL.giraffeBody)    // head in the leaves
        d(fx(13, 1), y - 7 + bob, 1, 1, COL.eye)
        d(fx(11, 1), y - 10 + bob, 1, 2, COL.giraffeMane)   // ossicones
        d(fx(15, 1), y - 10 + bob, 1, 2, COL.giraffeMane)
        d(fx(16, 2), y - 7 + bob, 2, 1, COL.treeLeaves)     // leaf at the mouth
        d(fx(16, 1), y - 6 + bob, 1, 1, COL.treeLeavesHighlight)
        d(fx(12, 1), y + 2 + bob, 1, 8, COL.giraffeSpot)    // neck spots
      } else {
        // Standing tall: neck up
        d(fx(11, 3), y + 4 + bob, 3, 9, COL.giraffeBody)
        d(fx(11, 5), y + bob, 5, 5, COL.giraffeBody) // head
        d(fx(14, 1), y + 1 + bob, 1, 1, COL.eye)
        d(fx(11, 1), y + bob, 1, 1, COL.giraffeMane) // ossicones
        d(fx(15, 1), y + bob, 1, 1, COL.giraffeMane)
        // Mane
        d(fx(10, 1), y + 4 + bob, 1, 5, COL.giraffeMane)
        // Neck spots
        d(fx(11, 1), y + 7 + bob, 1, 2, COL.giraffeSpot)
        d(fx(13, 1), y + 10 + bob, 1, 1, COL.giraffeSpot)
      }
    }

    /**
     * Warthog — squat body 14×6, low-slung with a fan of mane along the spine
     * and small tusks. Drops nose to the dirt when foraging.
     */
    const drawWarthog = (a: Animal, t: number) => {
      const x = a.x | 0
      const y = a.y | 0
      const flip = a.facing === 'left'
      const fx = (px: number, w: number) => flip ? x - px - w + 14 : x + px
      const bob = Math.sin((t + a.bobPhase * 40) * 0.06) * 0.4
      drawShadow(x + 7, y + 13, 16)

      const foraging = a.activity === 'forage'

      // Legs
      d(fx(2, 2), y + 8, 2, 5, COL.warthogBody); d(fx(2, 2), y + 13, 2, 1, COL.hoof)
      d(fx(5, 2), y + 8, 2, 5, COL.warthogBody); d(fx(5, 2), y + 13, 2, 1, COL.hoof)
      d(fx(9, 2), y + 8, 2, 5, COL.warthogBody); d(fx(9, 2), y + 13, 2, 1, COL.hoof)
      d(fx(12, 2), y + 8, 2, 5, COL.warthogBody); d(fx(12, 2), y + 13, 2, 1, COL.hoof)

      // Body
      d(fx(2, 11), y + 4 + bob, 11, 5, COL.warthogBody)
      d(fx(2, 11), y + 4 + bob, 11, 1, COL.warthogBack) // dorsal stripe

      // Mane spikes
      d(fx(4, 1), y + 3 + bob, 1, 1, COL.warthogBack)
      d(fx(6, 1), y + 2 + bob, 1, 2, COL.warthogBack)
      d(fx(8, 1), y + 3 + bob, 1, 1, COL.warthogBack)

      // Tail (corkscrew flick)
      d(fx(0, 1), y + 5 + bob, 1, 1, COL.warthogBody)
      d(fx(-1, 1), y + 4 + bob, 1, 1, COL.warthogBody)

      if (foraging) {
        // Head dipped down, snout in dirt
        d(fx(11, 4), y + 7 + bob, 4, 3, COL.warthogBody)
        d(fx(13, 2), y + 9 + bob, 2, 1, COL.warthogBack) // snout
        d(fx(11, 1), y + 8 + bob, 1, 1, COL.warthogTusk)
        d(fx(13, 1), y + 7 + bob, 1, 1, COL.eye)
      } else {
        // Standing: head forward
        d(fx(11, 4), y + 4 + bob, 4, 4, COL.warthogBody)
        d(fx(13, 2), y + 6 + bob, 2, 2, COL.warthogBack)
        d(fx(11, 1), y + 7 + bob, 1, 1, COL.warthogTusk)
        d(fx(12, 1), y + 7 + bob, 1, 1, COL.warthogTusk)
        d(fx(13, 1), y + 5 + bob, 1, 1, COL.eye)
      }
    }

    /**
     * Elephant — bulky 18×9 grey body with thick legs, a big head + ear,
     * short ivory tusks, and a trunk that hangs and curls when standing or
     * extends straight down to the water when drinking.
     */
    const drawElephant = (a: Animal, t: number) => {
      const x = a.x | 0
      const y = a.y | 0
      const flip = a.facing === 'left'
      const fx = (px: number, w: number) => flip ? x - px - w + 18 : x + px
      const bob = Math.sin((t + a.bobPhase * 30) * 0.035) * 0.3
      const drinking = a.activity === 'water'
      drawShadow(x + 9, y + 24, 26)

      // Legs (thick)
      const legY = y + 14
      d(fx(2, 3), legY, 3, 8, COL.elephantBody); d(fx(2, 3), legY + 8, 3, 1, COL.hoof)
      d(fx(6, 3), legY, 3, 8, COL.elephantBody); d(fx(6, 3), legY + 8, 3, 1, COL.hoof)
      d(fx(10, 3), legY, 3, 8, COL.elephantBody); d(fx(10, 3), legY + 8, 3, 1, COL.hoof)
      d(fx(14, 3), legY, 3, 8, COL.elephantBody); d(fx(14, 3), legY + 8, 3, 1, COL.hoof)

      // Body (big, rounded back)
      d(fx(2, 15), y + 5 + bob, 15, 9, COL.elephantBody)
      d(fx(3, 13), y + 4 + bob, 13, 1, COL.elephantBody)   // rounded crown
      d(fx(2, 15), y + 5 + bob, 15, 1, COL.elephantShade)  // dorsal shade

      // Tail (back-left)
      d(fx(1, 1), y + 7 + bob, 1, 5, COL.elephantBody)
      d(fx(1, 1), y + 12 + bob, 1, 1, COL.elephantShade)   // tuft

      // Head + big ear (front-right)
      d(fx(12, 6), y + 4 + bob, 6, 9, COL.elephantBody)    // head
      d(fx(11, 4), y + 4 + bob, 4, 6, COL.elephantEar)     // ear
      d(fx(11, 1), y + 4 + bob, 1, 6, COL.elephantShade)   // ear edge
      d(fx(15, 1), y + 6 + bob, 1, 1, COL.eye)             // eye

      // Tusks (ivory)
      d(fx(15, 1), y + 12 + bob, 1, 2, COL.elephantTusk)
      d(fx(17, 1), y + 12 + bob, 1, 1, COL.elephantTusk)

      // Trunk
      if (drinking) {
        // Extended straight down into the water
        d(fx(17, 2), y + 10 + bob, 2, 12, COL.elephantBody)
        d(fx(17, 2), y + 22 + bob, 2, 1, COL.elephantShade) // tip in water
      } else {
        // Hangs and curls forward
        d(fx(17, 2), y + 11 + bob, 2, 5, COL.elephantBody)
        d(fx(18, 2), y + 16 + bob, 2, 3, COL.elephantBody)
        d(fx(19, 1), y + 18 + bob, 1, 1, COL.elephantShade) // curl tip
      }
    }

    /* ── Update ── */
    const pickActivityForRunning = (a: Animal) => {
      // Pick a target activity + spot for a running animal that just finished
      // its previous dwell.
      const pickDrinkSpot = () => {
        const spot = DRINK_SPOTS[Math.random() * DRINK_SPOTS.length | 0]
        return { tx: spot.x - 8, ty: spot.y, activity: 'water' as Activity }
      }
      if (a.animal === 'giraffe') {
        // 50/50: tree vs water
        if (Math.random() < 0.5) {
          // Only foreground trees: the distant background tree (y < HORIZON_Y)
          // would put the giraffe above the horizon, walking into the sky.
          const eligible = TREES.filter(tr => tr.y + 10 >= HORIZON_Y)
          const tree = eligible[Math.random() * eligible.length | 0]
          // Stand just beside the trunk; ty is high enough that the raised
          // head reaches up into the canopy leaves.
          return { tx: tree.x - 12, ty: tree.y + 10, activity: 'tree' as Activity }
        }
        return pickDrinkSpot()
      }
      if (a.animal === 'warthog') {
        // 60/40: forage vs water
        if (Math.random() < 0.6) {
          const spot = FORAGE_SPOTS[Math.random() * FORAGE_SPOTS.length | 0]
          return { tx: spot.x - 7, ty: spot.y - 4, activity: 'forage' as Activity }
        }
        return pickDrinkSpot()
      }
      // Elephant: 45% drink, 35% amble across the savanna, 20% rest in shade
      const r = Math.random()
      if (r < 0.45) return pickDrinkSpot()
      if (r < 0.8) {
        return {
          tx: 80 + Math.random() * 320,
          ty: 230 + Math.random() * 60,
          activity: 'prowl' as Activity,
        }
      }
      const tree = TREES[Math.random() * TREES.length | 0]
      return { tx: tree.x - 12, ty: tree.y + 55, activity: 'shade' as Activity }
    }

    const update = (t: number) => {
      const animals = animalsRef.current

      // Movement: simple linear interp toward target.
      animals.forEach(a => {
        const dx = a.tx - a.x
        const dy = a.ty - a.y
        const dist = Math.sqrt(dx * dx + dy * dy)
        const speed = a.animal === 'elephant' ? 0.38 : a.animal === 'warthog' ? 0.6 : 0.55
        if (dist > 1.2) {
          a.x += (dx / dist) * speed
          a.y += (dy / dist) * speed
          if (Math.abs(dx) > 0.2) a.facing = dx > 0 ? 'right' : 'left'
        } else {
          a.x = a.tx; a.y = a.ty
          a.actTimer++
          // When a non-running animal is stationary at home, hold idle.
          if (a.activity !== 'idle' && !a.running && a.actTimer > 30) {
            a.activity = 'idle'
            a.tx = a.homeX; a.ty = a.homeY
            a.actTimer = 0
            a.actLimit = 200
            return
          }
          if (a.actTimer > a.actLimit) {
            if (a.running) {
              const next = pickActivityForRunning(a)
              a.tx = next.tx; a.ty = next.ty
              a.activity = next.activity
              a.actTimer = 0
              a.actLimit = a.activity === 'water' ? 240 : a.activity === 'shade' ? 360 : 180
            } else {
              // Wander home
              a.tx = a.homeX; a.ty = a.homeY
              a.activity = 'idle'
              a.actTimer = 0
              a.actLimit = 240
            }
          }
        }
      })

      // Spawn ripples when an animal is at the watering hole drinking
      if (t % 24 === 0) {
        animals.forEach(a => {
          if (a.activity === 'water' && Math.abs(a.x - a.tx) < 2 && Math.abs(a.y - a.ty) < 2) {
            ripplesRef.current.push({ x: a.x + 12, y: a.y + 18, r: 1, life: 0 })
          }
        })
      }

      // Age ripples
      for (let i = ripplesRef.current.length - 1; i >= 0; i--) {
        const r = ripplesRef.current[i]
        r.r += 0.4
        r.life += 1
        if (r.life > 40) ripplesRef.current.splice(i, 1)
      }
    }

    /* ── Draw ── */
    const drawAnimal = (a: Animal, t: number) => {
      if (a.animal === 'giraffe') drawGiraffe(a, t)
      else if (a.animal === 'warthog') drawWarthog(a, t)
      else drawElephant(a, t)

      // Status badge (running indicator) above each animal. Giraffes are tall
      // and reach even higher when eating, so lift the badge clear of the head.
      const labelY = a.animal === 'giraffe'
        ? (a.activity === 'tree' ? a.y - 14 : a.y - 8)
        : a.y - 4
      drawLabel(T, a.running ? '●' : '○', (a.x + 9) * S, labelY * S, {
        role: 'status', color: a.running ? '#7fbb3d' : '#aaa', align: 'center', scale: S,
      })

      // Real-message speech bubble — appears when the session's latest message changes
      if (a.lastMessage && Date.now() - a.msgAt < SPEECH_BUBBLE_MS) {
        const msgAge = Date.now() - a.msgAt
        const msgAlpha = msgAge > SPEECH_BUBBLE_MS - 1000 ? (SPEECH_BUBBLE_MS - msgAge) / 1000 : 1
        drawSpeechBubble(T, a.lastMessage, (a.x + 9) * S, (labelY - 4) * S, { scale: S, alpha: msgAlpha })
      }

      // Name + detail below the animal (name wraps up to ~45 chars)
      const nameY = a.y + 32
      const nameLines = drawLabel(T, a.name, (a.x + 9) * S, nameY * S, {
        role: 'name', weight: 'bold', color: '#fff', align: 'center', scale: S, maxWidth: 64 * S,
      })
      if (a.detail) {
        drawLabel(T, a.detail, (a.x + 9) * S, (nameY + 6) * S + (nameLines - 1) * sceneLineHeight('name'), {
          role: 'detail', color: '#dcd2b8', align: 'center', scale: S,
        })
      }
    }

    const draw = (t: number) => {
      T.clearRect(0, 0, W * S, H * S)
      drawBackground()

      // Background tree (smaller) drawn before animals so they overlap it
      drawTree(TREES[2].x, TREES[2].y, TREES[2].size)

      // Foreground trees behind animals
      drawTree(TREES[0].x, TREES[0].y, TREES[0].size)
      drawTree(TREES[1].x, TREES[1].y, TREES[1].size)

      // Ripples (drawn before animals so they appear in the water)
      ripplesRef.current.forEach(r => {
        const alpha = Math.max(0, 1 - r.life / 40)
        X.strokeStyle = `rgba(207, 230, 244, ${alpha.toFixed(2)})`
        X.lineWidth = S
        X.beginPath()
        X.ellipse(r.x * S, r.y * S, r.r * S, (r.r * 0.4) * S, 0, 0, Math.PI * 2)
        X.stroke()
      })

      // Animals sorted by y for crude depth
      const sorted = [...animalsRef.current].sort((a, b) => a.y - b.y)
      sorted.forEach(a => drawAnimal(a, t))

      // Title + counter
      T.fillStyle = '#5e3a1d'
      T.font = sceneFont('title', 'bold')
      T.textAlign = 'center'
      T.fillText(i18nT('pages.scenes.wateringHoleScene.watering_hole'), (W / 2) * S, 22 * S)
      T.textAlign = 'start'
      T.fillStyle = '#5e3a1d'
      T.font = sceneFont('status')
      T.fillText(i18nT('pages.scenes.wateringHoleScene.on_the_savanna', { n: animalsRef.current.length, total: MAX_ANIMALS }), 6 * S, (H - 4) * S)
    }

    const stop = runSceneLoop(visibleRef, tickRef, update, draw)
    return () => {
      stop()
      ripples.length = 0
    }
  }, [])

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label={i18nT('pages.scenes.wateringHoleScene.watering_hole_scene')} style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }} onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      <canvas ref={textRef} aria-label={i18nT('pages.scenes.wateringHoleScene.watering_hole_scene_labels')} style={TEXT_CANVAS_STYLE} />
      {tooltipEl}
    </div>
  )
}
