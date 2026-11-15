import { spawnSync } from 'node:child_process'

/**
 * How long one gate script may run. The slowest takes about a minute on a loaded
 * machine, so one still running at five is hung; killing it ends the step with the
 * script named, instead of silence until the CI job's own time cap.
 */
export const SCRIPT_TIMEOUT_MS = 5 * 60 * 1000

/**
 * Run one gate script with this node. Returns `{ status, text }` with every byte it
 * printed, or `{ fatal }` when there is no exit code to judge (not started, killed,
 * timed out, output overflowed). SIGKILL, because a child that ignores SIGTERM would
 * keep `spawnSync` waiting.
 */
export function runScript(argv, { cwd, timeoutMs = SCRIPT_TIMEOUT_MS }) {
  const res = spawnSync(process.execPath, argv, {
    cwd,
    encoding: 'utf-8',
    env: process.env,
    maxBuffer: 256 * 1024 * 1024,
    timeout: timeoutMs,
    killSignal: 'SIGKILL',
  })
  if (res.error?.code === 'ETIMEDOUT') {
    return { fatal: `still running after ${timeoutMs / 1000} s, so it was killed` }
  }
  if (res.error || res.status === null) {
    return { fatal: res.error?.message || `killed by ${res.signal}` }
  }
  return { status: res.status, text: `${res.stdout || ''}${res.stderr || ''}` }
}
