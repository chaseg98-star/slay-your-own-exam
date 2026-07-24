import { test } from 'node:test';
import assert from 'node:assert/strict';
import { htmlToText } from '../src/html2text.js';

test('decodes entities and normalizes whitespace', () => {
  const out = htmlToText('<p>A 54-year-old man &amp; his   dog</p>');
  assert.equal(out, 'A 54-year-old man & his dog');
});

test('br and block tags become newlines', () => {
  const out = htmlToText('Line one<br>Line two</p><div>Line three</div>');
  assert.equal(out, 'Line one\nLine two\nLine three');
});

test('lab tables render as speakable "name — value — range" lines', () => {
  const html = `Vignette here.
    <table class="labtable">
      <tr><td class="lk">Sodium</td><td class="lv">142 mEq/L</td><td class="lh">136-145</td></tr>
      <tr><td class="lk">Potassium</td><td class="lv">5.9 mEq/L</td><td class="lh">3.5-5.0</td></tr>
    </table>`;
  const out = htmlToText(html);
  assert.match(out, /Sodium — 142 mEq\/L — 136-145/);
  assert.match(out, /Potassium — 5\.9 mEq\/L — 3\.5-5\.0/);
});

test('strips scripty leftovers and never keeps tags', () => {
  const out = htmlToText('<span class="hl">Important</span> value');
  assert.equal(out, 'Important value');
  assert.doesNotMatch(out, /</);
});
