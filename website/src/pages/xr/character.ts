/**
 * What every Bot's look has in common, whatever draws its body: how tall it is, how bright its light is in each
 * state, and the colour it is known by.
 */
import type { BotState } from './body'
import { XR_GL } from './palette'
import { loadImage } from './sceneParts'

/**
 * How bright a Bot's light is, by state, from 0 to 1. Glasses that add light show every lit pixel on top of
 * the room, so only a Bot that asks is at full brightness. The others keep their shape and give up their glow.
 */
export const GLOW: Record<BotState, { ring: number; rim: number }> = {
  idle: { ring: 0, rim: 0.4 },
  working: { ring: 0.35, rim: 0.4 },
  failed: { ring: 0.6, rim: 0.4 },
  needs: { ring: 0.95, rim: 1 },
}

/** The top of a Bot's head, above its feet, at scale 1. Every size round a Bot is measured from it. */
export const HEAD_TOP_Y = 0.3266

/** The colour a Bot is known by: its avatar's tile colour. */
export async function tintOf(tileUri: string | undefined): Promise<number> {
  if (!tileUri) return XR_GL.dim
  const img = await loadImage(tileUri).catch(() => null)
  if (!img) return XR_GL.dim
  const c = document.createElement('canvas')
  c.width = c.height = 4
  const ctx = c.getContext('2d', { willReadFrequently: true })
  if (!ctx) return XR_GL.dim
  ctx.drawImage(img, 0, 0, 4, 4)
  const [r, g, b] = ctx.getImageData(2, 2, 1, 1).data
  return (r << 16) | (g << 8) | b
}
