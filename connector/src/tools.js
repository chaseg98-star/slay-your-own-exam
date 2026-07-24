// MCP tool definitions + handlers for the voice-exam connector.
//
// Every handler is answer-key-free by construction: the store never holds the
// correct option or explanation, so none of these can return it. Tools accept
// an explicit `code`, but also fall back to the code remembered for the current
// MCP session (set by start_exam) so the user need not repeat it each turn.

const LETTER_RE = /^[A-E]$/;

function textBlock(text) { return { type: 'text', text: String(text) }; }
function ok(blocks) { return { content: Array.isArray(blocks) ? blocks : [textBlock(blocks)] }; }
function fail(text) { return { content: [textBlock(text)], isError: true }; }

// Resolve which exam code a call applies to: explicit arg wins, else the
// session's active code. Returns { code } or throws a friendly error.
async function resolveCode(store, sessionId, args) {
  const explicit = args && args.code ? String(args.code) : '';
  const code = explicit || (await store.getSessionCode(sessionId)) || '';
  if (!code) throw new Error('No exam is active. Ask the user to say their exam code, then call start_exam.');
  return code;
}

async function loadQuestion(store, code, n) {
  const exam = await store.getExam(code);
  if (!exam) throw new Error(`No exam found for code ${code}. Double-check the code with the user.`);
  const total = exam.total;
  let idx = Number.isFinite(n) ? Math.round(n) : NaN;
  if (!Number.isFinite(idx)) idx = 1;
  idx = Math.min(Math.max(idx, 1), total);
  const q = exam.questions[idx - 1];
  return { exam, q, idx, total };
}

// Render a question into MCP content blocks: one text block (stem + labs +
// options) followed by an image block per figure so the model can describe it.
async function renderQuestion(store, exam, q, idx, total) {
  const lines = [`Question ${idx} of ${total}.`, '', q.stemText || '(no stem text)'];
  if (q.imageKeys && q.imageKeys.length) {
    lines.push('', `[There ${q.imageKeys.length === 1 ? 'is 1 figure' : `are ${q.imageKeys.length} figures`} with this question — attached below. Describe ${q.imageKeys.length === 1 ? 'it' : 'them'} to the user.]`);
  }
  lines.push('', 'Options:');
  (q.options || []).forEach((o) => lines.push(`${o.letter}. ${o.text}`));
  lines.push('', "Ask the user for their answer. Do not reveal or hint at which option is correct.");

  const blocks = [textBlock(lines.join('\n'))];
  for (const key of q.imageKeys || []) {
    const img = await store.getImage(key);
    if (img && img.dataBase64) blocks.push({ type: 'image', data: img.dataBase64, mimeType: img.mimeType });
  }
  return blocks;
}

function countState(exam, ansDoc) {
  const answers = (ansDoc && ansDoc.answers) || {};
  let answered = 0;
  const flagged = [];
  const unanswered = [];
  for (const q of exam.questions) {
    const a = answers[q.id];
    const has = a && a.selected && a.selected !== 'skip';
    if (has) answered++; else unanswered.push(q.n);
    if (a && a.flagged) flagged.push(q.n);
  }
  return { answered, total: exam.total, flagged, unanswered };
}

export const TOOL_DEFS = [
  {
    name: 'start_exam',
    description: 'Begin a voice exam session. Call this first, with the 5-character code the user reads aloud from the app. Returns the exam title and how many questions there are. Does NOT return any answers.',
    inputSchema: {
      type: 'object',
      properties: { code: { type: 'string', description: 'The 5-character exam code shown in the app (e.g. "ABC23").' } },
      required: ['code'],
    },
  },
  {
    name: 'get_question',
    description: 'Get one question to read aloud: its stem, any lab panel, its answer options, and any figures (attached as images for you to describe). Never includes the correct answer. Pass n for a specific item, or omit to (re)read the current one.',
    inputSchema: {
      type: 'object',
      properties: {
        n: { type: 'number', description: 'The item number to read (1-based). Omit to reread the current item.' },
        code: { type: 'string', description: 'Exam code. Optional if start_exam was already called this session.' },
      },
    },
  },
  {
    name: 'next_question',
    description: 'Advance to and return the next question. Never includes the correct answer.',
    inputSchema: { type: 'object', properties: { code: { type: 'string' } } },
  },
  {
    name: 'record_answer',
    description: "Record the user's spoken answer for a question. choice is a letter A–E, or 'skip'. Optionally flag the item. Confirms what was saved but NEVER says whether it is correct.",
    inputSchema: {
      type: 'object',
      properties: {
        choice: { type: 'string', description: "The answer letter A–E, or 'skip'." },
        n: { type: 'number', description: 'Item number (1-based). Omit to use the current item.' },
        flag: { type: 'boolean', description: 'Flag this item for later review.' },
        code: { type: 'string' },
      },
      required: ['choice'],
    },
  },
  {
    name: 'flag_question',
    description: 'Flag (or unflag) an item for later review, without recording an answer.',
    inputSchema: {
      type: 'object',
      properties: {
        n: { type: 'number' },
        on: { type: 'boolean', description: 'true to flag (default), false to unflag.' },
        code: { type: 'string' },
      },
    },
  },
  {
    name: 'exam_status',
    description: 'Report progress: how many answered, which items are flagged, and which are still unanswered. No answer key.',
    inputSchema: { type: 'object', properties: { code: { type: 'string' } } },
  },
  {
    name: 'finish_exam',
    description: "Mark the exam finished when the user is done. Their answers sync back to the app to be graded — do not score it yourself.",
    inputSchema: { type: 'object', properties: { code: { type: 'string' } } },
  },
];

export async function callTool(name, args, { store, sessionId }) {
  args = args || {};
  try {
    switch (name) {
      case 'start_exam': {
        const code = String(args.code || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
        if (!code) return fail('Please ask the user to read the 5-character exam code from the app.');
        const exam = await store.getExam(code);
        if (!exam) return fail(`No exam found for code ${code}. Ask the user to re-read the code, or to tap "Voice exam" in the app again for a fresh code.`);
        await store.setSessionCode(sessionId, code);
        const ans = await store.getAnswers(code);
        const { answered } = countState(exam, ans);
        const resume = answered ? ` ${answered} of ${exam.total} already answered — you can continue.` : '';
        return ok(`Exam "${exam.title}" is ready: ${exam.total} question${exam.total === 1 ? '' : 's'}.${resume}\nSay you're starting, then call get_question to read the first item. Read one question at a time and never reveal the correct answer.`);
      }

      case 'get_question': {
        const code = await resolveCode(store, sessionId, args);
        const { exam, q, idx, total } = await loadQuestion(store, code, args.n);
        return ok(await renderQuestion(store, exam, q, idx, total));
      }

      case 'next_question': {
        const code = await resolveCode(store, sessionId, args);
        // "next" relative to the highest-numbered answered item, else item 1.
        const exam = await store.getExam(code);
        if (!exam) return fail(`No exam found for code ${code}.`);
        const ans = await store.getAnswers(code);
        let last = 0;
        for (const qq of exam.questions) { if (ans.answers[qq.id]) last = Math.max(last, qq.n); }
        const { q, idx, total } = await loadQuestion(store, code, Math.min(last + 1, exam.total));
        return ok(await renderQuestion(store, exam, q, idx, total));
      }

      case 'record_answer': {
        const code = await resolveCode(store, sessionId, args);
        const { exam, q, idx } = await loadQuestion(store, code, args.n);
        let choice = String(args.choice || '').trim().toUpperCase();
        if (choice === 'SKIP' || choice === 'NONE' || choice === '') choice = 'skip';
        else {
          choice = choice[0]; // tolerate "B." / "B)" / "Bravo"->"B"
          if (!LETTER_RE.test(choice)) return fail(`"${args.choice}" isn't a valid choice. Ask the user for a letter A through E, or "skip".`);
          const maxLetter = String.fromCharCode(64 + (q.options ? q.options.length : 5));
          if (choice > maxLetter) return fail(`Question ${idx} only goes up to ${maxLetter}. Please re-ask.`);
        }
        const patch = { selected: choice };
        if (args.flag != null) patch.flagged = !!args.flag;
        await store.recordAnswer(code, q.id, patch);
        const ansDoc = await store.getAnswers(code);
        const { answered, total } = countState(exam, ansDoc);
        const said = choice === 'skip' ? 'skipped' : `answer ${choice}`;
        return ok(`Recorded ${said} for question ${idx}. ${answered} of ${total} answered.${args.flag ? ' Flagged for review.' : ''} (Not saying if it's right — that's graded in the app.)`);
      }

      case 'flag_question': {
        const code = await resolveCode(store, sessionId, args);
        const { q, idx } = await loadQuestion(store, code, args.n);
        const on = args.on == null ? true : !!args.on;
        await store.recordAnswer(code, q.id, { flagged: on });
        return ok(`${on ? 'Flagged' : 'Unflagged'} question ${idx}.`);
      }

      case 'exam_status': {
        const code = await resolveCode(store, sessionId, args);
        const exam = await store.getExam(code);
        if (!exam) return fail(`No exam found for code ${code}.`);
        const ansDoc = await store.getAnswers(code);
        const { answered, total, flagged, unanswered } = countState(exam, ansDoc);
        const parts = [`${answered} of ${total} answered.`];
        parts.push(flagged.length ? `Flagged: ${flagged.join(', ')}.` : 'Nothing flagged.');
        if (unanswered.length) parts.push(`Not yet answered: ${unanswered.slice(0, 25).join(', ')}${unanswered.length > 25 ? '…' : ''}.`);
        else parts.push('Every question is answered.');
        if (ansDoc.finished) parts.push('This exam is marked finished.');
        return ok(parts.join(' '));
      }

      case 'finish_exam': {
        const code = await resolveCode(store, sessionId, args);
        const exam = await store.getExam(code);
        if (!exam) return fail(`No exam found for code ${code}.`);
        await store.finishExam(code);
        const ansDoc = await store.getAnswers(code);
        const { answered, total, flagged } = countState(exam, ansDoc);
        return ok(`Done. ${answered} of ${total} answered${flagged.length ? `, ${flagged.length} flagged` : ''}. Your answers are saved and will sync back to the app to be graded on your device. Nice work — I'm not scoring it here.`);
      }

      default:
        return fail(`Unknown tool: ${name}`);
    }
  } catch (e) {
    return fail(e && e.message ? e.message : 'Something went wrong handling that.');
  }
}

export const _internal = { countState, resolveCode, loadQuestion };
