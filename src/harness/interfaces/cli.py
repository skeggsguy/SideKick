"""Terminal REPL interface.

A thin client over the Agent core: it renders streamed output, asks the user
for tool approvals, and reads input — but holds no agent logic. A future web
UI implements the same observe-and-render contract against the same Agent.
"""

from __future__ import annotations

import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.panel import Panel

from ..core.agent import Agent
from ..core.policy import ActionClass
from ..core.session import Session
from ..store.approvals import why_gated


class CLI:
    def __init__(self, agent: Agent, model_label: str, session: Session) -> None:
        self.console = Console()
        self.model_label = model_label
        self.session = session
        self.ptk: PromptSession = PromptSession(history=InMemoryHistory())
        # Wire the agent's hooks to rendering.
        agent.on_text_delta = self._render_delta
        agent.on_text = self._end_text
        agent.on_tool_call = self._render_tool_call
        agent.on_tool_result = self._render_tool_result
        self.agent = agent

    # --- agent hooks --------------------------------------------------------
    def _render_delta(self, text: str) -> None:
        # Raw write: rich markup would mangle streamed model text.
        sys.stdout.write(text)
        sys.stdout.flush()

    def _end_text(self, _text: str) -> None:
        # Full text already streamed via deltas; just close the line.
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _render_tool_call(self, name: str, args: dict) -> None:
        arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
        if len(arg_str) > 200:
            arg_str = arg_str[:200] + " […]"
        self.console.print(f"[dim]→ {name}({arg_str})[/dim]")

    def _render_tool_result(self, name: str, content: str, is_error: bool) -> None:
        style = "red" if is_error else "green"
        preview = content if len(content) <= 500 else content[:500] + " […]"
        self.console.print(
            Panel(preview, title=name, border_style=style, expand=False)
        )

    # --- approval prompt (PolicyEngine approver) -----------------------------
    async def approve(
        self,
        tool: str,
        args: dict,
        classification: ActionClass,
        gate_kind: str | None = None,
    ) -> str:
        arg_lines = "\n".join(f"  {k} = {v!r}" for k, v in args.items()) or "  (no args)"
        gate_line = (
            f"\n[yellow]egress gate: {why_gated(classification.value, 'interactive', tool, gate_kind)}[/yellow]"
            if gate_kind
            else ""
        )
        self.console.print(
            Panel(
                f"[bold]{tool}[/bold]  [{classification.value}]\n{arg_lines}{gate_line}",
                title="approval required",
                border_style="yellow",
                expand=False,
            )
        )
        if classification is ActionClass.DESTRUCTIVE:
            prompt, valid = "allow? [y/n] › ", {"y": "yes", "n": "no"}
        else:
            prompt, valid = "allow? [y/n/a(lways)] › ", {"y": "yes", "n": "no", "a": "always"}
        while True:
            try:
                answer = (await self.ptk.prompt_async(prompt)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "no"
            if answer in valid:
                return valid[answer]

    # --- main loop ----------------------------------------------------------
    async def run(self) -> None:
        resumed = f" · session [magenta]{self.session.id}[/magenta]" if self.session.id else ""
        history_note = (
            f"\nresumed with {len(self.session.messages)} prior messages"
            if self.session.messages
            else ""
        )
        self.console.print(
            Panel.fit(
                f"[bold]Agent Harness[/bold]\nmodel: [cyan]{self.model_label}[/cyan]"
                f"{resumed}{history_note}\n"
                "Type your message. /exit or Ctrl-D to quit.",
                border_style="blue",
            )
        )
        while True:
            try:
                user_input = (await self.ptk.prompt_async("\nyou › ")).strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]bye[/dim]")
                return

            if not user_input:
                continue
            if user_input in ("/exit", "/quit"):
                self.console.print("[dim]bye[/dim]")
                return

            self.console.print()  # spacer before the response
            try:
                await self.agent.run_turn(self.session, user_input)
                self._render_run_stats()
            except Exception as e:  # noqa: BLE001 - keep the REPL alive on errors
                self.console.print(f"[red]error:[/red] {e}")

    def _render_run_stats(self) -> None:
        stats = self.agent.last_run
        if not stats:
            return
        u = stats["usage"]
        self.console.print(
            f"[dim]tokens: {u.input_tokens} in / {u.output_tokens} out · "
            f"cache: {u.cache_read_input_tokens} read / "
            f"{u.cache_creation_input_tokens} written · "
            f"${stats['cost_usd']:.4f}[/dim]"
        )
