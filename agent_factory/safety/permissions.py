"""Permission policy: what an agent may do, and what needs a human first.

Every tool declares an :class:`ActionCategory`. The policy maps
(agent, tool, category) to a :class:`Decision`:

* an agent may only call tools on its allowlist (anything else: DENY);
* categories without a rule are DENIED;
* dangerous categories can never be ALLOWED: the policy refuses to be
  constructed that way, so the most permissive setting is REQUIRE_APPROVAL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class ActionCategory(StrEnum):
    READ_WORKSPACE = "read_workspace"
    WRITE_WORKSPACE = "write_workspace"
    EXECUTE_LOCAL = "execute_local"
    #: Running project code through the sandbox layer (isolation chosen by SandboxManager).
    EXECUTE_SANDBOXED = "execute_sandboxed"
    DESTRUCTIVE_DELETE = "destructive_delete"
    SPEND_MONEY = "spend_money"
    PRODUCTION_DEPLOY = "production_deploy"
    CREDENTIAL_CHANGE = "credential_change"
    EXTERNAL_COMMUNICATION = "external_communication"
    NETWORK_ACCESS = "network_access"
    #: Read-only access to an external cloud system through a typed, trusted adapter (docs/capabilities.md). It
    #: uses the network and a cloud identity and may disclose cloud data, but never mutates the cloud system.
    #: Not a local read: its rule depends on the environment (``PermissionPolicy.for_environment``).
    CLOUD_READ = "cloud_read"


class PolicyEnvironment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


#: Categories that always require explicit human approval (or are denied).
DANGEROUS_CATEGORIES = frozenset(
    {
        ActionCategory.DESTRUCTIVE_DELETE,
        ActionCategory.SPEND_MONEY,
        ActionCategory.PRODUCTION_DEPLOY,
        ActionCategory.CREDENTIAL_CHANGE,
        ActionCategory.EXTERNAL_COMMUNICATION,
        ActionCategory.NETWORK_ACCESS,
    }
)


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


DEFAULT_CATEGORY_RULES: Mapping[ActionCategory, Decision] = MappingProxyType(
    {
        ActionCategory.READ_WORKSPACE: Decision.ALLOW,
        ActionCategory.WRITE_WORKSPACE: Decision.ALLOW,
        ActionCategory.EXECUTE_LOCAL: Decision.ALLOW,
        ActionCategory.EXECUTE_SANDBOXED: Decision.ALLOW,
        ActionCategory.DESTRUCTIVE_DELETE: Decision.REQUIRE_APPROVAL,
        ActionCategory.SPEND_MONEY: Decision.REQUIRE_APPROVAL,
        ActionCategory.PRODUCTION_DEPLOY: Decision.REQUIRE_APPROVAL,
        ActionCategory.CREDENTIAL_CHANGE: Decision.REQUIRE_APPROVAL,
        ActionCategory.EXTERNAL_COMMUNICATION: Decision.REQUIRE_APPROVAL,
        # v0.1 makes no network calls at all; even approval cannot enable them.
        ActionCategory.NETWORK_ACCESS: Decision.DENY,
        # The development default. Agents still need the tool on their allowlist, and the capability must be
        # registered and available; production policies require approval (PermissionPolicy.for_environment).
        ActionCategory.CLOUD_READ: Decision.ALLOW,
    }
)

DEFAULT_AGENT_TOOLS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "planner": frozenset({"list_files", "read_file"}),
        "developer": frozenset({"list_files", "read_file", "write_file", "delete_file", "git_commit"}),
        "tester": frozenset({"list_files", "read_file", "run_tests"}),
        "reviewer": frozenset({"list_files", "read_file"}),
    }
)


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class PermissionPolicy:
    category_rules: Mapping[ActionCategory, Decision] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_RULES)
    )
    agent_tools: Mapping[str, frozenset[str]] = field(default_factory=lambda: dict(DEFAULT_AGENT_TOOLS))
    #: Where the policy applies. Production never auto-allows cloud reads, whatever the rules say.
    environment: PolicyEnvironment = PolicyEnvironment.DEVELOPMENT

    def __post_init__(self) -> None:
        environment = PolicyEnvironment(self.environment)
        rules = {ActionCategory(k): Decision(v) for k, v in self.category_rules.items()}
        for cat in DANGEROUS_CATEGORIES:
            if rules.get(cat) is Decision.ALLOW:
                raise PolicyError(f"dangerous category {cat.value!r} cannot be auto-allowed")
        if environment is PolicyEnvironment.PRODUCTION and rules.get(ActionCategory.CLOUD_READ) is Decision.ALLOW:
            raise PolicyError("cloud_read cannot be auto-allowed in production: it requires approval (or is denied)")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "category_rules", MappingProxyType(rules))
        object.__setattr__(
            self,
            "agent_tools",
            MappingProxyType({a: frozenset(t) for a, t in self.agent_tools.items()}),
        )

    @classmethod
    def for_environment(cls, environment: PolicyEnvironment | str, *,
                        staging_cloud_read: Decision = Decision.REQUIRE_APPROVAL) -> PermissionPolicy:  # fmt: skip
        """The default policy for an environment: cloud reads allowed in development, configurable in
        staging (approval by default), and approval-required in production."""
        environment = PolicyEnvironment(environment)
        rules = dict(DEFAULT_CATEGORY_RULES)
        if environment is PolicyEnvironment.STAGING:
            rules[ActionCategory.CLOUD_READ] = Decision(staging_cloud_read)
        elif environment is PolicyEnvironment.PRODUCTION:
            rules[ActionCategory.CLOUD_READ] = Decision.REQUIRE_APPROVAL
        return cls(category_rules=rules, environment=environment)

    def decide(self, agent: str, tool: str, category: ActionCategory) -> Decision:
        if tool not in self.agent_tools.get(agent, frozenset()):
            return Decision.DENY
        return self.category_rules.get(category, Decision.DENY)

    def with_rules(self, **overrides: Decision) -> PermissionPolicy:
        rules = dict(self.category_rules)
        rules.update({ActionCategory(k): Decision(v) for k, v in overrides.items()})
        return PermissionPolicy(category_rules=rules, agent_tools=dict(self.agent_tools), environment=self.environment)

    def with_agent_tools(self, agent: str, tools: set[str] | frozenset[str]) -> PermissionPolicy:
        agent_tools = dict(self.agent_tools)
        agent_tools[agent] = frozenset(tools)
        return PermissionPolicy(category_rules=dict(self.category_rules), agent_tools=agent_tools,
                                environment=self.environment)  # fmt: skip
