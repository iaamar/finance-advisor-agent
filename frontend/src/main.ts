import "./styles.css";
import { api, type ChatResponse, type Company } from "./api";
import {
  collectSources,
  escapeHtml,
  renderCandidates,
  renderQuoteStrip,
  renderReply,
  renderSources,
} from "./render";

const STORAGE_KEY = "financeagent.conversation_id";

const thread = document.querySelector<HTMLElement>("#thread")!;
const empty = document.querySelector<HTMLElement>("#empty")!;
const form = document.querySelector<HTMLFormElement>("#composer")!;
const input = document.querySelector<HTMLTextAreaElement>("#input")!;
const sendBtn = document.querySelector<HTMLButtonElement>("#send")!;
const newChatBtn = document.querySelector<HTMLButtonElement>("#new-chat")!;
const search = document.querySelector<HTMLInputElement>("#company-search")!;
const results = document.querySelector<HTMLUListElement>("#company-results")!;
const pill = document.querySelector<HTMLElement>("#company-pill")!;
const pillText = document.querySelector<HTMLElement>("#company-pill-text")!;
const pillClear = document.querySelector<HTMLButtonElement>("#company-clear")!;

let conversationId: string | null = loadId();
let busy = false;
/** Company picked in the search box; sent with the next message, then tracked by the backend. */
let pickedTicker: string | null = null;

function loadId(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function saveId(id: string | null): void {
  try {
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable (private mode etc.) - conversation just won't persist across reloads */
  }
}

// ---- active company ---------------------------------------------------------
function showCompany(c: Company | null | undefined): void {
  if (!c) return;
  pill.hidden = false;
  pillText.textContent = `${c.ticker} · ${c.name}`;
  search.value = "";
  search.placeholder = "Switch company…";
}

function clearCompany(): void {
  pickedTicker = null;
  pill.hidden = true;
  search.placeholder = "Search any company or ticker…";
}

function pickCompany(c: Company): void {
  pickedTicker = c.ticker;
  showCompany(c);
  closeResults();
  input.focus();
  if (!input.value.trim()) input.value = `Get me up to speed on ${c.name}`;
  autosize();
}

// ---- company autocomplete (SEC company_tickers.json via /api/companies) -----
let searchTimer = 0;
let searchSeq = 0;
let activeIndex = -1;
let lastResults: Company[] = [];

function closeResults(): void {
  results.hidden = true;
  search.setAttribute("aria-expanded", "false");
  activeIndex = -1;
}

function renderResults(list: Company[]): void {
  lastResults = list;
  activeIndex = -1;
  if (!list.length) {
    results.innerHTML = `<li class="no-results">No SEC-registered company matches.</li>`;
  } else {
    results.innerHTML = list
      .map(
        (c, i) =>
          `<li role="option" id="opt-${i}" data-i="${i}"><span class="cand-ticker">${escapeHtml(c.ticker)}</span>${escapeHtml(c.name)}</li>`,
      )
      .join("");
  }
  results.hidden = false;
  search.setAttribute("aria-expanded", "true");
}

function highlight(i: number): void {
  const items = results.querySelectorAll<HTMLLIElement>("li[role=option]");
  items.forEach((li, j) => li.classList.toggle("active", j === i));
  activeIndex = i;
  if (items[i]) search.setAttribute("aria-activedescendant", items[i].id);
}

search.addEventListener("input", () => {
  const q = search.value.trim();
  window.clearTimeout(searchTimer);
  if (!q) return closeResults();
  searchTimer = window.setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const { results: list } = await api.searchCompanies(q);
      if (seq === searchSeq) renderResults(list);
    } catch {
      closeResults();
    }
  }, 150);
});

search.addEventListener("keydown", (e) => {
  if (results.hidden) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    highlight(Math.min(activeIndex + 1, lastResults.length - 1));
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    highlight(Math.max(activeIndex - 1, 0));
  } else if (e.key === "Enter") {
    e.preventDefault();
    const c = lastResults[activeIndex >= 0 ? activeIndex : 0];
    if (c) pickCompany(c);
  } else if (e.key === "Escape") {
    closeResults();
  }
});

results.addEventListener("mousedown", (e) => {
  const li = (e.target as HTMLElement).closest<HTMLLIElement>("li[data-i]");
  if (!li) return;
  e.preventDefault(); // keep focus handling predictable
  pickCompany(lastResults[Number(li.dataset.i)]);
});

search.addEventListener("blur", () => window.setTimeout(closeResults, 100));
pillClear.addEventListener("click", clearCompany);

// ---- thread -------------------------------------------------------------------
function scrollToBottom(): void {
  thread.scrollTo({ top: thread.scrollHeight, behavior: "smooth" });
}

function addUserMessage(text: string): void {
  empty.hidden = true;
  const el = document.createElement("div");
  el.className = "msg msg-user";
  el.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  thread.appendChild(el);
  scrollToBottom();
}

function addAssistantMessage(html: string, extraClass = ""): HTMLElement {
  empty.hidden = true;
  const el = document.createElement("div");
  el.className = `msg msg-assistant ${extraClass}`.trim();
  el.innerHTML = `<div class="bubble">${html}</div>`;
  thread.appendChild(el);
  // Long answers: show the start of the reply, not its end.
  if (extraClass.includes("msg-pending")) scrollToBottom();
  else el.scrollIntoView({ block: "start" });
  return el;
}

const PENDING_STEPS = ["Reading the question", "Finding the company", "Fetching quote data", "Pulling SEC filings", "Summarizing"];

function addPending(): () => void {
  const el = addAssistantMessage(
    `<div class="pending"><span class="dots"><i></i><i></i><i></i></span><span class="pending-text">${PENDING_STEPS[0]}…</span></div>`,
    "msg-pending",
  );
  const label = el.querySelector<HTMLElement>(".pending-text")!;
  let i = 0;
  const timer = window.setInterval(() => {
    i = Math.min(i + 1, PENDING_STEPS.length - 1);
    label.textContent = `${PENDING_STEPS[i]}…`;
  }, 2000);
  return () => {
    window.clearInterval(timer);
    el.remove();
  };
}

function renderResponse(res: ChatResponse, question: string): void {
  const html =
    renderQuoteStrip(res) +
    `<div class="reply">${renderReply(res.reply)}</div>` +
    renderCandidates(res) +
    renderSources(collectSources(res));
  const el = addAssistantMessage(html);
  // "Did you mean": re-ask the same question about the chosen company.
  el.querySelectorAll<HTMLButtonElement>(".candidate").forEach((b) =>
    b.addEventListener("click", () => {
      const c = { ticker: b.dataset.ticker!, name: b.dataset.name!, cik: 0 };
      pickedTicker = c.ticker;
      showCompany(c);
      void send(question, `${question} (${c.name}, ${c.ticker})`);
    }),
  );
}

async function send(text: string, display?: string): Promise<void> {
  const message = text.trim();
  if (!message || busy) return;
  busy = true;
  sendBtn.disabled = true;
  input.value = "";
  autosize();
  addUserMessage(display ?? message);
  const stopPending = addPending();
  try {
    const res = await api.chat(message, conversationId, pickedTicker);
    pickedTicker = null; // the backend now holds it as the active company
    conversationId = res.conversation_id;
    saveId(conversationId);
    showCompany(res.company);
    stopPending();
    renderResponse(res, message);
  } catch (err) {
    stopPending();
    const msg = err instanceof Error ? err.message : String(err);
    addAssistantMessage(`<p>Something went wrong: ${escapeHtml(msg)}. Please try again.</p>`, "msg-error");
  } finally {
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

async function restore(): Promise<void> {
  if (!conversationId) return;
  try {
    const h = await api.history(conversationId);
    if (!h.messages.length) return;
    for (const m of h.messages) {
      if (m.role === "user") addUserMessage(m.content);
      else addAssistantMessage(`<div class="reply">${renderReply(m.content)}</div>`);
    }
    showCompany(h.company);
  } catch {
    /* expired or backend down: start fresh */
  }
}

function autosize(): void {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  void send(input.value);
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void send(input.value);
  }
});
input.addEventListener("input", autosize);

document.querySelectorAll<HTMLButtonElement>(".suggestion").forEach((b) =>
  b.addEventListener("click", () => void send(b.textContent ?? "")),
);

newChatBtn.addEventListener("click", async () => {
  if (conversationId) {
    try {
      await api.clear(conversationId);
    } catch {
      /* ignore */
    }
  }
  conversationId = null;
  saveId(null);
  thread.querySelectorAll(".msg").forEach((m) => m.remove());
  empty.hidden = false;
  clearCompany();
  input.focus();
});

void restore();
input.focus();
