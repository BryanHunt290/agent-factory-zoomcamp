from .base import Agent, AgentContext, AgentOutputError, AgentResult
from .developer import DeveloperAgent
from .planner import PlannerAgent
from .reviewer import ReviewerAgent
from .tester import TesterAgent


def default_agents() -> dict[str, Agent]:
    return {
        "planner": PlannerAgent(),
        "developer": DeveloperAgent(),
        "tester": TesterAgent(),
        "reviewer": ReviewerAgent(),
    }


__all__ = [
    "Agent",
    "AgentContext",
    "AgentOutputError",
    "AgentResult",
    "DeveloperAgent",
    "PlannerAgent",
    "ReviewerAgent",
    "TesterAgent",
    "default_agents",
]
