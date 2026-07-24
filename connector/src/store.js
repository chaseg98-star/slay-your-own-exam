// Storage layer for the voice-exam connector.
//
// Everything is keyed by a short pairing "code" the app shows on screen and the
// user speaks to Claude ("start exam ABC23"). We deliberately split the data so
// the hot path (reading a question, recording an answer) never has to move the
// figure bytes around:
//
//   exam:<code>   -> session meta + answer-KEY-FREE questions (no images bytes)
//   img:<code>:<n>:<i> -> one figure ({ mimeType, dataBase64 })
//   ans:<code>    -> the user's spoken answers, keyed by question id
//   sess:<id>     -> maps an MCP session id to its active code (convenience)
//
// The store is handed a KV-like namespace object with async get/put/delete so
// it works against Cloudflare Workers KV in production and an in-memory Map in
// tests. No secrets and no answer key ever land here — the app strips `correct`
// and `explanation` before pushing, so Claude structurally cannot reveal them.

const CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; // no I/O/0/1 (spoken safely)
const CODE_LEN = 5;
const TTL_SECONDS = 24 * 60 * 60; // sessions self-expire after a day
const MAX_IMAGE_BYTES = 4 * 1024 * 1024; // per figure, base64 length guard

function randomCode(rng) {
  const bytes = new Uint8Array(CODE_LEN);
  rng(bytes);
  let out = '';
  for (let i = 0; i < CODE_LEN; i++) out += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
  return out;
}

// data:image/png;base64,XXXX  ->  { mimeType, dataBase64 }  (null if not a real image)
function parseDataUrl(src) {
  const m = /^data:(image\/(?:png|jpe?g|gif|webp));base64,([a-z0-9+/=\s]+)$/i.exec(String(src || ''));
  if (!m) return null;
  const dataBase64 = m[2].replace(/\s+/g, '');
  if (!dataBase64 || dataBase64.length > MAX_IMAGE_BYTES) return null;
  return { mimeType: m[1].toLowerCase(), dataBase64 };
}

export class ExamStore {
  // kv: { get(key,{type}?), put(key,value,{expirationTtl}?), delete(key) }
  // rng: (Uint8Array)=>void  (crypto.getRandomValues); ttl override for tests
  constructor(kv, { rng, ttl } = {}) {
    this.kv = kv;
    this.rng = rng || ((buf) => crypto.getRandomValues(buf));
    this.ttl = ttl || TTL_SECONDS;
  }

  async _getJSON(key) {
    const raw = await this.kv.get(key);
    if (raw == null) return null;
    try { return typeof raw === 'string' ? JSON.parse(raw) : raw; } catch (_) { return null; }
  }
  async _putJSON(key, value) {
    await this.kv.put(key, JSON.stringify(value), { expirationTtl: this.ttl });
  }

  // Called by the app's /push. Accepts an answer-key-free exam and stores it.
  // questions: [{ id, number, stemText|stem, options:[{letter,text}], images:[dataURL] }]
  // Returns { code, total }.
  async createExam({ title, questions }, htmlToText) {
    if (!Array.isArray(questions) || !questions.length) throw new Error('No questions to start');
    const code = randomCode(this.rng);

    const stored = [];
    for (let n = 0; n < questions.length; n++) {
      const q = questions[n] || {};
      // Guard against a caller that leaks the key — never persist these.
      const stemText = q.stemText != null ? String(q.stemText)
        : (htmlToText ? htmlToText(q.stem || '') : String(q.stem || ''));
      const options = Array.isArray(q.options)
        ? q.options.map((o, j) => ({
            letter: (o && o.letter) || String.fromCharCode(65 + j),
            text: htmlToText ? htmlToText(o && o.text != null ? o.text : '') : String((o && o.text) || ''),
          }))
        : [];
      const imageKeys = [];
      const imgs = Array.isArray(q.images) ? q.images : [];
      for (let i = 0; i < imgs.length; i++) {
        const parsed = parseDataUrl(imgs[i]);
        if (!parsed) continue;
        const key = `img:${code}:${n}:${imageKeys.length}`;
        await this._putJSON(key, parsed);
        imageKeys.push(key);
      }
      stored.push({
        n: n + 1, // 1-based item number as spoken ("question 5")
        id: q.id || `q${n + 1}`,
        number: q.number != null ? String(q.number) : '',
        stemText,
        options,
        imageKeys,
      });
    }

    await this._putJSON(`exam:${code}`, {
      code,
      title: title ? String(title) : 'Practice exam',
      createdAt: Date.now(),
      total: stored.length,
      questions: stored,
    });
    await this._putJSON(`ans:${code}`, { answers: {}, finished: false });
    return { code, total: stored.length };
  }

  async getExam(code) {
    return this._getJSON(`exam:${this._norm(code)}`);
  }

  async getImage(key) {
    return this._getJSON(key);
  }

  async getAnswers(code) {
    return (await this._getJSON(`ans:${this._norm(code)}`)) || { answers: {}, finished: false };
  }

  async recordAnswer(code, id, patch) {
    code = this._norm(code);
    const doc = await this.getAnswers(code);
    const prev = doc.answers[id] || {};
    doc.answers[id] = Object.assign({}, prev, patch, { answeredAt: Date.now() });
    await this._putJSON(`ans:${code}`, doc);
    return doc.answers[id];
  }

  async finishExam(code) {
    code = this._norm(code);
    const doc = await this.getAnswers(code);
    doc.finished = true;
    doc.finishedAt = Date.now();
    await this._putJSON(`ans:${code}`, doc);
    return doc;
  }

  // MCP session -> active code (so the model needn't repeat the code each turn).
  async setSessionCode(sessionId, code) {
    if (!sessionId) return;
    await this._putJSON(`sess:${sessionId}`, { code: this._norm(code) });
  }
  async getSessionCode(sessionId) {
    if (!sessionId) return null;
    const d = await this._getJSON(`sess:${sessionId}`);
    return d && d.code ? d.code : null;
  }

  _norm(code) { return String(code || '').toUpperCase().replace(/[^A-Z0-9]/g, ''); }
}

export const _internal = { randomCode, parseDataUrl, CODE_ALPHABET, CODE_LEN };
