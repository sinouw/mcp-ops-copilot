-- Business data (owned by the MCP servers) ---------------------------------

CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    phone       TEXT,
    tier        TEXT NOT NULL DEFAULT 'standard',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id           TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled')),
    total_cents  INTEGER NOT NULL CHECK (total_cents >= 0),
    currency     TEXT NOT NULL DEFAULT 'USD',
    items        JSONB NOT NULL DEFAULT '[]',
    carrier      TEXT,
    eta          DATE,
    placed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refunds (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         TEXT NOT NULL REFERENCES orders(id),
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    reason           TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    approval_id      UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tickets (
    id              TEXT PRIMARY KEY,
    customer_email  TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'pending', 'resolved', 'escalated')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id           BIGSERIAL PRIMARY KEY,
    ticket_id    TEXT NOT NULL REFERENCES tickets(id),
    kind         TEXT NOT NULL CHECK (kind IN ('reply', 'internal_note', 'escalation')),
    to_email     TEXT,
    body         TEXT NOT NULL,
    approval_id  UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Agent runtime ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
                ('running', 'awaiting_approval', 'completed', 'failed', 'budget_exceeded')),
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    state       JSONB NOT NULL,
    outcome     JSONB,
    trace_id    TEXT,
    llm_calls   INTEGER NOT NULL DEFAULT 0,
    tool_calls  INTEGER NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS run_events (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    kind         TEXT NOT NULL,
    agent        TEXT,
    payload      JSONB NOT NULL DEFAULT '{}',
    duration_ms  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS run_events_run_idx ON run_events (run_id, id);

-- Approvals are the contract between the runtime and the MCP servers: a write tool that
-- needs a human decision only executes when it presents an approved, unconsumed row whose
-- arguments match the call exactly.
CREATE TABLE IF NOT EXISTS approvals (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    agent          TEXT NOT NULL,
    tool           TEXT NOT NULL,
    args           JSONB NOT NULL,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewer       TEXT,
    decision_note  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ,
    consumed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS approvals_status_idx ON approvals (status, created_at);
