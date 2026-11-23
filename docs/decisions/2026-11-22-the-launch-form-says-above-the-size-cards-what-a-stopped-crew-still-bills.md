# The launch form says above the size cards what a stopped crew still bills for

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

The Remote Crew launch form shows, in every Region, one sentence under the Size heading and
above the size cards: "A stopped crew still bills for its disk, and for any setup file kept in
S3, until you delete the crew."; the billing box above Launch stays where it is, and the CLI
price line is unchanged.

## Why

- Stopping a crew pauses only the instance's compute charge, and the form never said what
  keeps billing while a crew is stopped.
- What a stopped crew still costs belongs beside the per-size prices a person compares before
  launching.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34692#issuecomment-7071290523 -- the
  maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/34692 -- the issue.
