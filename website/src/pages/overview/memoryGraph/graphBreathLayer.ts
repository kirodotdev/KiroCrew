/**
 * The idle breath as one image: cosmos.gl draws the marks once onto a clear
 * background, this canvas over it keeps that frame as a texture, and each breath
 * step draws one quad with the same brightness wave the point shader used, so
 * a breath step costs one draw whatever the number of memories.
 */
import type { Rgb } from './graphColors'

/** Layout point = origin + uv * span, for uv in 0..1 from the bottom-left. */
export type BreathView = { x0: number; y0: number; w: number; h: number }
export type BreathParams = {
  /** Brightness amplitude (0 stops the wave), phase in radians, phase per layout unit. */
  amp: number; phase: number; perUnit: number
  /** The wave's centre, in layout units. */
  cx: number; cy: number
  view: BreathView
  background: Rgb
}

/** The layout box the canvas shows, from the camera (graphView's toLayout). */
export function breathView(view: { cx: number; cy: number; k: number }, size: { width: number; height: number }): BreathView {
  const w = size.width / view.k, h = size.height / view.k
  return { x0: view.cx - w / 2, y0: view.cy - h / 2, w, h }
}

const VERTEX = `#version 300 es
in vec2 corner;
out vec2 uv;
void main() { uv = corner * 0.5 + 0.5; gl_Position = vec4(corner, 0.0, 1.0); }`

// The point shader's `rgb *= 1 + amp * sin(phase - |p - centre| * perUnit)` on
// premultiplied marks, then over the background: a mark's colour breathes and
// the background does not.
const FRAGMENT = `#version 300 es
precision highp float;
in vec2 uv;
uniform sampler2D marks;
uniform vec4 wave;
uniform vec4 frame;
uniform vec3 background;
uniform float perUnit;
out vec4 color;
void main() {
  vec4 m = texture(marks, uv);
  vec2 p = frame.xy + uv * frame.zw;
  float b = 1.0 + wave.x * sin(wave.y - length(p - wave.zw) * perUnit);
  // Clamped per mark colour, as each mark's own fragment was.
  vec3 c = m.a > 0.0 ? min(m.rgb / m.a * b, vec3(1.0)) * m.a : vec3(0.0);
  color = vec4(c + background * (1.0 - m.a), 1.0);
}`

export class BreathLayer {
  readonly canvas: HTMLCanvasElement
  private readonly gl: WebGL2RenderingContext
  private readonly texture: WebGLTexture
  private readonly at: Record<'wave' | 'frame' | 'background' | 'perUnit', WebGLUniformLocation | null>
  private hasFrame = false
  private held = false

  /** Null when the browser gives no WebGL 2 context: the point shader breathes then. */
  static create(host: HTMLElement): BreathLayer | null {
    const canvas = document.createElement('canvas')
    const gl = canvas.getContext('webgl2', { alpha: false, antialias: false, depth: false, premultipliedAlpha: false })
    if (!gl) return null
    try { return new BreathLayer(host, canvas, gl) } catch { return null }
  }

  private constructor(host: HTMLElement, canvas: HTMLCanvasElement, gl: WebGL2RenderingContext) {
    this.canvas = canvas; this.gl = gl
    canvas.dataset.testid = 'memory-graph-breath'
    canvas.setAttribute('aria-hidden', 'true')
    // Over cosmos.gl's canvas, which still takes every pointer event.
    canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;visibility:hidden'
    const program = gl.createProgram()
    for (const [type, src] of [[gl.VERTEX_SHADER, VERTEX], [gl.FRAGMENT_SHADER, FRAGMENT]] as const) {
      const s = gl.createShader(type)
      if (!s) throw new Error('breath layer: no shader')
      gl.shaderSource(s, src)
      gl.compileShader(s)
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? 'breath layer: shader')
      gl.attachShader(program, s)
    }
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('breath layer: link')
    gl.useProgram(program)
    // One quad, uploaded once.
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer())
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW)
    const corner = gl.getAttribLocation(program, 'corner')
    gl.enableVertexAttribArray(corner)
    gl.vertexAttribPointer(corner, 2, gl.FLOAT, false, 0, 0)
    const texture = gl.createTexture()
    if (!texture) throw new Error('breath layer: no texture')
    this.texture = texture
    gl.bindTexture(gl.TEXTURE_2D, texture)
    for (const p of [gl.TEXTURE_MIN_FILTER, gl.TEXTURE_MAG_FILTER]) gl.texParameteri(gl.TEXTURE_2D, p, gl.NEAREST)
    for (const p of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) gl.texParameteri(gl.TEXTURE_2D, p, gl.CLAMP_TO_EDGE)
    // cosmos.gl's canvas is premultiplied: keep it so, as the blend above expects.
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true)
    // A canvas uploads top row first; the quad's uv runs from the bottom.
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true)
    gl.uniform1i(gl.getUniformLocation(program, 'marks'), 0)
    this.at = { wave: gl.getUniformLocation(program, 'wave'), frame: gl.getUniformLocation(program, 'frame'), background: gl.getUniformLocation(program, 'background'), perUnit: gl.getUniformLocation(program, 'perUnit') }
    host.appendChild(canvas)
  }

  /** Invalidate the image so an empty or unavailable source cannot leave stale marks. */
  clear() {
    this.hasFrame = false
    this.canvas.style.visibility = 'hidden'
  }

  /** While the view moves: hidden, so every frame shows cosmos.gl's own canvas
   *  under it, never a copied frame that is late or empty. */
  hold() {
    if (this.held) return
    this.held = true
    this.clear()
  }

  /** The view settled: the next capture shows the layer again. */
  release() { this.held = false }

  /** Keep the frame cosmos.gl just drew (call before the browser shows it). */
  capture(source: HTMLCanvasElement) {
    const { gl } = this
    if (this.held) return
    if (!source.width || !source.height) { this.clear(); return }
    if (this.canvas.width !== source.width || this.canvas.height !== source.height) {
      this.canvas.width = source.width; this.canvas.height = source.height
    }
    gl.bindTexture(gl.TEXTURE_2D, this.texture)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source)
    this.hasFrame = true
  }

  /** One quad: the kept frame with the breath wave at `p`. */
  draw(p: BreathParams) {
    if (!this.hasFrame) return
    const { gl } = this
    gl.viewport(0, 0, this.canvas.width, this.canvas.height)
    gl.uniform4f(this.at.wave, p.amp, p.phase, p.cx, p.cy)
    gl.uniform4f(this.at.frame, p.view.x0, p.view.y0, p.view.w, p.view.h)
    gl.uniform1f(this.at.perUnit, p.perUnit)
    gl.uniform3f(this.at.background, p.background[0], p.background[1], p.background[2])
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
    // An unpatched cosmos.gl never captures: leave its own canvas visible then.
    this.canvas.style.visibility = 'visible'
  }

  dispose() {
    this.canvas.remove()
    this.gl.getExtension('WEBGL_lose_context')?.loseContext()
  }
}
