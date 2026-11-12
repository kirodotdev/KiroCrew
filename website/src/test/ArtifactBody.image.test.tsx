import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { Artifact } from '../types'
import { ArtifactBodyImage } from '../components/ArtifactBody'

// The image body streams bytes from the artifact's asset endpoint (server sets
// Content-Type) — never base64 in JSON — and offers a Download control pointing
// at that same URL. These tests pin: the <img> and download anchor both target
// /api/artifacts/<slug>/asset, the download filename falls back sensibly, alt
// text degrades to the artifact name, and NO editor / iframe is rendered.

const SLUG = 'sunset-photo'
const ASSET_URL = `/api/artifacts/${SLUG}/asset`

function makeImageArtifact(overrides: Partial<Artifact> = {}): Artifact {
  return {
    slug: SLUG,
    name: 'Sunset Photo',
    kind: 'image',
    source: 'chat',
    description: '',
    tags: [],
    version: 1,
    created_at: '2026-05-21T22:00:00.000000+00:00',
    updated_at: '2026-05-21T22:00:00.000000+00:00',
    ...overrides,
  } as Artifact
}

describe('ArtifactBodyImage', () => {
  it('renders an <img> streamed from the asset URL', () => {
    render(<ArtifactBodyImage artifact={makeImageArtifact()} slug={SLUG} />)
    const img = document.querySelector('img')
    expect(img).not.toBeNull()
    expect(img?.getAttribute('src')).toBe(ASSET_URL)
  })

  it('exposes a Download control pointing at the same asset URL', () => {
    render(<ArtifactBodyImage artifact={makeImageArtifact()} slug={SLUG} />)
    const anchor = document.querySelector('a[download]') as HTMLAnchorElement | null
    expect(anchor).not.toBeNull()
    expect(anchor?.getAttribute('href')).toBe(ASSET_URL)
  })

  it('names the download from original_filename when present', () => {
    const art = makeImageArtifact({
      image: { mime: 'image/jpeg', ext: 'jpg', original_filename: 'holiday.jpg' },
    })
    render(<ArtifactBodyImage artifact={art} slug={SLUG} />)
    const anchor = document.querySelector('a[download]') as HTMLAnchorElement | null
    expect(anchor?.getAttribute('download')).toBe('holiday.jpg')
  })

  it('falls back to slug + ext for the download name when filename is absent', () => {
    const art = makeImageArtifact({ image: { mime: 'image/webp', ext: 'webp' } })
    render(<ArtifactBodyImage artifact={art} slug={SLUG} />)
    const anchor = document.querySelector('a[download]') as HTMLAnchorElement | null
    expect(anchor?.getAttribute('download')).toBe(`${SLUG}.webp`)
  })

  it('uses image.alt for alt text and falls back to the artifact name', () => {
    const withAlt = makeImageArtifact({
      image: { mime: 'image/png', ext: 'png', alt: 'A red sunset over the sea' },
    })
    const { rerender } = render(<ArtifactBodyImage artifact={withAlt} slug={SLUG} />)
    expect(screen.getByAltText('A red sunset over the sea')).toBeInTheDocument()

    rerender(<ArtifactBodyImage artifact={makeImageArtifact()} slug={SLUG} />)
    expect(screen.getByAltText('Sunset Photo')).toBeInTheDocument()
  })

  it('renders an explanatory placeholder when the asset fails to load', () => {
    // The asset endpoint legitimately 404s/500s (pruned sidecar, refused mime).
    // Without this the user gets the browser's broken-image glyph and no reason.
    render(<ArtifactBodyImage artifact={makeImageArtifact()} slug={SLUG} />)
    const img = document.querySelector('img') as HTMLImageElement
    fireEvent.error(img)
    expect(screen.getByText(/couldn't be loaded/i)).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
  })

  it('renders neither a Monaco editor nor an iframe', () => {
    render(<ArtifactBodyImage artifact={makeImageArtifact()} slug={SLUG} />)
    expect(document.querySelector('iframe')).toBeNull()
    expect(document.querySelector('textarea')).toBeNull()
    expect(document.querySelector('.monaco-editor')).toBeNull()
  })
})

// #28760: fit-to-view is the only size the body shows, so the image opens in
// the app-wide viewer (zoom and pan), the one a chat image opens. The viewer
// listens for a window `lightbox` event; this pins that the body sends it.
describe('ArtifactBodyImage full-size view', () => {
  function captureLightbox() {
    const seen: Array<{ images: Array<{ src: string; alt: string; filename?: string }>; index: number }> = []
    const onLightbox = (e: Event) => { seen.push((e as CustomEvent).detail) }
    window.addEventListener('lightbox', onLightbox)
    return { seen, stop: () => window.removeEventListener('lightbox', onLightbox) }
  }

  it('opens the image in the viewer from a button named by the image', () => {
    const art = makeImageArtifact({
      image: { mime: 'image/png', ext: 'png', alt: 'Architecture diagram', original_filename: 'diagram.png' },
    })
    render(<ArtifactBodyImage artifact={art} slug={SLUG} />)
    // The image's alt stays its accessible name; "View full size" is the hint.
    const button = screen.getByRole('button', { name: 'Architecture diagram' })
    expect(button).toHaveAccessibleDescription('View full size')
    const lb = captureLightbox()
    try {
      fireEvent.click(button)
    } finally {
      lb.stop()
    }
    // The viewer reads the <img> itself, so the src is the resolved URL.
    expect(lb.seen).toEqual([{ images: [{ src: new URL(ASSET_URL, document.baseURI).href, alt: 'Architecture diagram', filename: 'diagram.png' }], index: 0 }])
  })

  it('puts the image itself inside that button, so the image is what is clicked', () => {
    render(<ArtifactBodyImage artifact={makeImageArtifact()} slug={SLUG} />)
    const button = screen.getByRole('button', { name: 'Sunset Photo' })
    expect(button.querySelector('img')?.getAttribute('src')).toBe(ASSET_URL)
  })

  it('names the button for the artifact when the image has no alt of its own', () => {
    render(<ArtifactBodyImage artifact={makeImageArtifact({ image: { mime: 'image/png', ext: 'png', alt: '   ' } })} slug={SLUG} />)
    expect(screen.getByRole('button', { name: 'Sunset Photo' })).toHaveAccessibleDescription('View full size')
  })

  it('names the button "View full size" when neither the image nor the artifact has a name', () => {
    render(<ArtifactBodyImage artifact={makeImageArtifact({ name: '', image: { mime: 'image/png', ext: 'png' } })} slug={SLUG} />)
    expect(screen.getByRole('button', { name: 'View full size' })).toBeInTheDocument()
  })

  it('offers no viewer once the image failed to load', () => {
    render(<ArtifactBodyImage artifact={makeImageArtifact()} slug={SLUG} />)
    fireEvent.error(document.querySelector('img') as HTMLImageElement)
    expect(screen.queryByTitle('View full size')).toBeNull()
  })
})
