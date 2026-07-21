# Beeper AUR auto updater

This repository maintains Arch User Repository package templates for the Beeper v4 desktop AppImage.

A scheduled GitHub Actions workflow checks Beeper's Linux x64 stable update feed, updates the package metadata when a new build is available, validates the package, and commits the refreshed files back to the repository.

## What the workflow does

1. Fetches the latest Linux x64 Beeper Stable update metadata.
2. Reads the AppImage version and download URL from the update feed.
3. Computes the AppImage SHA256 checksum.
4. Updates the relevant `PKGBUILD` with the new version, source URL, and checksum.
5. Regenerates `.SRCINFO` from the updated package build file.
6. Builds and checks the package in an Arch Linux environment.
7. Commits the refreshed package files back to this repository.

## Repository layout

- `packages/beeper-bin/` - primary AUR package template.
- `packages/beeper-v4-bin/` - alternate package template for the existing Beeper v4 package name.
- `scripts/aur_update.py` - update-feed parser and package metadata updater.
- `scripts/publish_aur.py` - package publication helper used by the workflow.
- `tests/` - unit tests for update parsing and publication safety checks.
