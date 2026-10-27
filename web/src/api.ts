export type RunStatus = "running" | "awaiting_approval" | "completed" | "failed" | "budget_exceeded";

export interface Ticket {
  id: string;
  customer_email: string;
  subject: string;
  status: string;
  last_run_id: string | null;
  last_run_status: RunStatus | null;
}

export interface RunSummary {
  id: string;
  ticket_id: string;
  status: RunStatus;
  model: string;
  provider: string;
  llm_calls: number;
  tool_calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  tainted: boolean;
  created_at: string;
}

export interface RunEvent {
  id: number;
  ts: string;
  kind: string;
  agent: string | null;
  payload: Record<string, unknown>;
  duration_ms: number | null;
}

export interface Approval {
  id: string;
  run_id?: string;
  agent: string;
  tool: string;
  args: Record<string, unknown>;
  reason: string;
  status: "pending" | "approved" | "rejected";
  reviewer?: string | null;
  decision_note?: string | null;
  ticket_id?: string;
  subject?: string;
  customer_email?: string;
  injections?: { source: string; score: number; signals: string[] }[] | null;
}

export interface RunDetail extends RunSummary {
  trace_id: string;
  state: {
    path: string[];
    node: string;
    tainted: boolean;
    injections: { source: string; score: number; signals: string[] }[];
    findings: Record<string, Record<string, unknown>>;
    ticket: { id: string; subject: string; body: string; customer_email: string };
    error: string | null;
  };
  outcome: Record<string, unknown> | null;
  events: RunEvent[];
  approvals: Approval[];
}

export interface Health {
  ok: boolean;
  model: string;
  provider: string;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => call<Health>("/api/health"),
  tickets: () => call<Ticket[]>("/api/tickets"),
  runs: () => call<RunSummary[]>("/api/runs"),
  run: (id: string) => call<RunDetail>(`/api/runs/${id}`),
  approvals: () => call<Approval[]>("/api/approvals?status=pending"),
  startRun: (ticketId: string) =>
    call<{ run_id: string }>(`/api/tickets/${ticketId}/runs`, { method: "POST" }),
  decide: (id: string, approve: boolean, note: string, reviewer: string, token: string) =>
    call<{ run_id: string }>(`/api/approvals/${id}/decision`, {
      method: "POST",
      headers: token ? { "x-reviewer-token": token } : {},
      body: JSON.stringify({ approve, note: note || null, reviewer: reviewer || "reviewer" }),
    }),
  reset: (token: string) =>
    call<{ ok: boolean }>("/api/demo/reset", {
      method: "POST",
      headers: token ? { "x-reviewer-token": token } : {},
    }),
};
