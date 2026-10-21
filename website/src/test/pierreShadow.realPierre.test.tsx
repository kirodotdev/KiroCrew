import { beforeAll, describe, it, expect } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { CodeBlock } from '../components/CodeBlock'
import { codePassagePosition, codeShadowRoots, matchRanges, searchRuns } from '../utils/pierreShadow'

// Warm Pierre's lazy chunk once so no test pays the module load inside its wait.
beforeAll(() => import('../pierre/PierreImpl'))

const SOURCE = ['mycli rm NAME', 'mycli ls']
const asText = (runs: Text[][]) => runs.map(run => run.map(node => node.data).join(''))

/** Renders a real highlighted block and waits until its shown shadow root holds every source line. */
async function renderBlock() {
  const { container } = render(<CodeBlock code={SOURCE.join('\n')} lang="bash" complete />)
  // happy-dom lays nothing out, so the highlighted copy is shown at WarmSwap's
  // 2.5 s deadline rather than on its first paint. The ceiling leaves headroom
  // for a loaded worker and stays under the suite's 15 s test timeout.
  await waitFor(
    () => expect(codeShadowRoots(container).map(root => root.querySelectorAll('[data-line]').length)).toEqual([SOURCE.length]),
    { timeout: 10_000 },
  )
  return { container, root: codeShadowRoots(container)[0] }
}

/** A range from character `column` of `row` to the row's end, over its own text nodes. */
function rangeFrom(row: Element, column: number): Range {
  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text)
  let i = 0
  while (column >= nodes[i].length) column -= nodes[i++].length
  const last = nodes[nodes.length - 1]
  const range = document.createRange()
  range.setStart(nodes[i], column)
  range.setEnd(last, last.length)
  return range
}

describe('pierreShadow against a real Pierre code block', () => {
  it('draws one [data-line] row per source line, holding exactly its text', async () => {
    const { root } = await renderBlock()
    const rows = [...root.querySelectorAll('[data-line]')]
    expect(rows.map(row => [row.getAttribute('data-line'), row.textContent])).toEqual([['1', SOURCE[0]], ['2', SOURCE[1]]])
  })

  it('searches each row as one run and finds a phrase inside it', async () => {
    const { container, root } = await renderBlock()
    const runs = searchRuns(container)
    expect(asText(runs.filter(run => run[0]?.getRootNode() === root))).toEqual(SOURCE)
    const matches = matchRanges(runs, /rm NAME/g)
    expect(matches.map(range => range.toString())).toEqual(['rm NAME'])
  })

  it('keys a selection by its block, source line and column', async () => {
    const { container, root } = await renderBlock()
    const range = rangeFrom(root.querySelectorAll('[data-line]')[1], 'mycli '.length)
    expect(range.toString()).toBe('ls')
    expect(codePassagePosition(container, range)).toBe(200_006) // line 2, column 6
  })
})
