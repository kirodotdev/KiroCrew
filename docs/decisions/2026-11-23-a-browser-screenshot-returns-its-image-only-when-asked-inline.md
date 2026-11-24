# A browser screenshot returns its image in the tool result only when the agent asks with `inline: true`

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer, 2026-11-23
Date: 2026-11-23

## Decision

The browser `screenshot` op takes an opt-in `inline: true`. With it, the tool result carries the saved PNG as one image, held to the existing inline-image size budget, after the usual text with the file path. Without it, the result is the path text alone, as before. No other tool returns an image.

## Why

- The issue asks to drop the second call an agent makes to read the file each time it checks a step by eye.
- Opt-in keeps the default result, and the cost of an image the conversation sends again on every later turn, unchanged for every agent that does not ask.
- Options not chosen: keep the path only and close the issue (the second call stays); return the image by default (every screenshot is then paid for again on each later turn, and an existing behaviour changes).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36166#issuecomment-7114093215 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/issues/36166#issuecomment-6909009885 -- the options and the recommendation.
- https://github.com/kirodotdev/KiroCrew/issues/36166 -- the request.
