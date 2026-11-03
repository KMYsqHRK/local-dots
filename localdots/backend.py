"""LLM サーバーの生存確認と、必要なときだけの起動・停止。

自分で起動したサーバーだけを止める。もともと動いていたサーバー(人が手で起動したもの)には触らない。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .config import BackendConfig
from .llm import ChatClient, LLMError


class BackendError(Exception):
    pass


def _resolve(cmd: list[str]) -> list[str]:
    # Windows の strata は strata.cmd なので、PATHEXT を見て実体を探す
    exe = shutil.which(cmd[0])
    return [exe or cmd[0], *cmd[1:]]


class Backend:
    def __init__(self, cfg: BackendConfig, log_path: Path, *, manage: bool | None = None, log=print):
        self.cfg = cfg
        self.manage = cfg.manage if manage is None else manage
        self.log_path = log_path
        self.log = log
        self.client = ChatClient(cfg)
        self.started_here = False

    def healthy(self) -> bool:
        try:
            return bool(self.client.list_models(timeout=3))
        except LLMError:
            return False

    def ensure(self) -> None:
        if self.healthy():
            return
        if not self.manage:
            raise BackendError(f"{self.cfg.base_url} が応答しません(--no-manage なので起動はしません)")
        if not self.cfg.start_cmd:
            raise BackendError("サーバーが応答せず、backend.start_cmd も設定されていません")
        self.log(f"[backend] 起動します: {' '.join(self.cfg.start_cmd)}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as out:
            proc = subprocess.Popen(_resolve(self.cfg.start_cmd), stdout=out, stderr=subprocess.STDOUT)
        self.started_here = True
        deadline = time.monotonic() + self.cfg.startup_timeout_s
        while time.monotonic() < deadline:
            if self.healthy():
                self.log("[backend] 応答しました")
                return
            code = proc.poll()
            if code not in (None, 0):
                raise BackendError(f"起動コマンドが終了コード {code} で止まりました。{self.log_path} を見てください")
            time.sleep(3)
        raise BackendError(f"{self.cfg.startup_timeout_s:.0f} 秒待っても応答しませんでした。{self.log_path} を見てください")

    def release(self) -> None:
        if not (self.started_here and self.manage and self.cfg.stop_cmd):
            return
        self.log(f"[backend] 停止します: {' '.join(self.cfg.stop_cmd)}")
        with self.log_path.open("ab") as out:
            subprocess.run(_resolve(self.cfg.stop_cmd), stdout=out, stderr=subprocess.STDOUT, timeout=180)
        self.started_here = False

    def __enter__(self) -> Backend:
        self.ensure()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
