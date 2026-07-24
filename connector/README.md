# 🎙️ Voice-exam connector

Take a **Slay Your Own Exam** test **hands-free by voice** — perfect for reviewing
while you drive. You add this as a **custom connector** in Claude on your phone;
then Claude reads you each question, reads the labs, **describes the figures**,
and records your **spoken** answers. When you finish, your answers sync back to
the app and grade like a normal test.

The one rule that makes this safe: **the answer key never leaves your device.**
The app strips the correct option and the explanation *before* anything is sent,
so the connector — and therefore Claude — literally never has them. Claude can
quiz you, but it can't tell you the answer. Grading happens back in the app.

```
  Browser app  ──POST /push (no answer key)──►  Connector (Cloudflare Worker)
       ▲                                              ▲
       │  GET /pull/<code>  (your spoken answers)     │  POST /mcp  (JSON-RPC)
       │                                              │
   grades here                                   Claude (phone, voice)
```

---

## What you need

- A free [Cloudflare](https://dash.cloudflare.com/sign-up) account (to host the Worker).
- [Node.js](https://nodejs.org) 18+ locally (to run `wrangler` and the tests).
- A Claude plan that supports **custom connectors**, and the Claude app on your phone.

## 1. Deploy the connector (once, ~5 minutes)

```bash
cd connector
npm install
npx wrangler login                         # opens a browser to authorize Cloudflare
npx wrangler kv namespace create EXAMS     # prints an id=... line
```

Copy the printed **id** into `wrangler.toml`, replacing `REPLACE_WITH_YOUR_KV_NAMESPACE_ID`:

```toml
[[kv_namespaces]]
binding = "EXAMS"
id = "the-id-wrangler-printed"
```

Then deploy:

```bash
npx wrangler deploy
```

Wrangler prints a URL like `https://slay-your-own-exam-connector.YOURNAME.workers.dev`.
Keep it — you need it twice next.

## 2. Point the app at it

In `../index.html`, find `const CONNECTOR_URL=''` (it's right next to
`firebaseConfig`) and paste your Worker URL:

```js
const CONNECTOR_URL='https://slay-your-own-exam-connector.YOURNAME.workers.dev';
```

Commit & push (GitHub Pages redeploys). A **🎙 Voice** button now appears in the
exam toolbar. The app's Content-Security-Policy already allows `*.workers.dev`,
so no other change is needed unless you move the Worker to a custom domain (then
add that origin to the `connect-src` list in the CSP meta tag).

## 3. Add it to Claude on your phone

In the Claude app: **Settings → Connectors → Add custom connector**, and enter
your Worker URL **with `/mcp` on the end**:

```
https://slay-your-own-exam-connector.YOURNAME.workers.dev/mcp
```

Give it a name like "Slay exam". That's it.

## 4. Take a test by voice

1. In the app (on your computer), start a test as usual and tap **🎙 Voice**.
   It shows a 5-character code like `ABC23` and starts listening for answers.
2. On your phone, open Claude in **voice mode** and say: **"start exam ABC23"**.
3. Claude reads each question, the labs, and describes any figure, then asks for
   your answer. Say a letter ("B"), or "skip", "flag it", "repeat", "next",
   "how many left". It will **not** tell you if you're right.
4. Say **"I'm done"** when finished. Your answers sync back; tap **Apply answers
   & grade** in the app (it also auto-applies as answers arrive). Your code stays
   valid for 24 hours, so you can close the tab and pick it back up later from
   the same 🎙 Voice button.

---

## Tools Claude gets

| Tool | What it does |
|---|---|
| `start_exam(code)` | Loads the exam for that code. Returns the title + question count — **no answers**. |
| `get_question(n?)` | Reads a question: stem, labs, options, and figures (attached as images to describe). No key. |
| `next_question()` | The next unanswered-ish item. |
| `record_answer(choice, n?, flag?)` | Saves a spoken answer (A–E or "skip"). Confirms it, never judges it. |
| `flag_question(n?, on?)` | Flag/unflag for review. |
| `exam_status()` | Answered count, flagged items, what's left. |
| `finish_exam()` | Marks it done so the app can grade it. |

## Privacy & safety notes

- **No answer key, ever.** The app sends only stem, options, and figures. The
  Worker's storage and the tool results are provably key-free (see the tests).
- **Figures** are stored under their own keys and streamed to Claude only when a
  question is read, so the hot path stays small.
- **Sessions self-expire** after 24 hours (KV TTL). Nothing is kept longer.
- The pairing code is short because it's meant to be *spoken*; it's not a
  password. Don't push exams containing information you wouldn't want anyone with
  the code to hear. For personal study use this is fine; if you want to lock it
  down, set `ALLOWED_ORIGIN` in `wrangler.toml` and/or shorten the KV TTL.

## Develop / test

```bash
npm test          # node --test — unit + full worker round-trip, incl. the "no key leaks" checks
npm run dev       # wrangler dev — run the Worker locally
```

The protocol layer is hand-rolled (no MCP SDK dependency) so the whole thing is
a dependency-free, unit-testable Cloudflare Worker. Core logic lives in
`src/tools.js`, `src/store.js`, `src/mcp.js`, and `src/html2text.js`;
`src/worker.js` just wires them to HTTP + KV.
