# Image Authoring

Create images by **authoring them as code** — there is no text-to-image model
or image-generation tool in this environment. Use when the user says
"generate/create/make an image", "draw", "illustrate", "picture of", "logo",
"icon", "poster", "banner", "mascot", "sprite", "texture", "wallpaper", or
asks to change/tune an image you made earlier.

## HARD RULES

1. **Never look for an image-generation tool.** None exists. You ARE the
   artist — author the image as code and save the file.
2. **Never present this as AI text-to-image generation.** It is authored
   graphics. For photorealism requests, explain the output is stylized
   (vector / procedural art) and offer the closest achievable style.
3. **Never recreate from scratch when the user gives feedback.** Update the
   brief, then make targeted edits to the existing source.
4. **Always keep the editable source.** SVG is its own source; for raster
   output keep the generating script/HTML next to the image so it can be
   edited and re-run.

## Workflow

### 1. Brief — summarize the context

Before authoring, compose a short brief from EVERYTHING relevant in the
conversation (earlier descriptions, project context, corrections), not just
the last message: subject, style, palette, mood, 2–3 signature details,
size/aspect, output format. Echo it in one line — "Authoring: <brief>" — so
the user can correct course before you draw.

### 2. Choose the technique

| Request | Technique |
|---|---|
| Illustration, character, logo, icon, scene, stylized art | **SVG authoring** (primary) |
| Poster, banner, card, meme, text-heavy composition | **HTML/CSS → Playwright screenshot** |
| Texture, gradient art, pixel art, noise, filters, compositing | **Python + Pillow** (raster) |
| Raster copy of an SVG (user needs .png) | Convert: rsvg-convert → inkscape → magick → Playwright screenshot |
| Flowchart, architecture, data chart | Prefer mermaid / widgets; else SVG or Pillow |

Check tool availability before relying on it (`python3 -c "import PIL"`,
`which rsvg-convert`).

### 3. Author — structured for later tuning

- Save under `~/.kiro/crew/workspace/images/` as `<subject>-<style>.<ext>`.
- Put the brief at the top of the source: `<!-- BRIEF: ... -->` (SVG/HTML) or
  a docstring (script). Future iterations read intent from the file itself.
- **Give every major element a stable id** so feedback can target regions:
  - SVG: `<g id="head">`, `<g id="eyes">`, `<g id="collar">`, `id="background"`
  - Pillow: one function per region (`draw_head(d)`, `draw_collar(d)`)
  - HTML: semantic ids/classes per block
- SVG quality bar: `viewBox` + `xmlns`, layered shapes, gradients,
  highlights/shadows, background scene — not minimal flat clipart.

### 4. Validate and render

- SVG/HTML: `xmllint --noout <file>` (or re-read and parse-check).
- Scripts: run them; confirm the output file exists and is non-empty.
- Show the user: `![description](/absolute/path/to/file)`.

### 5. Iterate — blend in comments

When feedback arrives ("make it darker", "bigger eyes", "add stars"):

1. Update the BRIEF comment in the source to include the new requirement —
   the file stays the single source of truth.
2. Locate the region by id and edit ONLY that region. Leave approved parts
   untouched (byte-identical) so the user's accepted details never drift.
3. Re-validate, re-render inline, and note what changed in one line.

For raster: edit the kept script/HTML and re-run — never hand-patch pixels.

### Region tuning cheat-sheet

- "the eyes / the collar / the background" → edit that `id` group only.
- "top-left / bottom / center" → map to viewBox coordinates (0,0 is top-left).
- "colors feel dull" → adjust gradient stops / palette variables only.
- "more detail on X" → add children inside X's group, keep siblings intact.
- Ambiguous region? Ask one short clarifying question instead of guessing.

## Style hints that raise quality

- Name a concrete style: "retro comic", "neon cyberpunk", "watercolor wash",
  "flat geometric", "ukiyo-e", "vaporwave", "blueprint".
- Specify palette, background/scene, mood, and 2–3 signature details.
- For characters: distinct silhouette, expressive eyes, one accent prop.

## Multiple images

Author sequentially, or fan out with `spawn_run` (one task per image; each
task gets the full brief + exact output path).
