// Re-export shim: the renderer implementation is core's
// (website/src/components/appearancePacks/SpriteRenderer.tsx), shared with Crew
// Companion and with the crew avatar. This file keeps the vendored importers'
// './SpriteRenderer' path byte-identical to upstream so fixes to them still port
// line-for-line.
export { SpriteRenderer } from '../../../../components/appearancePacks/SpriteRenderer'
