/**
 * petdexImport — turn a PetDex sprite sheet into a Kiro Ghost sprite pack.
 *
 * PetDex pets are a fixed 8-col × 9-row grid of 192×208 frames. Each row is a
 * state (authoritative order from crafter-station/petdex `src/lib/pet-states.ts`).
 * We slice the rows we care about into per-state horizontal strips (the shape
 * our SpriteRenderer + gallery:save-sprite-pack already understand) and map them
 * onto our Idle / Status / Random buckets.
 */

const FRAME_W = 192
const FRAME_H = 208
const FPS = 7 // PetDex loops ~6 frames / 1100ms ≈ 5.5fps; 7 reads a touch livelier

/**
 * PetDex canonical rows → our slots. { key, row, frames }.
 *
 * PetDex's nine rows (src/lib/pet-states.ts): 0 idle · 1 running-right ·
 * 2 running-left · 3 waving · 4 jumping · 5 failed · 6 waiting · 7 running ·
 * 8 review.
 *
 * Mapped onto Idle + Status (done / error) + Random:
 *   failed  → error
 *   jumping → done      PetDex has no "success" row, so the celebratory hop is the
 *                       best fit — without it every PetDex pet fell back to idle
 *                       for the job-done reaction.
 *   running-right → walking (Wander)
 * Skipped on purpose: running-left, because the app mirrors art with flipX, so a
 * second directional loop would be redundant.
 */
export const STATE_MAP: Array<{ key: string; row: number; frames: number }> = [
  { key: 'idle', row: 0, frames: 6 },     // idle
  { key: 'error', row: 5, frames: 8 },    // failed
  { key: 'done', row: 4, frames: 5 },     // jumping — celebratory hop
  { key: 'walking', row: 1, frames: 8 },  // running-right
]

// PetDex rows → our open-ended Random "extras", played spontaneously. 'waiting'
// used to be forced into the `offline` slot, which is no longer a slot at all.
// With `review` here, every PetDex row is now consumed except running-left
// (deliberately skipped: the app mirrors with flipX, so a second directional
// loop is redundant).
export const RANDOM_MAP: Array<{ name: string; row: number; frames: number }> = [
  { name: 'wave', row: 3, frames: 4 },    // waving
  { name: 'waiting', row: 6, frames: 6 }, // waiting (patient idle variant)
  { name: 'run', row: 7, frames: 6 },     // running (in-place)
  /*
   * PetDex publishes no authoritative frame count for the review row, so this
   * slices at the sheet's maximum width. Over-counting is safe by construction:
   * SpriteRenderer detects and skips empty trailing frames, so a 5-frame clip
   * sliced as 8 plays as 5 — while UNDER-counting would silently truncate the
   * animation with no signal anywhere.
   */
  { name: 'review', row: 8, frames: 8 },  // review (thoughtful look)
]

/** Crop one grid row into a horizontal strip PNG data URI. */
function sliceRow(img: HTMLImageElement, row: number, frames: number): string {
  const canvas = document.createElement('canvas')
  canvas.width = frames * FRAME_W
  canvas.height = FRAME_H
  const ctx = canvas.getContext('2d')!
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(img, 0, row * FRAME_H, frames * FRAME_W, FRAME_H, 0, 0, frames * FRAME_W, FRAME_H)
  return canvas.toDataURL('image/png')
}

export interface PetdexMeta {
  displayName: string
  author: string
  description: string
}

/**
 * Crop the first idle frame out of a PetDex sheet so the import popup can show
 * what the pet actually looks like before the user commits.
 */
export function firstFramePreview(spriteBase64: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      try {
        const canvas = document.createElement('canvas')
        canvas.width = FRAME_W
        canvas.height = FRAME_H
        const ctx = canvas.getContext('2d')!
        ctx.clearRect(0, 0, FRAME_W, FRAME_H)
        ctx.drawImage(img, 0, 0, FRAME_W, FRAME_H, 0, 0, FRAME_W, FRAME_H)
        resolve(canvas.toDataURL('image/png'))
      } catch (err) {
        reject(err as Error)
      }
    }
    img.onerror = () => reject(new Error('Could not decode the sprite sheet'))
    img.src = `data:image/webp;base64,${spriteBase64}`
  })
}

/**
 * Build the payload accepted by `gallery:save-sprite-pack` from a PetDex sheet
 * (base64 webp/png). Resolves once the image has loaded and been sliced.
 */
export function buildSpritePackData(spriteBase64: string, meta: PetdexMeta): Promise<any> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      try {
        // Guard: expect at least an 8-wide grid; height determines rows.
        const assignments: Record<string, string> = {}
        const rowAssignments: Record<string, number> = {}
        for (const { key, row, frames } of STATE_MAP) {
          if ((row + 1) * FRAME_H > img.naturalHeight) continue
          assignments[key] = sliceRow(img, row, frames)
          rowAssignments[key] = row
        }
        if (!assignments['idle']) { reject(new Error('Sheet has no idle row (unexpected PetDex layout)')); return }

        const randomAssignments: Record<string, string> = {}
        for (const { name, row, frames } of RANDOM_MAP) {
          if ((row + 1) * FRAME_H > img.naturalHeight) continue
          randomAssignments[name] = sliceRow(img, row, frames)
        }

        resolve({
          name: meta.displayName,
          author: meta.author,
          description: meta.description,
          frameWidth: FRAME_W,
          frameHeight: FRAME_H,
          fps: FPS,
          flipX: false,
          offsetY: 0,
          // Omit sourceImage: the sheet is webp; we persist sliced PNG strips
          // instead, which the renderer + re-editor already handle.
          assignments,
          randomAssignments,
          rowAssignments,
        })
      } catch (err) {
        reject(err as Error)
      }
    }
    img.onerror = () => reject(new Error('Could not decode the PetDex sprite sheet'))
    img.src = `data:image/webp;base64,${spriteBase64}`
  })
}
