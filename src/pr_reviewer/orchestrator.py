from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .agents import BaseReviewAgent, run_agent
from .models import AgentRun, DiffFile, Finding


class ReviewState(TypedDict, total=False):
    files: list[DiffFile]
    findings: Annotated[list[Finding], operator.add]
    warnings: Annotated[list[str], operator.add]
    agent_runs: Annotated[list[AgentRun], operator.add]


class LangGraphOrchestrator:
    """Fan out specialists as parallel LangGraph nodes and join their typed results."""

    def __init__(self, agents: list[BaseReviewAgent], timeout: float = 45):
        self.agents = agents
        self.timeout = timeout

    def _compile(self):
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(ReviewState)
        nodes: list[str] = []
        for index, agent in enumerate(self.agents):
            name = f"agent_{index}_{agent.category.replace('-', '_')}"
            nodes.append(name)

            async def node(state: ReviewState, selected: BaseReviewAgent = agent) -> dict[str, Any]:
                findings, run, warning = await run_agent(selected, state["files"], self.timeout)
                return {
                    "findings": findings,
                    "agent_runs": [run],
                    "warnings": [warning] if warning else [],
                }

            graph.add_node(name, node)
            graph.add_edge(START, name)
        if nodes:
            graph.add_edge(nodes, END)
        else:
            graph.add_edge(START, END)
        return graph.compile()

    async def run(self, files: list[DiffFile]) -> tuple[list[Finding], list[str], list[AgentRun]]:
        result = await self._compile().ainvoke(
            {"files": files, "findings": [], "warnings": [], "agent_runs": []}
        )
        return result.get("findings", []), result.get("warnings", []), result.get("agent_runs", [])
