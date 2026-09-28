// Re-export shim: the renderer implementation is core's
// (website/src/components/appearancePacks/LottieRenderer.tsx), shared with Crew
// Companion and with the crew avatar. Core's player is the FENCED one: a clip
// that names a remote image or font is refused before `loadAnimation`, because
// lottie-web resolves those by requesting them from the dashboard's own
// authenticated origin. Mochi draws bundled presets today, but the moment it
// accepts an imported pack that fence has to already be in front of it, and one
// player means one fence. This file keeps the vendored importers'
// './LottieRenderer' path byte-identical to upstream so fixes to them still
// port line-for-line.
export { LottieRenderer } from '../../../../components/appearancePacks/LottieRenderer'
