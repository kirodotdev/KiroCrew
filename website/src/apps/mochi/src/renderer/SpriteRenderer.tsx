// Re-export shim: the renderer implementation is shared with Crew Companion
// (website/src/apps/shared/SpriteRenderer.tsx, sanctioned by #4211). This file
// keeps the vendored importers' './SpriteRenderer' path byte-identical to
// upstream so fixes to them still port line-for-line.
export { SpriteRenderer } from '../../../shared/SpriteRenderer'
