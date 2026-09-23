import type { ChatResponse } from "./api";

const escapeHtml = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

/** Inline formatting on already-escaped text: links and **bold**. */
function inline(s: string): string {
  return s
    .replace(/(https?:\/\/[^\s)]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

/**
 * Render the agent's plain-text reply: paragraphs, "-"/"•" bullet lists and
 * short heading-like lines ending in ":". Everything is escaped first.
 */
export function renderReply(text: string): string {
  const blocks: string[] = [];
  let list: string[] = [];
  let para: string[] = [];

  const flushList = () => {
    if (list.length) blocks.push(`<ul>${list.map((li) => `<li>${inline(li)}</li>`).join("")}</ul>`);
    list = [];
  };
  const flushPara = () => {
    if (para.length) blocks.push(`<p>${inline(para.join(" "))}</p>`);
    para = [];
  };

  for (const raw of escapeHtml(text).split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^(?:[-•*]|\d+\.)\s+(.*)$/);
    if (!line) {
      flushList();
      flushPara();
    } else if (bullet) {
      flushPara();
      list.push(bullet[1]);
    } else if (/^#{1,3}\s/.test(line) || (line.endsWith(":") && line.length < 70)) {
      flushList();
      flushPara();
      blocks.push(`<h4>${inline(line.replace(/^#{1,3}\s/, ""))}</h4>`);
    } else {
      flushList();
      para.push(line);
    }
  }
  flushList();
  flushPara();
  return blocks.join("");
}

interface Source {
  label: string;
  url?: string;
}

/** Collect the documents a reply was grounded in, for the sources footer. */
export function collectSources(res: ChatResponse): Source[] {
  const out: Source[] = [];
  const q = res.data.quote;
  if (q && q.stock_price != null && q.as_of) {
    out.push({ label: `Quote · ${q.source ?? "provider"} · ${q.as_of.slice(0, 16).replace("T", " ")} UTC` });
  }
  const f = res.data.filings;
  if (f) {
    for (const s of [f.annual, f.quarterly, ...(f.current ?? [])]) {
      if (s) out.push({ label: `${s.form} · filed ${s.filed}`, url: s.url });
    }
    if (f.insider && f.insider.filings_reviewed) {
      out.push({ label: `Form 4 × ${f.insider.filings_reviewed} · ${f.insider.window}` });
    }
  }
  return out;
}

export function renderSources(sources: Source[], traceUrl?: string | null): string {
  const trace = traceUrl
    ? `<a class="source source-trace" href="${escapeHtml(traceUrl)}" target="_blank" rel="noopener noreferrer">View trace ↗</a>`
    : "";
  if (!sources.length) return trace ? `<div class="sources">${trace}</div>` : "";
  const items = sources
    .map((s) =>
      s.url
        ? `<a class="source" href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(s.label)}</a>`
        : `<span class="source">${escapeHtml(s.label)}</span>`,
    )
    .join("");
  return `<div class="sources"><span class="sources-label">Sources</span>${items}${trace}</div>`;
}

export function renderQuoteStrip(res: ChatResponse): string {
  const q = res.data.quote;
  if (!q || q.stock_price == null || !q.delta_6m || !q.company) return "";
  const up = q.delta_6m.pct >= 0;
  const sign = up ? "+" : "−";
  return `
    <div class="quote-strip">
      <span class="qs-ticker">${escapeHtml(q.company.ticker)}</span>
      <span class="qs-price">${q.stock_price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} <small>${escapeHtml(q.currency ?? "")}</small></span>
      <span class="qs-delta ${up ? "up" : "down"}">${sign}${Math.abs(q.delta_6m.pct).toFixed(2)}% <small>6M</small></span>
      <span class="qs-range">6M range ${q.delta_6m.low.toFixed(2)}–${q.delta_6m.high.toFixed(2)}</span>
    </div>`;
}

/** "Did you mean" options when a name matched several registrants. */
export function renderCandidates(res: ChatResponse): string {
  if (!res.candidates?.length) return "";
  const chips = res.candidates
    .map(
      (c) =>
        `<button type="button" class="candidate" data-ticker="${escapeHtml(c.ticker)}" data-name="${escapeHtml(c.name)}">` +
        `<span class="cand-ticker">${escapeHtml(c.ticker)}</span> ${escapeHtml(c.name)}</button>`,
    )
    .join("");
  return `<div class="candidates">${chips}</div>`;
}

export { escapeHtml };
