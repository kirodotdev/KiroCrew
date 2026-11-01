import { describe, it, expect } from 'vitest'
import { boxBelongsToImage, boxMatchesImage, boxPositionStyle, boxRectOf, dragToImageBox, fileNameOf, imageAnchorOf, imageUseAt, parseImageBoxDraft, pictureRect, popoverBesideBox } from '../utils/imageComments'
import { formatCommentsMessage } from '../utils/commentsMessage.prompt'
import { loadCommentDrafts, saveCommentDrafts, setCommentsForFile, COMMENT_DRAFTS_KEY } from '../utils/commentDrafts'
import type { InlineComment } from '../components/CommentOverlay'

const RECT = { left: 100, top: 50, width: 400, height: 200 }

describe('dragToImageBox', () => {
  it('turns two corners into a box of fractions, whichever corner comes first', () => {
    const forward = dragToImageBox({ x: 200, y: 100 }, { x: 300, y: 150 }, RECT)
    const backward = dragToImageBox({ x: 300, y: 150 }, { x: 200, y: 100 }, RECT)
    expect(forward).toEqual({ x: 0.25, y: 0.25, w: 0.25, h: 0.25 })
    expect(backward).toEqual(forward)
  })

  it('clamps a drag that leaves the image to its edge', () => {
    expect(dragToImageBox({ x: 400, y: 200 }, { x: 900, y: 900 }, RECT)).toEqual({ x: 0.75, y: 0.75, w: 0.25, h: 0.25 })
    expect(dragToImageBox({ x: 0, y: 0 }, { x: 200, y: 100 }, RECT)).toEqual({ x: 0, y: 0, w: 0.25, h: 0.25 })
  })

  it('has no box on an image with no area', () => {
    expect(dragToImageBox({ x: 0, y: 0 }, { x: 10, y: 10 }, { ...RECT, width: 0 })).toBeNull()
  })
})

describe('popoverBesideBox', () => {
  const SIZE = { width: 150, height: 40 }
  const VIEW = { width: 1000, height: 800 }

  it('puts the comment to the right of its box, level with its top', () => {
    expect(popoverBesideBox({ left: 100, top: 100, width: 200, height: 100 }, SIZE, VIEW)).toEqual({ left: 308, top: 100 })
  })

  it('goes to the left when the right has no room', () => {
    expect(popoverBesideBox({ left: 700, top: 100, width: 250, height: 100 }, SIZE, VIEW)).toEqual({ left: 542, top: 100 })
  })

  it('goes below when neither side has room', () => {
    expect(popoverBesideBox({ left: 20, top: 100, width: 960, height: 200 }, SIZE, VIEW)).toEqual({ left: 20, top: 308 })
  })

  it('keeps a side placement inside the window', () => {
    expect(popoverBesideBox({ left: 100, top: 780, width: 100, height: 10 }, SIZE, VIEW)).toEqual({ left: 208, top: 754 })
  })

  it('has no place for a box that fills the window', () => {
    expect(popoverBesideBox({ left: 0, top: 0, width: 1000, height: 800 }, SIZE, VIEW)).toBeNull()
  })
})

describe('pictureRect', () => {
  const EL = { left: 100, top: 50, width: 400, height: 200 }
  it('is the element itself when the picture fills it', () => {
    expect(pictureRect(EL, { width: 800, height: 400 }, 'contain')).toEqual(EL)
  })
  it('drops the side bands of a square SVG held at a fixed width under a height cap', () => {
    expect(pictureRect(EL, { width: 100, height: 100 }, 'contain')).toEqual({ left: 200, top: 50, width: 200, height: 200 })
  })
  it('drops the top and bottom bands of a wide picture', () => {
    expect(pictureRect(EL, { width: 400, height: 100 }, 'contain')).toEqual({ left: 100, top: 100, width: 400, height: 100 })
  })
  it('does not upscale under scale-down', () => {
    expect(pictureRect(EL, { width: 50, height: 50 }, 'scale-down')).toEqual({ left: 275, top: 125, width: 50, height: 50 })
  })
  it('is the element before the picture loads, or when the fit does not letterbox', () => {
    expect(pictureRect(EL, { width: 0, height: 0 }, 'contain')).toEqual(EL)
    expect(pictureRect(EL, { width: 100, height: 100 }, 'fill')).toEqual(EL)
  })
})

describe('boxPositionStyle and boxMatchesImage', () => {
  it('positions a box by rounded percentages', () => {
    expect(boxPositionStyle({ x: 0.328, y: 0.1, w: 0.2, h: 0.05 })).toEqual({ left: '32.8%', top: '10%', width: '20%', height: '5%' })
  })

  it('separates two uses of one image by line and column', () => {
    expect(boxMatchesImage({ src: 'a.png', line: 3, column: 1 }, 'a.png', 3, 1)).toBe(true)
    expect(boxMatchesImage({ src: 'a.png', line: 3, column: 1 }, 'a.png', 3, 20)).toBe(false)
    expect(boxMatchesImage({ src: 'a.png', line: 3 }, 'a.png', 9, 1)).toBe(false)
    expect(boxMatchesImage({ src: 'a.png' }, 'b.png', undefined, undefined)).toBe(false)
  })

  it('keeps only the four box fields of a placement', () => {
    expect(boxRectOf({ x: 0.1, y: 0.2, w: 0.3, h: 0.4, ...{ src: 'a.png', clientX: 5 } } as never)).toEqual({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 })
  })
})

describe('boxBelongsToImage', () => {
  const box = { src: 'a.png', line: 5, column: 1 }

  it('follows its image to another line when that image is the only one with its src', () => {
    expect(boxBelongsToImage(box, 'a.png', { line: 9, column: 1 }, [{ line: 9, column: 1 }])).toBe(true)
    expect(boxBelongsToImage(box, 'b.png', { line: 9, column: 1 }, [{ line: 9, column: 1 }])).toBe(false)
  })

  it('picks the nearest of several uses of one image, as a text comment picks the nearest repeat', () => {
    // Drawn on the use at line 5; an edit above moved the uses from 5 and 20 to 8 and 23.
    const spots = [{ line: 8, column: 1 }, { line: 23, column: 1 }]
    expect(boxBelongsToImage(box, 'a.png', spots[0], spots)).toBe(true)
    expect(boxBelongsToImage(box, 'a.png', spots[1], spots)).toBe(false)
    // Two uses on one line: the column decides.
    const sameLine = [{ line: 5, column: 1 }, { line: 5, column: 30 }]
    expect(boxBelongsToImage({ src: 'a.png', line: 5, column: 30 }, 'a.png', sameLine[1], sameLine)).toBe(true)
    expect(boxBelongsToImage({ src: 'a.png', line: 5, column: 30 }, 'a.png', sameLine[0], sameLine)).toBe(false)
  })

  it('gives a tie to the earlier use, so a box is drawn on one image only', () => {
    const spots = [{ line: 3, column: 1 }, { line: 7, column: 1 }]
    expect(boxBelongsToImage(box, 'a.png', spots[0], spots)).toBe(true)
    expect(boxBelongsToImage(box, 'a.png', spots[1], spots)).toBe(false)
  })

  it('counts the image asking even before it is in the list', () => {
    expect(boxBelongsToImage(box, 'a.png', { line: 9, column: 1 }, [])).toBe(true)
  })

  it('stays on the same use while the number of uses is unchanged, however far the lines move', () => {
    // Drawn on the 1st of 2 uses at line 5; four lines inserted above put the
    // 2nd use on line 5, which nearest-line alone would pick.
    const drawn = { src: 'a.png', line: 5, column: 1, nth: 0, uses: 2 }
    const spots = [{ line: 1, column: 1 }, { line: 5, column: 1 }]
    expect(boxBelongsToImage(drawn, 'a.png', spots[0], spots)).toBe(true)
    expect(boxBelongsToImage(drawn, 'a.png', spots[1], spots)).toBe(false)
    // A third use added: the count no longer matches, so the nearest line decides.
    const three = [...spots, { line: 9, column: 1 }]
    expect(boxBelongsToImage(drawn, 'a.png', three[1], three)).toBe(true)
  })

  it('names the use an image is, counting each spot once', () => {
    const spots = [{ line: 9, column: 1 }, { line: 3, column: 1 }, { line: 9, column: 1 }]
    expect(imageUseAt({ line: 9, column: 1 }, spots)).toEqual({ nth: 1, uses: 2 })
    expect(imageUseAt({ line: 3, column: 1 }, [])).toEqual({ nth: 0, uses: 1 })
    expect(imageUseAt({}, spots)).toBeNull()
  })

  it('matches on what is known when either side has no line', () => {
    expect(boxBelongsToImage({ src: 'a.png' }, 'a.png', { line: 9, column: 1 }, [{ line: 9, column: 1 }])).toBe(true)
    expect(boxBelongsToImage(box, 'a.png', {}, [])).toBe(true)
  })
})

const imageComment = (id: string, text: string, image: Partial<NonNullable<InlineComment['image']>> = {}, line = 5): InlineComment => ({
  id,
  anchor: 'Shot',
  text,
  line,
  image: { src: 'shots/a.png', path: '/notes/shots/a.png', x: 0.12, y: 0.3, w: 0.25, h: 0.1, ...image },
})

describe('formatCommentsMessage with image boxes', () => {
  it('names the loaded image and the box, after a line saying how to read a box', () => {
    const lines = formatCommentsMessage('/notes/doc.md', [imageComment('1', 'too tight')]).split('\n')
    expect(lines).toContain('1. (line 5, image "/notes/shots/a.png", box x=0.120, y=0.300, w=0.250, h=0.100): "too tight"')
    expect(lines[2]).toBe('An image comment names a box on the image as fractions of its width and height: x and y are the top-left corner, w and h the size. An image that was attached shows as an image marker in place of its path; one still named by its path was not attached, so open it to see the box.')
  })

  it('names the destination as written when the preview had no file for it', () => {
    const text = formatCommentsMessage('/notes/doc.md', [imageComment('1', 'here', { src: 'https://x.invalid/r.png', path: undefined })])
    expect(text).toContain('1. (line 5, image "https://x.invalid/r.png", box')
  })

  it('adds no box line to a message of text comments only', () => {
    const text = formatCommentsMessage('/notes/doc.md', [{ id: 't', anchor: 'hello', text: 'fix', line: 1, column: 1 }])
    expect(text).not.toContain('An image comment')
  })

  it('opens with the feedback header, with no picture lines ahead of it', () => {
    const text = formatCommentsMessage('/notes/doc.md', [imageComment('1', 'here')])
    expect(text.startsWith('[Document feedback on /notes/doc.md')).toBe(true)
    expect(text).not.toContain('![image]')
  })

  it('escapes a quote in the image path', () => {
    expect(formatCommentsMessage('/notes/doc.md', [imageComment('1', 'x', { path: '/notes/a"b.png' })])).toContain('image "/notes/a\\"b.png"')
  })

  it('names a Windows image path as written, so the image still attaches', () => {
    // Windows paths cannot hold a quote, so nothing breaks out of the quoting,
    // and doubling the backslashes would name a path the attachment scan
    // then reads as written.
    const text = formatCommentsMessage('C:\\notes\\doc.md', [imageComment('1', 'x', { path: 'C:\\notes\\shots\\a.png' })])
    expect(text).toContain('image "C:\\notes\\shots\\a.png", box')
  })
})

describe('fileNameOf', () => {
  it('takes the last path segment, without query or fragment', () => {
    expect(fileNameOf('/notes/shots/a%20b.png?raw=1#top')).toBe('a b.png')
  })

  it('splits on a backslash only in a Windows-shaped path, as pathBasename does', () => {
    expect(fileNameOf('/notes/we\\ird.png')).toBe('we\\ird.png')
    expect(fileNameOf('C:\\notes\\shots\\a.png')).toBe('a.png')
  })
})

describe('imageAnchorOf', () => {
  it('uses the alt text, trimmed', () => {
    expect(imageAnchorOf(' Schedule page ', '/d/a.png')).toBe('Schedule page')
  })

  it('falls back to the file name when the alt text is missing or only spaces', () => {
    expect(imageAnchorOf(undefined, '/d/a.png')).toBe('a.png')
    expect(imageAnchorOf('   ', '/d/a.png')).toBe('a.png')
  })
})

describe('comment drafts with image boxes', () => {
  const roundTrip = (comments: unknown[]) => {
    localStorage.clear()
    localStorage.setItem(COMMENT_DRAFTS_KEY, JSON.stringify({ '/d.md': comments }))
    return loadCommentDrafts()['/d.md'] ?? null
  }

  it('keeps a box inside its image, as a copy', () => {
    const c = imageComment('1', 'here', { x: 0.7, w: 0.3 })
    const drafts = {}
    setCommentsForFile(drafts, '/d.md', [c])
    saveCommentDrafts(drafts)
    const loaded = loadCommentDrafts()['/d.md']
    expect(loaded?.[0].image).toEqual(c.image)
    expect(loaded?.[0].image).not.toBe(c.image)
  })

  it.each([
    ['no width', { w: 0 }],
    ['past the right edge', { x: 0.9, w: 0.2 }],
    ['past the bottom edge', { y: 0.95, h: 0.1 }],
    ['a negative corner', { x: -0.1 }],
    ['no height', { h: undefined }],
    ['a use with no count', { nth: 0 }],
    ['a use past the count', { nth: 2, uses: 2 }],
    ['a fractional use', { nth: 0.5, uses: 2 }],
  ])('drops a batch whose box has %s', (_name, bad) => {
    expect(roundTrip([{ id: 't', anchor: 'a', text: 'ok' }, imageComment('1', 'x', bad as never)])).toBeNull()
  })

  it('keeps which use of the file a box is on', () => {
    expect(roundTrip([imageComment('1', 'x', { nth: 1, uses: 2 })])?.[0].image).toMatchObject({ nth: 1, uses: 2 })
  })
})

describe('parseImageBoxDraft', () => {
  const draft = (extra: Record<string, unknown>) => JSON.stringify({ src: 'a.png', text: 'here', x: 0.1, y: 0.1, w: 0.2, h: 0.2, ...extra })

  it('keeps a box draft on a use that exists', () => {
    expect(parseImageBoxDraft(draft({ nth: 1, uses: 2 }))).toMatchObject({ nth: 1, uses: 2 })
    expect(parseImageBoxDraft(draft({}))).toMatchObject({ src: 'a.png' })
  })

  // The pending-comment check in commentDrafts refuses the same cases, and a
  // use past the count would crash the preview when the draft is restored.
  it.each([
    ['a use past the count', { nth: 4, uses: 1 }],
    ['a use equal to the count', { nth: 2, uses: 2 }],
    ['a use with no count', { nth: 0 }],
    ['a count with no use', { uses: 2 }],
  ])('refuses a box draft with %s', (_name, bad) => {
    expect(parseImageBoxDraft(draft(bad))).toBeNull()
  })
})
