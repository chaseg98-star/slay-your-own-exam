import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ExamStore } from '../src/store.js';
import { htmlToText } from '../src/html2text.js';
import { handleRpc } from '../src/mcp.js';
import worker from '../src/worker.js';

// ---- In-memory KV mock (get/put/delete), matching Workers KV's async API ----
function makeKV() {
  const map = new Map();
  return {
    map,
    async get(key) { return map.has(key) ? map.get(key) : null; },
    async put(key, value) { map.set(key, value); },
    async delete(key) { map.delete(key); },
  };
}

// Deterministic rng so the pairing code is stable in tests.
const seqRng = (buf) => { for (let i = 0; i < buf.length; i++) buf[i] = i; };

// A question object that DELIBERATELY carries the answer key, to prove the
// connector strips it. 'correct' and the explanation must never surface.
const SECRET_CORRECT = 'C';
const SECRET_EXPLANATION = 'SECRET-EXPLANATION-because-of-mechanism-X';
function sampleQuestions() {
  return [
    {
      id: 'qa', number: '1',
      stem: '<p>A 60-year-old man with chest pain.</p><table><tr><td>Troponin</td><td>0.9 ng/mL</td><td>&lt;0.04</td></tr></table>',
      options: [
        { letter: 'A', text: 'Aspirin' }, { letter: 'B', text: 'Heparin' },
        { letter: 'C', text: 'PCI' }, { letter: 'D', text: 'Observation' },
      ],
      correct: SECRET_CORRECT,
      explanation: SECRET_EXPLANATION,
      images: ['data:image/png;base64,iVBORw0KGgo='],
    },
    {
      id: 'qb', number: '2',
      stem: '<p>A child with a rash.</p>',
      options: [
        { letter: 'A', text: 'Measles' }, { letter: 'B', text: 'Rubella' },
        { letter: 'C', text: 'Roseola' },
      ],
      correct: 'A', explanation: SECRET_EXPLANATION,
      images: [],
    },
  ];
}

function rpc(store) {
  const sessionId = 'test-session';
  return async (method, params) =>
    (await handleRpc({ jsonrpc: '2.0', id: 1, method, params }, { store, sessionId })).responses;
}

test('createExam strips the answer key and stores figures separately', async () => {
  const store = new ExamStore(makeKV(), { rng: seqRng });
  const { code, total } = await store.createExam({ title: 'Cards', questions: sampleQuestions() }, htmlToText);
  assert.equal(total, 2);
  const exam = await store.getExam(code);
  const serialized = JSON.stringify(exam);
  assert.doesNotMatch(serialized, /correct/, 'stored exam must not contain a "correct" field');
  assert.ok(!serialized.includes(SECRET_EXPLANATION), 'stored exam must not contain the explanation');
  // Figure bytes live under their own key, not inline in the exam doc.
  assert.ok(!serialized.includes('iVBORw0KGgo='), 'image bytes must not be inline in the exam doc');
  assert.equal(exam.questions[0].imageKeys.length, 1);
  const img = await store.getImage(exam.questions[0].imageKeys[0]);
  assert.equal(img.mimeType, 'image/png');
});

test('MCP: initialize advertises tools + proctor instructions', async () => {
  const store = new ExamStore(makeKV(), { rng: seqRng });
  const res = await rpc(store)('initialize', { protocolVersion: '2025-06-18' });
  assert.equal(res.result.protocolVersion, '2025-06-18');
  assert.ok(res.result.capabilities.tools);
  assert.match(res.result.instructions, /never reveal/i);
});

test('MCP: tools/list exposes the exam tools', async () => {
  const store = new ExamStore(makeKV(), { rng: seqRng });
  const res = await rpc(store)('tools/list', {});
  const names = res.result.tools.map((t) => t.name);
  for (const n of ['start_exam', 'get_question', 'next_question', 'record_answer', 'exam_status', 'finish_exam']) {
    assert.ok(names.includes(n), `missing tool ${n}`);
  }
});

test('full voice round-trip never leaks the answer, and records choices', async () => {
  const store = new ExamStore(makeKV(), { rng: seqRng });
  const { code } = await store.createExam({ title: 'Cards', questions: sampleQuestions() }, htmlToText);
  const call = rpc(store);

  // start_exam remembers the code for the session
  const started = await call('tools/call', { name: 'start_exam', arguments: { code } });
  assert.match(started.result.content[0].text, /2 questions/);

  // get_question returns stem + labs + options + an image block, but no key
  const q1 = await call('tools/call', { name: 'get_question', arguments: {} });
  const textBlock = q1.result.content.find((b) => b.type === 'text').text;
  assert.match(textBlock, /Troponin — 0\.9 ng\/mL/);      // labs read cleanly
  assert.match(textBlock, /A\. Aspirin/);                   // options present
  assert.ok(!textBlock.includes(SECRET_EXPLANATION));       // no explanation
  assert.doesNotMatch(textBlock, /correct answer/i);        // no key
  const imgBlock = q1.result.content.find((b) => b.type === 'image');
  assert.ok(imgBlock && imgBlock.data, 'figure should be attached for Claude to describe');

  // record an answer (tolerate "C)" style) — confirmation must not judge it
  const rec = await call('tools/call', { name: 'record_answer', arguments: { choice: 'C)', n: 1 } });
  assert.match(rec.result.content[0].text, /Recorded answer C/);
  // Must not deliver a verdict. (The reassurance "not saying if it's right" is fine.)
  assert.doesNotMatch(rec.result.content[0].text, /\b(is correct|is right|incorrect|that's right|that's wrong|well done|good job|nice)\b/i);

  // status reflects progress
  const stat = await call('tools/call', { name: 'exam_status', arguments: {} });
  assert.match(stat.result.content[0].text, /1 of 2 answered/);

  // finish + confirm the answers are retrievable for the app to grade
  await call('tools/call', { name: 'record_answer', arguments: { choice: 'A', n: 2 } });
  const fin = await call('tools/call', { name: 'finish_exam', arguments: {} });
  assert.match(fin.result.content[0].text, /graded/i);

  const ans = await store.getAnswers(code);
  assert.equal(ans.finished, true);
  assert.equal(ans.answers.qa.selected, 'C');
  assert.equal(ans.answers.qb.selected, 'A');
});

test('record_answer rejects out-of-range letters', async () => {
  const store = new ExamStore(makeKV(), { rng: seqRng });
  const { code } = await store.createExam({ title: 'x', questions: sampleQuestions() }, htmlToText);
  const call = rpc(store);
  await call('tools/call', { name: 'start_exam', arguments: { code } });
  const res = await call('tools/call', { name: 'record_answer', arguments: { choice: 'E', n: 2 } });
  assert.equal(res.result.isError, true); // question 2 only has A–C
});

// ---- Worker integration: /push -> /mcp -> /pull, as the app + Claude use it ----
test('worker: push exam, answer over MCP, pull answers back', async () => {
  const env = { EXAMS: makeKV() };
  const base = 'https://connector.example.workers.dev';

  // App pushes an answer-key-free exam. (Even if the key sneaks in, worker strips it.)
  const pushRes = await worker.fetch(new Request(`${base}/push`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'Block 1', questions: sampleQuestions() }),
  }), env);
  assert.equal(pushRes.status, 200);
  const { code } = await pushRes.json();
  assert.ok(/^[A-Z0-9]{5}$/.test(code));

  // The stored exam must not contain the secret explanation anywhere.
  for (const v of env.EXAMS.map.values()) {
    assert.ok(!String(v).includes(SECRET_EXPLANATION), 'answer key leaked into storage');
  }

  // Claude initializes and gets a session id.
  const init = await worker.fetch(new Request(`${base}/mcp`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }),
  }), env);
  const sessionId = init.headers.get('Mcp-Session-Id');
  assert.ok(sessionId, 'initialize should mint a session id');

  const mcp = async (msg) => {
    const r = await worker.fetch(new Request(`${base}/mcp`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Mcp-Session-Id': sessionId },
      body: JSON.stringify(msg),
    }), env);
    return r.json();
  };

  await mcp({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'start_exam', arguments: { code } } });
  await mcp({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'record_answer', arguments: { choice: 'B', n: 1 } } });
  await mcp({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'finish_exam', arguments: {} } });

  // App pulls answers back for grading.
  const pull = await worker.fetch(new Request(`${base}/pull/${code}`), env);
  assert.equal(pull.status, 200);
  const body = await pull.json();
  assert.equal(body.finished, true);
  assert.equal(body.total, 2);
  assert.equal(body.answers.qa.selected, 'B');
});

test('worker: unknown pull code is a 404', async () => {
  const env = { EXAMS: makeKV() };
  const res = await worker.fetch(new Request('https://c.example.workers.dev/pull/ZZZZZ'), env);
  assert.equal(res.status, 404);
});
