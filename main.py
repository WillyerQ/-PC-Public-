"""
PC 远程控制插件
- Web 控制台：WOL 唤醒 / 关机 / 状态检查
- 米家自动化 API 支持
- WebUI 配置管理
"""
import asyncio
import json
import os
import subprocess
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register("pc_control", "AstrBot", "PC 远程控制", "1.0.0")
class PCControlPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._server = None
        self._task = None

    async def initialize(self):
        cfg = self._get_all_config()
        port = int(cfg.get("web_port", 5800))
        self._server = HTTPServer(("0.0.0.0", port), lambda *a: Handler(self, *a))
        self._task = asyncio.get_event_loop().run_in_executor(None, self._server.serve_forever)
        logger.info(f"[PC-Control] Web 控制台已启动: http://0.0.0.0:{port}")

    def _get_all_config(self):
        keys = ["nas_ip", "nas_user", "nas_pass", "pc_ip", "pc_user", "pc_pass",
                "pc_mac", "broadcast_ip", "nas_target"]
        cfg = {}
        for k in keys:
            cfg[k] = self.context.get_config(k) or ""
        cfg["web_port"] = int(self.context.get_config("web_port") or 5800)
        return cfg

    def _ssh_run(self, cmd):
        cfg = self._get_all_config()
        full = f'sshpass -p "{cfg["pc_pass"]}" ssh -o StrictHostKeyChecking=no {cfg["pc_user"]}@{cfg["pc_ip"]} {cmd}'
        return subprocess.run(
            ["sshpass", "-p", cfg["nas_pass"], "ssh", "-o", "StrictHostKeyChecking=no",
             f"{cfg['nas_user']}@{cfg['nas_ip']}", full],
            capture_output=True, text=True, timeout=30
        )

    @filter.command("pc")
    async def pc_cmd(self, event: AstrMessageEvent):
        """PC 远程控制指令"""
        msg = event.message_str.strip()
        args = msg.split()
        if len(args) < 2:
            yield event.plain_result("可用指令：/pc wol（唤醒）/pc off（关机）/pc status（状态）")
            return
        action = args[1]
        if action == "wol":
            cfg = self._get_all_config()
            r = subprocess.run(
                ["sshpass", "-p", cfg["nas_pass"], "ssh", "-o", "StrictHostKeyChecking=no",
                 f"{cfg['nas_user']}@{cfg['nas_ip']}",
                 f"wakeonlan -i {cfg['broadcast_ip']} {cfg['pc_mac']}"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                yield event.plain_result("📡 唤醒信号已发送")
            else:
                yield event.plain_result(f"❌ 唤醒失败: {r.stderr[:100]}")
        elif action == "off":
            r = self._ssh_run("shutdown /s /t 10")
            if r.returncode == 0:
                yield event.plain_result("🔌 关机指令已发送")
            else:
                yield event.plain_result(f"❌ 关机失败: {r.stderr[:100]}")
        elif action == "status":
            r = self._ssh_run("tasklist /fi \"IMAGENAME eq StarRail.exe\" /nh 2>&1")
            if r.returncode == 0 and "StarRail" in r.stdout:
                yield event.plain_result("💻 电脑在线 | 🎮 游戏运行中")
            else:
                yield event.plain_result("💻 电脑在线 | 游戏未运行")
        else:
            yield event.plain_result("未知指令。可用：/pc wol /pc off /pc status")

    async def terminate(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        logger.info("[PC-Control] 服务已停止")

    def _log(self, action, status, detail=""):
        """记录操作日志"""
        try:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "pc_control.log")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{action}] [{status}] {detail}\n")
        except Exception:
            pass


PAGE_INDEX = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC 远程控制</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui;background:#0f0f1a;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh}
.container{width:340px;padding:20px;text-align:center}
h1{font-size:22px;margin-bottom:4px;color:#8888ff}
.subtitle{font-size:13px;color:#666;margin-bottom:8px}
.nav{text-align:right;margin-bottom:16px}
.nav a{color:#8888ff;text-decoration:none;font-size:13px}
.btn{display:block;width:100%;padding:18px;margin:10px 0;border:none;border-radius:14px;font-size:17px;font-weight:600;cursor:pointer;transition:.2s}
.btn:active{transform:scale(.96)}
.btn-wol{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff}
.btn-off{background:linear-gradient(135deg,#f093fb,#f5576c);color:#fff}
.btn-status{background:#1a1a2e;color:#8888ff;border:1px solid #333}
.msg{margin-top:14px;padding:12px;border-radius:10px;font-size:14px;display:none}
.msg.ok{display:block;background:#1a3a2a;color:#4ade80}
.msg.err{display:block;background:#3a1a1a;color:#f87171}
</style></head><body>
<div class="container">
<div class="nav"><a href="/config">⚙️ 设置</a></div>
<h1>⚡ PC 控制</h1>
<p class="subtitle">远程控制台</p>
<button class="btn btn-wol" onclick="call('/api/wol')">📡 唤醒电脑</button>
<button class="btn btn-off" onclick="call('/api/shutdown')">🔌 关机</button>
<button class="btn btn-status" onclick="call('/api/status')">🔄 检查状态</button>
<button class="btn btn-status" onclick="call('/api/nas')">📡 测试 NAS</button>
<div id="msg" class="msg"></div>
</div>
<script>
function call(p){var m=document.getElementById('msg');m.className='msg';m.textContent='请求中...'
fetch(p).then(function(r){return r.json()}).then(function(d){
m.className=d.ok===false?'msg err':'msg.ok'
m.textContent=d.msg||(d.game?'🎮 游戏运行中':'💻 电脑在线')
}).catch(function(e){m.className='msg err';m.textContent='连接失败: '+e.message})}
</script></body></html>"""

PAGE_CONFIG = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC 控制 - 设置</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui;background:#0f0f1a;color:#fff;padding:20px;max-width:500px;margin:0 auto}
h1{font-size:20px;color:#8888ff;margin-bottom:20px}
.back{color:#8888ff;text-decoration:none;font-size:14px;display:inline-block;margin-bottom:16px}
.section{margin-bottom:20px}
.section h2{font-size:14px;color:#666;margin-bottom:10px;border-bottom:1px solid #222;padding-bottom:6px}
.field{margin-bottom:12px}
.field label{display:block;font-size:12px;color:#888;margin-bottom:4px}
.field input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #333;background:#1a1a2e;color:#fff;font-size:14px;outline:none}
.field input:focus{border-color:#667eea}
.btn-save{width:100%;padding:16px;border:none;border-radius:12px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;font-size:16px;font-weight:600;cursor:pointer}
.msg{padding:10px;border-radius:8px;font-size:13px;margin-top:10px;display:none;text-align:center}
.msg.ok{display:block;background:#1a3a2a;color:#4ade80}
.msg.err{display:block;background:#3a1a1a;color:#f87171}
</style></head><body>
<a class="back" href="/">← 返回控制台</a>
<h1>⚙️ 配置</h1>
<div id="fields"></div>
<button class="btn-save" onclick="save()">💾 保存</button>
<div id="msg" class="msg"></div>
<script>
var FIELDS=[
{section:"NAS 连接",fields:[
{key:"nas_ip",label:"NAS 地址",ph:"100.80.116.113"},
{key:"nas_user",label:"NAS 用户名",ph:"root"},
{key:"nas_pass",label:"NAS 密码",ph:"",type:"password"},
{key:"nas_target",label:"NAS 测试 URL",ph:"http://192.168.21.42/health"}]},
{section:"PC 配置",fields:[
{key:"pc_ip",label:"电脑 IP",ph:"192.168.31.206"},
{key:"pc_user",label:"电脑用户名",ph:"he"},
{key:"pc_pass",label:"电脑密码",ph:"",type:"password"},
{key:"pc_mac",label:"MAC 地址",ph:"a0:ad:9f:14:62:63"},
{key:"broadcast_ip",label:"WOL 广播地址",ph:"192.168.31.255"}]}];
var data={};
fetch('/api/config').then(function(r){return r.json()}).then(function(d){data=d;render()});
function render(){
var html='';FIELDS.forEach(function(s){
html+='<div class=\"section\"><h2>'+s.section+'</h2>';
s.fields.forEach(function(f){
var v=data[f.key]||'';
html+='<div class=\"field\"><label>'+f.label+'</label><input id=\"i_'+f.key+'\" value=\"'+v.replace(/\"/g,'&quot;')+'\" '+(f.type?'type=\"'+f.type+'\"':'')+' placeholder=\"'+f.ph+'\"></div>'});
html+='</div>'});document.getElementById('fields').innerHTML=html}
function save(){
FIELDS.forEach(function(s){s.fields.forEach(function(f){data[f.key]=document.getElementById('i_'+f.key).value})});
var m=document.getElementById('msg');m.className='msg';m.textContent='保存中...';
fetch('/api/config',{method:'POST',body:JSON.stringify(data),headers:{'Content-Type':'application/json'}}).then(function(r){return r.json()}).then(function(d){m.className='msg ok';m.textContent='✅ 保存成功！'}).catch(function(e){m.className='msg err';m.textContent='失败: '+e.message})}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def __init__(self, plugin, *args):
        self.plugin = plugin
        super().__init__(*args)

    def do_GET(self):
        p = self.path
        if p == "/": return self._html(PAGE_INDEX)
        if p == "/config": return self._html(PAGE_CONFIG)
        if p == "/api/config": return self._json(self.plugin._get_all_config())
        if p == "/api/wol": return self._api(self._wol())
        if p == "/api/shutdown": return self._api(self._shutdown())
        if p == "/api/status": return self._api(self._status())
        if p == "/api/nas": return self._api(self._nas())
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            for k, v in body.items():
                self.plugin.context.get_config().__setitem__(k, v)
            return self._json({"ok": True})
        self.send_error(404)

    def _wol(self):
        cfg = self.plugin._get_all_config()
        r = subprocess.run(
            ["sshpass", "-p", cfg["nas_pass"], "ssh", "-o", "StrictHostKeyChecking=no",
             f"{cfg['nas_user']}@{cfg['nas_ip']}",
             f"wakeonlan -i {cfg['broadcast_ip']} {cfg['pc_mac']}"],
            capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
        self.plugin._log("WOL", "OK" if ok else "FAIL", cfg["pc_mac"])
        return {"ok": ok, "msg": "唤醒信号已发送" if ok else f"失败"}

    def _shutdown(self):
        r = self.plugin._ssh_run("shutdown /s /t 10")
        ok = r.returncode == 0
        self.plugin._log("SHUTDOWN", "OK" if ok else "FAIL", "")
        return {"ok": ok, "msg": "关机指令已发送" if ok else f"失败"}

    def _status(self):
        r = self.plugin._ssh_run("tasklist /fi \"IMAGENAME eq StarRail.exe\" /nh 2>&1")
        return {"online": True, "game": r.returncode == 0 and "StarRail" in r.stdout}

    def _nas(self):
        cfg = self.plugin._get_all_config()
        r = subprocess.run(
            ["sshpass", "-p", cfg["nas_pass"], "ssh", "-o", "StrictHostKeyChecking=no",
             f"{cfg['nas_user']}@{cfg['nas_ip']}",
             f"curl -s --connect-timeout 3 {cfg['nas_target']} || echo TIMEOUT"],
            capture_output=True, text=True, timeout=8)
        return {"ok": r.returncode == 0, "msg": r.stdout.strip()[:200]}

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, fmt, *args):
        pass
