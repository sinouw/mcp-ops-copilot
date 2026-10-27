const NODES: Record<string, { x: number; y: number; label: string; tools: string }> = {
  supervisor: { x: 260, y: 40, label: "Supervisor", tools: "routes work" },
  triage: { x: 80, y: 150, label: "Triage", tools: "ticket history" },
  research: { x: 260, y: 150, label: "Research", tools: "CRM read-only" },
  resolution: { x: 440, y: 150, label: "Resolution", tools: "refund · reply · escalate" },
  end: { x: 260, y: 240, label: "Done", tools: "" },
};

interface Props {
  path: string[];
  current: string;
  status: string;
}

export function AgentGraph({ path, current, status }: Props) {
  const visited = new Set(path);
  const traversed = new Set<string>();
  for (let i = 1; i < path.length; i++) traversed.add(`${path[i - 1]}>${path[i]}`);
  const isTraversed = (a: string, b: string) => traversed.has(`${a}>${b}`) || traversed.has(`${b}>${a}`);
  const live = status === "running" || status === "awaiting_approval";

  return (
    <svg viewBox="0 0 520 280" className="graph" role="img" aria-label="Agent graph">
      {["triage", "research", "resolution", "end"].map((n) => (
        <line
          key={n}
          x1={NODES.supervisor.x}
          y1={NODES.supervisor.y + 18}
          x2={NODES[n].x}
          y2={NODES[n].y - 18}
          className={isTraversed("supervisor", n) ? "edge edge-on" : "edge"}
        />
      ))}
      {Object.entries(NODES).map(([id, n]) => {
        const cls = [
          "node",
          visited.has(id) ? "node-visited" : "",
          live && current === id ? (status === "awaiting_approval" ? "node-paused" : "node-current") : "",
        ].join(" ");
        return (
          <g key={id} className={cls} transform={`translate(${n.x - 70}, ${n.y - 18})`}>
            <rect width="140" height="36" rx="8" />
            <text x="70" y="16" textAnchor="middle" className="node-label">
              {n.label}
            </text>
            <text x="70" y="29" textAnchor="middle" className="node-sub">
              {id === current && status === "awaiting_approval" ? "waiting for approval" : n.tools}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
