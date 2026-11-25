# The daily token chart is one keyboard stop stepped by the arrow keys

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

The Usage tab's daily token chart is one keyboard stop, a slider over its days: Left or Down and Right or Up step one day, Home and End jump to the first and last day, and Escape hides the tooltip. While it has keyboard focus it shows a focus ring and only the selected day's tooltip, a screen reader hears that day's figures as the slider's value, and the hidden list of every day's figures stays.

## Why

- The "Spend over time" chart on the same tab already takes the keyboard this way, so the tab has one keyboard model.
- One stop keeps the chart from adding thirteen tab presses, and naming fourteen bars by their figures would read every day twice beside the list.
- The mouse and touch path is unchanged: the new styling applies only under keyboard focus.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36901#issuecomment-7084116622 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/36901 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/36902 -- the screen-reader list.
