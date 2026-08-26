import { describe, it, expect, vi, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { StrictMode } from 'react'
import Strands from '../components/Strands'
import { createAudioSample } from '../hooks/mic'

/**
 * The app mounts under StrictMode (main.tsx), which runs an effect's
 * setup -> cleanup -> setup on the SAME mounted node. A lost WebGL context
 * stays lost for the life of the canvas and getContext() returns that same dead
 * context, so calling WEBGL_lose_context.loseContext() in cleanup made the
 * second setup fail to compile and the panel render blank in development.
 */

const loseContext = vi.fn()

function stubGl() {
  const gl = {
    VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4,
    ARRAY_BUFFER: 5, FLOAT: 6, STATIC_DRAW: 7, BLEND: 8, ONE: 9,
    ONE_MINUS_SRC_ALPHA: 10, COLOR_BUFFER_BIT: 11, TRIANGLES: 12,
    createShader: () => ({}), shaderSource: vi.fn(), compileShader: vi.fn(),
    getShaderParameter: () => true, deleteShader: vi.fn(),
    createProgram: () => ({}), attachShader: vi.fn(), linkProgram: vi.fn(),
    getProgramParameter: () => true, deleteProgram: vi.fn(),
    createVertexArray: () => ({}), bindVertexArray: vi.fn(), deleteVertexArray: vi.fn(),
    createBuffer: () => ({}), bindBuffer: vi.fn(), bufferData: vi.fn(), deleteBuffer: vi.fn(),
    getAttribLocation: () => 0, enableVertexAttribArray: vi.fn(), vertexAttribPointer: vi.fn(),
    getUniformLocation: () => ({}), useProgram: vi.fn(),
    uniform1f: vi.fn(), uniform1i: vi.fn(), uniform2f: vi.fn(), uniform3fv: vi.fn(),
    clearColor: vi.fn(), enable: vi.fn(), blendFunc: vi.fn(), viewport: vi.fn(),
    clear: vi.fn(), drawArrays: vi.fn(),
    getExtension: (name: string) => (name === 'WEBGL_lose_context' ? { loseContext } : null),
  }
  return gl
}

afterEach(() => { vi.restoreAllMocks(); loseContext.mockClear() })

describe('Strands under StrictMode', () => {
  it('never force-loses the GL context on cleanup', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
      () => stubGl() as unknown as RenderingContext,
    )
    vi.stubGlobal('ResizeObserver', class {
      observe() {} unobserve() {} disconnect() {}
    })
    const sampleRef = { current: createAudioSample() }
    render(
      <StrictMode>
        <Strands sampleRef={sampleRef} />
      </StrictMode>,
    )
    // A single loseContext() call would leave the remount with a dead context.
    expect(loseContext).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('releases the resources it allocated', () => {
    const gl = stubGl()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
      () => gl as unknown as RenderingContext,
    )
    vi.stubGlobal('ResizeObserver', class {
      observe() {} unobserve() {} disconnect() {}
    })
    const sampleRef = { current: createAudioSample() }
    const { unmount } = render(<Strands sampleRef={sampleRef} />)
    unmount()
    // Dropping loseContext() must not mean leaking the objects themselves.
    expect(gl.deleteProgram).toHaveBeenCalled()
    expect(gl.deleteBuffer).toHaveBeenCalled()
    expect(gl.deleteVertexArray).toHaveBeenCalled()
    vi.unstubAllGlobals()
  })
})
