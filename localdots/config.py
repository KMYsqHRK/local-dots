"""全体設定 (localdots.toml) と dot の定義 (dots/<name>/dot.toml) の読み込み。"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "localdots.toml"


class ConfigError(Exception):
    pass


@dataclass
class BackendConfig:
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = ""  # 空なら /v1/models の先頭
    api_key: str = ""
    manage: bool = True  # 止まっていれば start_cmd で起動し、終わったら stop_cmd で止める
    start_cmd: list[str] = field(default_factory=list)
    stop_cmd: list[str] = field(default_factory=list)
    startup_timeout_s: float = 600.0
    request_timeout_s: float = 600.0
    temperature: float = 0.3
    max_tokens: int = 1024
    context_tokens: int = 8192
    thinking: bool = False  # Qwen の思考モード。8K では切っておくほうが安定する


@dataclass
class Schedule:
    every_s: float | None = None
    at: str | None = None  # "HH:MM"
    window: tuple[str, str] | None = None  # ("HH:MM", "HH:MM")
    manual: bool = False


@dataclass
class DotConfig:
    name: str
    path: Path
    description: str = ""
    enabled: bool = True
    schedule: Schedule = field(default_factory=Schedule)
    instructions: str = ""
    max_steps: int = 12
    temperature: float | None = None
    history_window: int | None = None  # 直近この回数分のやりとりだけを LLM に渡す(1件ずつ独立した仕事向け)
    max_observation_chars: int = 2000  # ツールの結果をこの文字数で切る。一覧を丸ごと読ませる dot では広げる


@dataclass
class AppConfig:
    root: Path
    backend: BackendConfig
    dots_dir: Path
    state_dir: Path


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", text)
    if not m:
        raise ConfigError(f"間隔の書き方が正しくありません: {text!r}(例: 30m, 2h, 1d)")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _check_hhmm(text: str) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        raise ConfigError(f"時刻は HH:MM で書いてください: {text!r}")
    return text


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_app(root: Path) -> AppConfig:
    root = root.resolve()
    raw = _read_toml(root / CONFIG_FILE) if (root / CONFIG_FILE).exists() else {}
    known = BackendConfig.__dataclass_fields__
    b = raw.get("backend", {})
    unknown = set(b) - set(known)
    if unknown:
        raise ConfigError(f"[backend] に知らない項目があります: {', '.join(sorted(unknown))}")
    paths = raw.get("paths", {})
    return AppConfig(
        root=root,
        backend=BackendConfig(**b),
        dots_dir=root / paths.get("dots_dir", "dots"),
        state_dir=root / paths.get("state_dir", "state"),
    )


def _parse_schedule(raw: dict, where: str) -> Schedule:
    s = Schedule(manual=bool(raw.get("manual", False)))
    if "every" in raw:
        s.every_s = parse_duration(raw["every"])
    if "at" in raw:
        s.at = _check_hhmm(raw["at"])
    if s.every_s is not None and s.at is not None:
        raise ConfigError(f"{where}: every と at は同時に指定できません")
    if "window" in raw:
        w = raw["window"]
        if not (isinstance(w, list) and len(w) == 2):
            raise ConfigError(f'{where}: window は ["HH:MM", "HH:MM"] で書いてください')
        s.window = (_check_hhmm(w[0]), _check_hhmm(w[1]))
    if not s.manual and s.every_s is None and s.at is None:
        raise ConfigError(f"{where}: [schedule] に every / at / manual のどれかが必要です")
    return s


def load_dot(path: Path) -> DotConfig:
    toml_path = path / "dot.toml"
    raw = _read_toml(toml_path)
    agent = raw.get("agent", {})
    instructions_file = path / agent.get("instructions_file", "instructions.md")
    instructions = instructions_file.read_text(encoding="utf-8") if instructions_file.exists() else ""
    return DotConfig(
        name=path.name,
        path=path,
        description=raw.get("description", ""),
        enabled=bool(raw.get("enabled", True)),
        schedule=_parse_schedule(raw.get("schedule", {}), str(toml_path)),
        instructions=instructions.strip(),
        max_steps=int(agent.get("max_steps", 12)),
        temperature=agent.get("temperature"),
        history_window=agent.get("history_window"),
        max_observation_chars=int(agent.get("max_observation_chars", 2000)),
    )


def load_dots(app: AppConfig) -> dict[str, DotConfig]:
    if not app.dots_dir.is_dir():
        return {}
    return {
        p.name: load_dot(p)
        for p in sorted(app.dots_dir.iterdir())
        if p.is_dir() and (p / "dot.toml").exists()
    }
