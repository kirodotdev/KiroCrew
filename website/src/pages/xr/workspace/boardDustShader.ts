/**
 * GLSL for the pieces of a leaving board (`BoardDust.tsx`). This module holds only shader text.
 *
 * A piece is slowed as by air: after t seconds it has gone the way a speed that falls off with uDrag takes it. Each
 * fades out on its own clock, so the cloud thins toward the end instead of going all at once. A piece is a soft round
 * core of its colour, a little brighter at its middle, in a faint glow of the same colour.
 */
export const DUST_VERTEX = /* glsl */ `
  attribute vec3 vel;
  attribute vec3 tint;
  attribute vec2 timing;
  attribute float grain;
  uniform float uTime;
  uniform float uScale;
  uniform float uSize;
  uniform float uDrag;
  uniform float uShrink;
  varying vec3 vTint;
  varying float vAlpha;
  void main() {
    float t = max(0.0, uTime - timing.x);
    float life = timing.y;
    vec3 p = position + vel * (1.0 - exp(-uDrag * t)) / uDrag;
    float k = clamp(t / life, 0.0, 1.0);
    vAlpha = (1.0 - k) * (1.0 - k);
    vTint = tint;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_PointSize = uSize * grain * uScale * (1.0 - uShrink * k) / -mv.z;
    gl_Position = projectionMatrix * mv;
  }
`

export const DUST_FRAGMENT = /* glsl */ `
  varying vec3 vTint;
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - vec2(0.5));
    float core = 1.0 - smoothstep(0.06, 0.26, d);
    float halo = (1.0 - smoothstep(0.18, 0.5, d)) * 0.4;
    float a = vAlpha * max(core, halo);
    if (a <= 0.01) discard;
    gl_FragColor = vec4(min(vec3(1.0), vTint * (0.8 + 0.5 * core) + core * 0.08), a);
  }
`
