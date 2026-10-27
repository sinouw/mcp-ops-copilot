"""A deliberately small graph engine: named nodes, declared edges, durable checkpoints.

Why not LangGraph? The two things this system must get right are (1) pausing a run for a
human decision and resuming it later, possibly in another process, and (2) putting a
deterministic policy check between the model and every tool call. Both live naturally in
our own Postgres schema (runs, approvals) and our own tool gateway. LangGraph's interrupt and
checkpointer would add a second persistence model next to the one the MCP servers already
verify approvals against. The engine below is ~60 lines, fully typed, and its only contract
is: a node returns the next node or a pause, and state is checkpointed after every step.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from opscopilot.agents.state import END, RunState


@dataclass(frozen=True)
class Goto:
    node: str


@dataclass(frozen=True)
class Pause:
    approval_id: str


Step = Goto | Pause
type NodeFn[Ctx] = Callable[[RunState, Ctx], Awaitable[Step]]


class GraphError(RuntimeError):
    pass


class Graph[Ctx]:
    def __init__(self, nodes: dict[str, NodeFn[Ctx]], edges: dict[str, list[str]]) -> None:
        for src, dsts in edges.items():
            unknown = [d for d in [src, *dsts] if d not in nodes and d != END]
            if unknown:
                raise GraphError(f"edge references unknown nodes: {unknown}")
        self.nodes, self.edges = nodes, edges

    async def run(
        self,
        state: RunState,
        ctx: Ctx,
        checkpoint: Callable[[RunState], Awaitable[None]],
        *,
        max_steps: int = 50,
    ) -> Literal["completed", "paused"]:
        for _ in range(max_steps):
            if state.node == END:
                return "completed"
            step = await self.nodes[state.node](state, ctx)
            if isinstance(step, Pause):
                await checkpoint(state)
                return "paused"
            if step.node not in self.edges.get(state.node, []):
                raise GraphError(f"illegal transition {state.node} -> {step.node}")
            state.node = step.node
            state.path.append(step.node)
            await checkpoint(state)
        raise GraphError(f"graph did not finish within {max_steps} steps")
