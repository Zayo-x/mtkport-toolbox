# 让 AI 通过 MCP 调用这套移植工具箱

`mcp_server.py` 把 `mtkport.py` 的一键移植命令包成了 **MCP 服务**（零依赖，纯 Python 标准库），
让别人（Claude Desktop / Cursor / Cline 等 AI 客户端）用 MCP 协议直接调用。

---

## 1. 它是怎么工作的

- MCP（Model Context Protocol）是 AI 客户端调用外部工具的通用协议，采用 stdio（标准输入输出）通信。
- 客户端把 `mcp_server.py` 作为子进程拉起，通过 JSON-RPC 问它「有哪些工具」「帮我跑这个命令」。
- `mcp_server.py` 收到调用后，把参数转成 `python mtkport.py <子命令> ...` 跑一遍，把结果文本回给客户端。

**不需要装任何第三方包**（只用了 json / sys / subprocess），Python 3.8+ 就能跑。

---

## 2. 客户端怎么接（三选一，照着填就行）

### Claude Desktop
编辑 `claude_desktop_config.json`（Claude → Settings → Developer → Edit Config）：

```json
{
  "mcpServers": {
    "mtkport": {
      "command": "python",
      "args": ["D:\\deepseek-harness\\安卓折腾\\移植工具箱\\mcp_server.py"]
    }
  }
}
```

### Cursor
在项目根目录建 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "mtkport": {
      "command": "python",
      "args": ["D:\\deepseek-harness\\安卓折腾\\移植工具箱\\mcp_server.py"]
    }
  }
}
```

### Cline（VS Code 插件）
Settings → MCP Servers → 添加：

```json
{
  "mcpServers": {
    "mtkport": {
      "command": "python",
      "args": ["D:\\deepseek-harness\\安卓折腾\\移植工具箱\\mcp_server.py"]
    }
  }
}
```

> 换台电脑就把 `args` 里的路径换成那台机器上工具箱的实际路径；`command` 用那台机器的 `python`。

---

## 3. 接好之后 AI 能用哪些工具

| MCP 工具 | 对应命令 | 干什么 |
|---|---|---|
| **`deploy`** ★ | doctor + auto | **一条命令部署（推荐入口）**：先体检环境，再一键移植 |
| `devices` | （内置） | 列出已做过的机型（免扫描直接锁定） |
| `device-info` | （内置） | 读某台已知设备的完整档案（分区表/芯片/内核/显示） |
| `doctor` | doctor | 体检环境（缺什么工具/驱动直接说） |
| `scan` | scan | 扫设备档案（芯片/内核/安卓版本/分区表） |
| `roms` | roms | 从 kc.fuquan.moe 检索并评分移植包 |
| `fetch` | fetch | 下载移植包 |
| `backup` | backup | 备份设备关键分区（双端 md5） |
| `auto` | auto | **一键移植**（扫描→选包→下载→备份→移植→出刷机目录） |
| `flash-check` | flash-check | 刷机前自检 |
| `flash-mtk` | flash-mtk | mtkclient 直写（BROM 模式） |
| `scatter-gen` | scatter-gen | 反推 scatter 分区表 |
| `run` | 任意 | 兜底：跑 patch / unpack / info 等其它子命令 |

> **一句话用法**：别人把工具箱丢给 AI、接上 MCP 后，AI 只需调 **`deploy`** 一条命令就完成整个部署（先 doctor 体检环境+驱动+设备，再 auto 一键移植）。其余工具是给需要分步控制、排查、或只刷机不移植时用的。

---

## 4. 本地怎么先验证它跑得通

先模拟一个 MCP 客户端，手工发几行 JSON-RPC 测试（Windows 下直接把下面三行粘进命令行）：

```powershell
'{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python mcp_server.py
```

或者更完整的握手测试（发多行）：

```powershell
@('{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}',
'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"doctor","arguments":{}}}') | python mcp_server.py
```

看到 `"result":{"tools":[...]}` 和 doctor 的输出就说明通了。

---

## 5. 注意事项

- **命令跑的是本机 Python 和本机 adb**：AI 通过 MCP 调 `scan`/`backup`/`auto` 时，需要这台电脑已经连上设备（adb 或 WiFi adb）。
- **`flash-mtk` 需要 UsbDk 驱动 + 设备进 BROM**：这一步有物理/提权门槛，AI 无法代办。装 UsbDk 的一键命令（AI 把这条丢给用户，让人点 UAC「是」即可，在工具箱根目录跑）：

  ```powershell
  Start-Process msiexec -ArgumentList '/i "工具\UsbDk_1.0.22_x64.msi" /qn /norestart' -Verb RunAs
  ```

  装完 `sc query UsbDk` 应显示 `RUNNING`。
- **`auto`/`backup` 可能跑几分钟到几十分钟**（要下包、备份 800MB system），客户端要留够超时时间。
- `run` 工具的参数是空格分隔字符串，路径里**别带空格**（这套工具箱的路径本身没有空格，中文没问题）。

---

## 6. 直接复制给 AI 的提示词

接好 MCP 后，把下面这段发给 AI，它就会调 `deploy`（内部自动 doctor + auto）完成部署：

> 我已经把「MTK 移植工具箱」通过 MCP 接好了，服务名叫 mtkport，里面有 deploy、scan、auto、backup、flash-mtk 等工具。
>
> 请帮我把这台老 MTK 安卓设备移植到新版安卓系统：
> 1. 直接调用 mtkport 的 deploy 工具（一条命令 = 环境体检 + 一键移植）。
> 2. 跑完把结果告诉我：移植了哪些分区、刷机目录在哪、下一步怎么刷。
>
> 如果 deploy 提示缺工具或缺驱动，照它给的清单告诉我要装什么、怎么装。

想控制得更细，就把上面第 1 句换成下面某条：

| 想要的效果 | 发给 AI 的话 |
|---|---|
| 只体检环境 | `先用 mtkport 的 doctor 工具体检一下` |
| 只扫设备 | `先用 mtkport 的 scan 工具扫一下这台设备` |
| 指定移植包（跳过下载） | `用 auto 工具，--donor 传我给的这个包路径` |
| 自动刷入 | `用 deploy 跑完后，再用 flash-mtk 刷进去`（需真人装 UsbDk + 设备进 BROM，AI 会提示） |
