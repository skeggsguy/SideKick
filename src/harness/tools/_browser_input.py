"""Lane B's raw input primitives over CDP `Input.*` — the only place mouse
and key events are dispatched. Mouse events always go to the top-level
session at viewport coords (the browser process hit-tests them into the
right frame — spike 2026-07-18 verified this routes into OOPIFs); key and
insertText events target the OWNING session, because an OOPIF field takes
its input on the frame's own session (same spike)."""

from __future__ import annotations

from ._cdp import CDPClient


async def click_at(cdp: CDPClient, sid: str, x: float, y: float) -> None:
    """move + press + release, left button, at viewport coords."""
    await cdp.send(
        "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, sid
    )
    for etype in ("mousePressed", "mouseReleased"):
        await cdp.send(
            "Input.dispatchMouseEvent",
            {"type": etype, "x": x, "y": y, "button": "left", "clickCount": 1},
            sid,
        )


async def scroll_wheel(cdp: CDPClient, sid: str, direction: str, pixels: int) -> None:
    delta = pixels if direction == "down" else -pixels
    await cdp.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseWheel", "x": 400, "y": 300, "deltaX": 0, "deltaY": delta},
        sid,
    )


async def press_key(cdp: CDPClient, sid: str, key: str, vk: int) -> None:
    """One rawKeyDown+keyUp pair (non-text key) on the owning session."""
    for etype in ("rawKeyDown", "keyUp"):
        await cdp.send(
            "Input.dispatchKeyEvent",
            {"type": etype, "key": key, "code": key, "windowsVirtualKeyCode": vk},
            sid,
        )


async def clear_field(cdp: CDPClient, sid: str) -> None:
    # Ctrl+A (modifiers=2) then Delete — on the focused field's session.
    for etype in ("rawKeyDown", "keyUp"):
        await cdp.send(
            "Input.dispatchKeyEvent",
            {"type": etype, "modifiers": 2, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
            sid,
        )
    for etype in ("rawKeyDown", "keyUp"):
        await cdp.send(
            "Input.dispatchKeyEvent",
            {"type": etype, "key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46},
            sid,
        )


async def press_enter(cdp: CDPClient, sid: str) -> None:
    await cdp.send(
        "Input.dispatchKeyEvent",
        {"type": "rawKeyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
        sid,
    )
    await cdp.send(
        "Input.dispatchKeyEvent",
        {"type": "char", "key": "Enter", "text": "\r", "windowsVirtualKeyCode": 13},
        sid,
    )
    await cdp.send(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
        sid,
    )
