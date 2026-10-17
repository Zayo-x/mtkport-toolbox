# -*- coding: utf-8 -*-
"""MTK 移植工具箱 —— MCP 服务器（零依赖，纯 Python 标准库）。

作用：让别人（Claude / Cursor / Cline 等 AI 客户端）通过 MCP 协议调用 mtkport.py。
不依赖任何第三方包，只用 json / sys / subprocess，Python 3.8+ 即可跑。

运行方式（stdio，由 MCP 客户端作为子进程拉起）：
    python mcp_server.py
"""
import json
import os
import subprocess
import sys

# 强制 stdio 用 UTF-8：Windows 下 Python 管道默认是 GBK，而 MCP 客户端按 UTF-8 读，必须对齐
for _s in (sys.stdout, sys.stdin, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

TOOLBOX = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(TOOLBOX, "mtkport.py")
SERVER_NAME = "mtkport"
SERVER_VERSION = "1.0"
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 3600  # 秒，备份/刷机可能很慢

# 已知设备档案（本项目已做过的机型，AI 可以免扫描直接锁定）
DEVICES_DIR = os.path.join(TOOLBOX, "devices")
KNOWN_DEVICES = {
    "p100": {
        "name": "博阅 P100SC-HELOWIN",
        "model": "P100SC-HELOWIN",
        "device": "A102",
        "chipset": "mt6572",
        "kernel": "3.4.67",
        "android": "7.1.2（LineageOS 14.1，原厂 4.4.2）",
        "profile": "configs/p100-lineage14.json",
        "archive": "devices/p100-helowin.json",
    },
}

# 工具定义：name 与 mtkport.py 子命令同名；params 描述参数。
# kind: positional=位置参数 / value=带值旗标 / flag=布尔开关
TOOLS = [
    {"name": "devices", "description": "列出已知的移植设备档案（本项目已做过的机型，免扫描直接锁定）", "params": []},

    {"name": "device-info", "description": "读取一个已知设备的完整档案（分区表/内核/芯片/属性/显示）。key 用 devices 工具查",
     "params": [
        {"kind": "positional", "name": "key", "type": "string", "required": True, "help": "设备代号，如 p100"},
    ]},

    {"name": "doctor", "description": "体检：检查这台电脑和设备够不够跑移植（缺什么工具/驱动直接说）", "params": []},

    {"name": "scan", "description": "扫描连着 adb 的设备，生成设备档案（芯片/内核/安卓版本/分辨率/分区表）",
     "params": [
        {"kind": "value", "flag": "--serial", "name": "serial", "type": "string", "help": "设备序列号（不给自动挑）"},
        {"kind": "value", "flag": "--adb-port", "name": "adb_port", "type": "integer", "default": 5037, "help": "adb 服务端口"},
        {"kind": "value", "flag": "--save", "name": "save", "type": "string", "help": "把档案存成 json 路径"},
    ]},

    {"name": "roms", "description": "从 kc.fuquan.moe 找合适的移植包并打分排序",
     "params": [
        {"kind": "value", "flag": "--profile", "name": "profile", "type": "string", "help": "已存的设备档案 json（不给就现场扫描）"},
        {"kind": "value", "flag": "--serial", "name": "serial", "type": "string"},
        {"kind": "value", "flag": "--android", "name": "android", "type": "string", "help": "只要这个安卓版本，如 7.1"},
        {"kind": "value", "flag": "--platform", "name": "platform", "type": "string", "help": "强制平台目录"},
        {"kind": "flag", "flag": "--any-kernel", "name": "any_kernel", "type": "boolean", "help": "没有同内核就就近挑（兜底）"},
        {"kind": "value", "flag": "--limit", "name": "limit", "type": "integer", "default": 15},
        {"kind": "value", "flag": "--json", "name": "json_out", "type": "string", "help": "候选存成 json 路径"},
    ]},

    {"name": "fetch", "description": "下载移植包（不给 path 就自动选第一名）",
     "params": [
        {"kind": "value", "flag": "--path", "name": "path", "type": "string", "help": "站内路径，如 /5.移植包大全/.../x.zip"},
        {"kind": "value", "flag": "--profile", "name": "profile", "type": "string"},
        {"kind": "value", "flag": "--serial", "name": "serial", "type": "string"},
        {"kind": "value", "flag": "--android", "name": "android", "type": "string"},
        {"kind": "value", "flag": "--platform", "name": "platform", "type": "string"},
        {"kind": "flag", "flag": "--any-kernel", "name": "any_kernel", "type": "boolean"},
        {"kind": "value", "flag": "--out", "name": "out", "type": "string", "help": "输出目录"},
    ]},

    {"name": "backup", "description": "把设备上的分区原样备份到本地（带双端 md5 校验）",
     "params": [
        {"kind": "value", "flag": "--out", "name": "out", "type": "string", "required": True, "help": "备份目录"},
        {"kind": "value", "flag": "--parts", "name": "parts", "type": "string", "help": "只备份这些分区，逗号分隔"},
        {"kind": "flag", "flag": "--no-system", "name": "no_system", "type": "boolean", "help": "不要 system（省 800MB）"},
        {"kind": "flag", "flag": "--with-userdata", "name": "with_userdata", "type": "boolean", "help": "连 userdata/fat 一起备（很大）"},
        {"kind": "flag", "flag": "--no-verify", "name": "no_verify", "type": "boolean", "help": "跳过一次设备端 md5 复核（快）"},
        {"kind": "value", "flag": "--serial", "name": "serial", "type": "string"},
    ]},

    {"name": "auto", "description": "一键移植：扫描→选包→下载→备份→移植→出刷机目录（主命令）",
     "params": [
        {"kind": "value", "flag": "--work", "name": "work", "type": "string", "help": "工作目录（默认 工具箱\\_工作区\\auto）"},
        {"kind": "value", "flag": "--profile", "name": "profile", "type": "string", "help": "已存的设备档案"},
        {"kind": "value", "flag": "--serial", "name": "serial", "type": "string"},
        {"kind": "value", "flag": "--donor", "name": "donor", "type": "string", "help": "手动指定移植包（给了就不下载）"},
        {"kind": "value", "flag": "--android", "name": "android", "type": "string"},
        {"kind": "value", "flag": "--stock-system", "name": "stock_system", "type": "string", "help": "原厂 system.img"},
        {"kind": "value", "flag": "--scatter", "name": "scatter", "type": "string", "help": "原厂 scatter（给了才生成刷机目录）"},
        {"kind": "value", "flag": "--only", "name": "only", "type": "string", "default": "BOOTIMG,ANDROID"},
        {"kind": "value", "flag": "--flash", "name": "flash", "type": "string", "help": "mtk 或 spft（自动刷入）"},
        {"kind": "flag", "flag": "--skip-backup", "name": "skip_backup", "type": "boolean"},
        {"kind": "flag", "flag": "--skip-fetch", "name": "skip_fetch", "type": "boolean"},
    ]},

    {"name": "flash-check", "description": "刷机前自检：镜像有没有超分区、要刷哪几个分区",
     "params": [
        {"kind": "positional", "name": "directory", "type": "string", "required": True, "help": "刷机目录"},
    ]},

    {"name": "flash-mtk", "description": "用 mtkclient 直写（BROM 模式，可无人值守；需先装 UsbDk 驱动）",
     "params": [
        {"kind": "positional", "name": "directory", "type": "string", "required": True, "help": "刷机目录"},
        {"kind": "value", "flag": "--mtk", "name": "mtk", "type": "string", "help": "mtkclient 根目录（含 runtime\\python.exe）"},
        {"kind": "flag", "flag": "--no-reset", "name": "no_reset", "type": "boolean", "help": "写完不自动重启"},
        {"kind": "flag", "flag": "--now", "name": "now", "type": "boolean", "help": "设备已在 BROM 里，别等提示"},
        {"kind": "flag", "flag": "--no-wait", "name": "no_wait", "type": "boolean", "help": "写完不等开机验证"},
        {"kind": "value", "flag": "--serial", "name": "serial", "type": "string"},
        {"kind": "value", "flag": "--timeout", "name": "timeout", "type": "integer", "default": 600},
    ]},

    {"name": "scatter-gen", "description": "没有原厂 scatter 时，按 /proc/dumchar_info 反推一张分区表",
     "params": [
        {"kind": "positional", "name": "dumchar", "type": "string", "help": "设备的 /proc/dumchar_info 文本（用 --profile 可省）"},
        {"kind": "value", "flag": "--profile", "name": "profile", "type": "string"},
        {"kind": "value", "flag": "--out", "name": "out", "type": "string", "required": True, "help": "输出 scatter 文件"},
        {"kind": "value", "flag": "--platform", "name": "platform", "type": "string", "default": "MT6572"},
        {"kind": "value", "flag": "--project", "name": "project", "type": "string", "default": "auto"},
        {"kind": "value", "flag": "--compare", "name": "compare", "type": "string", "help": "和已知正确 scatter 对比验证"},
        {"kind": "flag", "flag": "--yes", "name": "yes", "type": "boolean", "help": "确认可用于刷机"},
    ]},

    {"name": "run", "description": "通用兜底：跑任意 mtkport 子命令（patch/unpack/sys-pack/info 等）。arguments 为空格分隔的其余参数",
     "params": [
        {"kind": "positional", "name": "command", "type": "string", "required": True, "help": "子命令名，如 patch / unpack / info"},
        {"kind": "value", "flag": "--args", "name": "arguments", "type": "string", "help": "其余参数，空格分隔，原样传给 mtkport"},
    ]},
]

# deploy 复用 auto 的参数表（一条命令部署 = 先体检 doctor 再一键移植 auto）
for _t in TOOLS:
    if _t["name"] == "auto":
        TOOLS.append({
            "name": "deploy",
            "description": "★一条命令部署（推荐入口）：先跑 doctor 体检环境，再跑 auto 一键移植。别人把工具箱丢给 AI 后，只调这一个工具即可完成环境检查+移植",
            "params": _t["params"],
        })
        break

# git-sync：从 git 仓库一条命令拉取最新版（工具箱需从 git clone 出来且配了远程）
TOOLS.append({
    "name": "git-sync",
    "description": "从 git 仓库拉取工具箱最新版（一条命令更新；需要工具箱是从 git clone 出来的且配了远程仓库 origin）",
    "params": [],
})

TOOL_MAP = {t["name"]: t for t in TOOLS}


def _build_schema(params):
    props = {}
    required = []
    for p in params:
        prop = {"type": p["type"]}
        if p.get("help"):
            prop["description"] = p["help"]
        if p.get("default") is not None and p["kind"] != "flag":
            prop["default"] = p["default"]
        props[p["name"]] = prop
        if p.get("required"):
            required.append(p["name"])
    return {"type": "object", "properties": props, "required": required}


def _tool_list():
    return [{"name": t["name"], "description": t["description"],
             "inputSchema": _build_schema(t["params"])} for t in TOOLS]


def _build_args(tool, arguments):
    argv = [sys.executable, ENTRY, tool["name"]]
    for p in tool["params"]:
        val = arguments.get(p["name"], p.get("default"))
        if p["kind"] == "flag":
            if val:
                argv.append(p["flag"])
        elif p["kind"] == "positional":
            if val is not None and str(val) != "":
                argv.append(str(val))
        else:  # value
            if val is not None and str(val) != "":
                argv.append(p["flag"])
                argv.append(str(val))
    return argv


def _native_devices(arguments):
    lines = ["已知设备（本工具链已做过的机型）：", ""]
    for key, d in KNOWN_DEVICES.items():
        lines.append("  %s —— %s" % (key, d["name"]))
        lines.append("      芯片 %s / 内核 %s / Android %s / 设备代号 %s" %
                     (d["chipset"], d["kernel"], d["android"], d["device"]))
        lines.append("      移植规则: %s" % d["profile"])
    lines.append("")
    lines.append("用法：把 key 传给 device-info 看完整分区表；或用 --profile 指向上面规则文件直接做一键移植。")
    return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": False}


def _native_device_info(arguments):
    key = arguments.get("key", "")
    if key not in KNOWN_DEVICES:
        return {"content": [{"type": "text", "text": "未知设备代号 '%s'，可用: %s" % (key, ", ".join(KNOWN_DEVICES))}], "isError": True}
    d = KNOWN_DEVICES[key]
    arc = os.path.join(TOOLBOX, d["archive"])
    try:
        with open(arc, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": "读档案失败 %s: %s" % (arc, e)}], "isError": True}
    info = {
        "key": key,
        "name": d["name"],
        "model": d["model"],
        "device": d["device"],
        "chipset": d["chipset"],
        "kernel": d["kernel"],
        "android": d["android"],
        "profile": d["profile"],
        "archive": d["archive"],
        "last_serial": data.get("serial", ""),
        "display": data.get("display", {}),
        "memory_mb": data.get("memory_mb"),
        "partitions": [p["name"] for p in data.get("partitions", [])],
    }
    return {"content": [{"type": "text", "text": json.dumps(info, ensure_ascii=False, indent=2)}], "isError": False}


def _native_deploy(arguments):
    """一条命令部署：先体检环境（doctor），再一键移植（auto）。"""
    args = arguments or {}
    doctor_text, doctor_err = _run_cmd([sys.executable, ENTRY, "doctor"])
    auto_argv = _build_args(TOOL_MAP["auto"], args)
    auto_text, auto_err = _run_cmd(auto_argv)
    text = ("【第 1/2 步 · 环境体检 doctor】\n" + doctor_text +
            "\n\n【第 2/2 步 · 一键移植 auto】\n" + auto_text)
    return {"content": [{"type": "text", "text": text}], "isError": doctor_err or auto_err}


def _native_git_sync(arguments):
    """从 git 仓库拉取工具箱最新版。"""
    def _git(args):
        try:
            r = subprocess.run(["git", "-C", TOOLBOX] + args, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            return (r.returncode, (r.stdout or "") + (r.stderr or ""))
        except Exception as e:  # noqa: BLE001
            return (1, "[异常] %s" % e)

    rc, txt = _git(["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return {"content": [{"type": "text", "text":
            "工具箱目录不是 git 仓库（不是 git clone 出来的）。\n%s" % txt}], "isError": True}
    lines = []
    _, remote = _git(["remote", "-v"])
    lines.append("【当前远程仓库 remote -v】\n" + (remote or "（无远程，先 git remote add origin <仓库地址>）"))
    rc2, pull = _git(["pull", "--ff-only"])
    lines.append("【git pull --ff-only】[exit %d]\n" % rc2 + pull)
    return {"content": [{"type": "text", "text": "\n\n".join(lines)}], "isError": rc2 != 0}


NATIVE_HANDLERS = {
    "devices": _native_devices,
    "device-info": _native_device_info,
    "deploy": _native_deploy,
    "git-sync": _native_git_sync,
}


def _run_cmd(argv, timeout=DEFAULT_TIMEOUT):
    """跑一条 mtkport 子命令，返回 (输出文本, 是否失败)。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        text = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        text = ("[exit %d]\n" % p.returncode) + text
        return text, (p.returncode != 0)
    except subprocess.TimeoutExpired:
        return "[超时] 命令超过 %d 秒" % timeout, True
    except Exception as e:  # noqa: BLE001
        return "[异常] %s" % e, True


def _call_tool(name, arguments):
    if name in NATIVE_HANDLERS:
        return NATIVE_HANDLERS[name](arguments or {})
    if name not in TOOL_MAP:
        return {"content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}
    tool = TOOL_MAP[name]
    argv = _build_args(tool, arguments or {})
    text, is_err = _run_cmd(argv)
    return {"content": [{"type": "text", "text": text}], "isError": is_err}


def _send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        if rid is None:  # 通知类（initialized 等），不回
            continue
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": _tool_list()}})
        elif method == "tools/call":
            params = req.get("params", {})
            _send({"jsonrpc": "2.0", "id": rid, "result": _call_tool(params.get("name", ""), params.get("arguments", {}))})
        else:
            _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method: %s" % method}})


if __name__ == "__main__":
    main()
