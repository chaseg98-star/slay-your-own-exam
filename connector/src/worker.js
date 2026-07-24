// Cloudflare Worker entry point for the Slay-Your-Own-Exam voice connector.
//
// Three surfaces on one Worker:
//   POST /push          (browser app)   -> store an answer-key-free exam, return { code }
//   GET  /pull/<code>   (browser app)   -> the user's spoken answers so far + finished flag
//   POST /mcp           (Claude)        -> the MCP connector Claude adds in its settings
//
// Requires a KV namespace bound as EXAMS (see wrangler.toml).

import { ExamStore } from './store.js';
import { htmlToText } from './html2text.js';
import { handleRpc, SERVER_INFO } from './mcp.js';

function corsHeaders(env, req) {
  const allow = (env && env.ALLOWED_ORIGIN) || '*';
  const origin = req.headers.get('Origin') || '';
  const allowOrigin = allow === '*' ? (origin || '*') : allow;
  return {
    'Access-Control-Allow-Origin': allowOrigin,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Mcp-Session-Id, Mcp-Protocol-Version',
    'Access-Control-Expose-Headers': 'Mcp-Session-Id',
    'Vary': 'Origin',
  };
}

function json(obj, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
  });
}

const INFO_HTML = `<!doctype html><meta charset="utf-8"><title>Slay Your Own Exam — voice connector</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#123}</style>
<h1>🎙️ Slay Your Own Exam — voice connector</h1>
<p>This is the backend that lets Claude read your practice exam aloud, hands-free.</p>
<p>Add it to Claude as a custom connector using this URL with <code>/mcp</code> on the end:</p>
<pre>&lt;this-url&gt;/mcp</pre>
<p>It never receives the answer key, so Claude can quiz you but can't tell you the answers. Grading happens back in the app.</p>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '') || '/';

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(env, request) });
    }

    if (!env || !env.EXAMS) {
      return json({ error: 'Connector misconfigured: no EXAMS KV namespace bound.' }, { status: 500, headers: corsHeaders(env, request) });
    }
    const store = new ExamStore(env.EXAMS);

    try {
      // --- App: push an answer-key-free exam, get a pairing code ---
      if (path === '/push' && request.method === 'POST') {
        const payload = await request.json().catch(() => null);
        if (!payload || !Array.isArray(payload.questions) || !payload.questions.length) {
          return json({ error: 'Expected { title, questions:[...] }' }, { status: 400, headers: corsHeaders(env, request) });
        }
        // Defensively drop any key fields a caller might have included.
        const clean = payload.questions.map((q) => ({
          id: q.id, number: q.number, stem: q.stem, stemText: q.stemText,
          options: Array.isArray(q.options) ? q.options.map((o) => ({ letter: o.letter, text: o.text })) : [],
          images: Array.isArray(q.images) ? q.images : [],
        }));
        const { code, total } = await store.createExam({ title: payload.title, questions: clean }, htmlToText);
        return json({ code, total }, { headers: corsHeaders(env, request) });
      }

      // --- App: pull the spoken answers back for grading ---
      if (path.startsWith('/pull/') && request.method === 'GET') {
        const code = path.slice('/pull/'.length);
        const exam = await store.getExam(code);
        if (!exam) return json({ error: 'unknown code' }, { status: 404, headers: corsHeaders(env, request) });
        const ansDoc = await store.getAnswers(code);
        const answers = ansDoc.answers || {};
        const answered = Object.values(answers).filter((a) => a && a.selected && a.selected !== 'skip').length;
        return json({
          finished: !!ansDoc.finished,
          total: exam.total,
          answered,
          answers, // { <questionId>: { selected, flagged, ... } }
        }, { headers: corsHeaders(env, request) });
      }

      // --- Claude: the MCP connector endpoint ---
      if (path === '/mcp') {
        if (request.method === 'GET') {
          // We don't offer a server-initiated SSE stream; tell clients so.
          return new Response('Method Not Allowed', { status: 405, headers: corsHeaders(env, request) });
        }
        if (request.method !== 'POST') {
          return new Response('Method Not Allowed', { status: 405, headers: corsHeaders(env, request) });
        }
        const body = await request.json().catch(() => null);
        if (body == null) return json({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } }, { status: 400 });

        // Session id: reuse the client's, or mint one on initialize.
        let sessionId = request.headers.get('Mcp-Session-Id') || '';
        const isInit = (Array.isArray(body) ? body : [body]).some((m) => m && m.method === 'initialize');
        if (!sessionId && isInit) sessionId = crypto.randomUUID();

        const { responses } = await handleRpc(body, { store, sessionId });
        const headers = { 'Content-Type': 'application/json' };
        if (sessionId) headers['Mcp-Session-Id'] = sessionId;
        if (responses == null) return new Response(null, { status: 202, headers: { 'Mcp-Session-Id': sessionId || '' } });
        return new Response(JSON.stringify(responses), { status: 200, headers });
      }

      if (path === '/' || path === '/health') {
        if (path === '/health') return json({ ok: true, server: SERVER_INFO });
        return new Response(INFO_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
      }

      return new Response('Not found', { status: 404, headers: corsHeaders(env, request) });
    } catch (e) {
      return json({ error: String((e && e.message) || e) }, { status: 500, headers: corsHeaders(env, request) });
    }
  },
};
