"""
AstrBot PC 远程控制 / 米家自动化桥接插件

设计目标：
1. 遵循 AstrBot Star 插件结构：metadata.yaml、_conf_schema.json、@register、配置注入、数据写入 data/plugin_data。
2. 为米家/小米 IoT 自动化提供稳定、简单、可鉴权的 HTTP 入口（GET/POST 均可）。
3. 通过 NAS/跳板机执行 WOL、Windows SSH 关机和状态检查，不在插件目录保存运行数据。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # AstrBot 运行环境存在；本地语法检查时可能不存在。
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    get_astrbot_data_path = None

PLUGIN_NAME = "pc_control"
PLUGIN_VERSION = "2.1.0"
PLUGIN_DIR = Path(__file__).resolve().parent

CONFIG_KEYS = {
    "web_host",
    "web_port",
    "api_token",
    "allow_unsafe_without_token",
    "nas_ip",
    "nas_user",
    "nas_pass",
    "nas_ssh_port",
    "nas_target",
    "pc_ip",
    "pc_user",
    "pc_pass",
    "pc_ssh_port",
    "pc_mac",
    "broadcast_ip",
    "game_process",
    "shutdown_delay",
    "ha_mqtt_enabled",
    "ha_mqtt_host",
    "ha_mqtt_port",
    "ha_mqtt_username",
    "ha_mqtt_password",
    "ha_mqtt_discovery_prefix",
    "ha_mqtt_node_id",
    "ha_mqtt_device_name",
    "ha_mqtt_status_interval",
}
SENSITIVE_KEYS = {"api_token", "nas_pass", "pc_pass", "ha_mqtt_password"}

DEFAULTS: dict[str, Any] = {
    "web_host": "0.0.0.0",
    "web_port": 5800,
    "api_token": "",
    "allow_unsafe_without_token": False,
    "nas_ip": "",
    "nas_user": "root",
    "nas_pass": "",
    "nas_ssh_port": 22,
    "nas_target": "",
    "pc_ip": "",
    "pc_user": "",
    "pc_pass": "",
    "pc_ssh_port": 22,
    "pc_mac": "",
    "broadcast_ip": "192.168.31.255",
    "game_process": "StarRail.exe",
    "shutdown_delay": 10,
    "ha_mqtt_enabled": False,
    "ha_mqtt_host": "",
    "ha_mqtt_port": 1883,
    "ha_mqtt_username": "",
    "ha_mqtt_password": "",
    "ha_mqtt_discovery_prefix": "homeassistant",
    "ha_mqtt_node_id": "astrbot_pc_control",
    "ha_mqtt_device_name": "电脑",
    "ha_mqtt_status_interval": 60,
}


@dataclass(slots=True)
class ActionResult:
    ok: bool
    msg: str
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {"ok": self.ok, "code": 0 if self.ok else 1, "msg": self.msg}
        if self.data:
            payload["data"] = self.data
            payload.update(self.data)
        return payload


def _plugin_data_dir() -> Path:
    if get_astrbot_data_path:
        try:
            base = Path(get_astrbot_data_path())
        except TypeError:
            base = Path(get_astrbot_data_path(""))
    else:  # 本地开发兜底：模拟 AstrBot/data
        base = PLUGIN_DIR.parent.parent
    path = base / "plugin_data" / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mask_config(config: dict[str, Any]) -> dict[str, Any]:
    masked = dict(config)
    for key in SENSITIVE_KEYS:
        if masked.get(key):
            masked[key] = "******"
    return masked


def _clean_mac(mac: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", mac or "")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _safe_process_name(name: str) -> str:
    # Windows 进程名只需要普通文件名；避免嵌套 SSH 命令注入。
    name = (name or "StarRail.exe").strip()
    if not re.fullmatch(r"[\w. -]{1,80}", name):
        raise ValueError("game_process 只能包含字母、数字、空格、下划线、短横线和点")
    return name


@register(PLUGIN_NAME, "WillyerQ", "PC 远程控制 / 米家自动化 HTTP 桥接", PLUGIN_VERSION, "https://github.com/WillyerQ/-PC-Public-")
class PCControlPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = _plugin_data_dir()
        self.httpd: ThreadingHTTPServer | None = None
        self.http_thread: threading.Thread | None = None
        self.bound_addr: tuple[str, int] | None = None
        self.mqtt_bridge: HAMQTTBridge | None = None

    async def initialize(self):
        self._migrate_legacy_files()
        cfg = self.get_config()
        host = str(cfg["web_host"])
        port = int(cfg["web_port"])
        try:
            self.httpd = ThreadingHTTPServer((host, port), self._handler_factory())
            self.http_thread = threading.Thread(target=self.httpd.serve_forever, name="pc-control-http", daemon=True)
            self.http_thread.start()
            self.bound_addr = (host, port)
            logger.info(f"[PC-Control] HTTP 服务已启动: http://{host}:{port}")
            if not cfg.get("api_token") and not cfg.get("allow_unsafe_without_token"):
                logger.warning("[PC-Control] 未配置 api_token，HTTP API 将拒绝控制类请求；如仅内网测试可开启 allow_unsafe_without_token")
        except OSError as exc:
            logger.error(f"[PC-Control] HTTP 服务启动失败 {host}:{port}: {exc}")

        if cfg.get("ha_mqtt_enabled"):
            self.mqtt_bridge = HAMQTTBridge(self)
            self.mqtt_bridge.start()

    async def terminate(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.mqtt_bridge:
            self.mqtt_bridge.stop()
            self.mqtt_bridge = None
        self.http_thread = None
        logger.info("[PC-Control] HTTP 服务已停止")

    def _handler_factory(self):
        plugin = self

        class PCControlHandler(Handler):
            bound_plugin = plugin

        return PCControlHandler

    # ---------------- AstrBot 指令 ----------------

    @filter.command_group("pc")
    def pc(self):
        """PC 远程控制：/pc wol|off|status|nas|config"""
        pass

    @pc.command("wol")
    async def pc_wol(self, event: AstrMessageEvent):
        """唤醒电脑"""
        ret = await asyncio.to_thread(self.wol)
        yield event.plain_result(ret.msg)

    @pc.command("off")
    async def pc_off(self, event: AstrMessageEvent):
        """关闭电脑"""
        ret = await asyncio.to_thread(self.shutdown_pc)
        yield event.plain_result(ret.msg)

    @pc.command("status")
    async def pc_status(self, event: AstrMessageEvent):
        """检查电脑和目标进程状态"""
        ret = await asyncio.to_thread(self.status)
        if not ret.ok:
            yield event.plain_result(ret.msg)
            return
        game = ret.data.get("game") if ret.data else False
        yield event.plain_result("💻 电脑在线 | 🎮 目标进程运行中" if game else "💻 电脑在线 | 目标进程未运行")

    @pc.command("nas")
    async def pc_nas(self, event: AstrMessageEvent):
        """测试 NAS/跳板机连通性"""
        ret = await asyncio.to_thread(self.test_nas)
        yield event.plain_result(ret.msg)

    @pc.command("config")
    async def pc_config(self, event: AstrMessageEvent):
        """查看脱敏配置"""
        cfg = _mask_config(self.get_config())
        yield event.plain_result(json.dumps(cfg, ensure_ascii=False, indent=2))

    # ---------------- 配置与数据 ----------------

    def _data_path(self, *parts: str) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir.joinpath(*parts)

    def _local_config_path(self) -> Path:
        return self._data_path("config.json")

    def _log_path(self) -> Path:
        path = self._data_path("logs")
        path.mkdir(parents=True, exist_ok=True)
        return path / "pc_control.log"

    def _migrate_legacy_files(self):
        legacy_config = PLUGIN_DIR / "config.json"
        if legacy_config.exists() and not self._local_config_path().exists():
            try:
                legacy_config.replace(self._local_config_path())
                logger.info("[PC-Control] 已迁移旧 config.json 到 data/plugin_data/pc_control")
            except Exception as exc:
                logger.warning(f"[PC-Control] 迁移旧 config.json 失败: {exc}")
        legacy_logs = PLUGIN_DIR / "logs"
        new_logs = self._data_path("logs")
        if legacy_logs.exists() and (not new_logs.exists() or not any(new_logs.iterdir())):
            try:
                if new_logs.exists():
                    new_logs.rmdir()
                legacy_logs.replace(new_logs)
                logger.info("[PC-Control] 已迁移旧 logs 到 data/plugin_data/pc_control")
            except Exception as exc:
                logger.warning(f"[PC-Control] 迁移旧 logs 失败: {exc}")

    def _load_local_config(self) -> dict[str, Any]:
        path = self._local_config_path()
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning(f"[PC-Control] 本地配置读取失败: {exc}")
            return {}

    def save_local_config(self, data: dict[str, Any]) -> None:
        old = self._load_local_config()
        for key, value in data.items():
            if key in CONFIG_KEYS and value != "******":
                old[key] = value
        path = self._local_config_path()
        with path.open("w", encoding="utf-8") as fp:
            json.dump(old, fp, ensure_ascii=False, indent=2)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

    def get_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULTS)
        if isinstance(self.config, dict):
            for key in CONFIG_KEYS:
                val = self.config.get(key)
                if val not in (None, "", [], {}):
                    cfg[key] = val
        for key, val in self._load_local_config().items():
            if key in CONFIG_KEYS and val not in (None, "", [], {}):
                cfg[key] = val
        for key in ("web_port", "nas_ssh_port", "pc_ssh_port", "shutdown_delay", "ha_mqtt_port", "ha_mqtt_status_interval"):
            try:
                cfg[key] = int(cfg.get(key) or DEFAULTS[key])
            except Exception:
                cfg[key] = DEFAULTS[key]
        cfg["allow_unsafe_without_token"] = _as_bool(cfg.get("allow_unsafe_without_token"))
        cfg["ha_mqtt_enabled"] = _as_bool(cfg.get("ha_mqtt_enabled"))
        return cfg

    def _publish_ha_state(self, state: str, result: ActionResult):
        bridge = self.mqtt_bridge
        if not bridge or not bridge.client or not bridge.connected.is_set():
            return
        try:
            bridge.client.publish(bridge.state_topic, state, retain=True)
            bridge.client.publish(bridge.attributes_topic, json.dumps(result.as_dict(), ensure_ascii=False), retain=True)
        except Exception as exc:
            logger.warning(f"[PC-Control] HA MQTT 状态同步失败: {exc}")

    def _validate(self, *, need_pc: bool = False, need_wol: bool = False, need_nas_target: bool = False) -> list[str]:
        cfg = self.get_config()
        errors: list[str] = []
        for key, label in (("nas_ip", "NAS IP"), ("nas_user", "NAS 用户名"), ("nas_pass", "NAS 密码")):
            if not cfg.get(key):
                errors.append(f"缺少 {label}（{key}）")
        if need_pc:
            for key, label in (("pc_ip", "PC IP"), ("pc_user", "PC 用户名"), ("pc_pass", "PC 密码")):
                if not cfg.get(key):
                    errors.append(f"缺少 {label}（{key}）")
        if need_wol:
            if not cfg.get("pc_mac"):
                errors.append("缺少 PC MAC（pc_mac）")
            elif not re.fullmatch(r"[0-9A-Fa-f]{12}", _clean_mac(str(cfg["pc_mac"]))):
                errors.append("pc_mac 格式不正确")
            if not cfg.get("broadcast_ip"):
                errors.append("缺少广播地址（broadcast_ip）")
        if need_nas_target and not cfg.get("nas_target"):
            errors.append("缺少 NAS 测试 URL（nas_target）")
        return errors

    # ---------------- 远程执行 ----------------

    def _run_on_nas(self, command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        cfg = self.get_config()
        return subprocess.run(
            [
                "sshpass",
                "-p",
                str(cfg["nas_pass"]),
                "ssh",
                "-p",
                str(cfg["nas_ssh_port"]),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                f"{cfg['nas_user']}@{cfg['nas_ip']}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _run_on_pc_via_nas(self, windows_command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        cfg = self.get_config()
        nested = " ".join(
            [
                "sshpass",
                "-p",
                shlex.quote(str(cfg["pc_pass"])),
                "ssh",
                "-p",
                shlex.quote(str(cfg["pc_ssh_port"])),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                f"{shlex.quote(str(cfg['pc_user']))}@{shlex.quote(str(cfg['pc_ip']))}",
                shlex.quote(windows_command),
            ]
        )
        return self._run_on_nas(nested, timeout=timeout)

    def wol(self) -> ActionResult:
        errors = self._validate(need_wol=True)
        if errors:
            return ActionResult(False, "❌ 配置不完整：" + "；".join(errors))
        cfg = self.get_config()
        mac = _clean_mac(str(cfg["pc_mac"]))
        display_mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))
        command = f"wakeonlan -i {shlex.quote(str(cfg['broadcast_ip']))} {shlex.quote(display_mac)}"
        try:
            ret = self._run_on_nas(command, timeout=10)
            ok = ret.returncode == 0
            detail = ret.stdout.strip() or ret.stderr.strip()
            self._audit("WOL", ok, detail[:200])
            result = ActionResult(ok, "📡 唤醒信号已发送" if ok else f"❌ 唤醒失败：{detail[:200]}")
            if ok:
                self._publish_ha_state("ON", result)
            return result
        except Exception as exc:
            self._audit("WOL", False, str(exc))
            return ActionResult(False, f"❌ 唤醒失败：{exc}")

    def shutdown_pc(self) -> ActionResult:
        errors = self._validate(need_pc=True)
        if errors:
            return ActionResult(False, "❌ 配置不完整：" + "；".join(errors))
        cfg = self.get_config()
        delay = max(0, min(int(cfg.get("shutdown_delay") or 10), 3600))
        try:
            ret = self._run_on_pc_via_nas(f"shutdown /s /t {delay}", timeout=30)
            ok = ret.returncode == 0
            detail = ret.stdout.strip() or ret.stderr.strip()
            self._audit("SHUTDOWN", ok, detail[:200])
            result = ActionResult(ok, f"🔌 关机指令已发送（{delay} 秒后执行）" if ok else f"❌ 关机失败：{detail[:200]}")
            if ok:
                self._publish_ha_state("OFF", result)
            return result
        except Exception as exc:
            self._audit("SHUTDOWN", False, str(exc))
            return ActionResult(False, f"❌ 关机失败：{exc}")

    def status(self) -> ActionResult:
        errors = self._validate(need_pc=True)
        if errors:
            return ActionResult(False, "❌ 配置不完整：" + "；".join(errors))
        try:
            process = _safe_process_name(str(self.get_config().get("game_process") or "StarRail.exe"))
            ret = self._run_on_pc_via_nas(f'tasklist /fi "IMAGENAME eq {process}" /nh', timeout=30)
            online = ret.returncode == 0
            game = online and process.lower() in (ret.stdout or "").lower()
            msg = "💻 电脑在线" if online else "❌ 电脑离线或 SSH 不可达"
            return ActionResult(online, msg, {"online": online, "game": game, "process": process})
        except Exception as exc:
            return ActionResult(False, f"❌ 状态检查失败：{exc}")

    def test_nas(self) -> ActionResult:
        errors = self._validate(need_nas_target=True)
        if errors:
            return ActionResult(False, "❌ 配置不完整：" + "；".join(errors))
        cfg = self.get_config()
        command = f"curl -fsS --connect-timeout 3 {shlex.quote(str(cfg['nas_target']))}"
        try:
            ret = self._run_on_nas(command, timeout=8)
            detail = ret.stdout.strip() or ret.stderr.strip()
            ok = ret.returncode == 0
            return ActionResult(ok, detail[:200] if detail else ("✅ NAS 连通性正常" if ok else "❌ NAS 连通性异常"))
        except Exception as exc:
            return ActionResult(False, f"❌ NAS 测试失败：{exc}")

    def _audit(self, action: str, ok: bool, detail: str = ""):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._log_path().open("a", encoding="utf-8") as fp:
                fp.write(f"[{ts}] [{action}] [{'OK' if ok else 'FAIL'}] {detail}\n")
        except Exception:
            pass


class HAMQTTBridge:
    def __init__(self, plugin: PCControlPlugin):
        self.plugin = plugin
        self.client: Any = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.connected = threading.Event()
        self.node_id = "astrbot_pc_control"
        self.base_topic = "astrbot/pc_control"
        self.command_topic = "astrbot/pc_control/set"
        self.state_topic = "astrbot/pc_control/state"
        self.availability_topic = "astrbot/pc_control/availability"
        self.attributes_topic = "astrbot/pc_control/attributes"

    def start(self):
        cfg = self.plugin.get_config()
        if not cfg.get("ha_mqtt_host"):
            logger.warning("[PC-Control] 已启用 HA MQTT，但未填写 ha_mqtt_host")
            return
        self.node_id = self._safe_node_id(str(cfg.get("ha_mqtt_node_id") or "astrbot_pc_control"))
        self.base_topic = f"astrbot/{self.node_id}"
        self.command_topic = f"{self.base_topic}/set"
        self.state_topic = f"{self.base_topic}/state"
        self.availability_topic = f"{self.base_topic}/availability"
        self.attributes_topic = f"{self.base_topic}/attributes"
        self.thread = threading.Thread(target=self._run, name="pc-control-ha-mqtt", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.client:
            try:
                self.client.publish(self.availability_topic, "offline", retain=True)
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

    def publish_status_async(self):
        if not self.client or not self.connected.is_set():
            return
        threading.Thread(target=self.publish_status, name="pc-control-ha-status", daemon=True).start()

    def publish_status(self):
        if not self.client or not self.connected.is_set():
            return
        result = self.plugin.status()
        state = "ON" if result.ok and result.data and result.data.get("online") else "OFF"
        attrs = result.as_dict()
        try:
            self.client.publish(self.state_topic, state, retain=True)
            self.client.publish(self.attributes_topic, json.dumps(attrs, ensure_ascii=False), retain=True)
        except Exception as exc:
            logger.warning(f"[PC-Control] MQTT 状态发布失败: {exc}")

    def _run(self):
        try:
            import paho.mqtt.client as mqtt
        except Exception as exc:
            logger.error(f"[PC-Control] HA MQTT 需要依赖 paho-mqtt，请先安装：pip install paho-mqtt；错误: {exc}")
            return

        cfg = self.plugin.get_config()
        self.client = mqtt.Client(client_id=f"{self.node_id}_astrbot")
        username = str(cfg.get("ha_mqtt_username") or "")
        password = str(cfg.get("ha_mqtt_password") or "")
        if username:
            self.client.username_pw_set(username, password or None)
        self.client.will_set(self.availability_topic, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        try:
            self.client.connect(str(cfg["ha_mqtt_host"]), int(cfg["ha_mqtt_port"]), keepalive=60)
            self.client.loop_start()
            interval = max(15, int(cfg.get("ha_mqtt_status_interval") or 60))
            while not self.stop_event.wait(interval):
                self.publish_status()
        except Exception as exc:
            logger.error(f"[PC-Control] HA MQTT 连接失败: {exc}")
        finally:
            try:
                if self.client:
                    self.client.publish(self.availability_topic, "offline", retain=True)
                    self.client.loop_stop()
                    self.client.disconnect()
            except Exception:
                pass

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int, *extra: Any):
        if rc != 0:
            logger.error(f"[PC-Control] HA MQTT 连接失败，返回码: {rc}")
            return
        self.connected.set()
        client.publish(self.availability_topic, "online", retain=True)
        client.subscribe(self.command_topic)
        self._publish_discovery()
        self.publish_status_async()
        logger.info("[PC-Control] HA MQTT 已连接并发布自动发现配置")

    def _on_disconnect(self, client: Any, userdata: Any, rc: int, *extra: Any):
        self.connected.clear()
        if rc:
            logger.warning(f"[PC-Control] HA MQTT 连接断开，返回码: {rc}")

    def _on_message(self, client: Any, userdata: Any, message: Any):
        payload = message.payload.decode("utf-8", errors="ignore").strip().upper()
        if payload in {"ON", "1", "TRUE"}:
            result = self.plugin.wol()
            logger.info(f"[PC-Control] HA MQTT 开机命令: {result.msg}")
            client.publish(self.attributes_topic, json.dumps(result.as_dict(), ensure_ascii=False), retain=True)
            if result.ok:
                client.publish(self.state_topic, "ON", retain=True)
                time.sleep(3)
                self.publish_status_async()
        elif payload in {"OFF", "0", "FALSE"}:
            result = self.plugin.shutdown_pc()
            logger.info(f"[PC-Control] HA MQTT 关机命令: {result.msg}")
            client.publish(self.attributes_topic, json.dumps(result.as_dict(), ensure_ascii=False), retain=True)
            if result.ok:
                client.publish(self.state_topic, "OFF", retain=True)
        elif payload in {"STATUS", "QUERY"}:
            self.publish_status_async()
        else:
            logger.warning(f"[PC-Control] HA MQTT 未知命令: {payload}")

    def _publish_discovery(self):
        cfg = self.plugin.get_config()
        prefix = str(cfg.get("ha_mqtt_discovery_prefix") or "homeassistant").strip().strip("/")
        name = str(cfg.get("ha_mqtt_device_name") or "电脑")
        unique_id = f"{self.node_id}_power"
        discovery_topic = f"{prefix}/switch/{self.node_id}/power/config"
        payload = {
            "name": f"{name} 电源",
            "unique_id": unique_id,
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "availability_topic": self.availability_topic,
            "json_attributes_topic": self.attributes_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_on": "ON",
            "state_off": "OFF",
            "icon": "mdi:desktop-tower",
            "device": {
                "identifiers": [self.node_id],
                "name": name,
                "manufacturer": "AstrBot",
                "model": "PC Control Bridge",
                "sw_version": PLUGIN_VERSION,
            },
        }
        self.client.publish(discovery_topic, json.dumps(payload, ensure_ascii=False), retain=True)

    @staticmethod
    def _safe_node_id(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_-]", "_", value.strip())
        return value or "astrbot_pc_control"


PAGE_INDEX = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PC 远程控制</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1220;color:#eef;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.card{width:min(420px,92vw);padding:24px;border:1px solid #252a44;border-radius:22px;background:#171b2f;box-shadow:0 20px 60px #0006}h1{margin:0 0 6px;font-size:24px}.sub{margin:0 0 18px;color:#99a}.row{display:grid;gap:12px}.btn{border:0;border-radius:14px;padding:16px;font-size:16px;font-weight:700;color:white;cursor:pointer}.wol{background:linear-gradient(135deg,#667eea,#764ba2)}.off{background:linear-gradient(135deg,#f45b69,#c471ed)}.status{background:#242a48}.msg{margin-top:16px;padding:12px;border-radius:12px;background:#222842;color:#ccd;white-space:pre-wrap}.ok{background:#163523;color:#8df0a8}.err{background:#3a1d24;color:#ff9aa8}a{color:#9ab;text-decoration:none}.top{display:flex;justify-content:space-between;margin-bottom:18px}.token{width:100%;border:1px solid #303653;background:#101424;color:#eef;border-radius:12px;padding:11px;margin-bottom:12px}
</style></head><body><main class="card"><div class="top"><span>⚡ PC Control</span><a href="/config">设置</a></div><h1>远程控制台</h1><p class="sub">适配 AstrBot 指令与米家 HTTP 自动化</p><input id="token" class="token" type="password" placeholder="API Token（如已配置）"><div class="row"><button class="btn wol" onclick="call('/api/wol')">📡 唤醒电脑</button><button class="btn off" onclick="call('/api/shutdown')">🔌 关闭电脑</button><button class="btn status" onclick="call('/api/status')">🔄 检查状态</button><button class="btn status" onclick="call('/api/nas')">📶 测试 NAS</button></div><div id="msg" class="msg">等待操作...</div></main><script>
const token=document.getElementById('token');token.value=localStorage.pc_token||'';token.oninput=()=>localStorage.pc_token=token.value;
async function call(path){const m=document.getElementById('msg');m.className='msg';m.textContent='请求中...';try{const r=await fetch(path,{headers:{'X-Api-Token':token.value}});const d=await r.json();m.className='msg '+(d.ok?'ok':'err');m.textContent=d.msg||JSON.stringify(d,null,2)}catch(e){m.className='msg err';m.textContent='请求失败：'+e.message}}
</script></body></html>"""

PAGE_CONFIG = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PC 控制设置</title><style>
body{margin:0;padding:22px;background:#0f1220;color:#eef;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:760px;margin:0 auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.field{background:#171b2f;border:1px solid #252a44;border-radius:14px;padding:12px}label{display:block;color:#aab;font-size:13px;margin-bottom:6px}input{width:100%;border:1px solid #303653;background:#101424;color:#eef;border-radius:10px;padding:10px}.bar{display:flex;gap:10px;align-items:center;justify-content:space-between;margin-bottom:18px}.btn{border:0;border-radius:12px;padding:12px 16px;background:#667eea;color:white;font-weight:700;cursor:pointer}.msg{margin-top:14px;padding:12px;border-radius:12px;background:#222842}.ok{background:#163523;color:#8df0a8}.err{background:#3a1d24;color:#ff9aa8}a{color:#9ab;text-decoration:none}
</style></head><body><main><div class="bar"><a href="/">← 返回</a><button class="btn" onclick="save()">保存</button></div><h1>配置</h1><p>此页面写入 data/plugin_data/pc_control/config.json；也可优先在 AstrBot 插件配置页填写。</p><div class="field"><label>当前页面鉴权 Token</label><input id="page_token" type="password" placeholder="读取/保存配置需要 API Token"></div><div id="fields" class="grid"></div><div id="msg" class="msg">先填写 Token 后加载配置。</div></main><script>
const defs=[['web_host','Web 监听地址','0.0.0.0'],['web_port','Web 端口','5800'],['api_token','API Token','',true],['allow_unsafe_without_token','未配置 Token 时允许控制(true/false)','false'],['nas_ip','NAS/跳板机 IP',''],['nas_user','NAS SSH 用户','root'],['nas_pass','NAS SSH 密码','',true],['nas_ssh_port','NAS SSH 端口','22'],['nas_target','NAS 测试 URL','http://...'],['pc_ip','PC IP',''],['pc_user','Windows SSH 用户',''],['pc_pass','Windows SSH 密码','',true],['pc_ssh_port','Windows SSH 端口','22'],['pc_mac','PC MAC','AA:BB:CC:DD:EE:FF'],['broadcast_ip','WOL 广播地址','192.168.31.255'],['game_process','目标进程名','StarRail.exe'],['shutdown_delay','关机延迟秒数','10'],['ha_mqtt_enabled','启用 HA MQTT(true/false)','false'],['ha_mqtt_host','HA MQTT Broker 地址','192.168.31.x'],['ha_mqtt_port','HA MQTT 端口','1883'],['ha_mqtt_username','HA MQTT 用户名',''],['ha_mqtt_password','HA MQTT 密码','',true],['ha_mqtt_discovery_prefix','HA discovery prefix','homeassistant'],['ha_mqtt_node_id','HA MQTT 节点 ID','astrbot_pc_control'],['ha_mqtt_device_name','HA 设备名','电脑'],['ha_mqtt_status_interval','HA 状态刷新秒数','60']];
const token=document.getElementById('page_token');token.value=localStorage.pc_token||'';token.oninput=()=>localStorage.pc_token=token.value;let data={};
function render(){document.getElementById('fields').innerHTML=defs.map(([k,l,ph,pw])=>`<div class="field"><label>${l}<br><small>${k}</small></label><input id="i_${k}" ${pw?'type="password"':''} placeholder="${ph}" value="${String(data[k]??'').replaceAll('&','&amp;').replaceAll('"','&quot;')}"></div>`).join('')}
async function load(){try{const r=await fetch('/api/config',{headers:{'X-Api-Token':token.value}});data=await r.json();if(!r.ok||data.ok===false)throw new Error(data.msg||'读取失败');render();msg('配置已加载','ok')}catch(e){render();msg('读取失败：'+e.message,'err')}}
async function save(){defs.forEach(([k])=>{const v=document.getElementById('i_'+k).value;if(v!==''&&v!=='******')data[k]=v});try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json','X-Api-Token':token.value},body:JSON.stringify(data)});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.msg||'保存失败');msg('保存成功；端口/监听地址变更需重载插件','ok')}catch(e){msg('保存失败：'+e.message,'err')}}
function msg(t,c){const m=document.getElementById('msg');m.className='msg '+(c||'');m.textContent=t}render();if(token.value)load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    bound_plugin: PCControlPlugin

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _route(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items() if v}

        if method == "GET" and path == "/":
            return self._html(PAGE_INDEX)
        if method == "GET" and path == "/config":
            return self._html(PAGE_CONFIG)

        body = self._read_json() if method == "POST" else {}
        params = {**query, **(body if isinstance(body, dict) else {})}

        if path == "/health":
            return self._json({"ok": True, "msg": "ok", "name": PLUGIN_NAME, "version": PLUGIN_VERSION})

        if not path.startswith("/api") and not path.startswith("/mi"):
            return self.send_error(404)
        if not self._authorized(params):
            return self._json({"ok": False, "code": 401, "msg": "unauthorized"}, 401)

        plugin = self.bound_plugin
        try:
            if path in {"/api/config", "/mi/config"}:
                if method == "POST":
                    if not isinstance(body, dict):
                        return self._json({"ok": False, "msg": "invalid json body"}, 400)
                    plugin.save_local_config(body)
                    return self._json({"ok": True, "code": 0, "msg": "saved"})
                return self._json(_mask_config(plugin.get_config()))
            if path in {"/api/wol", "/mi/wol", "/mi/power_on"}:
                return self._json(plugin.wol().as_dict())
            if path in {"/api/shutdown", "/mi/shutdown", "/mi/power_off"}:
                return self._json(plugin.shutdown_pc().as_dict())
            if path in {"/api/status", "/mi/status"}:
                return self._json(plugin.status().as_dict())
            if path in {"/api/nas", "/mi/nas"}:
                return self._json(plugin.test_nas().as_dict())
        except Exception as exc:
            logger.exception(f"[PC-Control] HTTP request failed: {path}")
            return self._json({"ok": False, "code": 500, "msg": str(exc)}, 500)
        return self.send_error(404)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _authorized(self, params: dict[str, Any]) -> bool:
        cfg = self.bound_plugin.get_config()
        token = str(cfg.get("api_token") or "")
        if not token:
            return bool(cfg.get("allow_unsafe_without_token"))
        got = (
            self.headers.get("X-Api-Token")
            or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or str(params.get("token") or "")
        )
        return hmac.compare_digest(got, token)

    def _html(self, content: str):
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _json(self, data: dict[str, Any], status: int = 200):
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Api-Token")

    def log_message(self, fmt: str, *args):
        return
