/**
 * PanelCard — the reminder pet's panel, built as a self-contained CARD.
 *
 * The design it implements was specified in the standalone Crew Companion app's
 * own spec document, which did not move into this repository with the code — so
 * the intent is written out here rather than cited as a path that a reader cannot
 * open.
 *
 * This is a CARD, not a full-bleed page: it draws its own background, radius and
 * shadow, and expects to sit in a TRANSPARENT window with padding around it. An
 * earlier attempt retrofitted a card wrapper around the old full-bleed chat UI and
 * broke its layout — hence a purpose-built component.
 *
 * The pet is NOT drawn here. It stays on the desktop as its own overlay, keeps its
 * own notification bubbles, and is simply what you click to open this panel. Two
 * earlier designs (pet inside the card, pet perched on the edge) both required
 * either coupling two OS windows or concealing the real pet and duplicating it —
 * and concealing it meant rebuilding notification delivery for no real gain.
 *
 * The skin is a FIXED warm cream palette — it intentionally does NOT follow the
 * Kiro Crew dashboard theme, because the pet is its own product surface.
 */
import { X } from 'lucide-react'
import React, { useState } from 'react'
import { i18nT } from '../../i18n/t'
import './panel.css'
import type { AppRegionStyle } from './appRegion'
import { useSkin, PANEL_FONT } from './panelSkin'
import { ReminderInput } from './ReminderInput'
import { AllRemindersView, SettingsView, ViewHeader, ViewBoundary, type PanelView } from './PanelViews'

// ── Skin ────────────────────────────────────────────────────────────────────
// Fixed palette (not theme-derived). Kept in one place so the breathing overlay
// and any future panel surfaces can import the same values.

const FONT = PANEL_FONT

/**
 * Floor height for the card.
 *
 * The breathing overlay is absolutely positioned over this card, so the card's
 * height is also the overlay's. Without a floor, the short "Nothing scheduled"
 * state gave the overlay less room than its content needed and clipped it top and
 * bottom. Keep >= the overlay's content height.
 */
const CARD_MIN_HEIGHT = 300

// ── Data ────────────────────────────────────────────────────────────────────

/** One row in UP NEXT. */
export interface UpNextItem {
  id: string
  text: string
  /** Absolute time, shown only when the item is not imminent (e.g. "3:00 PM"). */
  absLabel?: string
  /** Relative countdown pill (e.g. "in 45 min"). */
  relLabel: string
  /** Break nudges read as ambient (green); scheduled reminders as accent. */
  tone?: 'accent' | 'ok'
  /** True when the reminder repeats — only then is Skip meaningful. */
  recurring?: boolean
}

export interface PanelCardProps {
  /** Up to ~3 chronological items. Empty renders the nothing-scheduled state. */
  upNext: UpNextItem[]
  /** Break cadence in minutes — the sentence itself is localized here. */
  breakMins: number
  /**
   * Whether session notifications are on — the summary line says so out loud.
   *
   * REQUIRED, unlike most props here, and deliberately so: as `sessionOn?` with a
   * `= true` default, a caller that never threaded the value got a footer that
   * claimed "task alerts on" no matter what was stored. The claim was wrong in the
   * one direction that matters — it told the user notifications were coming while
   * the backend was correctly suppressing them, so silence looked like a bug in
   * notifications rather than a lie in this sentence. `breakMins` beside it was
   * required and was threaded correctly; that is the whole difference.
   */
  sessionOn: boolean
  /** Which side of the pet the panel opened on — aims the spring's origin. */
  openSide?: 'left' | 'right'
  onBreathe?: () => void
  /**
   * Persist a reminder. Resolves to the label to confirm with, or null on failure.
   * Kept as a callback so the card stays presentational and testable.
   */
  onAdd?: (text: string, fireAtIso: string, everyMinutes?: number) => Promise<boolean>
  onSeeAll?: () => void
  /** Opens the in-card Settings view. */
  onSettings?: () => void
  /** Which body the card is showing. 'main' is the reminder list. */
  view?: PanelView
  /** Leave a secondary view and return to the reminder list. */
  onBack?: () => void
  /** Delete a reminder straight from the UP NEXT list. */
  onRemove?: (id: string) => void
  /** Skip a recurring reminder's next occurrence. */
  onSkip?: (id: string) => void
  /** Dismiss the panel. Always render an explicit exit. */
  onClose?: () => void
}

// ── Pieces ──────────────────────────────────────────────────────────────────

const SectionLabel: React.FC<{ children: React.ReactNode; right?: React.ReactNode }> =
({ children, right }) => {
  const skin = useSkin()
  return (
  <div style={{ display: 'flex', alignItems: 'center', padding: '0 14px 4px' }}>
    <span style={{
      flex: 1, fontSize: 10, fontWeight: 700, letterSpacing: '.05em', color: skin.accentText,
    }}>{children}</span>
    {right}
  </div>
  )
}

const Pill: React.FC<{ tone: 'accent' | 'ok'; children: React.ReactNode }> = ({ tone, children }) => {
  const skin = useSkin()
  return (
  <span style={{
    fontSize: 9,
    padding: '2px 6px',
    borderRadius: 9,
    whiteSpace: 'nowrap',
    fontWeight: 700,
    // FILLED, not tinted: accent-on-accent-tint measured 2.58:1 in the fallback
    // theme. A filled pill puts the label on `onAccent`, which the theme designs.
    background: tone === 'ok' ? skin.okInk : skin.accent,
    color: skin.onAccent,
  }}>{children}</span>
  )
}

// ── Card ────────────────────────────────────────────────────────────────────

export const PanelCard: React.FC<PanelCardProps> = ({
  upNext, breakMins, sessionOn, openSide = 'right',
  onBreathe, onAdd, onSeeAll, onSettings, onClose, view = 'main', onBack, onRemove, onSkip,
}) => {


  const skin = useSkin()

  /** Which row the cursor is on, so its ✕ can appear without a permanent one. */
  const [hoverRow, setHoverRow] = useState<string | null>(null)


  const empty = upNext.length === 0

  return (
    <div
      className="cc-card"
      style={{
        background: skin.card,
        color: skin.ink,
        borderRadius: skin.radius,
        boxShadow: skin.shadow,
        overflow: 'hidden',
        fontFamily: FONT,
        minHeight: CARD_MIN_HEIGHT,
        // The spring grows out of the pet, so the origin tracks the side the
        // panel opened on.
        transformOrigin: openSide === 'left' ? 'right top' : 'left top',
      }}
    >
      {view !== 'main' ? (
        /*
          A secondary view: same card, normal flow, replacing the body. NOT an
          absolutely-positioned layer over it — that reads as a second card stacked
          on the first.
        */
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <ViewHeader
            title={view === 'all' ? i18nT('apps.crewCompanion.view.allTitle') : i18nT('apps.crewCompanion.view.settingsTitle')}
            onBack={() => onBack?.()}
          />
          {/* 14px matches the main view's inset so content does not shift on switch. */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px 14px' }}>
            <ViewBoundary
              onBack={() => onBack?.()}
              label={i18nT('apps.crewCompanion.view.failed')}
              back={i18nT('apps.crewCompanion.view.back')}
            >
              {view === 'all'
                ? <AllRemindersView onAdd={onAdd} />
                : <SettingsView />}
            </ViewBoundary>
          </div>
        </div>
      ) : (
      <div className="cc-cascade">
        {/* 1 — breathing invite */}
        <div style={{ padding: '14px 14px 12px', position: 'relative' }}>
          {onClose && (
            <button
              onClick={onClose}
              aria-label={i18nT('apps.crewCompanion.panel.close')}
              title={i18nT('apps.crewCompanion.panel.close')}
              style={{
                /**
                 * 28px, not 20px. The glyph is small but the TARGET must not be:
                 * a 20px box with a 50% radius gives a ~20px circle, so its four
                 * corners fell through to the element behind (measured: 4 of 15
                 * probe points inside the box missed the button) and the whole
                 * thing sat under the 24px minimum target size. Growing the box
                 * costs nothing visually — the background is transparent until
                 * hover, so only the hit area changes.
                 */
                position: 'absolute', top: 6, right: 6,
                width: 28, height: 28, borderRadius: '50%',
                // Flex-centred rather than relying on line-height, which left the
                // glyph off-centre inside the larger box.
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                background: 'none', border: 'none', cursor: 'pointer',
                color: skin.faint, fontSize: 13, lineHeight: 1, padding: 0,
                // Sits inside the drag strip, so it has to opt out or it becomes
                // a handle instead of a button.
                WebkitAppRegion: 'no-drag',
              } as AppRegionStyle}
              onMouseEnter={(e) => {
                // A transparent target is hard to aim at; the tint confirms it.
                (e.currentTarget as HTMLElement).style.background = skin.hairline
                ;(e.currentTarget as HTMLElement).style.color = skin.ink
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = 'none'
                ;(e.currentTarget as HTMLElement).style.color = skin.faint
              }}
            ><X size={12} aria-hidden="true" /></button>
          )}
          {/*
            The window is frameless, so without a drag region there is nothing to
            grab and the panel cannot be moved at all. The heading block is the
            handle — it is the only sizeable non-interactive area, and a header
            drag is the convention users already expect.
          */}
          <div style={{
            WebkitAppRegion: 'drag', cursor: 'grab',
            // A drag over a text node otherwise SELECTS it, leaving the heading
            // highlighted after moving the window.
            userSelect: 'none', WebkitUserSelect: 'none',
            /**
             * Stop short of the ✕ so the two rectangles never overlap.
             *
             * `no-drag` on the button is not sufficient: Electron resolves drag
             * regions in the COMPOSITOR from element rectangles, and this block —
             * a full-width static sibling that paints after the absolutely
             * positioned button — covered the button's lower ~20px, swallowing
             * mouse-down over its middle while the top 8px (above this block)
             * still worked. DOM hit-testing cannot see app-region, which is why
             * an elementFromPoint probe reported the target as fully clickable.
             *
             * 34px = the button's 28px width + its 6px right offset.
             */
            marginRight: 34,
          } as AppRegionStyle}>
            <div style={{ fontSize: 14, fontWeight: 700, lineHeight: 1.25, paddingRight: 22 }}>
              {i18nT('apps.crewCompanion.panel.breathe.title')}
            </div>
            <div style={{ fontSize: 11, color: skin.muted, marginTop: 4 }}>
              {i18nT('apps.crewCompanion.panel.breathe.sub')}
            </div>
          </div>
          <button
            onClick={onBreathe}
            style={{
              marginTop: 10, fontSize: 11.5, padding: '7px 14px', borderRadius: 20,
              background: skin.accent, color: skin.onAccent, fontWeight: 700,
              border: 'none', cursor: 'pointer', fontFamily: FONT,
            }}
          >{i18nT('apps.crewCompanion.panel.breathe.cta')}</button>
        </div>

        {/* 2 — up next */}
        <div>
          <SectionLabel right={
            <button onClick={onSeeAll} style={{
              fontSize: 10, color: skin.accentText, background: 'none', border: 'none',
              /**
               * Padding + matching negative margin: a 12px-tall text link is a
               * 12px target (measured 68/113 points inside a 24px area). This
               * grows the hit area to clear the minimum while the negative margin
               * keeps the text exactly where it was, so no layout shifts.
               */
              padding: '7px 4px', margin: '-7px -4px',
              cursor: 'pointer', fontFamily: FONT,
            }}>{i18nT('apps.crewCompanion.panel.seeAll')}</button>
          }>{i18nT('apps.crewCompanion.panel.upNext')}</SectionLabel>

          {empty ? (
            <div style={{
              margin: '0 14px', background: skin.row, borderRadius: skin.rowRadius,
              padding: '12px 11px', textAlign: 'center',
            }}>
              <div style={{ fontSize: 12, fontWeight: 600 }}>{i18nT('apps.crewCompanion.panel.empty.title')}</div>
              <div style={{ fontSize: 10.5, color: skin.muted, marginTop: 3, lineHeight: 1.4 }}>
                {i18nT('apps.crewCompanion.panel.empty.sub')}
              </div>
            </div>
          ) : (
            <div style={{
              margin: '0 14px', background: skin.row,
              borderRadius: skin.rowRadius, overflow: 'hidden',
            }}>
              {upNext.map((item, i) => (
                <div
                  key={item.id}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 7, padding: '8px 10px',
                    borderTop: i === 0 ? 'none' : `1px solid ${skin.hairline}`,
                  }}
                  onMouseEnter={() => setHoverRow(item.id)}
                  onMouseLeave={() => setHoverRow(null)}
                >
                  {item.absLabel && (
                    <span style={{
                      fontSize: 12, fontWeight: 700, fontVariantNumeric: 'tabular-nums',
                    }}>{item.absLabel}</span>
                  )}
                  <span style={{
                    flex: 1, fontSize: 12, overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{item.text}</span>
                  {/*
                    At rest the pill is flush with the row's right edge. On hover it
                    steps aside and the controls appear in its place — so nothing is
                    permanently reserving edge space for a button nobody is using,
                    and the label the user reads stays where they expect it.

                    Crossfaded rather than reflowed: animating width would nudge the
                    reminder text on every hover.
                  */}
                  <span style={{
                    position: 'relative', flexShrink: 0,
                    display: 'inline-flex', alignItems: 'center', justifyContent: 'flex-end',
                    minWidth: item.recurring ? 62 : 34, height: 24,
                  }}>
                    <span style={{
                      opacity: hoverRow === item.id ? 0 : 1,
                      transition: 'opacity 120ms ease',
                      pointerEvents: 'none',
                    }}>
                      <Pill tone={item.tone ?? 'accent'}>{item.relLabel}</Pill>
                    </span>
                    <span style={{
                      position: 'absolute', right: 0, top: 0,
                      display: 'inline-flex', alignItems: 'center', gap: 2,
                      opacity: hoverRow === item.id ? 1 : 0,
                      transition: 'opacity 120ms ease',
                      pointerEvents: hoverRow === item.id ? 'auto' : 'none',
                    }}>
                      {/*
                        Skip only for recurring reminders: "not this time" needs a
                        next time to move to. A one-time reminder has none, so the
                        action would be indistinguishable from deleting it.
                      */}
                      {item.recurring && (
                        <button
                          onClick={() => onSkip?.(item.id)}
                          aria-label={i18nT('apps.crewCompanion.view.skip')}
                          title={i18nT('apps.crewCompanion.view.skip')}
                          style={{
                            height: 24, padding: '0 7px', borderRadius: 999,
                            border: 'none', background: skin.hairline, color: skin.ink,
                            fontSize: 9.5, fontWeight: 700, cursor: 'pointer',
                            fontFamily: FONT,
                          }}
                        >{i18nT('apps.crewCompanion.view.skip')}</button>
                      )}
                      <button
                        onClick={() => onRemove?.(item.id)}
                        aria-label={i18nT('apps.crewCompanion.view.remove')}
                        title={i18nT('apps.crewCompanion.view.remove')}
                        style={{
                          width: 24, height: 24, borderRadius: '50%', flexShrink: 0,
                          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                          background: 'none', border: 'none', cursor: 'pointer',
                          color: skin.muted, fontSize: 11, padding: 0, fontFamily: FONT,
                        }}
                      ><X size={11} aria-hidden="true" /></button>
                    </span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 3 — natural-language add (shared with the full list) */}
        {/* 14px matches the UP NEXT rows above, which use `margin: 0 14px`. */}
        <div style={{ padding: '0 14px' }}>
          <ReminderInput onAdd={onAdd} />
        </div>

        {/* 4 — footer: the one fact worth a glance, plus a door to settings */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '1px 14px 13px' }}>
          <span style={{ flex: 1, fontSize: 10, color: skin.faint }}>
            {i18nT('apps.crewCompanion.panel.breakSummary', { mins: String(breakMins) })}
            {/* Both nudge settings at a glance, so the footer states what the pet
                will actually interrupt you for. */}
            {' · '}
            {i18nT(sessionOn ? 'apps.crewCompanion.panel.sessionOn' : 'apps.crewCompanion.panel.sessionOff')}
          </span>
          <button
            onClick={onSettings}
            style={{
              fontSize: 10, color: skin.accentText, fontWeight: 600, background: 'none',
              border: 'none', cursor: 'pointer', fontFamily: FONT,
              // Same hit-area expansion as the other text links.
              padding: '7px 4px', margin: '-7px -4px -7px 0',
            }}
          >{i18nT('apps.crewCompanion.view.settingsTitle')}</button>
        </div>
      </div>
      )}
    </div>
  )
}
