export interface Company {
  ticker: string;
  cik: number;
  name: string;
}

export interface QuoteData {
  stock_price: number | null;
  summary: string;
  company?: Company;
  currency?: string;
  as_of?: string;
  delta_6m?: { abs: number; pct: number; from_price: number; from_date: string; high: number; low: number };
  source?: string;
  error?: string;
}

/** Summary of one filing (10-K, 10-Q, 20-F, 40-F, 8-K, 6-K). */
export interface FilingSummary {
  form: string;
  filed: string;
  url: string;
  headline: string;
  red_flags?: string[];
}

export interface InsiderSummary {
  headline: string;
  window: string;
  filings_reviewed: number;
}

/** Workflow #2 output: report categories, filled by whatever form the company files. */
export interface FilingsData {
  company: Company | null;
  profile?: { filer_type: string };
  annual?: FilingSummary;
  quarterly?: FilingSummary;
  current?: FilingSummary[];
  insider?: InsiderSummary;
  errors: string[];
}

export interface ChatResponse {
  conversation_id: string;
  reply: string;
  intent: string;
  company: Company | null;
  candidates: Company[];
  data: { quote?: QuoteData; filings?: FilingsData };
  errors: string[];
  /** LangSmith trace of this turn, when tracing is enabled on the backend. */
  trace_url?: string | null;
}

export interface HistoryResponse {
  conversation_id: string;
  messages: { role: "user" | "assistant"; content: string }[];
  company: Company | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  /** `company` is a ticker picked in the UI; it becomes the conversation's active company. */
  chat: (message: string, conversationId: string | null, company?: string | null) =>
    request<ChatResponse>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, conversation_id: conversationId, company: company ?? null }),
    }),
  searchCompanies: (q: string, limit = 8) =>
    request<{ results: Company[] }>(`/api/companies?q=${encodeURIComponent(q)}&limit=${limit}`),
  history: (conversationId: string) => request<HistoryResponse>(`/api/conversations/${conversationId}`),
  clear: (conversationId: string) =>
    request<{ ok: boolean }>(`/api/conversations/${conversationId}`, { method: "DELETE" }),
};
