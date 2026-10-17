/**
 * Capture-only stand-in for `MarkdownRenderer` (issue #10620, residual 3).
 *
 * The scene under capture is "the renderer crashed on one item": the case the
 * per-item `MessageErrorBoundary` exists for. The real renderer has no known
 * crash on current main (the nesting bound in #10753 closed the recorded ones),
 * and a security follow-up must not publish a new one. So the capture harness
 * aliases the renderer module to this file, which forwards every message to the
 * REAL renderer and throws only on one marker string -- the same stand-in the
 * regression test uses. Everything else in the frame (the surface, the boundary,
 * the fallback, theme tokens, i18n) is the production code.
 *
 * Only `scripts/capture-bare-markdown-error-boundary.mjs` wires this alias; the
 * app bundle never resolves here.
 */
// Extension spelled out so this specifier does not match the alias that routes
// `../components/MarkdownRenderer` here (that would loop back into this file).
import RealMarkdownRenderer from '/src/components/MarkdownRenderer.tsx'
export * from '/src/components/MarkdownRenderer.tsx'

export const CRASH_MARKER = '__capture_crash_marker__'

type Props = Parameters<typeof RealMarkdownRenderer>[0]

export default function CrashingMarkdownRenderer(props: Props) {
  if (typeof props.content === 'string' && props.content.includes(CRASH_MARKER)) {
    throw new Error('simulated markdown render crash (capture stand-in)')
  }
  return <RealMarkdownRenderer {...props} />
}
