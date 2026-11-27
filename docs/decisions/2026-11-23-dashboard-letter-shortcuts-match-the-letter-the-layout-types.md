# Dashboard letter shortcuts match the letter the keyboard layout types

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

A dashboard shortcut on a letter matches the Latin letter the keyboard layout
types, and the physical key only when that key types no Latin letter (Cyrillic or
Greek layouts, macOS Option characters, a punctuation key where QWERTY has a
letter); shortcuts on punctuation, digits and arrows match by position, except
where that position types a letter; and a shortcut recorded in Settings keeps
its letter, so on such a layout it answers to the key that types that letter.

## Why

- Matching letters by physical key made dashboard shortcuts take over standard
  ones on remapped Latin layouts: on Dvorak ⌘R popped the session out instead of
  reloading the page.
- The browser, the operating system and the dashboard's own terminal already
  read letters this way; the positional fallback keeps non-Latin layouts and
  macOS Option chords working.
- On Dvorak no key reaches ⌘/ (Ctrl+/) or Alt+,: the shortcuts reference stays
  on Option/Alt+K and Settings in the sidebar. Letting position win on
  punctuation instead would bring the same take-overs back.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39978#issuecomment-7092289542 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/39978 -- the issue.
