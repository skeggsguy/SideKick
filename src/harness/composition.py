"""The composition root shared by both entrypoints.

build_agent() assembles concrete pieces (provider, tools, policy, agent);
nothing below it imports config or env directly. The CLI (main.py) builds one
agent for the whole session; the daemon (gateway/agentd.py) builds one per
run (cheap — tools and stores are shared).
"""

from __future__ import annotations

import os
import sys
from typing import Callable

from .core.agent import Agent
from .core.context import Compactor, SUMMARY_BAND_MAX
from .core.envelope import DelegationEnvelope
from .core.policy import DEFAULT_EGRESS_BUDGET, Approver, Autonomy, PolicyEngine
from .core.registry import ToolRegistry
from .memory import MemoryExtractor, MemoryStore, OllamaEmbedder
from .providers.anthropic_provider import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_MODEL,
    EFFORTS,
    AnthropicProvider,
)
from .providers.apple_fm import AppleFMProvider
from .providers.base import LLMProvider
from .store import Database
from .store.approvals import ApprovalStore
from .store.audit import AllowlistStore, AuditStore
from .store.client_config import ClientConfigStore
from .store.runs import RunStore
from .store.triggers import TriggerStore
from .tools.base import Tool
from .tools.calibration import CalibrationReportTool
from .tools.files import CopyFileTool, EditFileTool, ListFilesTool, ReadFileTool
from .tools.memory_tools import memory_tools
from .tools.run_stats import RunStatsTool
from .tools.sandbox import SandboxRunTool
from .tools.schedule_tools import schedule_tools
from .tools.search import SearchFilesTool
from .tools.timezone import timezone_tools
from .tools._browser_manager import BrowserSessionManager
from .tools._refs import RefResolver
from .tools.browser_delegate import (
    BROWSER_CHILD_SYSTEM_PROMPT,
    BrowserDelegateTool,
)
from .tools.browser_fetch import BrowserFetchTool
from .tools.browser_interact import browser_interaction_tools
from .tools.web_fetch import WebFetchTool
from .tools.rss_fetch import RssFetchTool
from .tools.reddit_comments import RedditCommentsTool
from .tools.hn_comments import HnCommentsTool
from .tools.web_search import WebSearchTool
from .tools.weather import WeatherTool
from .tools.documents import ReadDocumentTool
from .tools.maps import maps_tools


from .prompt import system_prompt, user_name


# Pre-2026-09-28 HARNESS_EFFORT values (the cloud API's own vocabulary) that
# the four-level picker no longer has. Mapped, not fatal: a stale .env must not
# stop the daemon (the local template treats these as the top of the range).
_LEGACY_EFFORTS = {"high": "xhigh", "max": "xhigh"}

# The opt-in Sonnet chat option is pinned to this effort — one dropdown entry,
# not a model x effort matrix.
CHAT_SONNET_EFFORT = "medium"


def _env_effort() -> str:
    """HARNESS_EFFORT, with legacy values folded into the current vocabulary
    (stderr note) — unknown values still reach the provider's ValueError."""
    effort = os.environ.get("HARNESS_EFFORT", "medium")
    mapped = _LEGACY_EFFORTS.get(effort)
    if mapped is not None:
        print(
            f"HARNESS_EFFORT={effort} is no longer a level — using {mapped} "
            f"(levels: {', '.join(EFFORTS)})",
            file=sys.stderr,
        )
        return mapped
    return effort


def _tool_groups_enabled() -> bool:
    """Tool groups (core/tool_groups.py): defer the MCP tool definitions behind
    load_tools/call_tool. ON by default — the main lane is local, where every
    definition is prefill on a slow, single-prefix cache. HARNESS_TOOL_GROUPS=
    on|off overrides; any other value keeps the default with a stderr note (a
    stale .env must not stop the daemon)."""
    default = True
    raw = os.environ.get("HARNESS_TOOL_GROUPS", "").strip().lower()
    if raw in ("on", "off"):
        return raw == "on"
    if raw:
        print(
            f"HARNESS_TOOL_GROUPS={raw} is not on|off — using the default "
            f"({'on' if default else 'off'})",
            file=sys.stderr,
        )
    return default


# The summary band's top (context.py clamps the target into
# [SUMMARY_BAND_MIN, SUMMARY_BAND_MAX]) and the compaction summarizer's
# GUARANTEED think space (Step 4, think-on-low): max_tokens = text + think,
# both guaranteed — _local_effort_params caps thinking at
# max_tokens - answer_headroom, and compaction passes the band as headroom.
COMPACTION_THINKING_TOKENS = 10_000
COMPACTION_MAX_TOKENS = SUMMARY_BAND_MAX + COMPACTION_THINKING_TOKENS


def _providers(
    lane: str = "chat",
) -> tuple[AnthropicProvider, LLMProvider, LLMProvider | None, str]:
    """The main (thinking) provider plus the two utility tiers: the small one
    (triage-sized jobs: titles, extraction) and the big one (compaction — the
    only utility job that reads more than the Apple model's context window).
    A None big tier means compaction degrades to its elision note (traced).

    Everything is local, always: the main provider is the llama.cpp fork's
    Anthropic-compatible /v1/messages (HARNESS_LOCAL_MODEL), the small tier is
    the on-device Apple Foundation Model, and compaction rides the local server
    too — its persisted checkpoint freezes the prefix, so it costs ONE cold
    prefill per budget crossing, not per turn (decision 2026-10-22).
    No API key needed. The one remaining Anthropic-API path is the opt-in
    Sonnet chat option (sonnet_chat_provider) — never utility, never
    background runs. ``lane`` is the llama server's chat window lane: both
    local providers (main + compaction) ride it — the default "chat" for every
    app run, background ones too (2026-10-25); "code" stays an unused option.
    Side calls stay OFF the -np 1 server (they'd evict the chat's prefix)."""
    max_tokens = int(os.environ.get("HARNESS_MAX_TOKENS", "16000"))
    # Effort defaults to medium as the cost/quality middle ground; per-turn
    # overrides (the web UI picker) ride run_turn(effort=). max_tokens caps
    # thinking + text per API call.
    effort = _env_effort()
    local_url = os.environ.get("HARNESS_LOCAL_URL")
    if not local_url:
        sys.exit(
            "HARNESS_LOCAL_URL is not set — the harness runs fully local. "
            "Point it at your Anthropic-compatible llama.cpp server, e.g. "
            "HARNESS_LOCAL_URL=http://127.0.0.1:8080 in .env "
            "(docs/macos-host.md has the Mac mini setup)."
        )
    model = os.environ.get("HARNESS_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)
    provider = AnthropicProvider(
        api_key="local",
        model=model,
        max_tokens=max_tokens,
        effort=effort,
        base_url=local_url,
        lane=lane,
    )
    compaction = AnthropicProvider(
        api_key="local",
        model=model,
        max_tokens=COMPACTION_MAX_TOKENS,
        effort="low",
        answer_headroom=SUMMARY_BAND_MAX,
        base_url=local_url,
        lane=lane,
    )
    return provider, AppleFMProvider(), compaction, model


def sonnet_available() -> bool:
    """The opt-in Sonnet chat option: an API key in the environment is the
    only switch. Nothing else in the app reads ANTHROPIC_API_KEY."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def sonnet_chat_provider() -> AnthropicProvider:
    """The chat-only cloud option: Sonnet 5 with adaptive thinking, pinned to
    medium effort (the dropdown offers exactly one Sonnet entry). Used ONLY
    for the main model of an interactive chat turn that asked for it;
    utility, compaction, extraction and titles stay local regardless."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Sonnet chat needs ANTHROPIC_API_KEY in .env (it is the only "
            "thing the key is used for)"
        )
    return AnthropicProvider(
        api_key=api_key,
        model=DEFAULT_MODEL,
        max_tokens=int(os.environ.get("HARNESS_MAX_TOKENS", "16000")),
        thinking="adaptive",
        effort=CHAT_SONNET_EFFORT,
    )


def _register_tools(
    registry: ToolRegistry,
    *,
    client_config: ClientConfigStore | None,
    triggers: TriggerStore | None,
    run_store: RunStore | None,
    approvals: ApprovalStore | None,
    memory: MemoryStore | None,
    extra_tools: list[Tool] | None,
) -> None:
    """The flat tool catalog: in-process tools, store-backed tools when their
    store is wired, then the adapted MCP tools (extra_tools)."""
    registry.register(ReadFileTool())
    registry.register(ListFilesTool())
    registry.register(SearchFilesTool())
    registry.register(EditFileTool())
    registry.register(CopyFileTool())
    registry.register(SandboxRunTool())
    registry.register(ReadDocumentTool())
    for tool in timezone_tools(client_config):
        registry.register(tool)
    if triggers is not None:
        # Catalog callable, not a snapshot: schedule tools register before the
        # MCP tools, so tool-name validation must read the registry at call
        # time (when it's complete).
        for tool in schedule_tools(
            triggers, catalog=lambda: {t.name for t in registry.all()}
        ):
            registry.register(tool)
    if run_store is not None:
        registry.register(RunStatsTool(run_store))
    if approvals is not None:
        registry.register(CalibrationReportTool(approvals))
    registry.register(WebFetchTool())
    # Same SSRF-guarded fetch pattern, feeds-shaped output; no key, so like
    # web_fetch it registers unconditionally (deferred via the feeds group).
    registry.register(RssFetchTool())
    # Comment pulls for the feeds group: same guarded-fetch pattern, post URLs
    # validated to reddit.com / news.ycombinator.com before any fetch, no key.
    registry.register(RedditCommentsTool())
    registry.register(HnCommentsTool())
    # Registered unconditionally next to web_fetch; the agent Chrome is probed
    # at call time (the sandbox pattern), so a machine without it just gets an
    # actionable error from run().
    registry.register(BrowserFetchTool())
    # Lane B is envelope-only: the six interaction tools are NOT in the main
    # catalog. They are granted solely to a browser subagent, per delegation,
    # via browser_delegate (registered in build_agent). The classes stay
    # importable in harness.tools so the structural policy scans still see them.
    # Never reintroduce `claude --chrome` as a second lane.
    registry.register(WeatherTool())
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if brave_key:
        registry.register(WebSearchTool(brave_key))
    maps_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if maps_key:
        for tool in maps_tools(maps_key):
            registry.register(tool)
    if memory is not None:
        for tool in memory_tools(memory):
            registry.register(tool)
    for tool in extra_tools or []:
        registry.register(tool)


def _browser_child_factory(
    provider: AnthropicProvider,
    compaction: LLMProvider | None,
    client_config: ClientConfigStore | None,
    run_store: RunStore | None,
    audit: AuditStore | None,
    parent_approver: Callable[[], Approver | None],
) -> Callable[[DelegationEnvelope], tuple[Agent, BrowserSessionManager]]:
    """Build one browser subagent per delegation. Each call gets a FRESH tab
    (BrowserSessionManager) and a registry holding ONLY the six Lane B tools,
    all bound to that tab. The child runs SUPERVISED under the delegation
    envelope (core/policy.py owns the origin/ref/budget gates); it shares the
    parent's approver so a boundary prompt reaches the same human. Runs on the
    MAIN model (browsing needs reasoning, not the utility tier); no memory /
    extractor. Returns (child, manager) so browser_delegate can tear the tab
    down when the child finishes."""

    def make_child(
        envelope: DelegationEnvelope,
    ) -> tuple[Agent, BrowserSessionManager]:
        manager = BrowserSessionManager()
        # Refs resolve to real values only inside a delegation; with no client
        # config the resolver stays None and browser_type refuses ref use.
        ref_resolver = (
            RefResolver(client_config, envelope, manager)
            if client_config is not None
            else None
        )
        registry = ToolRegistry()
        for tool in browser_interaction_tools(manager, ref_resolver):
            registry.register(tool)
        child_policy = PolicyEngine(
            autonomy=Autonomy.SUPERVISED,
            egress_budget=envelope.budget,
            envelope=envelope,
            tab_host_fn=manager.current_host,
            element_host_fn=manager.element_host,
        )
        # Read the parent's approver lazily at spawn time (it is wired onto the
        # parent agent AFTER build_agent returns, e.g. the daemon's deferred
        # approver), not captured when the factory was constructed.
        child_policy.approver = parent_approver()
        child = Agent(
            provider=provider,
            registry=registry,
            system_prompt=BROWSER_CHILD_SYSTEM_PROMPT,
            policy=child_policy,
            run_store=run_store,
            audit=audit,
            # The child browses while the parent waits, so the local server
            # is free for this child's compaction.
            compactor=Compactor(summarizer=compaction),
            # Budget counts only page ACTIONS (navigate/click/type); each one
            # typically pairs with a free snapshot, so the loop bound leaves
            # room for that read traffic plus recovery/report steps.
            max_steps=envelope.budget * 2 + 15,
        )
        return child, manager

    return make_child


def build_agent(
    run_store: RunStore | None = None,
    audit: AuditStore | None = None,
    allowlist: AllowlistStore | None = None,
    triggers: TriggerStore | None = None,
    memory: MemoryStore | None = None,
    client_config: ClientConfigStore | None = None,
    approvals: ApprovalStore | None = None,
    extra_tools: list[Tool] | None = None,
    autonomy: Autonomy = Autonomy.INTERACTIVE,
    chat_model: str | None = None,
    lane: str = "chat",
) -> tuple[Agent, str]:
    """Composition for one agent. The CLI builds one for the whole session;
    the daemon builds one per run (cheap — tools and stores are shared).
    chat_model="sonnet" swaps ONLY this run's main provider for the opt-in
    Sonnet chat option — utility, compaction and extraction stay local.
    lane="code" puts the local calls on the llama server's code lane (no app
    caller since 2026-10-25; the browser child inherits the parent's provider)."""
    provider, utility, compaction, model = _providers(lane)
    if chat_model == "sonnet":
        provider = sonnet_chat_provider()
        model = provider.model

    registry = ToolRegistry()
    _register_tools(
        registry,
        client_config=client_config,
        triggers=triggers,
        run_store=run_store,
        approvals=approvals,
        memory=memory,
        extra_tools=extra_tools,
    )

    # Post-turn extraction runs ONLY on interactive turns. Supervised/
    # restricted runs process untrusted trigger content (emails, pages) —
    # auto-writing facts from them is a persistent prompt-injection path
    # into every future session's <memory> block, and (profile/preference)
    # into their frozen <user_profile> system-prompt suffix. Background runs
    # can still store facts via the `remember` tool, which parks a deferred
    # approval a human reviews.
    extractor = None
    if memory is not None and autonomy is Autonomy.INTERACTIVE:
        extractor = MemoryExtractor(utility, memory, run_store)

    # HARNESS_EGRESS_BUDGET=0 gates every outbound call (kill switch).
    policy = PolicyEngine(
        allowlist=allowlist,
        autonomy=autonomy,
        egress_budget=int(
            os.environ.get("HARNESS_EGRESS_BUDGET", str(DEFAULT_EGRESS_BUDGET))
        ),
    )
    agent = Agent(
        provider=provider,
        registry=registry,
        system_prompt=system_prompt(user_name()),
        policy=policy,
        run_store=run_store,
        audit=audit,
        memory=memory,
        memory_extractor=extractor,
        compactor=Compactor(summarizer=compaction),
        # Sonnet turns skip tool groups: the deferral exists for the local
        # lane's slow single-prefix cache; cloud prefixes are cheap.
        group_tools=_tool_groups_enabled() and chat_model != "sonnet",
    )

    # Registered last (it needs the built agent): browser_delegate spawns a
    # browser subagent per call (envelope-only Lane B).
    # The child shares the parent's approver (read lazily — the daemon wires a
    # deferred approver onto the parent after build_agent returns).
    registry.register(
        BrowserDelegateTool(
            _browser_child_factory(
                provider,
                compaction,
                client_config,
                run_store,
                audit,
                lambda: agent.policy.approver,
            ),
            lambda: agent.current_run_id,
            client_config=client_config,
            parent_effort=lambda: agent.current_effort,
        )
    )
    return agent, model


async def open_memory(db: Database) -> MemoryStore:
    """The shared memory store: Ollama embeddings when available, keyword
    fallback otherwise. Used by both composition roots."""
    memory = MemoryStore(db, OllamaEmbedder())
    await memory.setup()
    return memory
