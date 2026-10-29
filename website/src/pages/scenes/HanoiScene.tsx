import { SCENE_SCALE } from './config'
import { useEffect, useRef } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import {
  sceneFont,
  drawLabel,
  sceneLineHeight,
  drawSpeechBubble,
  SPEECH_BUBBLE_MS,
  TEXT_CANVAS_STYLE,
  SCENE_CONTAINER_STYLE,
  PIXEL_CANVAS_STYLE,
} from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'
import { i18nT } from '../../i18n/t'

type Hat = 'conical' | 'helmet' | 'hair'
type Garment = 'aodai' | 'tee'
type Paint = (x: number, y: number, w: number, h: number, c: string) => void

interface HanoiAgent {
  id: string
  name: string
  label: string
  kind: 'slot' | 'cron' | 'spawn'
  x: number
  y: number
  tx: number
  ty: number
  running: boolean
  detail: string
  color: string
  skin: string
  hat: Hat
  helmet: string
  garment: Garment
  phase: number
  blink: number
  sip: number
  enterProgress: number
  facing: 1 | -1
  lastMessage: string
  msgAt: number
}

interface Leaf {
  x: number
  y: number
  vx: number
  vy: number
  spin: number
  color: string
}

const W = 440
const H = 300
const S = SCENE_SCALE

const WATER_TOP = 100
const WATER_BOTTOM = 192
const ISLET_WATERLINE = 141
const ISLAND_WATERLINE = 165
const BRIDGE_FROM = 318
const BRIDGE_TO = 378
const BANK_X = 12
const SHORE_LIGHTS = Array.from({ length: 28 }, (_, i) => 6 + i * 16)
const FEET_OFFSET = 9
const LABEL_WIDTH = 54
const LAMPS = [102, 222, 342]
const LAMP_HEAD_Y = 172
const VENDOR_FEET = 262

const SPOTS = [
  { x: 72, y: 213 },
  { x: 132, y: 213 },
  { x: 192, y: 213 },
  { x: 252, y: 213 },
  { x: 312, y: 213 },
  { x: 140, y: 271 },
  { x: 220, y: 271 },
  { x: 300, y: 271 },
]

const OUTFITS = ['#c0392b', '#d4a017', '#1f618d', '#e8e2d0', '#7d3c98', '#ca6f1e', '#117864', '#d35d8a']
const SKINS = ['#f0c49a', '#e0ac7e', '#c98b5a', '#a86f45']
const HATS: Hat[] = ['conical', 'helmet', 'hair', 'conical', 'hair', 'helmet', 'conical', 'hair']
const GARMENTS: Garment[] = ['aodai', 'tee', 'tee', 'aodai', 'tee', 'tee', 'aodai', 'tee']
const HELMETS = ['#2e7d5b', '#c0392b', '#f1c40f', '#2471a3']
const STOOLS = ['#c0392b', '#2471a3', '#c0392b', '#1e8449', '#2471a3', '#c0392b', '#2471a3', '#d35400']

const LANTERN_TOP = 243

const LANTERNS = [
  { x: 7, color: '#e74c3c', inner: '#f5b7a8' },
  { x: 45, color: '#f1c40f', inner: '#fbe7a1' },
  { x: 355, color: '#e74c3c', inner: '#f5b7a8' },
  { x: 393, color: '#f1c40f', inner: '#fbe7a1' },
]

const WIRES = [
  { x1: -2, y1: 178, x2: LAMPS[0], y2: 176, sag: 5, bulbs: false },
  { x1: -2, y1: 181, x2: LAMPS[0], y2: 177, sag: 8, bulbs: false },
  { x1: -2, y1: 184, x2: LAMPS[0], y2: 178, sag: 4, bulbs: true },
  { x1: LAMPS[0], y1: 180, x2: LAMPS[1], y2: 180, sag: 7, bulbs: true },
  { x1: LAMPS[1], y1: 180, x2: LAMPS[2], y2: 180, sag: 7, bulbs: true },
  { x1: LAMPS[2], y1: 180, x2: W + 2, y2: 184, sag: 6, bulbs: true },
]

const BULB_COLORS = ['#ffd27a', '#ff9f68', '#ffe9b0']

const RIDERS = [
  { feet: 243, speed: 0.8, dir: 1 as const, offset: 0, bike: '#c0392b', helmet: '#f1c40f', shirt: '#1f618d' },
  { feet: 248, speed: 0.6, dir: -1 as const, offset: 260, bike: '#2471a3', helmet: '#ecf0f1', shirt: '#7d3c98' },
]

const CHAT_LINE_KEYS = [
  ['pages.scenes.hanoiScene.chat_deploy_ask', 'pages.scenes.hanoiScene.chat_deploy_reply'],
  ['pages.scenes.hanoiScene.chat_coffee_ask', 'pages.scenes.hanoiScene.chat_coffee_reply'],
  ['pages.scenes.hanoiScene.chat_review_ask', 'pages.scenes.hanoiScene.chat_review_reply'],
  ['pages.scenes.hanoiScene.chat_bug_ask', 'pages.scenes.hanoiScene.chat_bug_reply'],
  ['pages.scenes.hanoiScene.chat_tea_ask', 'pages.scenes.hanoiScene.chat_tea_reply'],
  ['pages.scenes.hanoiScene.chat_latency_ask', 'pages.scenes.hanoiScene.chat_latency_reply'],
] as const

function onStage(p: { x: number }) {
  return p.x > 6 && p.x < W - 6
}

function seeded(seed: number) {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6d2b79f5) >>> 0
    let t = s
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const STARS = (() => {
  const rand = seeded(3)
  return Array.from({ length: 26 }, () => ({
    x: Math.floor(rand() * W),
    y: 3 + Math.floor(rand() * 58),
    phase: rand() * Math.PI * 2,
  }))
})()

const BANK_PANES = (() => {
  const panes: { x: number; y: number }[] = []
  for (let y = 69; y < 86; y += 3) {
    for (let x = BANK_X + 1; x < BANK_X + 23; x += 4) panes.push({ x, y })
  }
  return panes
})()
const BANK_TOGGLE_FRAMES = 75
const BANK_FLICKER_FRAMES = 12

const HANOI_THEME: SceneTooltipTheme = {
  get active() { return i18nT('pages.scenes.hanoiScene.coding_at_the_cafe') },
  get idle() { return i18nT('pages.scenes.hanoiScene.egg_coffee_break') },
}

const RIPPLES = (() => {
  const rand = seeded(5)
  return Array.from({ length: 70 }, () => ({
    x: rand() * W,
    y: WATER_TOP + 2 + Math.floor(rand() * (WATER_BOTTOM - WATER_TOP - 3)),
    len: 3 + Math.floor(rand() * 7),
    speed: 0.02 + rand() * 0.05,
    phase: rand() * Math.PI * 2,
  }))
})()

function shade(hex: string, factor: number): string {
  const n = parseInt(hex.slice(1), 16)
  const ch = (v: number) => Math.max(0, Math.min(255, Math.round(v * factor)))
  const rgb = (ch(n >> 16) << 16) | (ch((n >> 8) & 255) << 8) | ch(n & 255)
  return `#${rgb.toString(16).padStart(6, '0')}`
}

function painter(C: CanvasRenderingContext2D): Paint {
  return (x, y, w, h, c) => {
    C.fillStyle = c
    C.fillRect(x * S, y * S, w * S, h * S)
  }
}

function glow(C: CanvasRenderingContext2D, x: number, y: number, r: number, rgb: string, alpha: number) {
  const g = C.createRadialGradient(x * S, y * S, 0, x * S, y * S, r * S)
  g.addColorStop(0, `rgba(${rgb},1)`)
  g.addColorStop(1, `rgba(${rgb},0)`)
  C.globalAlpha = alpha
  C.fillStyle = g
  C.fillRect((x - r) * S, (y - r) * S, r * 2 * S, r * 2 * S)
  C.globalAlpha = 1
}

function disc(p: Paint, cx: number, cy: number, r: number, c: string) {
  for (let dy = -r; dy <= r; dy++) {
    const half = Math.round(Math.sqrt(r * r - dy * dy))
    p(cx - half, cy + dy, half * 2 + 1, 1, c)
  }
}

function ring(p: Paint, cx: number, cy: number, r: number, c: string) {
  for (let a = 0; a < Math.PI * 2; a += 0.25) p(Math.round(cx + Math.cos(a) * r), Math.round(cy + Math.sin(a) * r), 1, 1, c)
}

function line(p: Paint, x1: number, y1: number, x2: number, y2: number, c: string) {
  const n = Math.max(Math.abs(x2 - x1), Math.abs(y2 - y1), 1)
  for (let i = 0; i <= n; i++) p(Math.round(x1 + ((x2 - x1) * i) / n), Math.round(y1 + ((y2 - y1) * i) / n), 1, 1, c)
}

function sagY(x: number, wire: (typeof WIRES)[number]) {
  const u = (x - wire.x1) / (wire.x2 - wire.x1)
  return Math.round(wire.y1 + (wire.y2 - wire.y1) * u + wire.sag * 4 * u * (1 - u))
}

function bridgeDeckY(x: number) {
  const u = (x - BRIDGE_FROM) / (BRIDGE_TO - BRIDGE_FROM)
  return Math.round(174 + (166 - 174) * u - Math.sin(u * Math.PI) * 5)
}

function bridgeWaterY(x: number) {
  const u = (x - BRIDGE_FROM) / (BRIDGE_TO - BRIDGE_FROM)
  return bridgeDeckY(x) + Math.round(8 - u * 2)
}

function createLayer(paint: (C: CanvasRenderingContext2D, p: Paint) => void): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  canvas.width = W * S
  canvas.height = H * S
  const C = canvas.getContext('2d')!
  C.imageSmoothingEnabled = false
  paint(C, painter(C))
  return canvas
}

function paintBase(C: CanvasRenderingContext2D, p: Paint) {
  const sky = C.createLinearGradient(0, 0, 0, WATER_TOP * S)
  sky.addColorStop(0, '#081625')
  sky.addColorStop(0.6, '#1b4058')
  sky.addColorStop(1, '#4a6b6e')
  C.fillStyle = sky
  C.fillRect(0, 0, W * S, WATER_TOP * S)
  C.globalAlpha = 0.14
  for (const [x, y, w] of [[18, 40, 70], [60, 44, 40], [230, 34, 90], [300, 38, 50], [120, 60, 60]]) p(x, y, w, 1, '#a9c2c8')
  C.globalAlpha = 1
  glow(C, 392, 28, 34, '255,240,200', 0.35)
  disc(p, 392, 28, 9, '#f6edc8')
  p(388, 24, 3, 2, '#e6d9ab')
  p(395, 30, 2, 2, '#e6d9ab')
  p(390, 32, 2, 1, '#e6d9ab')
  const lake = C.createLinearGradient(0, WATER_TOP * S, 0, WATER_BOTTOM * S)
  lake.addColorStop(0, '#1f5062')
  lake.addColorStop(0.35, '#14384a')
  lake.addColorStop(1, '#0b2230')
  C.fillStyle = lake
  C.fillRect(0, WATER_TOP * S, W * S, (H - WATER_TOP) * S)
}

function paintBank(C: CanvasRenderingContext2D, p: Paint) {
  const x = BANK_X
  p(x + 2, 63, 20, 1, '#5f5b50')
  p(x + 2, 64, 20, 4, '#1d3f3d')
  p(x + 2, 66, 20, 1, '#2f625c')
  p(x, 68, 24, 20, '#1d252c')
  for (let y = 68; y <= 86; y += 3) p(x, y, 24, 1, '#4c4940')
  for (let gx = x; gx <= x + 23; gx += 4) p(gx, 68, 1, 20, '#4c4940')
  p(x + 24, 67, 6, 21, '#1c4438')
  p(x + 25, 66, 4, 1, '#1c4438')
  p(x + 24, 67, 1, 21, '#14322a')
  for (let y = 69; y < 86; y += 3) p(x + 25, y, 5, 1, '#3a7560')
  glow(C, x + 15, 88, 14, '255,214,90', 0.12)
  p(x - 1, 86, 32, 4, '#c79f00')
  p(x - 1, 86, 32, 1, '#e8bd1a')
  p(x + 5, 88, 20, 1, '#a88600')
}

function paintSkyline(C: CanvasRenderingContext2D, p: Paint) {
  const rand = seeded(7)
  const tones = ['#0f1b27', '#12202d', '#162533']
  for (let x = -4; x < W; ) {
    const w = 8 + Math.floor(rand() * 14)
    const h = 6 + Math.floor(rand() * 16)
    const top = WATER_TOP - 6 - h
    const tone = tones[Math.floor(rand() * tones.length)]
    p(x, top, w, h + 6, tone)
    if (rand() < 0.12) p(x + 2, top - 1, 3, 1, '#ff4a4a')
    for (let wy = top + 2; wy < WATER_TOP - 8; wy += 3) {
      for (let wx = x + 1; wx < x + w - 1; wx += 3) {
        if (rand() < 0.08) {
          C.globalAlpha = 0.6
          p(wx, wy, 1, 1, rand() < 0.6 ? '#e2bf72' : '#a9c0bd')
          C.globalAlpha = 1
        }
      }
    }
    x += w - 1
  }
  p(BANK_X + 34, 60, 12, 32, '#14222f')
  p(BANK_X + 36, 59, 3, 1, '#ff4a4a')
  paintBank(C, p)
  for (let x = -2; x < W; ) {
    const r = x > BANK_X - 6 && x < BANK_X + 36 ? 3 : 5 + Math.floor(rand() * 5)
    const roll = rand()
    const crown = roll < 0.18 ? '#2e3a1e' : roll < 0.3 ? '#3a2c1a' : '#0c1f24'
    const cy = WATER_TOP - r + 1
    disc(p, x, cy, r, crown)
    disc(p, x - 1, cy - 1, Math.max(1, r - 3), shade(crown, 1.3))
    C.globalAlpha = 0.7
    p(x - r + 1, WATER_TOP - 1, r * 2 - 1, 1, '#8a7434')
    C.globalAlpha = 1
    x += r + 2 + Math.floor(rand() * 4)
  }
  for (const wx of [124, 284]) {
    disc(p, wx, 90, 6, '#1d4030')
    disc(p, wx - 1, 89, 4, '#25523a')
  }
  for (const x of SHORE_LIGHTS) {
    glow(C, x, WATER_TOP - 2, 4, '255,216,138', 0.5)
    p(x, WATER_TOP - 2, 1, 1, '#ffe2a0')
  }
}

function arch(p: Paint, x: number, y: number, w: number, h: number) {
  p(x, y + 1, w, h - 1, '#3a2e22')
  p(x + Math.floor(w / 2), y, w % 2 ? 1 : 0, 1, '#3a2e22')
  if (w > 2) p(x + 1, y, w - 2, 1, '#3a2e22')
}

function balustrade(p: Paint, x: number, y: number, w: number) {
  p(x, y, w, 1, '#d9c890')
  for (let bx = x + 1; bx < x + w - 1; bx += 2) p(bx, y + 1, 1, 1, '#b9a46a')
  p(x, y, 2, 3, '#e3d39c')
  p(x + w - 2, y, 2, 3, '#e3d39c')
}

function paintTurtleTower(C: CanvasRenderingContext2D, p: Paint) {
  glow(C, 160, 122, 30, '255,217,138', 0.24)
  const islet: [number, number, number][] = [[137, 144, 177], [138, 141, 180], [139, 139, 182], [140, 138, 183]]
  for (const [y, x1, x2] of islet) p(x1, y, x2 - x1, 1, y < 139 ? '#4a6a34' : '#2f4628')
  p(138, 140, 46, 1, '#77745f')
  p(149, 126, 24, 11, '#eadba3')
  p(168, 126, 5, 11, '#c4ad72')
  p(148, 136, 26, 1, '#9c8a58')
  arch(p, 151, 131, 2, 5)
  arch(p, 155, 128, 4, 8)
  arch(p, 163, 128, 4, 8)
  arch(p, 169, 131, 2, 5)
  p(157, 134, 1, 1, '#ffb347')
  p(148, 125, 26, 1, '#7a6a44')
  balustrade(p, 149, 122, 24)
  p(152, 115, 18, 7, '#eadba3')
  p(166, 115, 4, 7, '#c4ad72')
  arch(p, 153, 117, 2, 4)
  arch(p, 156, 116, 3, 6)
  arch(p, 162, 116, 3, 6)
  arch(p, 167, 117, 2, 4)
  p(151, 114, 20, 1, '#7a6a44')
  balustrade(p, 152, 111, 18)
  p(156, 106, 10, 6, '#e3d39c')
  p(163, 106, 3, 6, '#bfa86c')
  p(160, 108, 2, 3, '#3a2e22')
  p(154, 104, 14, 2, '#5c4a32')
  p(155, 103, 12, 1, '#6e5a3c')
  p(157, 102, 8, 1, '#6e5a3c')
  p(152, 102, 2, 2, '#8fa3b5')
  p(168, 102, 2, 2, '#8fa3b5')
  p(160, 100, 2, 2, '#c9b47a')
}

function paintPenTower(C: CanvasRenderingContext2D, p: Paint) {
  glow(C, 306, 160, 20, '255,224,160', 0.18)
  const rand = seeded(23)
  for (let y = 176; y < WATER_BOTTOM; y++) {
    const half = Math.min(16, 6 + Math.floor((y - 176) * 1.2))
    p(306 - half, y, half * 2, 1, '#55564c')
  }
  for (let i = 0; i < 40; i++) {
    const y = 177 + Math.floor(rand() * 14)
    const half = Math.min(16, 6 + Math.floor((y - 176) * 1.2))
    p(306 - half + Math.floor(rand() * half * 2), y, 2, 1, rand() < 0.5 ? '#7d7d72' : '#3f4038')
  }
  disc(p, 294, 182, 5, '#1f4a34')
  disc(p, 319, 184, 4, '#1f4a34')
  disc(p, 293, 181, 3, '#2b6a44')
  let top = 177
  for (const [w, h] of [[8, 7], [7, 6], [6, 6], [5, 5], [4, 5]]) {
    const x = 306 - Math.floor(w / 2)
    top -= h
    p(x, top, w, h, '#b8b2a2')
    p(x + w - 2, top, 2, h, '#86806f')
    p(x + 1, top + 2, 2, h - 3, '#9d9786')
    p(x - 1, top, w + 2, 1, '#6f6a5c')
  }
  p(305, top - 2, 2, 2, '#cfc8b4')
  p(305, top - 4, 2, 2, '#b8b2a2')
  p(320, 172, 6, 2, '#6f6a5c')
  p(321, 169, 4, 3, '#5f7480')
}

function paintBridge(C: CanvasRenderingContext2D, p: Paint) {
  glow(C, 348, 168, 34, '255,74,58', 0.16)
  for (let x = BRIDGE_FROM; x <= BRIDGE_TO; x++) {
    const y = bridgeDeckY(x)
    const i = x - BRIDGE_FROM
    if (i % 4 === 0) p(x, y + 2, 1, bridgeWaterY(x) - y - 2, '#c8302a')
    p(x, y, 1, 2, '#ff4a3a')
    p(x, y + 1, 1, 1, '#b02a22')
    p(x, y - 4, 1, 1, '#ff7a5c')
    if (i % 4 === 0) p(x, y - 4, 1, 4, '#ff5a44')
    else p(x, y - 3 + (i % 4 === 2 ? 1 : 0), 1, 1, '#e8443a')
  }
}

function upturnedRoof(p: Paint, x: number, y: number, w: number, h: number) {
  p(x, y, w, h, '#4a3528')
  p(x + 1, y - 1, w - 2, 1, '#5c4332')
  p(x - 2, y - 2, 2, 2, '#4a3528')
  p(x + w, y - 2, 2, 2, '#4a3528')
}

function paintJadeIsland(C: CanvasRenderingContext2D, p: Paint) {
  glow(C, 392, 146, 32, '255,200,120', 0.2)
  for (const [x, y, r, c] of [
    [376, 146, 8, '#5a5a22'],
    [388, 136, 10, '#1f4a34'],
    [402, 132, 11, '#6a4a1e'],
    [414, 140, 10, '#1f4a34'],
    [370, 152, 6, '#2b5a2e'],
  ] as const) {
    disc(p, x, y, r, c)
    disc(p, x - 2, y - 2, r - 3, shade(c, 1.35))
    C.globalAlpha = 0.6
    p(x - r + 2, y + r - 2, r * 2 - 3, 1, '#e0b84a')
    C.globalAlpha = 1
  }
  p(368, 156, 44, 9, '#1d3324')
  p(368, 156, 44, 1, '#2f5a3a')
  p(367, 164, 46, 1, '#6a6858')
  p(394, 151, 16, 5, '#b98b4e')
  p(398, 152, 3, 4, '#e8a040')
  upturnedRoof(p, 392, 147, 20, 4)
  p(376, 148, 15, 8, '#dcc07a')
  p(387, 148, 4, 8, '#b39a5c')
  p(381, 151, 5, 5, '#3a2a1c')
  upturnedRoof(p, 374, 145, 19, 3)
  p(379, 140, 9, 5, '#dcc07a')
  p(382, 141, 3, 3, '#e8a040')
  upturnedRoof(p, 377, 137, 13, 3)
  p(382, 134, 3, 2, '#c9a35c')
}

function paintLandmarks(C: CanvasRenderingContext2D, p: Paint) {
  paintTurtleTower(C, p)
  paintJadeIsland(C, p)
  paintPenTower(C, p)
  paintBridge(C, p)
}

function paintLampPost(p: Paint, x: number) {
  p(x - 2, 226, 5, 4, '#1b1e22')
  p(x - 1, 222, 3, 4, '#23272c')
  p(x, 180, 1, 42, '#23272c')
  p(x - 1, 200, 3, 1, '#23272c')
  p(x - 1, 186, 3, 1, '#23272c')
  p(x - 1, LAMP_HEAD_Y - 2, 3, 1, '#1b1e22')
  p(x - 2, LAMP_HEAD_Y - 1, 5, 1, '#1b1e22')
  p(x - 2, LAMP_HEAD_Y, 5, 6, '#ffe6a0')
  p(x - 2, LAMP_HEAD_Y, 1, 6, '#2a2e33')
  p(x + 2, LAMP_HEAD_Y, 1, 6, '#2a2e33')
  p(x - 1, LAMP_HEAD_Y + 6, 3, 2, '#1b1e22')
}

function paintBigTree(C: CanvasRenderingContext2D, p: Paint) {
  p(48, 150, 5, 80, '#3e2c20')
  p(51, 150, 2, 80, '#2e2018')
  p(46, 226, 9, 4, '#3e2c20')
  p(44, 160, 4, 2, '#3e2c20')
  p(53, 156, 6, 2, '#3e2c20')
  const clusters = [[50, 132, 18], [34, 142, 12], [74, 136, 15], [92, 148, 10], [60, 152, 12], [40, 124, 10]]
  clusters.forEach(([x, y, r]) => disc(p, x, y, r, '#1c4630'))
  clusters.forEach(([x, y, r]) => disc(p, x - 2, y - 3, r - 4, '#2b6a44'))
  const rand = seeded(41)
  for (let i = 0; i < 90; i++) {
    const [x, y, r] = clusters[i % clusters.length]
    const a = rand() * Math.PI * 2
    const dist = rand() * r
    p(Math.round(x + Math.cos(a) * dist), Math.round(y + Math.sin(a) * dist), 2, 1, rand() < 0.6 ? '#c9a640' : '#e0c060')
  }
  C.globalAlpha = 0.5
  clusters.forEach(([x, y, r]) => p(x - r + 3, y + r - 2, r * 2 - 5, 2, '#9a8a3a'))
  C.globalAlpha = 1
}

function paintSeat(p: Paint, index: number) {
  const { x } = SPOTS[index]
  const feet = SPOTS[index].y + FEET_OFFSET
  const stool = STOOLS[index]
  p(x - 4, feet - 5, 8, 2, stool)
  p(x - 4, feet - 3, 1, 3, shade(stool, 0.7))
  p(x + 3, feet - 3, 1, 3, shade(stool, 0.7))
  p(x - 3, feet - 2, 6, 1, shade(stool, 0.6))
  const table = index % 2 ? '#2e86c1' : '#d9d4c7'
  p(x + 6, feet - 7, 12, 2, table)
  p(x + 7, feet - 5, 1, 5, shade(table, 0.7))
  p(x + 16, feet - 5, 1, 5, shade(table, 0.7))
}

function drawMotorbike(p: Paint, x: number, feet: number, color: string, facing: 1 | -1) {
  const fx = (dx: number, w: number) => (facing === 1 ? x + dx : x + 18 - dx - w)
  disc(p, fx(3, 1), feet - 3, 3, '#111111')
  disc(p, fx(15, 1), feet - 3, 3, '#111111')
  p(fx(3, 1), feet - 3, 1, 1, '#777777')
  p(fx(15, 1), feet - 3, 1, 1, '#777777')
  p(fx(2, 10), feet - 9, 10, 4, color)
  p(fx(3, 7), feet - 11, 7, 2, '#1a1a1a')
  p(fx(6, 7), feet - 5, 7, 1, '#3a3a3a')
  p(fx(14, 2), feet - 12, 2, 8, '#4a4a4a')
  p(fx(12, 5), feet - 14, 5, 1, '#222222')
  p(fx(13, 3), feet - 11, 3, 3, color)
  p(fx(17, 1), feet - 11, 1, 2, '#ffe9a0')
  p(fx(0, 1), feet - 9, 1, 1, '#e03a2a')
}

function paintFlowerBike(p: Paint) {
  const feet = 228
  const x = 352
  ring(p, x + 4, feet - 4, 4, '#2a2e33')
  ring(p, x + 20, feet - 4, 4, '#2a2e33')
  line(p, x + 4, feet - 4, x + 11, feet - 11, '#3f7f78')
  line(p, x + 11, feet - 11, x + 20, feet - 4, '#3f7f78')
  line(p, x + 11, feet - 11, x + 11, feet - 4, '#3f7f78')
  line(p, x + 4, feet - 4, x + 11, feet - 4, '#3f7f78')
  line(p, x + 20, feet - 4, x + 18, feet - 14, '#3f7f78')
  p(x + 16, feet - 15, 5, 1, '#222222')
  p(x + 9, feet - 13, 4, 1, '#1a1a1a')
  p(x - 4, feet - 20, 18, 6, '#8a6a3f')
  for (let bx = x - 4; bx < x + 14; bx += 3) p(bx, feet - 20, 1, 6, '#6e5230')
  const rand = seeded(53)
  for (let i = 0; i < 80; i++) {
    const dx = rand() * 18
    const height = 8 * Math.sin((dx / 18) * Math.PI)
    const dy = rand() * height
    const roll = rand()
    p(Math.round(x - 4 + dx), Math.round(feet - 21 - dy), 1, 1, roll < 0.7 ? '#f4f1e6' : roll < 0.9 ? '#f1c40f' : '#e8a0b0')
  }
  p(x + 18, feet - 19, 5, 4, '#8a6a3f')
  p(x + 18, feet - 21, 5, 2, '#f1c40f')
}

function paintTeaBoard(p: Paint) {
  line(p, 52, 266, 48, 282, '#8a6a3f')
  line(p, 62, 266, 66, 282, '#8a6a3f')
  p(50, 266, 14, 11, '#1f2a24')
  p(50, 266, 14, 1, '#8a6a3f')
  p(50, 276, 14, 1, '#8a6a3f')
}

function paintUmbrella(p: Paint, cx: number, stripe: string) {
  for (let k = 0; k < 6; k++) {
    const half = 10 + k * 2
    for (let x = cx - half; x < cx + half; x++) p(x, 236 + k, 1, 1, Math.floor((x - cx + 40) / 4) % 2 ? stripe : '#efe6d4')
  }
  p(cx - 22, 242, 44, 1, shade(stripe, 0.6))
  p(cx - 1, 234, 2, 2, shade(stripe, 0.6))
  p(cx, 243, 1, 26, '#3a3a3a')
  p(cx - 1, 244, 3, 2, '#ffe6a0')
}

function paintCart(p: Paint, x: number, signBg: string, signBorder: string) {
  p(x, 268, 40, 2, '#c9a35c')
  p(x + 1, 270, 38, 16, '#7a4a24')
  p(x + 1, 270, 38, 1, '#9a6a3a')
  p(x + 4, 272, 32, 9, signBg)
  p(x + 4, 272, 32, 1, signBorder)
  p(x + 4, 280, 32, 1, signBorder)
  p(x + 1, 285, 38, 1, '#4a2a14')
  for (const wx of [x + 7, x + 33]) {
    disc(p, wx, 289, 3, '#1a1a1a')
    p(wx, 289, 1, 1, '#777777')
  }
}

function paintStalls(C: CanvasRenderingContext2D, p: Paint) {
  glow(C, 26, 262, 34, '255,179,92', 0.22)
  glow(C, 374, 262, 34, '255,179,92', 0.22)
  paintUmbrella(p, 26, '#c0392b')
  paintUmbrella(p, 374, '#1e8449')
  p(28, 259, 6, 9, '#7a5a3a')
  p(29, 254, 4, 5, '#d9a06b')
  p(26, 253, 10, 1, '#e8d8a8')
  p(28, 252, 6, 1, '#d9c48a')
  p(30, 251, 2, 1, '#c9b47a')
  paintCart(p, 6, '#8e1f1f', '#f1c40f')
  for (const cx of [12, 16, 20]) {
    p(cx, 265, 2, 3, '#f3ede0')
    p(cx, 265, 2, 1, '#f3dca0')
  }
  p(36, 262, 5, 6, '#c9d6cf')
  p(36, 262, 5, 1, '#e8f0ea')
  p(364, 259, 6, 9, '#2e5a8a')
  p(365, 254, 4, 5, '#c98b5a')
  p(365, 253, 4, 2, '#2c1e16')
  paintCart(p, 354, '#1f4e79', '#d6eaf8')
  p(378, 258, 11, 10, '#9aa3a8')
  p(378, 258, 11, 1, '#c9d0d4')
  p(377, 260, 1, 2, '#6b7378')
  p(389, 260, 1, 2, '#6b7378')
  p(358, 266, 4, 2, '#efe6d4')
  p(364, 266, 4, 2, '#efe6d4')
  p(370, 265, 5, 3, '#2f8a3a')
}

function paintGround(C: CanvasRenderingContext2D, p: Paint) {
  p(0, 192, W, 2, '#8a8174')
  p(0, 194, W, 36, '#45403a')
  for (let y = 199; y < 230; y += 6) p(0, y, W, 1, '#3b3732')
  for (let y = 194, row = 0; y < 230; y += 6, row++) {
    for (let x = (row % 2) * 6; x < W; x += 12) p(x, y, 1, 6, '#3b3732')
  }
  for (const x of LAMPS) glow(C, x, 226, 26, '255,210,122', 0.16)
  p(0, 230, W, 2, '#8a8174')
  p(0, 232, W, 16, '#24272d')
  const rand = seeded(31)
  for (let i = 0; i < 120; i++) p(Math.floor(rand() * W), 232 + Math.floor(rand() * 16), 1, 1, '#2d3138')
  for (let x = 4; x < W; x += 18) p(x, 240, 8, 1, '#8c8a7c')
  p(0, 248, W, 2, '#8a8174')
  p(0, 250, W, 50, '#544d47')
  for (let y = 258; y < H; y += 8) p(0, y, W, 1, '#4a443f')
  for (let y = 250, row = 0; y < H; y += 8, row++) {
    for (let x = (row % 2) * 7; x < W; x += 14) p(x, y, 1, 8, '#4a443f')
  }
  for (let x = 0; x < W; x += 10) {
    if (x > 290 && x < 320) continue
    p(x, 186, 1, 6, '#1b1e22')
  }
  for (const [from, to] of [[0, 291], [320, W]]) {
    p(from, 187, to - from, 1, '#2a2e33')
    p(from, 190, to - from, 1, '#2a2e33')
  }
  paintBigTree(C, p)
  LAMPS.forEach(x => paintLampPost(p, x))
  SPOTS.forEach((_, i) => paintSeat(p, i))
  drawMotorbike(p, 74, 272, '#7f8c8d', 1)
  drawMotorbike(p, 410, 274, '#c0392b', -1)
  paintFlowerBike(p)
  paintTeaBoard(p)
  paintStalls(C, p)
}

export default function HanoiScene({ agents, visible = true }: { agents: AgentSource[]; visible?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const peopleRef = useRef<HanoiAgent[]>([])
  const visibleRef = useRef(visible)
  const leavesRef = useRef<Leaf[]>([])
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, peopleRef, W, H, HANOI_THEME, 11, undefined, agents)

  useEffect(() => {
    leavesRef.current = Array.from({ length: 14 }, () => ({
      x: 28 + Math.random() * 72,
      y: 150 + Math.random() * 70,
      vx: -0.05 - Math.random() * 0.1,
      vy: 0.1 + Math.random() * 0.15,
      spin: Math.random() * Math.PI * 2,
      color: Math.random() < 0.6 ? '#d4b45a' : '#c98a3a',
    }))
  }, [])

  useEffect(() => {
    const existing = peopleRef.current
    const next: HanoiAgent[] = []
    agents.slice(0, SPOTS.length).forEach((src, i) => {
      const spot = SPOTS[i]
      const prev = existing.find(p => p.id === src.id)
      if (prev) {
        prev.name = src.name
        prev.label = src.label
        prev.kind = src.kind
        prev.running = src.running
        prev.detail = src.detail
        prev.tx = spot.x
        prev.ty = spot.y
        if ((src.lastMessage || '') !== prev.lastMessage) {
          prev.lastMessage = src.lastMessage || ''
          prev.msgAt = Date.now()
        }
        next.push(prev)
        return
      }
      const known = isKnownAgent('hanoi', src.id)
      const fromLeft = spot.x < W / 2
      next.push({
        id: src.id,
        name: src.name,
        label: src.label,
        kind: src.kind,
        x: known ? spot.x : fromLeft ? -16 - i * 30 : W + 16 + i * 30,
        y: spot.y,
        tx: spot.x,
        ty: spot.y,
        running: src.running,
        detail: src.detail,
        color: OUTFITS[i % OUTFITS.length],
        skin: SKINS[i % SKINS.length],
        hat: HATS[i % HATS.length],
        helmet: HELMETS[i % HELMETS.length],
        garment: GARMENTS[i % GARMENTS.length],
        phase: Math.random() * Math.PI * 2,
        blink: 140 + Math.random() * 260,
        sip: Math.floor(Math.random() * 240),
        enterProgress: known ? 1 : 0,
        facing: fromLeft ? 1 : -1,
        lastMessage: src.lastMessage || '',
        msgAt: 0,
      })
      markAgentsKnown('hanoi', [src.id])
    })
    peopleRef.current = next
    pruneAgents('hanoi', next.map(p => p.id))
  }, [agents])

  useEffect(() => {
    const { X, T, d } = initSceneCanvases(canvasRef.current!, textRef.current!, W, H, S)
    const baseLayer = createLayer(paintBase)
    const skylineLayer = createLayer(paintSkyline)
    const landmarkLayer = createLayer(paintLandmarks)
    const groundLayer = createLayer(paintGround)

    const bankRand = seeded(11)
    const bankPanes = BANK_PANES.map(pane => ({ ...pane, lit: bankRand() < 0.3, warm: bankRand() < 0.5, flicker: 0 }))

    const officeLights = () => {
      bankPanes.forEach(pane => {
        if (!pane.lit || Math.floor(pane.flicker / 3) % 2 === 1) return
        X.globalAlpha = 0.6
        d(pane.x, pane.y, 3, 2, pane.warm ? '#d9c48e' : '#c3cdc6')
      })
      X.globalAlpha = 1
    }

    const reflect = (layer: HTMLCanvasElement, x: number, waterline: number, w: number, rows: number, t: number, alpha: number) => {
      for (let r = 0; r < rows; r++) {
        const dy = waterline + r
        if (dy >= WATER_BOTTOM) break
        if ((r + Math.floor(t / 6)) % 4 === 3) continue
        const dx = Math.round(Math.sin(r * 0.6 + t * 0.06) * (0.6 + r * 0.04))
        X.globalAlpha = alpha * (1 - (r / rows) * 0.6)
        X.drawImage(layer, x * S, (waterline - 1 - r) * S, w * S, S, (x + dx) * S, dy * S, w * S, S)
      }
      X.globalAlpha = 1
    }

    const stars = (t: number) => {
      STARS.forEach(s => {
        X.globalAlpha = 0.25 + Math.abs(Math.sin(t * 0.015 + s.phase)) * 0.55
        d(s.x, s.y, 1, 1, '#e8f0ff')
      })
      X.globalAlpha = 1
    }

    const streak = (x: number, y0: number, rows: number, step: number, color: string, t: number, alpha: number) => {
      for (let k = 0; k < rows; k++) {
        if ((k + Math.floor(t / 8)) % 3 === 0) continue
        const width = 1 + ((k * 7 + Math.floor(t / 10)) % 3)
        X.globalAlpha = alpha * (1 - k / rows)
        d(x - Math.floor(width / 2), y0 + k * step, width, 1, color)
      }
      X.globalAlpha = 1
    }

    const water = (t: number) => {
      RIPPLES.forEach(r => {
        const x = ((r.x + t * r.speed) % (W + 20)) - 10
        X.globalAlpha = 0.18 + Math.abs(Math.sin(t * 0.03 + r.phase)) * 0.22
        d(Math.round(x), r.y, r.len, 1, r.y < 140 ? '#3b7a8a' : '#2a6475')
      })
      X.globalAlpha = 1
      SHORE_LIGHTS.forEach((x, i) => streak(x, WATER_TOP + 1, 10 + (i % 3) * 3, 2, '#ffd88a', t + i * 7, 0.5))
      LAMPS.forEach(x => streak(x, 180, 6, 2, '#ffe6a0', t + x, 0.5))
      const bx = Math.round(236 + Math.sin(t * 0.004) * 40)
      d(bx, 176, 12, 2, '#3e2a1e')
      d(bx + 1, 178, 10, 1, '#2a1c14')
      d(bx + 5, 172, 2, 4, '#1a1a1a')
      d(bx + 3, 171, 6, 1, '#c9b47a')
      d(bx + 4, 170, 4, 1, '#d9c48a')
      d(bx + 10, 173, 1, 2, '#ffb347')
      glow(X, bx + 10, 174, 6, '255,179,71', 0.3)
      X.globalAlpha = 0.3
      d(bx, 180, 12, 1, '#000000')
      X.globalAlpha = 1
    }

    const willows = (t: number) => {
      for (const base of [125, 285]) {
        for (let k = 0; k < 7; k++) {
          const sway = Math.round(Math.sin(t * 0.02 + k * 0.8) * 1)
          d(base - 6 + k * 2 + sway, 90, 1, 6 + (k % 3) * 3, k % 2 ? '#2a5a3a' : '#3a6a40')
        }
      }
    }

    const bridgeReflection = (t: number) => {
      for (let x = BRIDGE_FROM; x <= BRIDGE_TO; x++) {
        const waterY = bridgeWaterY(x)
        const pillar = waterY - bridgeDeckY(x)
        const depth = (x - BRIDGE_FROM) % 4 === 0 ? pillar + 2 : 2
        for (let k = pillar - depth; k < pillar + 3; k++) {
          const y = waterY + k
          if (y >= WATER_BOTTOM || (k + Math.floor(t / 6)) % 4 === 3) continue
          const dx = Math.round(Math.sin(y * 0.6 + t * 0.06) * 0.8)
          X.globalAlpha = 0.55 * (1 - k / (pillar + 6))
          d(x + dx, y, 1, 1, k >= pillar ? '#ff5a40' : '#d9362a')
        }
      }
      X.globalAlpha = 1
    }

    const bridgeLights = (t: number) => {
      for (let x = BRIDGE_FROM + 2; x < BRIDGE_TO; x += 8) {
        X.globalAlpha = 0.6 + Math.abs(Math.sin(t * 0.03 + x)) * 0.4
        d(x, bridgeDeckY(x) - 5, 1, 1, '#ffd27a')
      }
      X.globalAlpha = 1
    }

    const potSteam = (t: number) => {
      for (let k = 0; k < 6; k++) {
        const rise = (t * 0.3 + k * 4) % 14
        X.globalAlpha = 0.3 * (1 - rise / 14)
        d(380 + (k % 3) * 3 + Math.round(Math.sin(t * 0.05 + k)), Math.round(256 - rise), 1, 1, '#ffffff')
      }
      X.globalAlpha = 1
    }

    const lampGlows = (t: number) => {
      LAMPS.forEach((x, i) => {
        glow(X, x + 0.5, LAMP_HEAD_Y + 3, 16, '255,216,138', 0.24 + Math.sin(t * 0.05 + i) * 0.03)
      })
    }

    const lanterns = (t: number) => {
      LANTERNS.forEach((l, i) => {
        const sway = Math.round(Math.sin(t * 0.03 + i) * 1.2)
        const top = LANTERN_TOP
        d(l.x, top, 1, 3, '#2a1d17')
        d(l.x - 2 + sway, top + 3, 5, 6, l.color)
        d(l.x - 1 + sway, top + 4, 3, 4, l.inner)
        d(l.x - 2 + sway, top + 2, 5, 1, '#5c4332')
        d(l.x - 1 + sway, top + 9, 3, 1, '#5c4332')
        d(l.x + sway, top + 10, 1, 2, '#f1c40f')
        glow(X, l.x + sway + 0.5, top + 6, 10, l.color === '#e74c3c' ? '231,76,60' : '241,196,15', 0.25)
      })
    }

    const wires = (t: number) => {
      WIRES.forEach((wire, wi) => {
        for (let x = wire.x1; x <= wire.x2; x++) d(x, sagY(x, wire), 1, 1, '#14171b')
        if (!wire.bulbs) return
        for (let x = wire.x1 + 5, k = 0; x < wire.x2 - 2; x += 9, k++) {
          const on = 0.5 + Math.abs(Math.sin(t * 0.04 + k * 1.3 + wi)) * 0.5
          const color = BULB_COLORS[k % BULB_COLORS.length]
          const y = sagY(x, wire) + 1
          X.globalAlpha = on * 0.35
          d(x - 1, y - 1, 3, 3, color)
          X.globalAlpha = on
          d(x, y, 1, 1, color)
        }
        X.globalAlpha = 1
      })
    }

    const leaves = () => {
      leavesRef.current.forEach(leaf => {
        leaf.x += leaf.vx
        leaf.y += leaf.vy
        leaf.spin += 0.04
        if (leaf.y > 228) {
          leaf.x = 28 + Math.random() * 72
          leaf.y = 150 + Math.random() * 12
        }
        X.globalAlpha = 0.8
        d(Math.round(leaf.x + Math.sin(leaf.spin) * 2), Math.round(leaf.y), 2, 1, leaf.color)
      })
      X.globalAlpha = 1
    }

    const riders = (t: number) => {
      RIDERS.forEach(r => {
        const travel = (t * r.speed + r.offset) % (W + 80)
        const x = Math.round(r.dir === 1 ? travel - 40 : W + 40 - travel - 18)
        const fx = (dx: number, w: number) => (r.dir === 1 ? x + dx : x + 18 - dx - w)
        for (let k = 0; k < 22; k++) {
          X.globalAlpha = 0.16 * (1 - k / 22)
          const spread = Math.floor(k / 4)
          d(r.dir === 1 ? x + 18 + k : x - 1 - k, r.feet - 11 - spread, 1, 2 + spread * 2, '#fff2b0')
        }
        X.globalAlpha = 1
        drawMotorbike(d, x, r.feet, r.bike, r.dir)
        d(fx(7, 4), r.feet - 12, 4, 2, '#263238')
        d(fx(6, 4), r.feet - 18, 4, 6, r.shirt)
        d(fx(9, 4), r.feet - 16, 4, 1, r.shirt)
        d(fx(6, 4), r.feet - 22, 4, 4, r.helmet)
        d(fx(9, 1), r.feet - 20, 1, 1, '#1a1a1a')
        d(fx(0, 2), r.feet - 10, 2, 1, '#ff4b3a')
      })
    }

    const vendor = (t: number) => {
      const x = Math.round(((t * 0.22) % (W + 60)) - 30)
      const feet = VENDOR_FEET
      const step = Math.floor(t / 10) % 2
      d(x - 2, feet - 6, 2, 6 - step, '#2c2c2c')
      d(x + 1, feet - 6, 2, 5 + step, '#2c2c2c')
      d(x - 2, feet - 13, 5, 7, '#8e6fa8')
      d(x - 1, feet - 17, 4, 4, '#d9a06b')
      d(x - 4, feet - 18, 10, 1, '#e8d8a8')
      d(x - 2, feet - 19, 6, 1, '#d9c48a')
      d(x, feet - 20, 2, 1, '#c9b47a')
      const bounce = step
      d(x - 13, feet - 14 + bounce, 27, 1, '#8a6a3f')
      for (const side of [-12, 8]) {
        d(x + side, feet - 13 + bounce, 1, 5, '#6e5230')
        d(x + side + 4, feet - 13 + bounce, 1, 5, '#6e5230')
        d(x + side - 1, feet - 8 + bounce, 7, 4, '#a07a4a')
        for (let k = 0; k < 5; k++) d(x + side + k, feet - 9 + bounce - (k % 2), 1, 1, side < 0 ? '#f4f1e6' : '#e67e22')
      }
    }

    const statusIcon = (p: HanoiAgent, t: number) => {
      const feet = Math.round(p.y) + FEET_OFFSET
      const x = Math.round(p.x)
      X.globalAlpha = 0.55 * p.enterProgress
      d(x - 5, feet - 34, 11, 7, '#0b0f14')
      X.globalAlpha = p.enterProgress
      if (p.running) {
        d(x - 3, feet - 33, 7, 4, '#7ef7b0')
        d(x - 2, feet - 32, 5, 2, '#0b3a26')
        if (Math.floor(t / 15) % 2) d(x - 1, feet - 32, 1, 2, '#7ef7b0')
        d(x - 4, feet - 29, 9, 1, '#7ef7b0')
      } else {
        d(x - 2, feet - 32, 4, 3, '#f7d774')
        d(x + 2, feet - 31, 1, 1, '#f7d774')
        d(x - 3, feet - 29, 6, 1, '#f7d774')
        X.globalAlpha = p.enterProgress * (0.4 + Math.abs(Math.sin(t * 0.05 + p.phase)) * 0.5)
        d(x - 1 + (Math.floor(t / 20) % 2), feet - 33, 1, 1, '#ffffff')
      }
      X.globalAlpha = 1
    }

    const steam = (x: number, y: number, t: number, phase: number) => {
      for (let k = 0; k < 3; k++) {
        X.globalAlpha = 0.35 * (1 - k / 3)
        d(x + Math.round(Math.sin(t * 0.08 + k + phase)), y - k * 2 - (Math.floor(t / 12) % 2), 1, 1, '#ffffff')
      }
      X.globalAlpha = 1
    }

    const hat = (p: HanoiAgent, x: number, feet: number, facing: 1 | -1) => {
      if (p.hat === 'conical') {
        d(x - 5, feet - 22, 11, 1, '#e8d8a8')
        d(x - 3, feet - 23, 7, 1, '#d9c48a')
        d(x - 1, feet - 24, 3, 1, '#c9b47a')
        d(x, feet - 25, 1, 1, '#b8a36b')
        return
      }
      if (p.hat === 'helmet') {
        d(x - 3, feet - 23, 7, 3, p.helmet)
        d(x - 2, feet - 24, 5, 1, shade(p.helmet, 1.25))
        d(facing === 1 ? x + 3 : x - 4, feet - 21, 2, 1, '#222222')
        return
      }
      d(x - 3, feet - 22, 6, 2, '#2c1e16')
      d(facing === 1 ? x - 3 : x + 1, feet - 20, 2, p.garment === 'aodai' ? 7 : 3, '#2c1e16')
    }

    const drawAgent = (p: HanoiAgent, t: number) => {
      const feet = Math.round(p.y) + FEET_OFFSET
      const x = Math.round(p.x)
      const seated = Math.abs(p.tx - p.x) < 1 && Math.abs(p.ty - p.y) < 1
      const facing: 1 | -1 = seated ? 1 : p.facing
      const pants = p.garment === 'aodai' ? '#efeae0' : '#2c3e66'
      X.globalAlpha = 0.3 * p.enterProgress
      X.fillStyle = '#000000'
      X.beginPath()
      X.ellipse(x * S, feet * S, 6 * S, 1.5 * S, 0, 0, Math.PI * 2)
      X.fill()
      X.globalAlpha = p.enterProgress
      if (seated) {
        d(x - 2, feet - 7, 6, 2, pants)
        d(x + 3, feet - 5, 2, 4, pants)
        d(x + 3, feet - 1, 3, 1, '#1a1a1a')
      } else {
        const step = Math.floor(t / 6 + p.phase) % 2
        d(x - 2, feet - 7, 2, 7 - step, pants)
        d(x + 1, feet - 7, 2, 6 + step, pants)
      }
      d(x - 3, feet - 15, 6, 8, p.color)
      d(facing === 1 ? x - 3 : x + 2, feet - 15, 1, 8, shade(p.color, 0.75))
      if (p.garment === 'aodai') {
        d(x - 3, feet - 8, seated ? 8 : 6, 2, p.color)
        d(x - 1, feet - 16, 3, 1, p.color)
      } else {
        d(x, feet - 15, 2, 1, p.skin)
      }
      const lifting = !p.running && p.sip < 60
      if (!seated) {
        const swing = Math.floor(t / 6 + p.phase) % 2 ? 3 : -4
        d(x + swing, feet - 14, 1, 5, p.color)
      } else if (p.running) {
        const tap = Math.floor((t + p.phase * 20) / 5) % 2
        d(x + 1, feet - 14, 2, 3, p.color)
        d(x + 3, feet - 11, 4, 1, p.skin)
        d(x + 7, feet - 10 - tap, 1, 1, p.skin)
        d(x + 8, feet - 8, 8, 1, '#90a4ae')
        d(x + 15, feet - 14, 1, 6, '#37474f')
        d(x + 14, feet - 13, 1, 4, '#7ee8c8')
        glow(X, x + 11, feet - 11, 9, '126,232,200', 0.2 + Math.abs(Math.sin(t * 0.05 + p.phase)) * 0.08)
        d(x + 16, feet - 10, 2, 3, '#f3ede0')
        d(x + 16, feet - 10, 2, 1, '#f3dca0')
      } else if (lifting) {
        d(x + 1, feet - 14, 2, 2, p.color)
        d(x + 2, feet - 13, 2, 1, p.skin)
        d(x + 3, feet - 17, 2, 3, '#f3ede0')
        d(x + 3, feet - 17, 2, 1, '#f3dca0')
        steam(x + 4, feet - 19, t, p.phase)
      } else {
        d(x + 1, feet - 13, 2, 4, p.color)
        d(x + 2, feet - 9, 2, 1, p.skin)
        d(x + 11, feet - 10, 2, 3, '#f3ede0')
        d(x + 11, feet - 10, 2, 1, '#f3dca0')
        steam(x + 12, feet - 12, t, p.phase)
      }
      d(x - 3, feet - 21, 6, 6, p.skin)
      d(facing === 1 ? x - 2 : x + 1, feet - 18, 1, 1, shade(p.skin, 0.8))
      if (p.blink > 6) d(facing === 1 ? x + 1 : x - 2, feet - 19, 1, 1, '#1b1b1b')
      d(facing === 1 ? x + 2 : x - 3, feet - 17, 1, 1, shade(p.skin, 0.7))
      hat(p, x, feet, facing)
      X.globalAlpha = 1
    }

    const labels = (p: HanoiAgent) => {
      const feet = Math.round(p.y) + FEET_OFFSET
      const nameLines = drawLabel(T, p.name, p.x * S, (feet + 3.6) * S, {
        role: 'name',
        weight: 'bold',
        color: '#fff7e8',
        bgColor: 'rgba(10,14,20,0.55)',
        padX: 2,
        padY: 0.5,
        align: 'center',
        scale: S,
        maxWidth: LABEL_WIDTH * S,
      })
      if (p.detail) {
        drawLabel(T, p.detail, p.x * S, (feet + 3.6) * S + nameLines * sceneLineHeight('name'), {
          role: 'detail',
          color: '#efe3cb',
          bgColor: 'rgba(10,14,20,0.45)',
          padX: 2,
          padY: 0.3,
          align: 'center',
          scale: S,
          maxWidth: LABEL_WIDTH * S,
        })
      }
    }

    const bubbles = (t: number) => {
      const people = peopleRef.current
      let anyFresh = false
      people.forEach(p => {
        if (!onStage(p) || !p.lastMessage || Date.now() - p.msgAt >= SPEECH_BUBBLE_MS) return
        anyFresh = true
        const age = Date.now() - p.msgAt
        const alpha = age > SPEECH_BUBBLE_MS - 1000 ? (SPEECH_BUBBLE_MS - age) / 1000 : 1
        drawSpeechBubble(T, p.lastMessage, p.x * S, (p.y + FEET_OFFSET - 35) * S, { scale: S, alpha })
      })
      if (anyFresh || people.length < 2 || t % 240 >= 140) return
      const pairIndex = Math.floor(t / 240) % (people.length - 1)
      const a = people[pairIndex]
      const b = people[pairIndex + 1]
      if (a.ty !== b.ty || Math.abs(a.tx - a.x) > 1 || Math.abs(b.tx - b.x) > 1) return
      const [askKey, replyKey] = CHAT_LINE_KEYS[Math.floor(t / 240) % CHAT_LINE_KEYS.length]
      const pair = [i18nT(askKey), i18nT(replyKey)]
      drawSpeechBubble(T, pair[0], a.x * S, (a.y + FEET_OFFSET - 35) * S, { scale: S, alpha: 0.9, maxWidth: 50 * S })
      drawSpeechBubble(T, pair[1], b.x * S, (b.y + FEET_OFFSET - 35) * S, { scale: S, alpha: 0.9, maxWidth: 50 * S })
    }

    const signs = () => {
      T.textAlign = 'center'
      T.textBaseline = 'middle'
      T.font = sceneFont('label', 'bold')
      T.fillStyle = '#f7dc6f'
      T.fillText(i18nT('pages.scenes.hanoiScene.sign_egg_coffee'), 26 * S, 276.5 * S)
      T.fillStyle = '#fef9e7'
      T.fillText(i18nT('pages.scenes.hanoiScene.sign_pho'), 374 * S, 276.5 * S)
      T.fillStyle = '#f4f1e6'
      T.fillText(i18nT('pages.scenes.hanoiScene.sign_iced_tea'), 57 * S, 271.5 * S)
      T.textAlign = 'start'
      T.textBaseline = 'alphabetic'
    }

    const update = (t: number) => {
      if (t % BANK_TOGGLE_FRAMES === 0) {
        const pane = bankPanes[Math.floor(Math.random() * bankPanes.length)]
        pane.lit = !pane.lit
        pane.flicker = pane.lit ? BANK_FLICKER_FRAMES : 0
      }
      bankPanes.forEach(pane => {
        if (pane.flicker > 0) pane.flicker -= 1
      })
      peopleRef.current.forEach(p => {
        if (p.enterProgress < 1) p.enterProgress = Math.min(1, p.enterProgress + 0.03)
        const dx = p.tx - p.x
        const dy = p.ty - p.y
        if (Math.abs(dx) > 0.01) {
          p.x += Math.sign(dx) * Math.min(Math.abs(dx), 0.7)
          p.facing = dx > 0 ? 1 : -1
        }
        if (Math.abs(dy) > 0.01) p.y += Math.sign(dy) * Math.min(Math.abs(dy), 0.7)
        p.blink -= 1
        if (p.blink <= 0) p.blink = 150 + Math.random() * 280
        p.sip = (p.sip + 1) % 260
      })
    }

    const draw = (t: number) => {
      T.clearRect(0, 0, W * S, H * S)
      X.drawImage(baseLayer, 0, 0)
      stars(t)
      X.drawImage(skylineLayer, 0, 0)
      officeLights()
      reflect(skylineLayer, 0, WATER_TOP, W, 28, t, 0.3)
      water(t)
      willows(t)
      X.drawImage(landmarkLayer, 0, 0)
      reflect(landmarkLayer, 138, ISLET_WATERLINE, 46, 42, t, 0.55)
      reflect(landmarkLayer, BRIDGE_TO + 2, ISLAND_WATERLINE, W - BRIDGE_TO - 2, 40, t, 0.45)
      bridgeReflection(t)
      bridgeLights(t)
      X.drawImage(groundLayer, 0, 0)
      leaves()
      const people = [...peopleRef.current].sort((a, b) => a.y - b.y || a.x - b.x)
      people.filter(p => p.y < 240).forEach(p => drawAgent(p, t))
      riders(t)
      vendor(t)
      people.filter(p => p.y >= 240).forEach(p => drawAgent(p, t))
      lampGlows(t)
      lanterns(t)
      potSteam(t)
      wires(t)
      const visiblePeople = people.filter(onStage)
      visiblePeople.forEach(p => statusIcon(p, t))
      signs()
      visiblePeople.forEach(labels)
      bubbles(t)
      T.textAlign = 'center'
      T.fillStyle = '#f7e7c6'
      T.font = sceneFont('title', 'bold')
      T.fillText(i18nT('pages.scenes.hanoiScene.hanoi_old_quarter'), (W / 2) * S, 15 * S)
      T.fillStyle = '#a8c7c2'
      T.font = sceneFont('detail')
      T.fillText(i18nT('pages.scenes.hanoiScene.hoan_kiem_lakeside'), (W / 2) * S, 24 * S)
      T.textAlign = 'start'
      drawLabel(
        T,
        i18nT('pages.scenes.hanoiScene.at_the_cafe', { n: peopleRef.current.length, total: SPOTS.length }),
        5 * S,
        (H - 5) * S,
        { role: 'label', color: '#f7e7c6', bgColor: 'rgba(10,14,20,0.7)', padX: 2, padY: 1, scale: S },
      )
    }

    return runSceneLoop(visibleRef, tickRef, update, draw)
  }, [])

  useVisibleSync(visibleRef, visible)

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas
        ref={canvasRef}
        aria-label={i18nT('pages.scenes.hanoiScene.hanoi_old_quarter_animation')}
        style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }}
        onMouseMove={canvasProps.onMouseMove}
        onMouseLeave={canvasProps.onMouseLeave}
        onClick={canvasProps.onClick}
      />
      <canvas
        ref={textRef}
        aria-label={i18nT('pages.scenes.hanoiScene.hanoi_old_quarter_text_overlay')}
        style={TEXT_CANVAS_STYLE}
      />
      {tooltipEl}
    </div>
  )
}
