"""
PC 远程控制插件
- Web 控制台：WOL 唤醒 / 关机 / 状态检查
- 米家自动化 HTTP API 支持
- AstrBot WebUI 配置 + 插件数据目录持久化
"""
import asyncio
import json
import os
import re
import shlex
import subprocess
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - 本地语法检查环境兜底
    get_astrbot_data_path = None

PLUGIN_NAME = "pc_control"
PLUGIN_VERSION = "1.1.0"
PLUGIN_DIR = Path(__file__).resolve().parent

CONFIG_KEYS = [
    "nas_ip", "nas_user", "nas_pass", "pc_ip", "pc_user", "pc_pass",
    "pc_mac", "broadcast_ip", "nas_target", "web_host", "web_port",
    "api_token", "game_process",
]
SENSITIVE_KEYS = {"nas_pass", "pc_pass", "api_token"}
DEFAULTS: dict[str, Any] = {
    "nas_ip": "",
    "nas_user": "root",
    "nas_pass": "",
    "pc_ip": "",
    "pc_user": "",
    "pc_pass": "",
    "pc_mac": "",
    "broadcast_ip": "192.168.31.255",
    "nas_target": "",
    "web_host": "0.0.0.0",
    "web_port": 5800,
    "api_token": "",
    "game_process": "StarRail.exe",
}


def _get_plugin_data_dir() -> Path:
    """获取 AstrBot 规范插件数据目录：data/plugin_data/{plugin_name}/。"""
    if get_astrbot_data_path:
        try:
            base = Path(get_astrbot_data_path())
        except TypeError:
            base = Path(get_astrbot_data_path(""))
    else:
        base = PLUGIN_DIR.parent.parent
    data_dir = base / "plugin_data" / PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _mask_config(cfg: dict[str, Any]) -> dict[str, Any]:
    ret = dict(cfg)
    for key in SENSITIVE_KEYS:
        if ret.get(key):
            ret[key] = "******"
    return ret


@register(PLUGIN_NAME, "AstrBot", "PC 远程控制", PLUGIN_VERSION)
class PCControlPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.plugin_config = config or {}
        self.data_dir = _get_plugin_data_dir()
        self._server: ThreadingHTTPServer | None = None
        self._task: asyncio.Future | None = None
        self._bound_addr: tuple[str, int] | None = None

    async def initialize(self):
        self._migrate_legacy_files()
        cfg = self._get_all_config()
        host = str(cfg.get("web_host") or "0.0.0.0")
        port = int(cfg.get("web_port") or 5800)
        try:
            self._server = ThreadingHTTPServer((host, port), lambda *a: Handler(self, *a))
            self._bound_addr = (host, port)
            loop = asyncio.get_running_loop()
            self._task = loop.run_in_executor(None, self._server.serve_forever)
            logger.info(f"[PC-Control] Web 控制台已启动: http://{host}:{port}")
        except OSError as e:
            logger.error(f"[PC-Control] Web 控制台启动失败 {host}:{port}: {e}")

    async def terminate(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        logger.info("[PC-Control] 服务已停止")

    # ========== AstrBot 指令 ==========

    @filter.command("pc")
    async def pc_cmd(self, event: AstrMessageEvent):
        """PC 远程控制指令：/pc wol|off|status|nas|config"""
        args = (event.message_str or "").strip().split()
        if len(args) < 2:
            yield event.plain_result(self._help_text())
            return

        action = args[1].lower()
        if action == "wol":
            result = await asyncio.to_thread(self.wol)
            yield event.plain_result(result["msg"])
        elif action == "off":
            result = await asyncio.to_thread(self.shutdown_pc)
            yield event.plain_result(result["msg"])
        elif action == "status":
            result = await asyncio.to_thread(self.status)
            if result.get("ok") is False:
                yield event.plain_result(result.get("msg", "❌ 状态检查失败"))
            else:
                yield event.plain_result("💻 电脑在线 | 🎮 游戏运行中" if result.get("game") else "💻 电脑在线 | 游戏未运行")
        elif action == "nas":
            result = await asyncio.to_thread(self.test_nas)
            yield event.plain_result(result["msg"])
        elif action == "config":
            cfg = _mask_config(self._get_all_config())
            yield event.plain_result("当前配置：\n" + json.dumps(cfg, ensure_ascii=False, indent=2))
        else:
            yield event.plain_result(self._help_text())

    def _help_text(self) -> str:
        web = ""
        if self._bound_addr:
            web = f"\nWeb 控制台：http://{self._bound_addr[0]}:{self._bound_addr[1]}"
        return "可用指令：/pc wol（唤醒）/pc off（关机）/pc status（状态）/pc nas（测试NAS）/pc config（查看配置）" + web

    # ========== 配置 / 数据目录 ==========

    def _data_path(self, *parts: str) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir.joinpath(*parts)

    def _local_config_path(self) -> Path:
        return self._data_path("config.json")

    def _log_path(self) -> Path:
        log_dir = self._data_path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "pc_control.log"

    def _migrate_legacy_files(self):
        legacy_config = PLUGIN_DIR / "config.json"
        if legacy_config.exists() and not self._local_config_path().exists():
            try:
                legacy_config.replace(self._local_config_path())
                logger.info("[PC-Control] 已迁移旧 config.json 到 plugin_data")
            except Exception as e:
                logger.warning(f"[PC-Control] 迁移旧 config.json 失败: {e}")
        legacy_logs = PLUGIN_DIR / "logs"
        new_logs = self._data_path("logs")
        if legacy_logs.exists() and not new_logs.exists():
            try:
                legacy_logs.replace(new_logs)
                logger.info("[PC-Control] 已迁移旧 logs 目录到 plugin_data")
            except Exception as e:
                logger.warning(f"[PC-Control] 迁移旧 logs 目录失败: {e}")

    def _load_local_config(self) -> dict[str, Any]:
        path = self._local_config_path()
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[PC-Control] 本地配置读取失败: {e}")
            return {}

    def _save_local_config(self, cfg: dict[str, Any]):
        existing = self._load_local_config()
        for key, val in cfg.items():
            if key in CONFIG_KEYS:
                existing[key] = val
        path = self._local_config_path()
        with path.open("w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

    def _get_all_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULTS)
        if isinstance(self.plugin_config, dict):
            for key in CONFIG_KEYS:
                val = self.plugin_config.get(key)
                if val not in (None, "", [], {}):
                    cfg[key] = val
        local = self._load_local_config()
        for key in CONFIG_KEYS:
            val = local.get(key)
            if val not in (None, "", [], {}):
                cfg[key] = val
        try:
            cfg["web_port"] = int(cfg.get("web_port") or 5800)
        except Exception:
            cfg["web_port"] = 5800
        return cfg

    def _validate(self, need_pc: bool = False, need_wol: bool = False, need_nas_target: bool = False) -> list[str]:
        cfg = self._get_all_config()
        errors = []
        for key, label in (("nas_ip", "NAS IP"), ("nas_user", "NAS 用户名"), ("nas_pass", "NAS 密码")):
            if not cfg.get(key):
                errors.append(f"缺少 {label}（{key}）")
        if need_pc:
            for key, label in (("pc_ip", "PC IP"), ("pc_user", "PC 用户名"), ("pc_pass", "PC 密码")):
                if not cfg.get(key):
                    errors.append(f"缺少 {label}（{key}）")
        if need_wol:
            for key, label in (("pc_mac", "PC MAC"), ("broadcast_ip", "广播地址")):
                if not cfg.get(key):
                    errors.append(f"缺少 {label}（{key}）")
            mac = str(cfg.get("pc_mac", "")).replace(":", "").replace("-", "").replace(" ", "")
            if mac and not re.fullmatch(r"[0-9A-Fa-f]{12}", mac):
                errors.append("pc_mac 格式不正确")
        if need_nas_target and not cfg.get("nas_target"):
            errors.append("缺少 NAS 测试 URL（nas_target）")
        return errors

    # ========== 远程操作 ==========

    def _run_on_nas(self, command: str, timeout: int = 30) -> subprocess.CompletedProcess:
        cfg = self._get_all_config()
        return subprocess.run(
            [
                "sshpass", "-p", str(cfg["nas_pass"]),
                "ssh", "-o", "StrictHostKeyChecking=no",
                f"{cfg['nas_user']}@{cfg['nas_ip']}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _run_on_pc_via_nas(self, command: str, timeout: int = 30) -> subprocess.CompletedProcess:
        cfg = self._get_all_config()
        pc_cmd = (
            f"sshpass -p {shlex.quote(str(cfg['pc_pass']))} "
            f"ssh -o StrictHostKeyChecking=no {shlex.quote(str(cfg['pc_user']))}@{shlex.quote(str(cfg['pc_ip']))} "
            f"{shlex.quote(command)}"
        )
        return self._run_on_nas(pc_cmd, timeout=timeout)

    def wol(self) -> dict[str, Any]:
        errors = self._validate(need_wol=True)
        if errors:
            return {"ok": False, "msg": "❌ 配置不完整：" + "；".join(errors)}
        cfg = self._get_all_config()
        cmd = f"wakeonlan -i {shlex.quote(str(cfg['broadcast_ip']))} {shlex.quote(str(cfg['pc_mac']))}"
        try:
            r = self._run_on_nas(cmd, timeout=10)
            ok = r.returncode == 0
            self._log("WOL", "OK" if ok else "FAIL", cfg["pc_mac"] if ok else r.stderr[:200])
            return {"ok": ok, "msg": "📡 唤醒信号已发送" if ok else f"❌ 唤醒失败: {r.stderr[:200] or r.stdout[:200]}"}
        except Exception as e:
            self._log("WOL", "FAIL", str(e))
            return {"ok": False, "msg": f"❌ 唤醒失败: {e}"}

    def shutdown_pc(self) -> dict[str, Any]:
        errors = self._validate(need_pc=True)
        if errors:
            return {"ok": False, "msg": "❌ 配置不完整：" + "；".join(errors)}
        try:
            r = self._run_on_pc_via_nas("shutdown /s /t 10", timeout=30)
            ok = r.returncode == 0
            self._log("SHUTDOWN", "OK" if ok else "FAIL", r.stderr[:200])
            return {"ok": ok, "msg": "🔌 关机指令已发送" if ok else f"❌ 关机失败: {r.stderr[:200] or r.stdout[:200]}"}
        except Exception as e:
            self._log("SHUTDOWN", "FAIL", str(e))
            return {"ok": False, "msg": f"❌ 关机失败: {e}"}

    def status(self) -> dict[str, Any]:
        errors = self._validate(need_pc=True)
        if errors:
            return {"ok": False, "msg": "❌ 配置不完整：" + "；".join(errors)}
        cfg = self._get_all_config()
        game_process = str(cfg.get("game_process") or "StarRail.exe")
        try:
            r = self._run_on_pc_via_nas(f'tasklist /fi "IMAGENAME eq {game_process}" /nh 2>&1', timeout=30)
            return {"ok": r.returncode == 0, "online": r.returncode == 0, "game": r.returncode == 0 and game_process.lower() in r.stdout.lower()}
        except Exception as e:
            return {"ok": False, "msg": f"❌ 状态检查失败: {e}"}

    def test_nas(self) -> dict[str, Any]:
        errors = self._validate(need_nas_target=True)
        if errors:
            return {"ok": False, "msg": "❌ 配置不完整：" + "；".join(errors)}
        cfg = self._get_all_config()
        cmd = f"curl -s --connect-timeout 3 {shlex.quote(str(cfg['nas_target']))} || echo TIMEOUT"
        try:
            r = self._run_on_nas(cmd, timeout=8)
            return {"ok": r.returncode == 0, "msg": r.stdout.strip()[:200] or r.stderr.strip()[:200] or "OK"}
        except Exception as e:
            return {"ok": False, "msg": f"❌ NAS 测试失败: {e}"}

    def _log(self, action: str, status: str, detail: str = ""):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._log_path().open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{action}] [{status}] {detail}\n")
        except Exception:
            pass

PAGE_INDEX = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC 远程控制</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui;background:#0f0f1a;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh}.container{width:340px;padding:20px;text-align:center}h1{font-size:22px;margin-bottom:4px;color:#8888ff}.subtitle{font-size:13px;color:#666;margin-bottom:8px}.nav{text-align:right;margin-bottom:16px}.nav a{color:#8888ff;text-decoration:none;font-size:13px}.btn{display:block;width:100%;padding:18px;margin:10px 0;border:none;border-radius:14px;font-size:17px;font-weight:600;cursor:pointer;transition:.2s}.btn:active{transform:scale(.96)}.btn-wol{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff}.btn-off{background:linear-gradient(135deg,#f093fb,#f5576c);color:#fff}.btn-status{background:#1a1a2e;color:#8888ff;border:1px solid #333}.msg{margin-top:14px;padding:12px;border-radius:10px;font-size:14px;display:none}.msg.ok{display:block;background:#1a3a2a;color:#4ade80}.msg.err{display:block;background:#3a1a1a;color:#f87171}</style></head><body>
<div class="container"><div class="nav"><a href="/config">⚙️ 设置</a></div><h1>⚡ PC 控制</h1><p class="subtitle">远程控制台</p>
<button class="btn btn-wol" onclick="call('/api/wol')">📡 唤醒电脑</button><button class="btn btn-off" onclick="call('/api/shutdown')">🔌 关机</button><button class="btn btn-status" onclick="call('/api/status')">🔄 检查状态</button><button class="btn btn-status" onclick="call('/api/nas')">📡 测试 NAS</button><div id="msg" class="msg"></div></div>
<script>function call(p){var m=document.getElementById('msg');m.className='msg';m.textContent='请求中...';fetch(p).then(r=>r.json()).then(d=>{m.className=d.ok===false?'msg err':'msg ok';m.textContent=d.msg||(d.game?'🎮 游戏运行中':'💻 电脑在线')}).catch(e=>{m.className='msg err';m.textContent='连接失败: '+e.message})}</script></body></html>"""

PAGE_CONFIG = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PC 控制 - 设置</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui;background:#0f0f1a;color:#fff;padding:20px;max-width:500px;margin:0 auto}h1{font-size:20px;color:#8888ff;margin-bottom:20px}.back{color:#8888ff;text-decoration:none;font-size:14px;display:inline-block;margin-bottom:16px}.section{margin-bottom:20px}.section h2{font-size:14px;color:#666;margin-bottom:10px;border-bottom:1px solid #222;padding-bottom:6px}.field{margin-bottom:12px}.field label{display:block;font-size:12px;color:#888;margin-bottom:4px}.field input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #333;background:#1a1a2e;color:#fff;font-size:14px;outline:none}.field input:focus{border-color:#667eea}.btn-save{width:100%;padding:16px;border:none;border-radius:12px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;font-size:16px;font-weight:600;cursor:pointer}.msg{padding:10px;border-radius:8px;font-size:13px;margin-top:10px;display:none;text-align:center}.msg.ok{display:block;background:#1a3a2a;color:#4ade80}.msg.err{display:block;background:#3a1a1a;color:#f87171}</style></head><body><a class="back" href="/">← 返回控制台</a><h1>⚙️ 配置</h1><div id="fields"></div><button class="btn-save" onclick="save()">💾 保存</button><div id="msg" class="msg"></div><script>
var FIELDS=[{section:'Web',fields:[{key:'web_host',label:'监听地址',ph:'0.0.0.0'},{key:'web_port',label:'端口',ph:'5800'},{key:'api_token',label:'API Token（可选）',ph:'',type:'password'}]},{section:'NAS 连接',fields:[{key:'nas_ip',label:'NAS 地址',ph:'100.80.116.113'},{key:'nas_user',label:'NAS 用户名',ph:'root'},{key:'nas_pass',label:'NAS 密码',ph:'',type:'password'},{key:'nas_target',label:'NAS 测试 URL',ph:'http://192.168.21.42/health'}]},{section:'PC 配置',fields:[{key:'pc_ip',label:'电脑 IP',ph:'192.168.31.206'},{key:'pc_user',label:'电脑用户名',ph:'he'},{key:'pc_pass',label:'电脑密码',ph:'',type:'password'},{key:'pc_mac',label:'MAC 地址',ph:'a0:ad:9f:14:62:63'},{key:'broadcast_ip',label:'WOL 广播地址',ph:'192.168.31.255'},{key:'game_process',label:'游戏进程名',ph:'StarRail.exe'}]}];
var data={};fetch('/api/config').then(r=>r.json()).then(d=>{data=d;render()});function render(){var html='';FIELDS.forEach(s=>{html+='<div class="section"><h2>'+s.section+'</h2>';s.fields.forEach(f=>{var v=data[f.key]||'';if(v==='******')v='';html+='<div class="field"><label>'+f.label+'</label><input id="i_'+f.key+'" value="'+String(v).replace(/"/g,'&quot;')+'" '+(f.type?'type="'+f.type+'"':'')+' placeholder="'+f.ph+'"></div>'});html+='</div>'});document.getElementById('fields').innerHTML=html}function save(){FIELDS.forEach(s=>s.fields.forEach(f=>{var v=document.getElementById('i_'+f.key).value;if(v)data[f.key]=v}));var m=document.getElementById('msg');m.className='msg';m.textContent='保存中...';fetch('/api/config',{method:'POST',body:JSON.stringify(data),headers:{'Content-Type':'application/json'}}).then(r=>r.json()).then(d=>{m.className=d.ok?'msg ok':'msg err';m.textContent=d.ok?'✅ 保存成功，重启插件后端口变更生效':(d.msg||'失败')}).catch(e=>{m.className='msg err';m.textContent='失败: '+e.message})}</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def __init__(self, plugin: PCControlPlugin, *args):
        self.plugin = plugin
        super().__init__(*args)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._html(PAGE_INDEX)
        if path == "/config":
            return self._html(PAGE_CONFIG)
        if not self._authorized():
            return self._json({"ok": False, "msg": "unauthorized"}, status=401)
        if path == "/api/config":
            return self._json(_mask_config(self.plugin._get_all_config()))
        if path == "/api/wol":
            return self._json(self.plugin.wol())
        if path == "/api/shutdown":
            return self._json(self.plugin.shutdown_pc())
        if path == "/api/status":
            return self._json(self.plugin.status())
        if path == "/api/nas":
            return self._json(self.plugin.test_nas())
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/config":
            return self.send_error(404)
        if not self._authorized():
            return self._json({"ok": False, "msg": "unauthorized"}, status=401)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                return self._json({"ok": False, "msg": "invalid body"}, status=400)
            self.plugin._save_local_config(body)
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)}, status=500)

    def _authorized(self) -> bool:
        token = str(self.plugin._get_all_config().get("api_token") or "")
        if not token:
            return True
        got = self.headers.get("X-Api-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        return got == token

    def _html(self, content: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _json(self, data: dict[str, Any], status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        return
