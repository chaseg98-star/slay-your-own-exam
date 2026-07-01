# Slay Your Own Exam

A single-file web app that turns **your own** question-bank PDF into a realistic, UWorld/NBME-style board exam — then tutors you on exactly what you missed.

Everything runs **in your browser**. Your PDFs, answers, and notes never leave your device. There is no backend and no database.

## Features

- **Verbatim extraction** — questions are pulled out of your PDF exactly as written. Nothing is paraphrased or AI-generated.
- **Exam interface** — A–E options, answer **elimination**, text **highlighting** and **strike-through**, flagging, and a question navigator.
- **90-second per-question timer** with a pause button on every item.
- **Aligned lab panels** — lab/data tables render cleanly instead of as jumbled text.
- **Full USMLE/NBME lab values** reference sheet with search.
- **Themes** — Light, Mint, Green, Ocean Blue, Dark.
- **Durable saves** — your in-progress test survives a refresh. Export/Import a whole test as a `.json` file to move it between devices or versions.
- **AI tutoring** — after a block, get targeted, high-yield feedback based on your answers, timing, what you highlighted, and what you crossed out (uses your own Anthropic API key, entered in Settings).

## Run it locally

Just open `index.html` in any modern browser (Chrome, Edge, Safari, Firefox). No build step, no install.

> Tip: open it from a saved file on your computer (not a temporary preview) so your saved tests persist between sessions.

## Use AI extraction / tutoring (optional)

1. Get an Anthropic API key from <https://console.anthropic.com>.
2. Open **Settings** in the app and paste your key. It is stored only in your browser's local storage.

## Deploy as a website (GitHub Pages)

This repo is ready to host as-is. See **DEPLOY** steps in the project chat, or:

1. Push these files to a GitHub repo.
2. Repo **Settings → Pages → Build and deployment → Source: Deploy from a branch**, branch `main`, folder `/ (root)`.
3. Your site goes live at `https://<your-username>.github.io/<repo-name>/`.

`.nojekyll` is included so GitHub Pages serves the files unmodified.

## Privacy

No analytics, no tracking, no server. The only outbound network calls are:
- loading `pdf.js` and the web font from a CDN, and
- the Anthropic API calls **you** trigger with **your** key.

## License

MIT — see [LICENSE](LICENSE).
