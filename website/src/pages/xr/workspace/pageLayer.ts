/**
 * A page on a composition layer of its own, fed by the page itself.
 *
 * The framework's own layer component cannot carry a live page cheaply: with a picture it makes a layer that is
 * written once, and with children it draws into the layer on every frame, changed or not. So the page makes the
 * layer (`createQuadLayer`, not static), hands it to the framework's store, which keeps the order of the layers and
 * draws the scene last, and writes a picture into it only when a new one has arrived: one `getSubImage` and one
 * `texSubImage2D` for one new picture, and nothing at all for a page at rest. On the frames between, the headset's
 * compositor shows the picture that was written last.
 *
 * WHICH WAY IS UP. A layer has no texture coordinates that could turn a picture, and WebGL ignores
 * `UNPACK_FLIP_Y_WEBGL` for an `ImageBitmap`. A bitmap that was decoded with no options would stand on its head on
 * a layer. So a picture for a layer is decoded with `imageOrientation: 'flipY'`, and `offer()` refuses one that
 * was not.
 *
 * NOTHING HERE HAS RUN ON A DEVICE, or in a session that grants layers: the emulator has none. It is written
 * against the small interfaces below, which are the parts of WebXR's layers and of WebGL that it calls, and tested
 * against fakes of them (`pageLayer.test.ts`): which calls, in which order, how often.
 */
import { layerBudget, offGaze, orderLayers, pickLayers, type LayerPage } from './layerPick'

/** The part of a WebXR quad layer that is used. */
export interface QuadLayer { destroy(): void }

/** What a quad layer is made with (`XRQuadLayerInit`), as far as it is set here. */
export interface QuadLayerInit {
  space: unknown
  viewPixelWidth: number
  viewPixelHeight: number
  layout: 'mono'
  /** Not static: a static layer is written once and can never change. */
  isStatic: false
  /** A plain texture: the browser's fast upload is off for texture arrays. */
  textureType: 'texture'
  mipLevels: 1
}

/** The part of `XRWebGLBinding` that is used. */
export interface LayerBinding {
  createQuadLayer(init: QuadLayerInit): QuadLayer
  getSubImage(layer: QuadLayer, frame: unknown): { colorTexture: unknown }
}

/** The part of the WebGL context that is used. */
export interface LayerGl {
  readonly TEXTURE_2D: number
  readonly RGBA: number
  readonly UNSIGNED_BYTE: number
  texSubImage2D(target: number, level: number, x: number, y: number, format: number, type: number, source: ImageBitmap): void
}

/** One picture for a layer. */
export interface LayerPicture {
  bitmap: ImageBitmap
  width: number
  height: number
  /** The bitmap was decoded with `imageOrientation: 'flipY'`. */
  flipped: boolean
  /** Called once the picture is in the layer: the source then tells the gateway that it arrived. */
  done?: () => void
}

/** One page's layer: made in the picture's own size, written when a new picture waits, and never otherwise. */
export class PageLayer {
  readonly layer: QuadLayer
  private waiting: LayerPicture | null = null
  private gone = false
  /** How many pictures were written, for the probe and the tests. */
  writes = 0

  constructor(
    private readonly binding: LayerBinding,
    private readonly gl: LayerGl,
    /** Binds a texture as the renderer keeps track of it (`renderer.state.bindTexture`). */
    private readonly bind: (target: number, texture: unknown) => void,
    space: unknown,
    readonly width: number,
    readonly height: number,
  ) {
    this.layer = binding.createQuadLayer({ space, viewPixelWidth: width, viewPixelHeight: height, layout: 'mono', isStatic: false, textureType: 'texture', mipLevels: 1 })
  }

  /**
   * A new picture waits for the next frame. False when the layer cannot take it: it is not of the layer's size (a
   * layer has one size for life, so the page needs a new layer), it stands on its head, or the layer is gone. The
   * caller keeps a picture that was refused. A picture that waited and was never written gives way to the newer one.
   */
  offer(picture: LayerPicture): boolean {
    if (this.gone || !picture.flipped || picture.width !== this.width || picture.height !== this.height) return false
    this.waiting?.bitmap.close()
    this.waiting = picture
    return true
  }

  /** Inside an XR frame: write the picture that waits, if one does. A page at rest costs nothing here. */
  write(frame: unknown): boolean {
    const next = this.waiting
    if (!next || this.gone || !frame) return false
    this.waiting = null
    const image = this.binding.getSubImage(this.layer, frame)
    this.bind(this.gl.TEXTURE_2D, image.colorTexture)
    // The whole picture, in one call, with no size named: the picture is the layer's own size.
    this.gl.texSubImage2D(this.gl.TEXTURE_2D, 0, 0, 0, this.gl.RGBA, this.gl.UNSIGNED_BYTE, next.bitmap)
    next.bitmap.close()
    this.writes += 1
    next.done?.()
    return true
  }

  dispose(): void {
    if (this.gone) return
    this.gone = true
    this.waiting?.bitmap.close()
    this.waiting = null
    this.layer.destroy()
  }
}

/** A page's source, as far as layers need it: where its pictures go, and how big its picture is now. */
export interface LayerSource {
  /** Send the pictures to this layer (decoded the layer's way up), or back to the mesh's texture with `null`. */
  toLayer(layer: PageLayer | null): void
  /** The size of the newest picture in pixels, or null while there is none. */
  pictureSize(): [number, number] | null
}

/** A panel, as the director sees it. */
export interface LayerPanel {
  id: string
  source: LayerSource | null
  /** Where the page's middle is in the room. */
  position(): [number, number, number]
  inView(): boolean
  /** The thing in the scene that stands where the page is, scaled to the page's size: the layer follows it. */
  anchor: unknown
}

/** One entry of the framework's list of layers (`store.addLayerEntry`). */
export interface LayerEntry { layer: QuadLayer; renderOrder: number; object3D: unknown }

/** What the director needs from the renderer, the session and the framework. All of it is handed in, so a test can stand in for each part. */
export interface LayerHost {
  /** The session's granted features and its limit of layers; null outside a session. */
  session(): { enabledFeatures?: readonly string[] | null; maxRenderLayers?: number | null } | null
  binding(): LayerBinding | null
  gl(): LayerGl
  bind(target: number, texture: unknown): void
  /** The space the layers stand in: the session's reference space. */
  space(): unknown
  add(entry: LayerEntry): void
  remove(entry: LayerEntry): void
  /** Put a layer where its anchor is, at the anchor's size (`updateXRLayerTransform`). */
  place(layer: QuadLayer, anchor: unknown): void
}

interface Held { panel: LayerPanel; page: PageLayer; entry: LayerEntry }

/**
 * Keeps the pages nearest to the gaze on layers. Called once a frame, before the frame is drawn. With no layers in
 * the session it does nothing and holds nothing: the mesh path is as it is without it.
 */
export class LayerDirector {
  private readonly held = new Map<string, Held>()
  /** Layers that were replaced: freed one frame later, when the session's list no longer holds them. */
  private retired: PageLayer[] = []
  /**
   * The pages whose layer shows a picture now: a panel whose id is here draws its mesh for depth only. A page whose
   * layer still waits for its first picture is not here, so its mesh goes on showing the page meanwhile.
   */
  readonly onLayer: Set<string>

  constructor(private readonly host: LayerHost, onLayer: Set<string> = new Set()) { this.onLayer = onLayer }

  frame(frame: unknown, head: readonly [number, number, number], forward: readonly [number, number, number], panels: readonly LayerPanel[]): void {
    for (const old of this.retired) old.dispose()
    this.retired = []
    const session = this.host.session()
    const budget = session && frame ? layerBudget(session.enabledFeatures, session.maxRenderLayers) : 0
    // The renderer is asked for its binding only in a session that has layers: without them nothing here runs.
    const binding = budget > 0 ? this.host.binding() : null
    if (budget === 0 || !binding) { if (this.held.size) this.release([...this.held.keys()]); return }

    const pages: LayerPage[] = panels.map(p => {
      const at = p.position()
      return { id: p.id, offGazeDeg: offGaze(head, forward, at), distanceM: Math.hypot(at[0] - head[0], at[1] - head[1], at[2] - head[2]), inView: p.inView(), ready: !!p.source?.pictureSize() }
    })
    const picked = pickLayers(pages, budget, new Set(this.held.keys()))
    this.release([...this.held.keys()].filter(id => !picked.includes(id)))

    for (const id of picked) {
      const panel = panels.find(p => p.id === id)
      const size = panel?.source?.pictureSize()
      if (!panel || !panel.source || !size) continue
      const now = this.held.get(id)
      if (now && now.page.width === size[0] && now.page.height === size[1] && now.panel === panel) continue
      // A new page on a layer, or a picture of another size: a layer has one size for life.
      if (now) { this.host.remove(now.entry); this.retired.push(now.page) }
      const page = new PageLayer(binding, this.host.gl(), (t, x) => this.host.bind(t, x), this.host.space(), size[0], size[1])
      const entry: LayerEntry = { layer: page.layer, renderOrder: 0, object3D: panel.anchor }
      this.held.set(id, { panel, page, entry })
      this.onLayer.delete(id)
      this.host.add(entry)
      panel.source.toLayer(page)
    }
    // The farthest first. The framework lays the scene over all of them.
    const order = orderLayers(pages.filter(p => this.held.has(p.id)))
    for (const [id, h] of this.held) {
      h.entry.renderOrder = order.indexOf(id)
      this.host.place(h.page.layer, h.panel.anchor)
      h.page.write(frame)
      if (h.page.writes > 0) this.onLayer.add(id)
    }
  }

  /** How many pictures each layer has had written, for the probe. */
  writes(): Record<string, number> {
    return Object.fromEntries([...this.held].map(([id, h]) => [id, h.page.writes]))
  }

  private release(ids: readonly string[]): void {
    for (const id of ids) {
      const h = this.held.get(id)
      if (!h) continue
      this.held.delete(id)
      this.onLayer.delete(id)
      // Out of the session's list now; freed on the next frame.
      this.host.remove(h.entry)
      this.retired.push(h.page)
      h.panel.source?.toLayer(null)
    }
  }

  /** The session ended or the workspace is gone: every layer goes. */
  dispose(): void {
    this.release([...this.held.keys()])
    for (const old of this.retired) old.dispose()
    this.retired = []
  }
}
