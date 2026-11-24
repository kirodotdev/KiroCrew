/**
 * A Bot's face in XR is the face it has in the dashboard, cut into layers.
 *
 * The dashboard draws a face from the roster row's `avatar` field in tiers:
 * an uploaded picture, a pose icon, a pinned ghost, or the ghost seeded from
 * the name. This module uses the same readers and the same art, and returns
 * the layers the scene stacks a few millimetres apart: the tile, the body,
 * the eyes, and what the Bot wears or holds. Nothing here draws a new face.
 */
import { agentPath } from '../../api/agentPath'
import { ghostTraitsFrom, iconAvatarFrom, imageAvatarFrom, seededTraits } from '../../components/CrewAvatar'
import { poseBodyDataUri, poseDataUri, poseEyesDataUri, resolvePoseBg } from '../../lib/avatarPoses'
import { ACCESSORIES, EYES, PROPS, compose, ghostDataUri, type KiroGhostTraits } from '../../lib/kiroGhostAvatar'

export type FaceLayerId = 'tile' | 'body' | 'eyes' | 'extra' | 'art'

export interface FaceLayer {
  id: FaceLayerId
  uri: string
  /** Metres in front of the tile. */
  depth: number
}

export interface FaceArt {
  kind: 'image' | 'icon' | 'ghost'
  layers: FaceLayer[]
  /** The whole face as one picture, for the 3D-body variant. */
  flat: string
  /** The ghost in the art faces the other way. */
  flip: boolean
}

export const FACE_DEPTH: Record<FaceLayerId, number> = { tile: 0, art: 0, body: 0.006, eyes: 0.012, extra: 0.016 }

const svg = (size: number, inner: string): string =>
  `data:image/svg+xml;utf8,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}">${inner}</svg>`,
  )}`

const layer = (id: FaceLayerId, uri: string): FaceLayer => ({ id, uri, depth: FACE_DEPTH[id] })

const GHOST_TILE = /<rect width="1200" height="1200"[^>]*\/>/

function ghostArt(t: KiroGhostTraits): FaceArt {
  const flip = (inner: string) => (t.flip ? `<g transform="translate(1200,0) scale(-1,1)">${inner}</g>` : inner)
  const body = compose({ ...t, eyes: '', accessory: '', prop: '' }).replace(GHOST_TILE, '')
  const extra = (ACCESSORIES[t.accessory] ?? '') + (PROPS[t.prop] ?? '')
  const layers = [
    layer('tile', svg(1200, `<circle cx="600" cy="600" r="600" fill="${t.tile}"/>`)),
    layer('body', svg(1200, body)),
    layer('eyes', svg(1200, flip(EYES[t.eyes] ?? ''))),
  ]
  if (extra) layers.push(layer('extra', svg(1200, flip(extra))))
  return { kind: 'ghost', layers, flat: ghostDataUri(t), flip: t.flip }
}

/** The layers of the face the dashboard shows for this roster row. */
export function faceArtFor(avatar: unknown, name: string): FaceArt {
  const image = imageAvatarFrom(avatar)
  if (image) {
    // An uploaded picture has no parts to separate: one layer, same picture.
    const uri = image.pendingData ?? `${agentPath(name, '/avatar')}${image.v ? `?v=${image.v}` : ''}`
    return { kind: 'image', layers: [layer('art', uri)], flat: uri, flip: false }
  }
  const icon = iconAvatarFrom(avatar)
  if (icon) {
    return {
      kind: 'icon',
      layers: [
        layer('tile', svg(300, `<circle cx="150" cy="150" r="150" fill="${resolvePoseBg(icon.bg)}"/>`)),
        layer('body', poseBodyDataUri(icon.pose)),
        layer('eyes', poseEyesDataUri(icon.pose)),
      ],
      flat: poseDataUri(icon.pose, icon.bg),
      flip: false,
    }
  }
  // shortcut: a pack face (`kind: 'pack'`) falls to the seeded ghost; read its slot art when packs ship per-layer files.
  return ghostArt(ghostTraitsFrom(avatar) ?? seededTraits(name))
}
