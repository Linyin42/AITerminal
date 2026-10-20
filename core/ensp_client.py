# -*- coding: utf-8 -*-
"""eNSP Pro **平台** REST 客户端：登录 / 列沙箱与设备 / 启停设备。

与 `ensp_connector.py` 的分工：
    - 本模块管"平台本身"（纯 REST）：把 sandboxId / nodeId 列出来、启停设备；
    - `ensp_connector.py` 管"设备的命令行"（HTTP + STOMP + SockJS）。

界面上「从平台拉取设备」就靠它 —— 用户不必去别处抄那两个数字。

接口族（从前端 bundle 逆向 + 本机实测跑通，探针 tools/probe_ensp_api.py）：
    GET  /api/login?service={origin}                免登录会话（拿 csrfToken / JSESSIONID）
    GET  /api/getUserInfo
    POST /service/ensp/sandbox/getByPage            {"projectId":1,"currentPage":1,"pageSize":20}
    POST /service/ensp/device/batchStart            {"sandboxId":N,"deviceIds":[M]}
    POST /service/ensp/device/batchStop             {"sandboxId":N,"deviceIds":[M]}

用法::

    cli = EnspProClient("192.168.31.11", 8443)
    cli.login()
    for sid, sname, dev in cli.devices():
        print(sid, sname, dev["id"], dev.get("name"))
"""
from __future__ import annotations

import requests


class EnspProError(Exception):
    """平台接口调用失败（给界面直接显示用，文案面向用户）"""


class EnspProClient:
    """eNSP Pro 平台 REST 客户端（standalone 部署，免密自动登录）"""

    def __init__(self, host: str = "", port: int = 8443, timeout: float = 20):
        self.host = (host or "").strip()
        self.port = int(port or 8443)
        self.timeout = float(timeout or 20)
        self._session = requests.Session()
        self._session.verify = False
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    # ------------------------------------------------------------ 基础
    def login(self) -> dict:
        """取得会话（GET /api/login?service=... 即完成自动登录）"""
        if not self.host:
            raise EnspProError("请先填写平台地址（eNSP Pro 服务器的 IP）")
        try:
            self._session.get(f"{self.base_url}/api/login?service={self.base_url}",
                              timeout=self.timeout)
            resp = self._session.get(self.base_url + "/api/getUserInfo",
                                     timeout=self.timeout)
        except Exception as exc:
            raise EnspProError(f"连不上 eNSP Pro 平台 {self.base_url}：{exc}") from exc
        try:
            payload = resp.json()
        except Exception as exc:
            raise EnspProError(f"平台返回的不是 JSON（地址或端口可能不对）：{exc}") from exc
        if payload.get("code") != 0 or not payload.get("data"):
            raise EnspProError("eNSP Pro 登录失败：没取到用户信息")
        return payload["data"]

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = self._session.post(self.base_url + path, json=body,
                                      timeout=self.timeout)
        except Exception as exc:
            raise EnspProError(f"调用平台接口失败 {path}：{exc}") from exc
        try:
            return resp.json() or {}
        except Exception as exc:
            raise EnspProError(f"平台接口 {path} 返回非 JSON：{resp.text[:120]}") from exc

    # ------------------------------------------------------------ 沙箱 / 设备
    def sandboxes(self) -> list:
        """列沙箱；每个沙箱的 `devices` 里就带着设备（含 nodeId）"""
        payload = self._post("/service/ensp/sandbox/getByPage",
                             {"projectId": 1, "currentPage": 1, "pageSize": 50})
        if payload.get("code") not in (0, None):
            raise EnspProError(f"取沙箱列表失败：{payload.get('message')}")
        return payload.get("data") or []

    def devices(self) -> list:
        """拍平成 [(sandbox_id, sandbox_name, device_dict), ...]"""
        out = []
        for sb in self.sandboxes():
            for dev in (sb.get("devices") or []):
                out.append((sb.get("id"), sb.get("name"), dev))
        return out

    def start_device(self, sandbox_id, device_id) -> bool:
        return self._device_action("batchStart", sandbox_id, device_id)

    def stop_device(self, sandbox_id, device_id) -> bool:
        return self._device_action("batchStop", sandbox_id, device_id)

    def _device_action(self, action: str, sandbox_id, device_id) -> bool:
        payload = self._post(f"/service/ensp/device/{action}",
                             {"sandboxId": int(sandbox_id),
                              "deviceIds": [int(device_id)]})
        if payload.get("code") != 0:
            raise EnspProError(f"{action} 失败：{payload.get('message')}")
        return True
