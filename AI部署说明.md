# MTK 移植工具箱 —— 给 AI 的部署与使用说明

> 读这份文件的你，是一个 AI 代理。有人把这个工具箱丢给你，让你在一台（很可能是全新的）Windows 电脑上：
> ① 自动部署运行环境，② 把一台旧的 MTK(4.x/5.x) 平板移植到较新的 Android 并准备好刷机产物。
> 照下面做即可，不需要人工点鼠标（唯一需要人配合的是装驱动时的管理员权限，见 §3）。

---

## 0. 这是什么

纯 Python 命令行工具，把一次「MT6572 + 内核 3.4.67 + 原厂 4.4.2 → LineageOS 14.1 / 7.1.2」的成功移植固化成可复用的命令。
入口是仓库根目录的 `mtkport.py`，所有命令都是 `python mtkport.py <子命令> ...`。

它只干三类事（占了移植工作量 90%）：
1. **boot.img**：解包 / 按声明式规则改（fstab、props、*.rc、脚本）/ 重打包
2. **system.img**：解包（含 sparse）/ 把原厂文件搬进移植包 / 重新打成 raw ext4（尺寸严格等于分区）
3. **scatter**：读分区表 / 生成「只刷某几个分区」的刷机文件夹 / 校验镜像尺寸

---

## 1. Python 依赖：零

工具箱**只用标准库**，没有第三方包，**不需要 pip install**。要求 Python 3.8+（3.11 实测通过）。

```
python --version      # 确认 >= 3.8
```

---

## 2. 外部程序依赖：已随包自带，开箱即用

所有外部程序都已经打进本包，**新机器不需要另外找/下工具**，也不用手动设环境变量：

| 位置 | 内容 |
|---|---|
| `bin\` | `7z.exe`(+`7z.dll`)、`make_ext4fs.exe`(+`cygwin1.dll`)、`magiskboot.exe`、`adb.exe`(+两个 `AdbWin*.dll`)、`mke2fs.exe`、`simg2img.exe`、`img2simg.exe` |
| `vendor\mtkclient\` | 整套 mtkclient（含自带 `runtime\python.exe` 运行时） |
| `工具\` | `UsbDk_1.0.22_x64.msi` + 装驱动的 bat（见 §3） |

查找顺序：**环境变量 `MTKPORT_BIN`（可选覆盖）→ 随包 `bin\` → 系统 PATH**。mtkclient 同理：环境变量 `MTKCLIENT_DIR`（可选覆盖）→ 随包 `vendor\mtkclient\`。

> 唯一还需要联网的是**移植包本身**：`auto` 会从 kc.fuquan.moe 自动下载（§4.4），或用 `--donor <zip>` 指定本地包。

> ⚠ 打包时注意：`7z.exe` 必须和 `7z.dll` 一起、`make_ext4fs.exe` 必须和 `cygwin1.dll` 一起，缺 DLL 会直接启动失败（rc 0xC0000135）。

---

## 3. UsbDk 驱动（仅 `--flash mtk` 需要，且必须装一次）

mtkclient 在 Windows 上要用 UsbDk 接管 BROM 设备。**AI 直接用命令行装，不要依赖「右键管理员运行 bat」**：

**一键装（会弹 UAC，让人点「是」）**——PowerShell 提权跑：

```powershell
Start-Process msiexec -ArgumentList '/i "工具\UsbDk_1.0.22_x64.msi" /qn /norestart' -Verb RunAs
```

（若你的 shell 已经是管理员，可直接 `msiexec /i "工具\UsbDk_1.0.22_x64.msi" /qn /norestart`，不用 `-Verb RunAs`。）

- **MSI 已随包打进 `工具\UsbDk_1.0.22_x64.msi`**（6,348,800 B），路径相对于仓库根目录；同目录还有一个 `装UsbDk驱动-右键管理员运行.bat`（纯 ASCII + 用 `%~dp0` 引用同目录 MSI，搬到哪都能用），是给人类手动装的兜底。
- `-Verb RunAs` 会弹 **UAC 提权框**，这一步 **AI 自己点不了**，必须把上面那条命令交给有管理员权限的人、请人点一下「是」即静默装完——这是整条链里唯一必须真人介入的环节。
- 装完验证（两条都要过）：

```bat
sc query UsbDk                 & rem 应显示 RUNNING / SERVICE_NAME: UsbDk
dir C:\Windows\System32\drivers\UsbDk.sys
```

- 若包内 `工具\` 没有 MSI（比如被裁剪过），从 bkerler/mtkclient 或 UsbDk 官网取 `UsbDk_1.0.22_x64.msi` 补回。
- 装完最好**拔插一次 USB**，让系统重新枚举设备。

---

## 4. 完整自动流程（命令序列）

### 4.1 体检（先跑，缺什么它会直说）

```
python mtkport.py doctor
```

退出码 0 = 环境齐了；2 = 缺东西，按它打印的 `→` 提示补齐。

### 4.2 连设备 + 生成/确认设备档案

设备通过 adb 连（USB 或 `adb connect <ip>:5555`），需要 root（`adb root`）：

```
adb devices
adb root
python mtkport.py scan --serial <serial>
```

- `scan` 生成该设备的档案 json（含分区表、内核版本、芯片、内存等）。
- **如果就是本项目调好的博阅 P100，直接用现成档案**，跳过 scan：
  `configs\p100-lineage14.json`（博阅 P100SC-HELOWIN / MT6572 / 内核 3.4.67 → LineageOS 14.1）。
- 新机型没有档案时，用 `scan` 现场生成，或照着 `configs\模板-新机型.json` 手写。

### 4.3 选移植包（打分排序）

```
python mtkport.py roms --profile configs\p100-lineage14.json --android 7.1
```

站点是 kc.fuquan.moe。命令列出候选并按匹配度打分。老机型匹配不到平台目录时，加 `--platform`（强制目录）或 `--any-kernel`（就近挑内核）。

### 4.4 一键跑完（核心命令）

```
python mtkport.py auto --profile configs\p100-lineage14.json --flash mtk --work _工作区\auto
```

这一条会依次：扫描档案 → 选包 → 下载 → 备份设备分区（双端 md5 校验）→ 打 boot 补丁 → 合并打包 system → 生成「只刷 BOOTIMG/ANDROID」的刷机目录 → mtkclient 直写 → 等开机验证。

常用参数：
- `--flash mtk`：生成后自动用 mtkclient 刷（无人值守，要 BROM 模式 + UsbDk）。
- `--flash spft`：改成拉起 SP Flash Tool 图形界面（人工点 Download）。
- `--donor <zip>`：手动指定移植包（给了就不下载）。
- `--only BOOTIMG`：先只刷 boot 验证开机。
- `--skip-backup` / `--skip-fetch`：跳过备份 / 下载（已有备份或已有包时用）。

### 4.5 刷机产物在哪

`<work>\flash\` 目录：scatter + 勾选要刷的镜像 + `刷机说明.txt`。`<work>\out\` 是打好的 `boot-patched.img` / `system.img`。

---

## 5. 硬件连接（刷机时）

- **mtkclient / SP Flash Tool** 都要设备进 **BROM/DA 模式**：关机，按住**音量下**（或音量上）不放插 USB，等电脑认出 `MT65xx PreLoader` / `MediaTek DA` 设备。
- 日常 adb 操作不需要 BROM，USB 直连（已把 USB vid/pid 改成 18D1，Windows 免驱）或 WiFi `adb connect <ip>:5555`。
- 多设备时每条命令都带 `--serial <serial>` / `-s`，避免歧义。

---

## 6. 防坑清单（都是本项目踩过的真实坑）

1. **中文路径 + .bat**：cmd 按 GBK(936) 读 UTF-8 的中文路径会变乱码。给 Windows 写 bat 一律**纯 ASCII + CRLF + 无 BOM**。
2. **scatter 偏移**：`/proc/dumchar_info` 的 StartAddr 就是 `/dev/block/mmcblk0` 字节偏移；scatter（flasher 线性坐标）= sysfs 扇区偏移 + preloader 大小（本机 0xC00000）。两者差一个 preloader，别搞混。
3. **vold fstab**：老 4.4 的 `voldmanaged=sdcard0:emmc@fat` 在新 vold 里解析不了 ⇒ 要写成数字分区号 `voldmanaged=sdcard0:7,nonremovable,noemulatedsd`（7 = 本机 FAT 分区号，别硬套）。
4. **toybox sed 不支持 `\b`**：正则里别用 `\b`，用普通字符模式，改完必须 `grep` 回读确认。
5. **adb 多设备歧义**：`adb devices` 会同时列出 USB 的 16 位 serial 和 WiFi 的 `<ip>:5555`，跑命令务必带 `-s <serial>`。
6. **后台输出乱码**：Windows 后台跑 Python，开头 `sys.stdout.reconfigure(encoding='utf-8', errors='replace')`。
7. **规则「静默不生效」**：`apply_to_ramdisk` 只读规则文件里的 boot 段；传整份规则 dict 时顶层没 edits 会静默输出「与原始一致」。已修（自动取 `rules['boot']`），但改规则时留意「n 条规则零命中」红字警告。
8. **lz4/xz ramdisk**：只有非 gzip 的移植包才需要 magiskboot，普通 gzip 不用。

---

## 7. 交付与验证

- 移植是否可复现的自测基准：用移植包 boot.img 当输入跑 `patch`，输出应与 `F-boot-v6-修存储与zram.img` 逐字节相同（md5 `ee0cf8cf39e9a3dbca3ee32f825ee63e`）。
- 两条自测脚本：`自测-自动化.py`（默认 37 项）、`自测-端到端.py --full`（37 项）。环境搭好后先跑一遍自测再动真机。
- 详细命令参考：同目录 `用法说明.txt`（人类向，含每个子命令的参数表）。

---

## 8. 速查：最小可用命令集

```bat
python mtkport.py doctor
python mtkport.py roms --profile configs\p100-lineage14.json --android 7.1
python mtkport.py auto --profile configs\p100-lineage14.json --flash mtk --work _工作区\auto
```

（`--flash mtk` 前，先确认 §3 的 UsbDk 已装好、设备能进 BROM。外部工具全部随包自带，无需再配环境变量。）
