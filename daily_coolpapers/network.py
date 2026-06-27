import logging
import os
import re
from typing import Any


logger = logging.getLogger(__name__)


def normalize_proxy_url(proxy_url: str | None) -> str:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return ""
    if "://" not in proxy_url:
        return f"http://{proxy_url}"
    return proxy_url


def httpx_proxy_kwargs(explicit_proxy_url: str | None = "", use_system_proxy: bool = False) -> dict[str, Any]:
    proxy_url = normalize_proxy_url(explicit_proxy_url)
    if not proxy_url and use_system_proxy:
        proxy_url = detect_windows_proxy_url()

    if proxy_url:
        return {"trust_env": False, "proxy": proxy_url}
    return {"trust_env": bool(use_system_proxy)}


def detect_windows_proxy_url() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if int(enabled) != 1:
                return ""
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except FileNotFoundError:
        return ""
    except Exception:
        logger.exception("Failed detecting Windows proxy settings")
        return ""

    return _select_proxy_from_windows_value(str(proxy_server))


def _select_proxy_from_windows_value(proxy_server: str) -> str:
    values = [item for item in re.split(r"[;\s]+", proxy_server.strip()) if item]
    named: dict[str, str] = {}
    bare: list[str] = []
    for item in values:
        if "=" in item:
            key, value = item.split("=", 1)
            if value:
                named[key.lower()] = normalize_proxy_url(value)
        else:
            bare.append(normalize_proxy_url(item))
    return named.get("https") or named.get("http") or next(iter(named.values()), "") or (bare[0] if bare else "")
