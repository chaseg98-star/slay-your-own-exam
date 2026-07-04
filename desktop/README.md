# Slay Your Own Exam — Desktop app

Electron wrapper around the live web app (https://cg1k.github.io/slay-your-own-exam/).
It loads the hosted site (falling back to https://slayyourexam.web.app/, then to a
bundled copy in `site/`); the app's service worker makes it work offline after the
first successful load.

## Run locally

```bash
cd desktop
npm install
npm start
```

## Build installers / releases

Built by the GitHub Actions workflow **Build desktop app**
(`.github/workflows/desktop-release.yml`), which bundles the site files into
`desktop/site/` and runs electron-builder on Windows, macOS and Linux. Two ways
to trigger it:

- **Actions → "Build desktop app" → Run workflow.** Installers are attached as
  workflow artifacts. If you fill in the optional `version` input (e.g.
  `v1.0.0`), a GitHub Release with that tag is also created/updated with the
  installers.
- **Push a tag like `desktop-v1.0.0`.** The workflow builds and publishes the
  installers straight to a GitHub Release (electron-builder `--publish always`).
  The version in the tag is written into `package.json` for that build, so keep
  tags in `desktop-vX.Y.Z` form.

Downloads land on this repo's **GitHub Releases** page:
https://github.com/CG1k/slay-your-own-exam/releases

Builds are unsigned: macOS users must right-click → Open (or clear the
quarantine flag) the first time; Windows SmartScreen shows a "More info → Run
anyway" prompt.
