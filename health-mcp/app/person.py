"""Two people can share one Claude account, so the connector cannot tell them apart by sign-in alone.
Every personal tool therefore takes an optional `person`; the caller sets it to whoever is asking."""
from __future__ import annotations

import functools
import inspect
from contextvars import ContextVar
from typing import Any, Callable

CURRENT: ContextVar[str | None] = ContextVar("person", default=None)
NOTE = ("\n\nperson: whose data this is (a name from WEB_USERS, for example khushal or shivani). Leave it empty for the "
        "default person. Set it whenever someone else is the one asking.")


def personal(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Give a tool an optional `person` argument that selects whose data the call reads or writes."""
    sig = inspect.signature(fn, eval_str=True)
    if "person" in sig.parameters:
        return fn
    new_sig = sig.replace(parameters=[*sig.parameters.values(), inspect.Parameter(
        "person", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None)])

    def pick(person: str | None) -> Any:
        return CURRENT.set(person.strip().lower() if person and person.strip() else None)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*a: Any, person: str | None = None, **kw: Any) -> Any:
            tok = pick(person)
            try:
                return await fn(*a, **kw)
            finally:
                CURRENT.reset(tok)
        wrapper: Any = awrapper
    else:
        @functools.wraps(fn)
        def swrapper(*a: Any, person: str | None = None, **kw: Any) -> Any:
            tok = pick(person)
            try:
                return fn(*a, **kw)
            finally:
                CURRENT.reset(tok)
        wrapper = swrapper
    wrapper.__signature__ = new_sig
    wrapper.__annotations__ = {**{k: v.annotation for k, v in new_sig.parameters.items()}, "return": new_sig.return_annotation}
    wrapper.__doc__ = (fn.__doc__ or "") + NOTE
    return wrapper


class PersonalTools:
    """Wraps an MCP server so every tool registered through it gets the `person` argument."""

    def __init__(self, mcp: Any) -> None:
        self._mcp = mcp

    def tool(self, *a: Any, **kw: Any) -> Callable[[Callable[..., Any]], Any]:
        deco = self._mcp.tool(*a, **kw)
        return lambda fn: deco(personal(fn))
