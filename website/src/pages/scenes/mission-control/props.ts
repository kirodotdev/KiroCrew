/* ── Mission Control — room props ── */
import { type DrawFn, drawText, C } from './parts'

export function drawCoffeeStation(d: DrawFn, x: number, y: number, _t: number) {
  // Counter
  d(x, y, 30, 8, '#3a2a1a')
  d(x, y, 30, 2, '#4a3a2a') // top highlight
  d(x, y + 8, 30, 14, '#2a1a0a')
  // Coffee machine
  d(x + 2, y - 16, 14, 16, '#444')
  d(x + 2, y - 16, 14, 2, '#555') // top highlight
  d(x + 4, y - 12, 10, 8, '#333')
  d(x + 6, y - 10, 6, 4, '#222') // dispenser
  // Pot on warmer
  d(x + 4, y - 2, 8, 4, '#664422')
  d(x + 5, y - 2, 6, 1, '#886644') // coffee surface
  // Mugs
  d(x + 20, y - 4, 4, 4, '#ddd')
  d(x + 19, y - 2, 1, 2, '#ccc') // handle
  d(x + 26, y - 4, 4, 4, '#e74c3c')
  d(x + 25, y - 2, 1, 2, '#c0392b')
  // Label
  drawText(d, 'COFFEE', x + 4, y + 10, '#665544', 1)
}

export function drawVendingMachine(d: DrawFn, x: number, y: number, t: number) {
  // Body
  d(x, y, 28, 50, '#2a2a3a')
  d(x, y, 28, 2, '#3a3a4a')
  d(x + 1, y + 1, 26, 48, '#222238')
  // Glass front with interior backlight + brief flicker
  const flickerOn = (t % 300 < 3) || (t % 500 < 2) // 2-3 frame flash every few seconds
  d(x + 3, y + 4, 22, 24, flickerOn ? '#0e0e1a' : '#1a1a2a')
  d(x + 3, y + 4, 22, 1, '#2a2a5a') // top edge glow
  // Snack rows
  for (let row = 0; row < 3; row++) {
    for (let col = 0; col < 4; col++) {
      const colors = ['#e74c3c','#3498db','#f39c12','#2ecc71','#9b59b6','#e67e22','#1abc9c','#e84393','#ff6b6b','#4ecdc4','#ffe66d','#a8e6cf']
      d(x + 5 + col * 5, y + 6 + row * 8, 3, 5, colors[(row * 4 + col) % colors.length])
    }
  }
  // Glass reflection (faint diagonal line)
  d(x + 5, y + 6, 1, 8, '#ffffff0a')
  d(x + 6, y + 5, 1, 8, '#ffffff08')
  // Dispenser slot
  d(x + 6, y + 32, 16, 6, '#111')
  d(x + 7, y + 33, 14, 4, '#0a0a0a')
  // Coin slot + buttons
  d(x + 20, y + 32, 4, 2, '#888')
  d(x + 4, y + 40, 4, 3, '#444'); d(x + 12, y + 40, 4, 3, '#444')
  d(x + 20, y + 40, 4, 3, '#444')
  // Light bleed — glass glow on front face and floor
  d(x - 1, y + 4, 1, 24, '#1a1a3a') // left bleed
  d(x + 25, y + 4, 1, 24, '#1a1a3a') // right bleed
  // Label inside machine
  drawText(d, 'SNACKS', x + 2, y + 44, '#555', 1)
}

export function drawEquipmentRack(d: DrawFn, x: number, y: number, t: number) {
  // Frame
  d(x, y, 22, 50, '#222')
  d(x + 1, y + 1, 20, 48, '#1a1a1a')
  d(x, y, 22, 1, '#333') // top highlight
  // Server units (4 rows)
  for (let row = 0; row < 4; row++) {
    const ry = y + 3 + row * 11
    d(x + 2, ry, 18, 9, '#2a2a2a')
    d(x + 2, ry, 18, 1, '#333') // unit top
    // LEDs
    for (let led = 0; led < 3; led++) {
      const on = ((t >> (3 + led + row)) & 1)
      d(x + 4 + led * 3, ry + 2, 1, 1, on ? C.led.on : C.led.off)
    }
    // Vent holes
    for (let v = 0; v < 4; v++) d(x + 14 + v * 2, ry + 4, 1, 3, '#1a1a1a')
  }
  // Bottom vent
  d(x + 4, y + 46, 14, 2, '#1a1a1a')
}

export function drawTrashCan(d: DrawFn, x: number, y: number) {
  // Shadow on floor
  d(x - 1, y + 18, 16, 2, '#0a0c16')
  // Can body — tapers slightly wider at top
  d(x + 1, y + 4, 12, 14, '#555')
  d(x, y + 2, 14, 2, '#666') // rim
  d(x + 2, y + 5, 10, 12, '#4a4a4a') // inner shadow
  // Vertical ridges
  d(x + 4, y + 5, 1, 12, '#5a5a5a')
  d(x + 7, y + 5, 1, 12, '#5a5a5a')
  d(x + 10, y + 5, 1, 12, '#5a5a5a')
  // Bag liner poking out
  d(x + 1, y + 1, 12, 2, '#222')
  d(x + 2, y, 10, 2, '#1a1a1a')
  // Trash items
  d(x + 3, y - 2, 4, 3, '#ddd') // crumpled paper
  d(x + 3, y - 2, 1, 1, '#ccc')
  d(x + 8, y - 1, 3, 2, '#8B4513') // coffee cup
  d(x + 8, y - 1, 3, 1, '#a0522d') // cup rim
  d(x + 5, y - 3, 2, 2, '#3498db') // blue wrapper
}

export function drawPlant(d: DrawFn, x: number, y: number, variant: number) {
  // Pot
  d(x, y + 8, 8, 6, '#8B4513')
  d(x + 1, y + 8, 6, 1, '#a0522d') // pot rim highlight
  d(x + 1, y + 14, 6, 1, '#6b3410') // pot shadow
  // Soil
  d(x + 1, y + 7, 6, 2, '#3a2a1a')
  if (variant === 0) {
    // Tall leafy
    d(x + 3, y, 2, 8, '#2d6b2e')
    d(x + 1, y + 1, 2, 3, '#3a8a3a')
    d(x + 5, y + 2, 2, 3, '#3a8a3a')
    d(x + 0, y, 3, 2, '#4a9a4a')
    d(x + 5, y + 1, 3, 2, '#4a9a4a')
  } else {
    // Small round bush
    d(x + 1, y + 2, 6, 6, '#2d6b2e')
    d(x + 2, y + 1, 4, 2, '#3a8a3a')
    d(x + 2, y + 3, 4, 3, '#4a9a4a') // highlight
  }
}

export function drawSpeaker(d: DrawFn, x: number, y: number, t: number) {
  // Mount bracket
  d(x, y, 16, 4, '#444')
  d(x, y, 16, 1, '#555')
  // Speaker body
  d(x + 1, y + 4, 14, 10, '#333')
  d(x + 1, y + 4, 14, 1, '#444')
  // Grille
  for (let gy = 0; gy < 8; gy += 2) d(x + 3, y + 6 + gy, 10, 1, '#2a2a2a')
  // Sound waves (when active)
  if ((t >> 6) & 1) {
    d(x + 16, y + 7, 1, 1, '#88aaff44')
    d(x + 18, y + 6, 1, 3, '#88aaff22')
  }
  // Power LED
  d(x + 13, y + 12, 1, 1, C.led.on)
}

export function drawWaterCooler(d: DrawFn, x: number, y: number) {
  // Stand
  d(x + 4, y + 16, 8, 14, '#888')
  d(x + 4, y + 16, 8, 1, '#999')
  d(x + 2, y + 28, 12, 2, '#777') // base
  // Body
  d(x + 2, y, 12, 16, '#ccc')
  d(x + 2, y, 12, 2, '#ddd') // top highlight
  d(x + 3, y + 1, 10, 14, '#bbb')
  // Water jug on top
  d(x + 4, y - 8, 8, 8, '#aaddff')
  d(x + 5, y - 8, 6, 2, '#cceeFF') // highlight
  d(x + 4, y - 9, 8, 1, '#88bbdd') // cap
  // Tap
  d(x + 6, y + 12, 4, 2, '#666')
  d(x + 7, y + 14, 2, 1, '#555')
  // Drip tray
  d(x + 3, y + 15, 10, 1, '#999')
}
