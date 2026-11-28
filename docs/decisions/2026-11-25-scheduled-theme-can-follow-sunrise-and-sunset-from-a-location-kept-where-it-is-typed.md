# Scheduled theme mode can follow local sunrise and sunset, from a location kept only where it is typed

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

Scheduled, in Settings > Display, offers two methods: the two fixed times
(the default, unchanged) or sunrise and sunset, computed on the device from a
latitude and longitude the person types. The method and the location are kept
only in the browser or app, at the dashboard address, where they were typed;
anywhere else the two fixed times apply until a location is typed there. The
workspace keeps only the two times, nothing looks a location up, and the browser
geolocation API is not used.

## Why

- Sunrise and sunset scheduling is the one acceptance criterion of the issue
  that the fixed-time schedule left open, because it needs a location source.
- The workspace's theme settings are served by a boot read that needs no
  sign-in, so a location stored there would be served with them. Keeping it
  where it was typed sends it nowhere.
- A typed location needs no permission prompt and works the same in every
  browser and in the desktop app.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/21097#issuecomment-7165265335 --
  the maintainer decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/21097 -- the request and its
  acceptance criteria.
- https://github.com/kirodotdev/KiroCrew/pull/34653 -- the fixed-time schedule
  this builds on, which left sunrise and sunset out for want of a location.
- https://github.com/kirodotdev/KiroCrew/pull/31196 -- the earlier sunrise and
  sunset work whose sun equation this reuses, and the maintainer's invitation to
  finish the mode on top of the merged schedule.
