import { getConfig } from '@testing-library/react'

/**
 * A named lost-run ceiling for waitFor/findBy (website/docs/testing.md). A wait
 * that runs out fails BY NAME and says how long it really waited, so a starved
 * runner reads as "PANE_READY ran out (ceiling_ms=5000, elapsed_ms=5310)" rather
 * than as a missing element. The unit rides in the key names because the i18n
 * unit-literal gate refuses a number glued to a unit word. Testing Library reads
 * `timeout` once, as a wait starts, which is when the elapsed time starts. Pass
 * it as waitFor's options, or as a findBy's THIRD argument: the second is
 * matcher options, and a timeout there is ignored.
 */
export function namedCeiling(name: string, timeout: number) {
  let startedAt = 0
  return {
    get timeout() {
      startedAt = performance.now()
      return timeout
    },
    onTimeout: (error: Error) =>
      getConfig().getElementError(
        `${name} ran out (ceiling_ms=${timeout}, elapsed_ms=${Math.round(performance.now() - startedAt)}): ${error.message}`,
        document.body,
      ),
  }
}
