# Slay Your Own Exam

A single-file web app that turns **your own** question-bank PDF into a realistic, UWorld/NBME-style board exam — then tutors you on exactly what you missed.

Everything runs **in your browser**. Your PDFs are never uploaded anywhere. By default there is no backend and no database — saved tests live only in your browser's local storage. Accounts (see below) are an optional add-on that syncs saved tests to the cloud instead.

## Features

- **Verbatim extraction** — questions are pulled out of your PDF exactly as written. Nothing is paraphrased or AI-generated.
- **Exam interface** — A–E options, answer **elimination**, text **highlighting** and **strike-through**, flagging, and a question navigator.
- **90-second per-question timer** with a pause button on every item.
- **Aligned lab panels** — lab/data tables render cleanly instead of as jumbled text.
- **Full USMLE/NBME lab values** reference sheet with search.
- **Themes** — Light, Mint, Green, Ocean Blue, Dark.
- **Durable saves** — every answer, highlight, cross-out and flag is saved continuously. Close the tab mid-test and it's waiting in **History** as an in-progress entry (with your % done) — resume exactly where you left off. Export/Import a whole test as a `.json` file to move it between devices or versions.
- **Focus Lock** — a web version of the FocusLock Mac app. Arm it for a test and leaving the tab (or clicking off the browser) sounds a looping voice alarm (minimum 5-second blast). The only ways out: say **"I am done with my test"** out loud (speech recognition), or a typed emergency fallback. Limits a web page can't escape: it cannot raise your system volume, and closing the tab kills any alarm a website can make.
- **Clean lab panels** — lab values are pulled out of the vignette into their own "Laboratory values" section between the story and the question, instead of being jumbled through the stem.
- **AI tutoring** — after a block, get targeted, high-yield feedback based on your answers, timing, what you highlighted, and what you crossed out (uses your own Anthropic API key, entered in Settings).
- **Voice exam (hands-free)** — pair a test to Claude's voice chat on your phone and take it by ear: Claude reads the questions, reads the labs, **describes the figures**, and records your **spoken** answers — ideal for reviewing while driving. It never receives the answer key, so it can quiz you but can't give anything away; when you finish, your answers sync back and grade like a normal test. Optional; see [`connector/`](connector/).
- **Accounts (optional)** — sign up / log in to sync every saved test, and every in-progress answer, highlight, and cross-out, to your account in real time so you can pick up on another device. One designated admin account can see every account's saved tests. Off by default; see [Set up accounts](#set-up-accounts-optional) below.

## Run it locally

Just open `index.html` in any modern browser (Chrome, Edge, Safari, Firefox). No build step, no install.

> Tip: open it from a saved file on your computer (not a temporary preview) so your saved tests persist between sessions.

## Use AI extraction / tutoring (optional)

1. Get an Anthropic API key from <https://console.anthropic.com>.
2. Open **Settings** in the app and paste your key. It is stored only in your browser's local storage.

## Take a test by voice (optional)

Deploy the small connector in [`connector/`](connector/) and you can take any
test **hands-free** through Claude's voice chat on your phone — Claude reads you
the questions, the labs, and the figures, and records your spoken answers,
without ever seeing the answer key. It's a real [MCP](https://modelcontextprotocol.io)
connector you add once in Claude's settings; the whole backend is a single
free Cloudflare Worker. Full setup (about five minutes) is in
[`connector/README.md`](connector/README.md).

## Set up accounts (optional)

Accounts are **off by default** — the app works exactly as before until you configure this. Turning them on lets people sign up / log in and syncs their saved tests to the cloud instead of just the local browser, and gives one email address (the admin) a screen that lists every account's saved tests.

This needs a free [Firebase](https://firebase.google.com) project (Google's backend-as-a-service — free tier is generous and this app's usage won't come close to the limits). Firebase can't be set up by an AI assistant on your behalf because it requires your own Google login — these steps are for you to run once, by hand:

1. Go to <https://console.firebase.google.com>, click **Add project**, give it any name, and finish the wizard (you can decline Google Analytics).
2. In the left sidebar: **Build → Authentication → Get started → Sign-in method → Email/Password → Enable → Save**.
3. In the left sidebar: **Build → Firestore Database → Create database**. Choose **production mode** and any region.
4. Still in Firestore, open the **Rules** tab, replace the contents with the block below, and click **Publish**:

   ```
   rules_version = '2';
   service cloud.firestore {
     match /databases/{database}/documents {
       match /sessions/{sessionId} {
         allow read, update, delete: if request.auth != null &&
           (resource.data.uid == request.auth.uid || request.auth.token.email == 'chase.g98@icloud.com');
         allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;
       }
       match /liveState/{uid} {
         allow read, write: if request.auth != null && request.auth.uid == uid;
       }
     }
   }
   ```

   `sessions` holds finished tests: private to their owner, except the admin email, which can read (and moderate) everyone's. `liveState` holds each account's *in-progress* test (every highlight, answer, and cross-out, saved continuously) so it can resume on another device — private to the owner only, one document per account.

5. In the left sidebar: **Project settings (gear icon) → General → Your apps → Add app → Web (`</>`)**. Register the app (no need for Firebase Hosting). Copy the `firebaseConfig` object it shows you.
6. Open `index.html`, find the block that starts with `const firebaseConfig={` (search for `PASTE_YOUR`), and replace the six placeholder strings with the real values from step 5.
7. If the admin account should use a different email than `chase.g98@icloud.com`, change the `ADMIN_EMAIL` constant just above `firebaseConfig` **and** the email in the security rules from step 4, then re-publish the rules.
8. Commit and push — GitHub Pages redeploys automatically. Sign up on the live site with the admin email to get the admin view (an **Admin** button appears in the top bar).

Until you complete this, the "Sign in" button in the top bar shows "Accounts unavailable" and the app behaves exactly as it did before.

## Deploy as a website (GitHub Pages)

This repo is ready to host as-is. See **DEPLOY** steps in the project chat, or:

1. Push these files to a GitHub repo.
2. Repo **Settings → Pages → Build and deployment → Source: Deploy from a branch**, branch `main`, folder `/ (root)`.
3. Your site goes live at `https://<your-username>.github.io/<repo-name>/`.

`.nojekyll` is included so GitHub Pages serves the files unmodified.

## Security

- **Firestore rules** live in [`firestore.rules`](firestore.rules) and must match what's published in the Firebase console. Verified enforced 2026-07-03: signed-out reads and cross-account reads (tested with a throwaway account) both return `PERMISSION_DENIED`.
- The `firebaseConfig` in `index.html` is Firebase's *public* web config — it is designed to be public and is not a secret. Data access is controlled entirely by the rules above.
- A strict **Content-Security-Policy** meta tag limits scripts to this site + the two CDNs (cdnjs, gstatic) and network calls to Firebase/Anthropic — injected or tampered content can't call out anywhere else.
- Restored test saves (from disk import or cloud sync) are **sanitized** before rendering: stem HTML is whitelist-filtered and images must be real embedded `data:image/*` payloads, so a crafted save file can't run script.
- Your Anthropic API key never leaves this browser's local storage and is only sent to `api.anthropic.com`.

## Privacy

No analytics, no tracking. The only outbound network calls are:
- loading `pdf.js`, the web font, and (if accounts are configured) the Firebase SDK from a CDN,
- the Anthropic API calls **you** trigger with **your** key, and
- if accounts are configured and you sign in, your email and saved test results are sent to and stored in the site owner's Firebase project (not this repo, not Anthropic).

## License

MIT — see [LICENSE](LICENSE).
