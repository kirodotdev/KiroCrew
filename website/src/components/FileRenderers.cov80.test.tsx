import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import {
  ImageViewer,
  SvgViewer,
  CsvViewer,
  JsonViewer,
  JsonlViewer,
  HtmlViewer,
  PdfViewer,
} from './FileRenderers'
import { i18nT } from '../i18n/t'

describe('ImageViewer', () => {
  it('fetches the raw bytes by encoded path and names the image by basename', () => {
    render(<ImageViewer filePath="/zzq dir/pic name.png" />)
    const img = screen.getByAltText('pic name.png') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('/api/file-raw?path=%2Fzzq%20dir%2Fpic%20name.png')
    expect(img.getAttribute('draggable')).toBe('false')
  })
})

describe('SvgViewer', () => {
  it('keeps ordinary SVG markup and strips a script element', () => {
    const { container } = render(
      <SvgViewer content={'<svg><circle r="4" /><script>zzqPwn()</script></svg>'} />,
    )
    expect(container.querySelector('circle')).toBeTruthy()
    expect(container.querySelector('script')).toBeNull()
    expect(container.innerHTML).not.toContain('zzqPwn')
  })
})

describe('CsvViewer', () => {
  it('renders a header row plus body cells', () => {
    render(<CsvViewer filePath="/zzq.csv" content={'a,b\n1,2\n3,4\n'} />)
    expect(screen.getByRole('columnheader', { name: 'a' })).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(3)
    expect(screen.getByRole('cell', { name: '4' })).toBeInTheDocument()
  })

  it('splits on tabs for a .tsv file', () => {
    render(<CsvViewer filePath="/zzq.tsv" content={'h1\th2\nv1\tv2'} />)
    expect(screen.getByRole('columnheader', { name: 'h2' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'v2' })).toBeInTheDocument()
  })

  it('honours quoting: a quoted delimiter stays in the cell and "" is one quote', () => {
    render(<CsvViewer filePath="/zzq.csv" content={'h\n"a,b"\n"say ""hi"""'} />)
    expect(screen.getByRole('cell', { name: 'a,b' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'say "hi"' })).toBeInTheDocument()
  })

  it('reports an empty file instead of an empty table', () => {
    render(<CsvViewer filePath="/zzq.csv" content={'\n  \n'} />)
    expect(screen.getByText(i18nT('components.fileRenderers.empty_file'))).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('caps the body at 500 rows and says how many there really are', () => {
    const content = ['h', ...Array.from({ length: 512 }, (_, i) => `r${i}`)].join('\n')
    render(<CsvViewer filePath="/zzq.csv" content={content} />)
    // 500 body rows + the header row
    expect(screen.getAllByRole('row')).toHaveLength(501)
    expect(screen.getByText(/512/)).toBeInTheDocument()
    expect(screen.queryByRole('cell', { name: 'r511' })).not.toBeInTheDocument()
  })
})

describe('JsonViewer', () => {
  it('renders every scalar leaf type', () => {
    render(<JsonViewer content={'{"n":null,"b":true,"i":42,"s":"zzq"}'} />)
    expect(screen.getByText(i18nT('components.fileRenderers.null'))).toBeInTheDocument()
    expect(screen.getByText('true')).toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
    expect(screen.getByText('"zzq"')).toBeInTheDocument()
  })

  it('truncates a long string leaf and marks it with an ellipsis outside the quotes', () => {
    render(<JsonViewer content={JSON.stringify({ s: 'z'.repeat(240) })} />)
    // the ellipsis sits OUTSIDE the quotes, in the same span as the value
    const leaf = screen.getByText(/^"z{200}"…$/)
    expect(leaf).toBeInTheDocument()
  })

  it('collapses and re-expands a nested node', () => {
    render(<JsonViewer content={'{"deep":{"deeper":{"leaf":1}}}'} />)
    // depth >= 2 starts collapsed, so the leaf is hidden behind a count summary
    expect(screen.queryByText('1')).not.toBeInTheDocument()
    const collapsed = screen.getByText(new RegExp(`1 ${i18nT('components.fileRenderers.items')}`))
    fireEvent.click(collapsed.closest('button')!)
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('caps an array at 200 entries and reports the remainder', () => {
    render(<JsonViewer content={JSON.stringify(Array.from({ length: 231 }, (_, i) => i))} />)
    expect(screen.getByText('199')).toBeInTheDocument()
    expect(screen.queryByText('200')).not.toBeInTheDocument()
    expect(
      screen.getByText(new RegExp(`31 ${i18nT('components.fileRenderers.more')}`)),
    ).toBeInTheDocument()
  })

  it('surfaces the parse error with a raw preview for invalid JSON', () => {
    render(<JsonViewer content={'{zzq'} />)
    expect(
      screen.getByText(new RegExp(i18nT('components.fileRenderers.invalid_json'))),
    ).toBeInTheDocument()
    expect(screen.getByText(/\{zzq/)).toBeInTheDocument()
  })

  it('marks the preview as truncated past 2000 characters', () => {
    render(<JsonViewer content={'{'.repeat(2400)} />)
    const shown = screen.getByText(
      i18nT('components.fileRenderers.showing_raw_content_truncated_count', {
        count: 2400,
        shown: 2000,
      }),
    )
    expect(shown).toBeInTheDocument()
  })
})

describe('JsonlViewer', () => {
  function jsonl(n: number) {
    return Array.from({ length: n }, (_, i) => JSON.stringify({ i })).join('\n')
  }

  it('pages in the next 100 lines when the scroll reaches the bottom', () => {
    const { container } = render(<JsonlViewer content={jsonl(150)} />)
    const scroller = container.firstChild as HTMLDivElement

    expect(
      screen.getByText(i18nT('components.fileRenderers.scroll_for_more_count', { count: 50 })),
    ).toBeInTheDocument()

    Object.defineProperty(scroller, 'scrollHeight', { value: 1000, configurable: true })
    Object.defineProperty(scroller, 'clientHeight', { value: 400, configurable: true })
    scroller.scrollTop = 600
    fireEvent.scroll(scroller)

    expect(
      screen.queryByText(i18nT('components.fileRenderers.scroll_for_more_count', { count: 50 })),
    ).not.toBeInTheDocument()
  })

  it('ignores a scroll that has not reached the bottom', () => {
    const { container } = render(<JsonlViewer content={jsonl(150)} />)
    const scroller = container.firstChild as HTMLDivElement
    Object.defineProperty(scroller, 'scrollHeight', { value: 1000, configurable: true })
    Object.defineProperty(scroller, 'clientHeight', { value: 100, configurable: true })
    scroller.scrollTop = 10
    fireEvent.scroll(scroller)

    expect(
      screen.getByText(i18nT('components.fileRenderers.scroll_for_more_count', { count: 50 })),
    ).toBeInTheDocument()
  })

  it('does not offer paging when everything already fits', () => {
    const { container } = render(<JsonlViewer content={jsonl(3)} />)
    const scroller = container.firstChild as HTMLDivElement
    fireEvent.scroll(scroller)
    expect(screen.getByText(`3 ${i18nT('components.fileRenderers.lines')}`)).toBeInTheDocument()
  })
})

describe('HtmlViewer', () => {
  it('sandboxes the preview iframe with no allowances', () => {
    render(<HtmlViewer content={'<p>zzq body</p>'} />)
    const frame = screen.getByTitle(
      i18nT('components.fileRenderers.html_preview'),
    ) as HTMLIFrameElement
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('srcdoc')).toBe('<p>zzq body</p>')
  })
})

describe('PdfViewer', () => {
  it('embeds the raw url and opens the same url in a new tab on request', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    render(<PdfViewer filePath="/zzq dir/a.pdf" />)

    const url = '/api/file-raw?path=%2Fzzq%20dir%2Fa.pdf'
    expect(
      screen.getByTitle(i18nT('components.fileRenderers.pdf_preview')).getAttribute('src'),
    ).toBe(url)

    fireEvent.click(
      screen.getByRole('button', { name: i18nT('components.fileRenderers.open_in_new_tab') }),
    )
    expect(open).toHaveBeenCalledWith(url, '_blank')
    open.mockRestore()
  })
})
