"""Notificações nativas do macOS (osascript). Em outros sistemas não faz nada. Nunca levanta exceção."""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("eleicoes.notify")


def _applescript_str(s: str) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r", " ").replace("\n", " ")
    return f'"{s}"'


def build_script(title: str, message: str) -> str:
    return f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["osascript", "-e", build_script(title, message)], check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        log.debug("notificação falhou: %s", e)
