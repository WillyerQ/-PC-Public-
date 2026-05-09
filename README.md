# PC 远程控制插件

通过 Web 控制台或米家自动化远程管理你的 PC。

## 功能

| 功能 | 说明 |
|------|------|
| 📡 WOL 网络唤醒 | 通过 NAS 发送魔术包唤醒电脑 |
| 🔌 远程关机 | SSH 发送关机指令 |
| 🔄 状态检查 | 查询电脑在线状态和游戏是否运行 |
| 📡 NAS 测试 | 测试 NAS 连通性 |
| ⚙️ WebUI 配置 | 浏览器上修改 NAS/PC 连接信息 |

## 安装

1. 将 `astrbot_plugin_pc_control` 放入 `AstrBot/data/plugins/`
2. WebUI → 插件管理 → 重载插件
3. 在插件配置页填写 NAS 和 PC 的连接信息

## 配置

| 配置项 | 说明 |
|--------|------|
| nas_ip | NAS / 跳板机 IP（用于 SSH 转发 WOL 和关机指令） |
| nas_user | NAS SSH 用户名 |
| nas_pass | NAS SSH 密码 |
| pc_ip | 目标电脑内网 IP |
| pc_user | Windows 登录用户名 |
| pc_pass | Windows 登录密码 |
| pc_mac | 目标电脑 MAC 地址 |
| broadcast_ip | WOL 广播地址（根据局域网网段填写） |
| nas_target | NAS 连通性测试 URL |
| web_port | Web 控制台端口（默认 5800） |

## 使用

### Web 控制台

浏览器打开 `http://你的服务器IP:5800`

### 米家自动化

```
唤醒: http://你的服务器IP:5800/api/wol
关机: http://你的服务器IP:5800/api/shutdown
```

方法选 **GET**。

### QQ 指令

```
/pc wol    唤醒电脑
/pc off    关机
/pc status 检查状态
```

## 文件结构

```
astrbot_plugin_pc_control/
├── metadata.yaml       # 插件元数据
├── _conf_schema.json   # WebUI 配置模式
├── main.py             # 主逻辑（含 Web 控制台）
├── README.md           # 本文件
└── logs/               # 运行日志
    └── pc_control.log
```
