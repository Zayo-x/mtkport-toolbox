# -*- coding: utf-8 -*-
"""用 mtkclient（BROM/preloader 直写）把刷机目录写进设备 —— 这代的"无人值守刷机"。

和 SP Flash Tool 的区别：
    SPFT    图形界面，要人点 Download、要拔插一次线、要驱动装成 USB VCOM
    mtkclient  命令行，直接跟芯片的 BROM/Preloader 说话，可以脚本化
                （Windows 上需要一个 UsbDk 驱动，装一次即可）

本模块只做"把成品安全写进去"这件事，前置条件会逐条检查，不满足就明确说缺什么。
"""
import os
import re
import shutil
import subprocess
import time

from . import scatter as scatter_mod
from .util import PortError, log, size

CANDIDATES = [
    os.environ.get('MTKCLIENT_DIR') or '',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'vendor', 'mtkclient'),
    r'D:\MTK工具\MTKClient GUI 2.0\mtkclient',
    r'D:\MTK工具\mtkclient',
]
SCRIPT_REL = os.path.join('runtime', 'Lib', 'site-packages',
                          'mtkclient-1.55-py3.10.egg', 'EGG-INFO', 'scripts', 'mtk')
USBDK_MSI = 'UsbDk_1.0.22_x64.msi'


def find_mtkclient(explicit=None):
    """找那套自带 python 运行时的 mtkclient，返回 (python.exe, cli 脚本, 根目录)。"""
    roots = []
    if explicit:
        roots.append(explicit)
    roots += [c for c in CANDIDATES if c]
    for root in roots:
        py = os.path.join(root, 'runtime', 'python.exe')
        script = os.path.join(root, SCRIPT_REL)
        if os.path.isfile(py) and os.path.isfile(script):
            return py, script, root
    raise PortError('找不到 mtkclient：用 --mtk 指定目录（里面要有 runtime\\python.exe）')


def _cli_copy(script, workdir):
    """包里的 cli 在 .egg（zip）里，不能直接执行，复制出来一份。"""
    os.makedirs(workdir, exist_ok=True)
    dst = os.path.abspath(os.path.join(workdir, 'mtk_cli.py'))
    if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(script):
        shutil.copyfile(script, dst)
    return dst


def usbdk_state():
    """Windows 上 mtkclient 需要 UsbDk；返回 (是否装了, 说明)。"""
    drv = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                       'System32', 'drivers', 'UsbDk.sys')
    have_file = os.path.isfile(drv)
    have_svc = False
    try:
        out = subprocess.run(['sc', 'query', 'UsbDk'], capture_output=True,
                             text=True, timeout=20).stdout or ''
        have_svc = 'SERVICE_NAME' in out or 'UsbDk' in out
    except Exception:
        pass
    if have_file or have_svc:
        return True, 'UsbDk 已安装'
    return False, 'UsbDk 没装（mtkclient 在 Windows 上要用它接管 BROM 设备）'


def install_hint(root):
    """找 UsbDk 的 MSI，优先随包自带的 工具\\UsbDk_1.0.22_x64.msi。"""
    tk_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [
        os.path.join(tk_root, '工具', USBDK_MSI),
        os.path.join(tk_root, 'bin', USBDK_MSI),
        os.path.join(os.path.dirname(root.rstrip('\\/')), USBDK_MSI),
        os.path.join(root, USBDK_MSI),
    ]
    for msi in cands:
        if os.path.isfile(msi):
            return msi
    return cands[0]


def run_mtk(py, cli, args, timeout=900, echo=True):
    """跑一条 mtkclient 命令，返回 (rc, 输出)。"""
    cmd = [py, cli] + list(args)
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env,
                           cwd=os.path.dirname(cli))
    except subprocess.TimeoutExpired:
        raise PortError('mtkclient 超时（%d 秒）：设备可能没进 BROM，或者卡住了' % timeout)
    out = (p.stdout or b'').decode('utf-8', 'replace') + \
          (p.stderr or b'').decode('utf-8', 'replace')
    if echo:
        for ln in out.splitlines():
            if ln.strip():
                log('     ' + ln.rstrip()[:150])
    return p.returncode, out


def plan(flash_dir):
    """按 scatter 里"勾了要刷"的分区，列出 (分区名, 文件路径)。"""
    sc = None
    for f in sorted(os.listdir(flash_dir)):
        if f.lower().endswith('.txt') and 'scatter' in f.lower():
            sc = os.path.join(flash_dir, f)
            break
    if not sc:
        raise PortError('%s 里没有 scatter 文件' % flash_dir)
    rows = scatter_mod.parse(sc)
    out, missing = [], []
    for p in rows:
        if str(p.get('is_download')).lower() not in ('true', '1'):
            continue
        name = p['partition_name']
        fn = os.path.basename(p.get('file_name') or '')
        fp = os.path.join(flash_dir, fn) if fn else None
        if fp and os.path.isfile(fp):
            out.append((name, fp))
        else:
            missing.append('%s（scatter 里写 %s，目录里没有）' % (name, fn or '空'))
    return out, missing


def write(flash_dir, mtk_dir=None, do_reset=True, workdir=None, wait_prompt=True,
          handshake_timeout=600):
    """把刷机目录里勾选的分区写进设备。

    wait_prompt=False 时不会等"设备进 BROM"，适合你先手动进好再跑。
    """
    py, script, root = find_mtkclient(mtk_dir)
    workdir = workdir or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '_工作区')
    cli = _cli_copy(script, workdir)

    log('■ mtkclient：%s' % root)
    ok_usbdk, why = usbdk_state()
    if not ok_usbdk:
        msi = install_hint(root)
        log('   ! %s' % why)
        log('   装法：右键用管理员运行 %s' % msi)
        log('       或：msiexec /i "%s" /qn' % msi)
        log('   装完不用重启，直接再跑这条命令。')
        raise PortError('缺 UsbDk 驱动')

    parts, missing = plan(flash_dir)
    if missing:
        for m in missing:
            log('   ! 跳过：%s' % m)
    if not parts:
        raise PortError('刷机目录里没有任何可写的镜像')
    log('   计划写入 %d 个分区：%s' % (len(parts), '、'.join('%s(%s)' % (n, size(
        os.path.getsize(f))) for n, f in parts)))

    # 1. 先握手，确认真的进了 BROM/Preloader
    #    mtkclient 自己会在端口上等设备（没设备就阻塞），所以这里给它一个长超时，
    #    等于"边等边连"：你一插上，它就连上了。
    log('\n■ 握手（printgpt）：没连上时会一直等，最多 %d 秒' % handshake_timeout)
    if wait_prompt:
        log('   现在就去做：平板【关机】→ 插 USB 线（有的机器要按住音量键）')
    t0 = time.time()
    try:
        rc, out = run_mtk(py, cli, ['printgpt'], timeout=handshake_timeout, echo=False)
    except PortError:
        raise PortError('等不到 BROM：确认平板已关机、线插在主板 USB 口（别插前面板/HUB）、'
                        '需要时按住音量下键；也可能 UsbDk 没生效（重装一次驱动）')
    if rc != 0:
        tail = [l for l in out.splitlines() if l.strip()][-6:]
        for ln in tail:
            log('     ' + ln.strip()[:150])
        if re.search(r'ImportError|No module|not found|Traceback', out):
            raise PortError('mtkclient 启动失败（见上面的报错）')
        raise PortError('握手失败（rc=%d）：设备可能没在 BROM 模式' % rc)
    for ln in out.splitlines()[:14]:
        if ln.strip():
            log('     ' + ln.strip()[:140])
    log('   ✓ 连上了，用时 %.1f 秒' % (time.time() - t0))

    # 2. 逐个写
    log('\n■ 开始写入')
    for name, fp in parts:
        log('   → %s  %s' % (name, size(os.path.getsize(fp))))
        rc, out = run_mtk(py, cli, ['w', name, fp], timeout=1800, echo=False)
        tail = [l for l in out.splitlines() if l.strip()][-4:]
        for ln in tail:
            log('     ' + ln.strip()[:150])
        if rc != 0 or not re.search(r'written|Wrote|done|success', out, re.I):
            raise PortError('写 %s 失败（rc=%d）' % (name, rc))
        log('     ✓ 完成')

    # 3. 重启
    if do_reset:
        log('\n■ 重启设备（reset）')
        run_mtk(py, cli, ['reset'], timeout=120, echo=False)
        log('   已发送 reset，等它自己开机')
    log('\n■ 全部写完')
    return parts


def wait_boot(serial=None, timeout=600):
    from .flash import wait_boot as _wb
    return _wb(serial, timeout=timeout)
