"""Plain terminal replay and a small searchable session picker."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from omnivoice.history import ChatHistory, ChatSession, render_turn, safe_text


async def replay(history: ChatHistory, session: ChatSession, write: Callable[[str], None]) -> None:
    write(f"\n=== Chat: {safe_text(session.name, multiline=False)} ({session.id}) ===\n")
    after = 0
    while True:
        rows = await history.turns(session.id, after=after)
        if not rows:
            if after == 0:
                write("No messages yet.\n")
            break
        for turn in rows:
            write(render_turn(turn))
        after = rows[-1]["id"]


class SessionPicker:
    def __init__(self, sessions: list[ChatSession]) -> None:
        self.sessions = sessions
        self.query = ""
        self.index = 0

    @property
    def matches(self) -> list[ChatSession]:
        query = self.query.casefold()
        return [s for s in self.sessions if query in (s.name + " " + s.id + " " + s.preview).casefold()]

    def filter(self, query: str) -> None:
        self.query = query
        self.index = 0

    def move(self, amount: int) -> None:
        self.index = (self.index + amount) % max(1, len(self.matches))

    def selected(self) -> str | None:
        matches = self.matches
        return matches[self.index].id if matches else None

    async def run(self) -> str | None:
        bindings = KeyBindings()
        search = TextArea(height=1, multiline=False, prompt="Filter: ")
        search.buffer.on_text_changed += lambda _: self.filter(search.text)

        def rows() -> str:
            matches = self.matches
            if not matches:
                return "No matching chats."
            start = max(0, self.index - 7)
            return "\n".join(
                ("> " if i == self.index else "  ") +
                f"{safe_text(s.name, multiline=False)} [{s.id[:8]}] {s.updated_at}  {safe_text(s.preview, multiline=False)}"
                for i, s in enumerate(matches[start:start + 10], start)
            )

        @bindings.add("up", eager=True)
        def up(event):
            self.move(-1)

        @bindings.add("down", eager=True)
        def down(event):
            self.move(1)

        @bindings.add("enter", eager=True)
        def accept(event):
            if self.selected() is not None:
                event.app.exit(result=self.selected())

        @bindings.add("escape", eager=True)
        @bindings.add("c-c", eager=True)
        @bindings.add("c-d", eager=True)
        def cancel(event):
            event.app.exit(result=None)

        app: Application[str | None] = Application(
            layout=Layout(HSplit([
                Window(FormattedTextControl("Choose chat — type to filter, ↑/↓, Enter, Esc"), height=1),
                search,
                Window(FormattedTextControl(rows), height=10),
            ]), focused_element=search), key_bindings=bindings, full_screen=False,
        )
        return await app.run_async()
