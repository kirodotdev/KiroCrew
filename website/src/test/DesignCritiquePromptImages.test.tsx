import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { IMAGES_PROMPT } from '../apps/design-critique/prompts'

/**
 * Design Critique's prompt is an ordinary chat message — `designCritiqueApi.send`
 * POSTs it to `/api/chat` — so the screens it references reach the transcript in
 * the same `![alt](dest)` wire format every other attachment uses, and are drawn
 * by this same `MarkdownRenderer`.
 *
 * `MarkdownRenderer.windowsImagePath.test.tsx` pins that chain for
 * `prepareSendPayload` (issue #3497). These cases pin it for `IMAGES_PROMPT`,
 * which was the one producer of that format not going through `mdImageDest`:
 *
 *  - a drive path's separators are backslashes, and CommonMark drops a backslash
 *    before ASCII punctuation, so `\.kiro\` parsed as `.kiro` and the `<img>`
 *    named a path one directory short of the file;
 *  - a bare destination ends at the first space, so a home directory with a
 *    space in it (`C:\Users\John Doe\…`) produced no `<img>` at all — the line
 *    stayed in the bubble as literal markdown text.
 *
 * The upload endpoint returns a server-native absolute path
 * (`api_upload_file` -> `str(dest)` under `data_home()/uploads`), so this is the
 * shape the app actually hands `IMAGES_PROMPT`.
 */

function imgs(container: HTMLElement): string[] {
  return [...container.querySelectorAll('img')].map((i) => i.getAttribute('src') || '')
}

function fileRaw(path: string): string {
  return `/api/file-raw?path=${encodeURIComponent(path)}`
}

describe('design critique prompt screens render as images', () => {
  it('a Windows drive path reaches file-raw as the file it names', () => {
    const prompt = IMAGES_PROMPT(['C:\\Users\\me\\.kiro\\crew\\uploads\\shot.png'])
    const { container } = render(<MarkdownRenderer content={prompt} />)
    expect(imgs(container)).toEqual([fileRaw('C:/Users/me/.kiro/crew/uploads/shot.png')])
  })

  it('a home directory with a space still renders a screen', () => {
    const prompt = IMAGES_PROMPT(['C:\\Users\\John Doe\\.kiro\\crew\\uploads\\shot.png'])
    const { container } = render(<MarkdownRenderer content={prompt} />)
    expect(imgs(container)).toEqual([fileRaw('C:/Users/John Doe/.kiro/crew/uploads/shot.png')])
  })

  it('a POSIX path is unchanged', () => {
    const prompt = IMAGES_PROMPT(['/home/me/.kiro/crew/uploads/shot.png'])
    const { container } = render(<MarkdownRenderer content={prompt} />)
    expect(imgs(container)).toEqual([fileRaw('/home/me/.kiro/crew/uploads/shot.png')])
  })

  it('a flow renders every screen, in the order it was given', () => {
    const paths = [
      'C:\\Users\\me\\.kiro\\crew\\uploads\\1-cart.png',
      'C:\\Users\\me\\.kiro\\crew\\uploads\\2-pay.png',
    ]
    const { container } = render(<MarkdownRenderer content={IMAGES_PROMPT(paths)} />)
    expect(imgs(container)).toEqual([
      fileRaw('C:/Users/me/.kiro/crew/uploads/1-cart.png'),
      fileRaw('C:/Users/me/.kiro/crew/uploads/2-pay.png'),
    ])
  })

  it('the "screens" instruction still carries the paths the backend gave us', () => {
    // The markdown destination is ENCODED for markdown; the JSON list the model
    // is told to echo back is not, and must stay byte-identical to what the
    // upload endpoint returned. `resolveScreens` pairs the report to the
    // uploads by index, so nothing here is a matching key -- but the model is
    // asked for "the absolute image path", and that is this list.
    const paths = ['C:\\Users\\me\\.kiro\\crew\\uploads\\shot.png']
    expect(IMAGES_PROMPT(paths)).toContain(JSON.stringify(paths))
  })
})
