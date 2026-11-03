"""ツールの定義。関数のシグネチャと docstring から LLM 向けの説明を作る。

    @tool
    def evaluate(ctx, x: float, y: float = 0.0) -> dict:
        \"\"\"(x, y) で1回評価して loss を返す。\"\"\"

第1引数は必ず ctx (ToolContext)。残りの引数が LLM から受け取る値になる。
"""

from __future__ import annotations

import importlib.util
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .config import AppConfig, DotConfig
    from .inbox import Inbox
    from .state import DotState

_TYPES: dict[Any, tuple[str, Callable[[Any], Any]]] = {
    float: ("float", float),
    int: ("int", int),
    str: ("str", str),
    bool: ("bool", lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")),
    list: ("list", lambda v: v if isinstance(v, list) else [v]),
    dict: ("dict", lambda v: v if isinstance(v, dict) else dict(v)),
}
_TYPE_NAMES = {name: t for t, (name, _) in _TYPES.items()}


class ToolArgError(Exception):
    pass


@dataclass
class Control:
    """組み込みツールが返す「この実行をここで終える」合図。"""

    kind: str  # "done" / "blocked" / "snoozed"
    summary: str
    question_id: str | None = None
    next_due: datetime | None = None


@dataclass
class ToolContext:
    dot: DotConfig
    state: DotState
    inbox: Inbox
    now: datetime
    app: AppConfig | None = None  # 他の dot の状態や、レポートの置き場所(app.root)を知りたいとき
    run: dict[str, Any] = field(default_factory=dict)  # この実行の間だけ使うメモ(1回あたりの上限の数え上げなど)


@dataclass
class Param:
    name: str
    type_name: str
    convert: Callable[[Any], Any]
    default: Any = inspect.Parameter.empty

    @property
    def required(self) -> bool:
        return self.default is inspect.Parameter.empty


@dataclass
class Tool:
    name: str
    description: str
    params: list[Param]
    func: Callable[..., Any]

    def signature(self) -> str:
        parts = []
        for p in self.params:
            s = f"{p.name}: {p.type_name}"
            if not p.required:
                s += f" = {p.default!r}"
            parts.append(s)
        return f"{self.name}({', '.join(parts)})"

    def call(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        if not isinstance(args, dict):
            raise ToolArgError("args はオブジェクト({...})で渡してください")
        known = {p.name for p in self.params}
        extra = set(args) - known
        if extra:
            raise ToolArgError(f"知らない引数があります: {', '.join(sorted(extra))}。使える引数: {', '.join(sorted(known)) or 'なし'}")
        kwargs = {}
        for p in self.params:
            if p.name in args:
                try:
                    kwargs[p.name] = p.convert(args[p.name])
                except (TypeError, ValueError) as e:
                    raise ToolArgError(f"{p.name} を {p.type_name} にできません: {args[p.name]!r}") from e
            elif p.required:
                raise ToolArgError(f"引数 {p.name} がありません。呼び方: {self.signature()}")
        return self.func(ctx, **kwargs)


def tool(func: Callable[..., Any] | None = None, *, name: str | None = None):
    def mark(f: Callable[..., Any]) -> Callable[..., Any]:
        f._localdots_tool = name or f.__name__  # type: ignore[attr-defined]
        return f

    return mark(func) if func is not None else mark


def tool_from_func(func: Callable[..., Any]) -> Tool:
    sig = inspect.signature(func)
    ps = list(sig.parameters.values())
    if not ps or ps[0].name != "ctx":
        raise TypeError(f"{func.__name__}: ツール関数の第1引数は ctx にしてください")
    params = []
    for p in ps[1:]:
        ann = p.annotation
        if isinstance(ann, str):  # from __future__ import annotations のとき
            ann = _TYPE_NAMES.get(ann, str)
        type_name, convert = _TYPES.get(ann, ("str", str))
        params.append(Param(p.name, type_name, convert, p.default))
    doc = inspect.getdoc(func) or ""
    return Tool(
        name=getattr(func, "_localdots_tool", func.__name__),
        description=doc.split("\n\n")[0].replace("\n", " ").strip(),
        params=params,
        func=func,
    )


def collect_tools(namespace: dict[str, Any]) -> dict[str, Tool]:
    tools = [tool_from_func(v) for v in namespace.values() if callable(v) and hasattr(v, "_localdots_tool")]
    return {t.name: t for t in tools}


def load_dot_tools(dot_path: Path) -> dict[str, Tool]:
    path = dot_path / "tools.py"
    if not path.exists():
        return {}
    spec = importlib.util.spec_from_file_location(f"localdots_dot_{dot_path.name.replace('-', '_')}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return collect_tools(vars(module))
