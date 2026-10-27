import { useCallback, useEffect, useState } from "react";
import { AgentGraph } from "./AgentGraph";
import { api, type Approval, type Health, type RunDetail, type RunEvent, type Ticket } from "./api";

const TOKEN_KEY = "opscopilot.reviewerToken";

function readToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function usePoll<T>(fn: () => Promise<T>, deps: unknown[], ms = 1500): [T | null, () => void] {
  const [data, setData] = useState<T | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => fn().then((d) => alive && setData(d)).catch(() => undefined);
    load();
    const t = setInterval(load, ms);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [...deps, tick]);
  return [data, () => setTick((n) => n + 1)];
}

const money = (cents: unknown) => (typeof cents === "number" ? `$${(cents / 100).toFixed(2)}` : "");

function StatusPill({ status }: { status: string | null }) {
  if (!status) return <span className="pill pill-muted">no run</span>;
  return <span className={`pill pill-${status}`}>{status.replace("_", " ")}</span>;
}

export default function App() {
  const [health] = usePoll<Health>(api.health, [], 10000);
  const [tickets, refreshTickets] = usePoll<Ticket[]>(api.tickets, []);
  const [approvals, refreshApprovals] = usePoll<Approval[]>(api.approvals, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [token, setToken] = useState(readToken);
  const [error, setError] = useState<string | null>(null);

  const saveToken = (t: string) => {
    setToken(t);
    try {
      localStorage.setItem(TOKEN_KEY, t);
    } catch {
      /* storage unavailable: keep in memory only */
    }
  };

  const start = async (ticketId: string) => {
    setError(null);
    try {
      const { run_id } = await api.startRun(ticketId);
      setSelected(run_id);
      refreshTickets();
    } catch (e) {
      setError(String(e));
    }
  };

  const decide = useCallback(
    async (a: Approval, approve: boolean, note: string) => {
      setError(null);
      try {
        await api.decide(a.id, approve, note, "console", token);
        refreshApprovals();
      } catch (e) {
        setError(String(e));
      }
    },
    [token, refreshApprovals],
  );

  return (
    <div className="app">
      <header>
        <div>
          <h1>Ops Copilot</h1>
          <p className="muted">Multi-agent support automation over MCP · human approval for risky actions</p>
        </div>
        <div className="header-right">
          {health && (
            <span className="pill pill-muted">
              {health.provider} / {health.model}
            </span>
          )}
          <input
            className="token"
            type="password"
            placeholder="reviewer token"
            value={token}
            onChange={(e) => saveToken(e.target.value)}
          />
          <button className="ghost" onClick={() => api.reset(token).then(() => { setSelected(null); refreshTickets(); })}>
            Reset demo data
          </button>
        </div>
      </header>
      {error && <div className="error">{error}</div>}

      <main>
        <section className="col">
          <h2>
            Approvals <span className="count">{approvals?.length ?? 0}</span>
          </h2>
          {approvals?.length === 0 && <p className="muted">Nothing waiting for review.</p>}
          {approvals?.map((a) => (
            <ApprovalCard key={a.id} approval={a} onDecide={decide} onOpen={() => setSelected(a.run_id ?? null)} />
          ))}

          <h2>Inbox</h2>
          <table className="tickets">
            <tbody>
              {tickets?.map((t) => (
                <tr key={t.id} className={selected && selected === t.last_run_id ? "row-selected" : ""}>
                  <td className="mono">{t.id}</td>
                  <td>
                    <div className="subject" onClick={() => t.last_run_id && setSelected(t.last_run_id)}>
                      {t.subject}
                    </div>
                    <div className="muted small">
                      {t.customer_email} · ticket {t.status}
                    </div>
                  </td>
                  <td>
                    <StatusPill status={t.last_run_status} />
                  </td>
                  <td>
                    <button onClick={() => start(t.id)}>Run agents</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="col col-wide">
          {selected ? <RunView runId={selected} onDecide={decide} /> : <Placeholder />}
        </section>
      </main>
    </div>
  );
}

function Placeholder() {
  return (
    <div className="placeholder">
      <h2>Pick a ticket</h2>
      <p className="muted">
        Run the agents on a ticket to watch the supervisor route it through triage, research and resolution. Refunds
        above the threshold and every customer email stop here for your approval.
      </p>
    </div>
  );
}

function ApprovalCard({
  approval: a,
  onDecide,
  onOpen,
}: {
  approval: Approval;
  onDecide: (a: Approval, approve: boolean, note: string) => void;
  onOpen?: () => void;
}) {
  const [note, setNote] = useState("");
  const injected = (a.injections ?? []).length > 0;
  return (
    <div className={`card approval ${injected ? "approval-warn" : ""}`}>
      <div className="approval-head">
        <span className="mono tool">{a.tool}</span>
        {a.ticket_id && (
          <button className="link" onClick={onOpen}>
            {a.ticket_id} · {a.subject}
          </button>
        )}
      </div>
      <p className="reason">{a.reason}</p>
      {injected && (
        <p className="warn small">
          Possible prompt injection: {(a.injections ?? []).flatMap((i) => i.signals).join(", ")}
        </p>
      )}
      {a.tool === "ticketing.send_reply" ? (
        <blockquote>{String(a.args.body ?? "")}</blockquote>
      ) : (
        <pre>{JSON.stringify(a.args, null, 2)}</pre>
      )}
      {a.status === "pending" ? (
        <div className="actions">
          <input placeholder="note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="approve" onClick={() => onDecide(a, true, note)}>
            Approve
          </button>
          <button className="reject" onClick={() => onDecide(a, false, note || "rejected by reviewer")}>
            Reject
          </button>
        </div>
      ) : (
        <p className="small muted">
          {a.status} by {a.reviewer ?? "?"} {a.decision_note ? `· ${a.decision_note}` : ""}
        </p>
      )}
    </div>
  );
}

function RunView({ runId, onDecide }: { runId: string; onDecide: (a: Approval, ok: boolean, note: string) => void }) {
  const [run] = usePoll<RunDetail>(() => api.run(runId), [runId], 1000);
  if (!run) return <p className="muted">Loading run…</p>;
  const s = run.state;
  return (
    <div>
      <div className="run-head">
        <div>
          <h2>
            {s.ticket.id} · {s.ticket.subject}
          </h2>
          <p className="muted small mono">
            run {run.id} · trace {run.trace_id}
          </p>
        </div>
        <StatusPill status={run.status} />
      </div>

      {s.tainted && (
        <div className="warn-banner">
          Untrusted content tried to instruct the agents ({s.injections.map((i) => `${i.source}: ${i.signals.join(", ")}`).join(" · ")}).
          All write actions on this run require approval.
        </div>
      )}
      {s.error && <div className="error">{s.error}</div>}

      <div className="grid2">
        <div className="card">
          <AgentGraph path={s.path} current={s.node} status={run.status} />
        </div>
        <div className="card metrics">
          <Metric label="LLM calls" value={run.llm_calls} />
          <Metric label="Tool calls" value={run.tool_calls} />
          <Metric label="Tokens in / out" value={`${run.tokens_in} / ${run.tokens_out}`} />
          <Metric label="Cost" value={`$${run.cost_usd.toFixed(4)}`} />
          <Metric label="Model" value={`${run.provider}/${run.model}`} />
          <div className="ticket-body">
            <div className="small muted">Ticket from {s.ticket.customer_email} (untrusted)</div>
            <p>{s.ticket.body}</p>
          </div>
        </div>
      </div>

      {run.approvals.length > 0 && (
        <>
          <h3>Approvals</h3>
          {run.approvals.map((a) => (
            <ApprovalCard key={a.id} approval={a} onDecide={onDecide} />
          ))}
        </>
      )}

      <h3>Timeline</h3>
      <ol className="timeline">
        {run.events.map((e) => (
          <EventRow key={e.id} e={e} />
        ))}
      </ol>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <div className="small muted">{label}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}

function describe(e: RunEvent): string {
  const p = e.payload as Record<string, any>;
  switch (e.kind) {
    case "route":
      return `→ ${p.next === "end" ? "finish" : p.next} (${p.decided_by}: ${p.reason})`;
    case "llm":
      return `${p.model} · ${p.input_tokens}+${p.output_tokens} tok${p.tokens_estimated ? " (est.)" : ""} → ${(p.tool_calls ?? []).join(", ") || "text"}`;
    case "policy":
      return `${p.verdict.toUpperCase()} ${p.tool} · ${p.rule}${p.verdict !== "allow" ? `: ${p.reason}` : ""}`;
    case "tool_call":
      return `${p.tool} ${p.ok ? "ok" : "error"}${p.args?.amount_cents ? ` · ${money(p.args.amount_cents)}` : ""}${p.ok ? "" : ` · ${String(p.result).slice(0, 140)}`}`;
    case "approval_requested":
      return `waiting for human: ${p.reason}`;
    case "approval_decided":
      return `human ${p.status} ${p.tool}${p.note ? ` · ${p.note}` : ""}`;
    case "guardrail":
      return `${p.rule}${p.source ? ` in ${p.source}` : ""}${p.signals ? ` (${p.signals.join(", ")})` : ""}`;
    case "agent_finish":
      return JSON.stringify(p.result).slice(0, 200);
    case "run_finish":
      return `${p.status}${p.error ? ` · ${p.error}` : ""}`;
    case "agent_start":
      return `tools: ${(p.tools ?? []).join(", ")}`;
    default:
      return JSON.stringify(p).slice(0, 160);
  }
}

function EventRow({ e }: { e: RunEvent }) {
  const verdict = e.kind === "policy" ? String(e.payload.verdict) : "";
  return (
    <li className={`ev ev-${e.kind} ${verdict ? `ev-${verdict}` : ""}`}>
      <span className="ev-kind">{e.kind.replace("_", " ")}</span>
      <span className="ev-agent">{e.agent ?? "run"}</span>
      <span className="ev-text">{describe(e)}</span>
      <span className="ev-ms">{e.duration_ms != null ? `${e.duration_ms.toFixed(0)} ms` : ""}</span>
    </li>
  );
}
