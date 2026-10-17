import { useEffect, useRef } from 'react'
import type { AudioSample } from '../hooks/mic'

/**
 * Audio-reactive "strands" shader.
 *
 * Adapted from reactbits.dev/animations/strands (MIT). Three deliberate
 * departures from upstream:
 *
 *  1. **Raw WebGL2 instead of ogl.** Upstream ships an ogl-based component;
 *     this is one full-screen triangle and two programs, which does not
 *     justify a runtime dependency.
 *  2. **`uStretch` (see the shader comment).** Upstream normalizes both axes
 *     by `resolution.y`, so on a wide-and-thin element `uv.x` runs to +/-12
 *     and the taper term repeats — you get a tiled row of lens shapes instead
 *     of continuous strands. `uStretch` compresses x only.
 *  3. **`uPinch` (see the shader comment).** Upstream hard-codes the taper
 *     frequency at 1.3, which puts the convergence points outside the element:
 *     both ends read as clipped lines rather than as nodes.
 *
 * Uniforms are pulled from `sampleRef` INSIDE the render loop, so audio drives
 * the visuals at frame rate without a single React re-render. Passing the
 * sample as a prop would re-render the tree ~60x/sec.
 */

const MAX_STRANDS = 12
const MAX_COLORS = 8

const VERT = `#version 300 es
in vec2 position;
void main(){ gl_Position = vec4(position, 0.0, 1.0); }`

const FRAG = `#version 300 es
precision highp float;
uniform float uTime;
uniform vec2  uResolution;
uniform vec3  uColors[${MAX_COLORS}];
uniform int   uColorCount;
uniform int   uStrandCount;
uniform float uSpeed, uAmplitude, uWaviness, uThickness, uGlow, uTaper,
              uSpread, uHueShift, uIntensity, uOpacity, uScale, uSaturation,
              uStretch, uPinch;
out vec4 fragColor;
const float PI = 3.14159265;
vec3 spectrum(float t){ return 0.5 + 0.5*cos(2.0*PI*(t+vec3(0.00,0.33,0.67))); }
vec3 samplePalette(float t){
  t = fract(t);
  float scaled = t * float(uColorCount);
  int idx = int(floor(scaled));
  float blend = fract(scaled);
  int nextIdx = idx + 1;
  if (nextIdx >= uColorCount) nextIdx = 0;
  return mix(uColors[idx], uColors[nextIdx], blend);
}
vec3 strandColor(float t){ if (uColorCount > 0) return samplePalette(t); return spectrum(t); }
void main(){
  vec2 uv = (gl_FragCoord.xy - 0.5*uResolution) / uResolution.y;
  uv /= max(uScale, 0.0001);
  // Decouple horizontal from vertical. Upstream normalizes both axes by
  // resolution.y; on a wide element that pushes uv.x far past the first lobe
  // of max(cos(uv.x*PI*uPinch), 0.0), so the taper envelope repeats across the
  // width. Compressing x alone makes the envelope span the element exactly
  // once while amplitude and thickness stay in their original vertical units.
  uv.x /= max(uStretch, 0.0001);
  float e = 0.06 + uIntensity*0.94;
  // uPinch sets where the taper envelope reaches zero — i.e. where the strands
  // converge into the two end nodes, at |uv.x| = 0.5/uPinch. Raising it pulls
  // those nodes inward so they land INSIDE the element and read as nodes;
  // upstream's 1.3 puts them past the edge, leaving both ends clipped mid-line.
  float env = pow(max(cos(uv.x*PI*uPinch), 0.0), uTaper);
  vec3 col = vec3(0.0);
  for (int i = 0; i < ${MAX_STRANDS}; i++){
    if (i >= uStrandCount) break;
    float fi = float(i);
    float ph = fi*1.7*uSpread;
    float freq = (2.0 + fi*0.35)*uWaviness;
    float spd = 1.4 + fi*1.2;
    float tt = uTime*uSpeed;
    float w = sin(uv.x*freq + tt*spd + ph)*0.60
            + sin(uv.x*freq*1.1 - tt*spd*0.7 + ph*1.7)*0.40;
    float amp = (0.1 + 0.02*e)*env*uAmplitude;
    float y = w*amp;
    float d = abs(uv.y - y);
    float thick = (0.001 + 0.05*e)*(0.35 + env)*uThickness;
    float g = thick/(d + thick*0.45);
    g = g*g;
    float h = fi/float(uStrandCount) + uv.x*0.30 + uTime*0.04 + uHueShift;
    col += strandColor(h)*g*env;
  }
  col *= 0.45 + 0.7*e;
  col = 1.0 - exp(-col*uGlow);
  float gray = dot(col, vec3(0.2126, 0.7152, 0.0722));
  col = max(mix(vec3(gray), col, uSaturation), 0.0);
  float lum = max(max(col.r, col.g), col.b);
  float alpha = clamp(lum, 0.0, 1.0)*uOpacity;
  fragColor = vec4(col*uOpacity, alpha);
}`

/** Audio -> uniform mapping. `count` and `speed` are deliberately NOT driven:
 * strand count changes pop discretely, and volume-driven speed reads as jitter. */
const MAP = {
  count: 7,
  speed: 0.5,
  thickness: 0.78,
  taper: 2.0,
  spread: 1,
  hueShift: 0,
  opacity: 1,
  scale: 1.5,
  /** Target half-width in shader units; drives uStretch. */
  fillX: 0.34,
  /** Where the strands converge, at |uv.x| = 0.5/pinch. See the shader comment. */
  pinch: 2.0,
  saturation: 1.45,
  amplitude0: 0.3,
  amplitudeK: 2.3,
  intensity0: 0.1,
  intensityK: 0.6,
  waviness0: 0.8,
  wavinessK: 1.3,
  glow0: 1.8,
  glowK: 1.9,
}

/** Theme tokens the palette is sampled from, so it tracks every theme. */
const PALETTE_TOKENS = ['--accent', '--ok', '--info', '--aim']

function parseColor(raw: string): [number, number, number] | null {
  const v = raw.trim()
  let m = v.match(/^#([0-9a-f]{3})$/i)
  if (m) {
    const h = m[1]
    return [0, 1, 2].map(i => parseInt(h[i] + h[i], 16) / 255) as [number, number, number]
  }
  m = v.match(/^#([0-9a-f]{6})$/i)
  if (m) {
    const h = m[1]
    return [0, 2, 4].map(i => parseInt(h.substr(i, 2), 16) / 255) as [number, number, number]
  }
  m = v.match(/rgba?\(([^)]+)\)/)
  if (m) {
    const parts = m[1].split(',').slice(0, 3).map(p => parseFloat(p) / 255)
    if (parts.length === 3 && parts.every(n => Number.isFinite(n))) {
      return parts as [number, number, number]
    }
  }
  return null
}

/** Read the palette from live CSS custom properties. */
function readPalette(el: Element): { flat: Float32Array; count: number } {
  const cs = getComputedStyle(el)
  const cols: [number, number, number][] = []
  for (const token of PALETTE_TOKENS) {
    const c = parseColor(cs.getPropertyValue(token))
    if (c) cols.push(c)
  }
  const flat = new Float32Array(MAX_COLORS * 3)
  if (!cols.length) return { flat, count: 0 } // count 0 -> shader's built-in spectrum
  for (let i = 0; i < MAX_COLORS; i++) {
    const c = cols[Math.min(i, cols.length - 1)]
    flat[i * 3] = c[0]
    flat[i * 3 + 1] = c[1]
    flat[i * 3 + 2] = c[2]
  }
  return { flat, count: cols.length }
}

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader | null {
  const s = gl.createShader(type)
  if (!s) return null
  gl.shaderSource(s, src)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    gl.deleteShader(s)
    return null
  }
  return s
}

/** True when the browser can run the shader at all. Callers use this to pick
 * the bar-meter fallback BEFORE mounting, so no empty canvas is ever shown. */
export function strandsSupported(): boolean {
  if (typeof document === 'undefined') return false
  try {
    const c = document.createElement('canvas')
    return !!c.getContext('webgl2')
  } catch {
    return false
  }
}

interface Props {
  /** Live audio features, read every frame. Never passed as a value prop. */
  sampleRef: { current: AudioSample }
  className?: string
}

export default function Strands({ sampleRef, className = '' }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const gl = canvas.getContext('webgl2', {
      alpha: true,
      premultipliedAlpha: true,
      antialias: true,
    })
    if (!gl) return

    const vs = compile(gl, gl.VERTEX_SHADER, VERT)
    const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG)
    if (!vs || !fs) return
    const prog = gl.createProgram()
    if (!prog) return
    gl.attachShader(prog, vs)
    gl.attachShader(prog, fs)
    gl.linkProgram(prog)
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return

    gl.clearColor(0, 0, 0, 0)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)

    // One full-screen triangle (cheaper than a quad, no UVs needed).
    const vao = gl.createVertexArray()
    gl.bindVertexArray(vao)
    const vbo = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
    const posLoc = gl.getAttribLocation(prog, 'position')
    if (posLoc >= 0) {
      gl.enableVertexAttribArray(posLoc)
      gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0)
    }

    const u = (name: string) => gl.getUniformLocation(prog, name)
    const loc = {
      time: u('uTime'), res: u('uResolution'), colors: u('uColors'),
      colorCount: u('uColorCount'), strandCount: u('uStrandCount'),
      speed: u('uSpeed'), amplitude: u('uAmplitude'), waviness: u('uWaviness'),
      thickness: u('uThickness'), glow: u('uGlow'), taper: u('uTaper'),
      spread: u('uSpread'), hueShift: u('uHueShift'), intensity: u('uIntensity'),
      opacity: u('uOpacity'), scale: u('uScale'), saturation: u('uSaturation'),
      stretch: u('uStretch'), pinch: u('uPinch'),
    }

    let w = 1
    let h = 1
    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const r = canvas.getBoundingClientRect()
      const nw = Math.max(1, Math.round(r.width * dpr))
      const nh = Math.max(1, Math.round(r.height * dpr))
      if (nw !== w || nh !== h) {
        w = nw
        h = nh
        canvas.width = w
        canvas.height = h
      }
    }
    const ro = new ResizeObserver(resize)
    ro.observe(canvas)
    resize()

    let raf = 0
    const frame = (t: number) => {
      raf = requestAnimationFrame(frame)
      resize()
      const s = sampleRef.current
      const pal = readPalette(canvas)

      gl.bindVertexArray(vao)
      gl.useProgram(prog)
      gl.uniform1f(loc.time, t * 0.001)
      gl.uniform2f(loc.res, w, h)
      gl.uniform3fv(loc.colors, pal.flat)
      gl.uniform1i(loc.colorCount, pal.count)
      gl.uniform1i(loc.strandCount, MAP.count)
      gl.uniform1f(loc.speed, MAP.speed)
      gl.uniform1f(loc.thickness, MAP.thickness)
      gl.uniform1f(loc.taper, MAP.taper)
      gl.uniform1f(loc.spread, MAP.spread)
      gl.uniform1f(loc.hueShift, MAP.hueShift)
      gl.uniform1f(loc.opacity, MAP.opacity)
      gl.uniform1f(loc.scale, MAP.scale)
      gl.uniform1f(loc.saturation, MAP.saturation)
      gl.uniform1f(loc.pinch, MAP.pinch)
      // The mapping itself.
      gl.uniform1f(loc.amplitude, MAP.amplitude0 + s.level * MAP.amplitudeK)
      gl.uniform1f(loc.intensity, MAP.intensity0 + s.level * MAP.intensityK)
      gl.uniform1f(loc.waviness, MAP.waviness0 + s.centroid * MAP.wavinessK)
      gl.uniform1f(loc.glow, MAP.glow0 + s.onset * MAP.glowK)
      // Recomputed per frame so the taper stays correct across resizes.
      gl.uniform1f(loc.stretch, Math.max(1, w / h / 2 / MAP.scale / MAP.fillX))

      gl.viewport(0, 0, w, h)
      gl.clear(gl.COLOR_BUFFER_BIT)
      gl.drawArrays(gl.TRIANGLES, 0, 3)
    }
    raf = requestAnimationFrame(frame)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      gl.deleteProgram(prog)
      gl.deleteShader(vs)
      gl.deleteShader(fs)
      gl.deleteBuffer(vbo)
      gl.deleteVertexArray(vao)
      // Deliberately NOT calling WEBGL_lose_context.loseContext() here. A lost
      // context stays lost for the life of the canvas, and getContext() on the
      // same element hands back that same dead context — so under StrictMode
      // (setup -> cleanup -> setup on one mounted node) the second setup cannot
      // compile and the panel renders blank in development. The resources above
      // are already released, and the canvas is unmounted with the panel when
      // recording stops, so the context is collected with it.
    }
  }, [sampleRef])

  return <canvas ref={canvasRef} aria-hidden="true" className={`block w-full h-full ${className}`} />
}
