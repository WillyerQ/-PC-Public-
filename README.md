# PC 远程控制 / 米家自动化桥接插件

这是一个按 AstrBot Star 插件规范重写的 PC 远程控制插件，可通过：

- AstrBot 指令：`/pc wol`、`/pc off`、`/pc status`、`/pc nas`、`/pc config`
- Web 控制台：浏览器操作
- HTTP API：给米家/小米 IoT 自动化或其它 Webhook 调用

实现远程 WOL 唤醒、Windows 关机、进程状态检查和 NAS 连通性测试。

## 安装

1. 将本目录放到 `AstrBot/data/plugins/pc_control/`。
2. 确保 AstrBot 所在机器能访问 NAS/跳板机。
3. AstrBot 运行环境安装 `sshpass`、`ssh`。
4. NAS/跳板机安装 `wakeonlan` 和 `curl`，并允许 SSH 登录。
5. Windows 电脑开启 OpenSSH Server，并允许 NAS 访问。
6. 在 AstrBot 插件管理页重载插件并填写配置。

## 关键配置

| 配置项 | 说明 |
|---|---|
| `web_host` / `web_port` | HTTP/Web 控制台监听地址和端口，默认 `0.0.0.0:5800` |
| `api_token` | HTTP API 鉴权 Token，强烈建议填写 |
| `allow_unsafe_without_token` | 未配置 Token 时是否允许控制请求，仅建议内网临时测试开启 |
| `nas_ip` / `nas_user` / `nas_pass` / `nas_ssh_port` | NAS/跳板机 SSH 信息 |
| `pc_ip` / `pc_user` / `pc_pass` / `pc_ssh_port` | Windows SSH 信息 |
| `pc_mac` / `broadcast_ip` | WOL 唤醒需要的 MAC 与广播地址 |
| `game_process` | 状态检查匹配的进程名 |
| `shutdown_delay` | 关机延迟秒数 |

插件本地覆盖配置写入：

```text
AstrBot/data/plugin_data/pc_control/config.json
```

操作审计日志写入：

```text
AstrBot/data/plugin_data/pc_control/logs/pc_control.log
```

## HTTP API

所有接口支持 `GET`；`/api/config` 保存配置使用 `POST JSON`。

| 接口 | 别名 | 说明 |
|---|---|---|
| `/health` | - | 健康检查，无需鉴权 |
| `/api/wol` | `/mi/wol`、`/mi/power_on` | 唤醒电脑 |
| `/api/shutdown` | `/mi/shutdown`、`/mi/power_off` | 关闭电脑 |
| `/api/status` | `/mi/status` | 检查电脑/目标进程状态 |
| `/api/nas` | `/mi/nas` | 测试 NAS 到目标 URL 的连通性 |
| `/api/config` | `/mi/config` | 读取/保存脱敏配置 |

鉴权方式任选一种：

```http
X-Api-Token: 你的token
Authorization: Bearer 你的token
```

或在 URL 后追加：

```text
?token=你的token
```

## 米家 / 小米 IoT 自动化建议

在小米 IoT 平台按产品工作流完成产品创建、设备调试、联调和发布后，可将自动化动作配置为请求本插件的 HTTP URL。示例：

```text
开机：http://你的AstrBot服务器:5800/mi/power_on?token=你的token
关机：http://你的AstrBot服务器:5800/mi/power_off?token=你的token
状态：http://你的AstrBot服务器:5800/mi/status?token=你的token
```

如果平台支持自定义 Header，优先使用 `X-Api-Token`，避免 Token 出现在 URL 日志中。

> 安全提示：不要把控制端口直接暴露到公网。建议使用内网、VPN、反向代理鉴权或防火墙白名单。

## AstrBot 指令

```text
/pc wol      唤醒电脑
/pc off      关闭电脑
/pc status   检查在线与目标进程
/pc nas      测试 NAS 连通性
/pc config   查看脱敏配置
```

## 相比旧版的重写点

- 使用 `@register`、`@filter.command_group` 等 AstrBot Star 插件风格组织代码。
- 配置 schema 独立在 `_conf_schema.json`。
- 运行数据不写插件源码目录，统一放入 `data/plugin_data/pc_control/`。
- HTTP API 同时提供 `/api/*` 与面向米家自动化更直观的 `/mi/*` 别名。
- 默认要求 Token；避免误暴露控制接口。
- 增加 SSH 端口、关机延迟、CORS、健康检查与更清晰的 JSON 返回。
