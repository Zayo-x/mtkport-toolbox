# MTK 移植工具箱（含 MCP 服务器）

把老 MTK 安卓设备（MT6572 / MT6582 等 KK 时代）移植到较新 Android 的一键工具链，并包了一层 **MCP 服务器**，让别人（Claude / Cursor / Cline 等 AI 客户端）直接用自然语言调用。

## 两条入口

| 入口 | 文件 | 用途 |
|---|---|---|
| MCP 服务器 | `mcp_server.py` | 给 AI 客户端用：接上后调 `deploy` 一条命令即可完成「环境体检 + 一键移植」 |
| 命令行 | `mtkport.py` | 给人手动用：doctor / scan / roms / fetch / backup / auto / flash-mtk 等 |

## 快速开始

- **MCP 方式（推荐，一条命令部署）**：见 [`MCP接入说明.md`](MCP接入说明.md) —— 在客户端加一条 mcpServers 配置指向 `mcp_server.py`，然后让 AI 调 `deploy`。
- **命令行方式**：见 [`AI部署说明.md`](AI部署说明.md) —— `python mtkport.py doctor` 先体检。

## 直接复制给 AI 的话

接好 MCP 后（见 [`MCP接入说明.md`](MCP接入说明.md)），把下面这段发给你的 AI 即可开始部署：

> 我已经把「MTK 移植工具箱」通过 MCP 接好了，服务名叫 mtkport，里面有 deploy、scan、auto、backup、flash-mtk 等工具。
>
> 请帮我把这台老 MTK 安卓设备移植到新版安卓系统：
> 1. 直接调用 mtkport 的 deploy 工具（一条命令 = 环境体检 + 一键移植）。
> 2. 跑完把结果告诉我：移植了哪些分区、刷机目录在哪、下一步怎么刷。
>
> 如果 deploy 提示缺工具或缺驱动，照它给的清单告诉我要装什么、怎么装。

## 目录

- `mtkport/` —— 移植引擎（扫描 / 选包 / 下载 / 备份 / 移植 / 刷机 / 反推分区表）
- `mcp_server.py` —— MCP 服务器（零依赖，纯标准库）
- `configs/` —— 机型预设（移植规则）
- `devices/` —— 已知设备档案（已做过的机型）
- `bin/` —— 打包 / 解包 / 刷机小工具（7z / make_ext4fs / magiskboot / adb 等）
- `工具/` —— UsbDk 驱动安装包 + 一键装脚本
- `自测-自动化.py` / `自测-端到端.py` —— 自测

## 注意

- `vendor/mtkclient`（约 462MB 刷机后端，开源 github.com/bkerler/mtkclient）**不在本仓库**，需单独下载放到 `vendor/mtkclient`（含 `runtime/python.exe`），否则 `flash-mtk` 不可用。
- 首次跑 `doctor` 会列出缺什么，照着补齐即可。
