# Download now waits while another speech model downloads, and names that model

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

In Voice settings, while another model's transfer runs, the selected not-yet-downloaded model's Download now is disabled and one line under it names the model being downloaded and says Download now becomes available when that download finishes.

## Why

- The gateway runs one model transfer at a time, so pressing Download now for a different model while one runs starts nothing, and nothing on screen said why.

## Evidence

- The decision: https://github.com/kirodotdev/KiroCrew/issues/20717#issuecomment-7116402312
- The issue: https://github.com/kirodotdev/KiroCrew/issues/20717
