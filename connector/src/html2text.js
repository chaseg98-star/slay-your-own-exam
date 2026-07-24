// Turn the app's sanitized stem HTML into clean, speakable text.
//
// The stem stored by "Slay Your Own Exam" is a small, whitelisted subset of
// HTML (see sanitizeStemHTML in index.html): p, span, div, table/tr/td/th,
// b/i/em/strong/u, br. Lab panels are rendered as <table> rows. We only ever
// see that controlled subset, so a careful string transform (rather than a full
// DOM parser) is enough and keeps this file dependency-free so it runs both on
// Cloudflare Workers and under `node --test`.

const ENTITIES = {
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'",
  '&apos;': "'", '&nbsp;': ' ', '&mdash;': '—', '&ndash;': '–',
  '&times;': '×', '&deg;': '°', '&mu;': 'µ', '&plusmn;': '±',
};

function decodeEntities(s) {
  return String(s)
    .replace(/&#(\d+);/g, (_, n) => {
      const code = parseInt(n, 10);
      return code > 0 && code < 0x10ffff ? String.fromCodePoint(code) : '';
    })
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => {
      const code = parseInt(h, 16);
      return code > 0 && code < 0x10ffff ? String.fromCodePoint(code) : '';
    })
    .replace(/&[a-z0-9]+;/gi, (m) => (ENTITIES[m] != null ? ENTITIES[m] : ' '));
}

// Convert a single <table>...</table> into readable lines. Each row becomes its
// cells joined by " — " (an em dash), which reads naturally aloud for lab
// panels like "Sodium — 142 mEq/L — (136-145)".
function tableToLines(tableHtml) {
  const rows = [];
  const trRe = /<tr\b[^>]*>([\s\S]*?)<\/tr>/gi;
  let m;
  while ((m = trRe.exec(tableHtml))) {
    const cells = [];
    const cellRe = /<t[hd]\b[^>]*>([\s\S]*?)<\/t[hd]>/gi;
    let c;
    while ((c = cellRe.exec(m[1]))) {
      const text = stripTags(c[1]).replace(/\s+/g, ' ').trim();
      if (text) cells.push(text);
    }
    if (cells.length) rows.push(cells.join(' — '));
  }
  return rows.join('\n');
}

function stripTags(s) {
  return decodeEntities(String(s).replace(/<[^>]+>/g, ''));
}

// Main entry: HTML stem -> plain speakable text.
export function htmlToText(html) {
  let s = String(html == null ? '' : html);

  // Pull tables out first and replace each with its line-based rendering,
  // bracketed by blank lines so the labs read as their own section.
  s = s.replace(/<table\b[^>]*>([\s\S]*?)<\/table>/gi, (_, inner) => {
    const lines = tableToLines(inner);
    return lines ? '\n\n' + lines + '\n\n' : '\n';
  });

  // Block boundaries -> newlines.
  s = s
    .replace(/<\s*br\s*\/?\s*>/gi, '\n')
    .replace(/<\/(p|div|h[1-6]|li|tr)\s*>/gi, '\n')
    .replace(/<\s*li\b[^>]*>/gi, '• ');

  // Everything else: drop tags, decode entities.
  s = stripTags(s);

  // Tidy whitespace: collapse runs of spaces/tabs, cap blank lines at one.
  s = s
    .replace(/[ \t ]+/g, ' ')
    .replace(/ *\n */g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  return s;
}

export const _internal = { decodeEntities, stripTags, tableToLines };
