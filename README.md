# PC 远程控制插件

通过 AstrBot 指令、Web 控制台或米家自动化 HTTP API 远程管理你的 PC。

## 功能

| 功能 | 说明 |
|------|------|
| 📡 WOL 网络唤醒 | 通过 NAS/跳板机发送魔术包唤醒电脑 |
| 🔌 远程关机 | 通过 NAS 转发 SSH 到 Windows 发送关机指令 |
| 🔄 状态检查 | 查询电脑在线状态和指定进程是否运行 |
| 📡 NAS 测试 | 测试 NAS 到指定 URL 的连通性 |
| ⚙️ Web 控制台 | 浏览器上操作和修改本地覆盖配置 |

## 安装

1. 将插件目录放入 `AstrBot/data/plugins/`
2. 确保 AstrBot 所在环境可用 `sshpass`、`ssh`
3. NAS/跳板机需可 SSH 登录，且安装 `wakeonlan`
4. 目标 Windows 电脑需开启 OpenSSH Server
5. AstrBot WebUI → 插件管理 → 重载插件
6. 在插件配置页填写 NAS 和 PC 的连接信息

## 配置

| 配置项 | 说明 |
|--------|------|
| web_host | Web 控制台监听地址，默认 `0.0.0.0` |
| web_port | Web 控制台端口，默认 `5800` |
| api_token | HTTP API Token，可选；填写后 `/api/*` 需要鉴权 |
| nas_ip | NAS / 跳板机 IP |
| nas_user | NAS SSH 用户名 |
| nas_pass | NAS SSH 密码 |
| pc_ip | 目标电脑内网 IP |
| pc_user | Windows 登录用户名 |
| pc_pass | Windows 登录密码 |
| pc_mac | 目标电脑 MAC 地址 |
| broadcast_ip | WOL 广播地址 |
| game_process | 状态检查使用的进程名，默认 `StarRail.exe` |
| nas_target | NAS 连通性测试 URL |

Web 控制台保存的本地覆盖配置会写入：

```text
AstrBot/data/plugin_data/pc_control/config.json
```

日志写入：

```text
AstrBot/data/plugin_data/pc_control/logs/pc_control.log
```

## 使用

### Web 控制台

浏览器打开：

```text
http://你的服务器IP:5800
```

### HTTP API / 米家自动化

```text
唤醒: http://你的服务器IP:5800/api/wol
关机: http://你的服务器IP:5800/api/shutdown
状态: http://你的服务器IP:5800/api/status
NAS测试: http://你的服务器IP:5800/api/nas
```

方法选 **GET**。

如果配置了 `api_token`，请求需带：

```text
X-Api-Token: 你的token
```

或：

```text
Authorization: Bearer 你的token
```

### QQ / IM 指令

```text
/pc wol     唤醒电脑
/pc off     关机
/pc status  检查状态
/pc nas     测试 NAS
/pc config  查看脱敏配置
```

## AstrBot 规范适配

- 插件元数据在 `metadata.yaml`
- WebUI 配置 schema 在 `_conf_schema.json`
- 运行数据不写入插件源码目录，统一存储到 `data/plugin_data/pc_control/`
- 旧版本插件目录下的 `config.json` 和 `logs/` 会在启动时尝试迁移
- 使用 AstrBot `logger` 输出日志，文件日志仅保存操作审计

## 文件结构

```text
astrbot_plugin_pc_control/
├── metadata.yaml       # 插件元数据
├── _conf_schema.json   # WebUI 配置模式
├── main.py             # 主逻辑（含 Web 控制台）
└── README.md           # 本文件
```
