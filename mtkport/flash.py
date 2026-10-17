# -*- coding: utf-8 -*-
"""刷机这一段：把成品变成"插上就能刷"的状态，并在刷完自动验开机。

为什么不能全自动：MT6572 刷 system 必须进 preloader/BROM（yaffs/ext4 直写），
没有 fastboot。电脑这边只有两条路——
    1. SP Flash Tool（图形界面，必须有人点一下 Download、拔插一次 USB）
    2. mtkclient（命令行可全自动，但要装 python 包 + 给 BROM 设备装 libusb 驱动）
这个模块做的是"把 SPFT 这条路铺到最后一厘米"：
    · 检查镜像有没有超出分区（超了 SPFT 会报错，机器会停在半途）
    · 把 SPFT 的"上次打开目录"预置成本次刷机目录（省掉找文件的功夫）
    · 拉起 SPFT 并弹出刷机目录
    · 刷完之后轮询 adb，把新系统的型号/内核/版本念出来 = 开机自检
"""
import os
import subprocess
import time

from . import scatter as scatter_mod
from .util import PortError, log, size, find_tool

SPFT_CANDIDATES = [
    r'D:\MTK工具\SP_Flash_tool\flash_tool.exe',
    r'D:\deepseek-harness\spflash\SPFlashTool-5.1812\SP_Flash_Tool_v5.1812_Win\flash_tool.exe',
]


def find_spft(exe=None):
    if exe:
        if os.path.isfile(exe):
            return exe
        raise PortError('找不到 SP Flash Tool：%s' % exe)
    for c in SPFT_CANDIDATES:
        if os.path.isfile(c):
            return c
    raise PortError('找不到 flash_tool.exe：用 --spft 指定路径')


def check_dir(flash_dir):
    """刷机前自检：scatter 在不在、镜像有没有超分区、文件名对不对得上。"""
    sc = None
    for f in os.listdir(flash_dir):
        if f.lower().endswith('.txt') and 'scatter' in f.lower():
            sc = os.path.join(flash_dir, f)
    if not sc:
        cands = [f for f in os.listdir(flash_dir) if f.lower().endswith('.txt')]
        if len(cands) == 1:
            sc = os.path.join(flash_dir, cands[0])
    if not sc:
        raise PortError('%s 里没有 scatter 文件（.txt）' % flash_dir)
    log('■ 刷机目录自检：%s' % flash_dir)
    log('   scatter：%s' % os.path.basename(sc))
    try:
        probs = scatter_mod.check(sc, flash_dir)
    except Exception as e:  # check 里可能抛 PortError
        log('   ! 自检没跑完：%s' % e)
        probs = ['自检异常：%s' % e]
    if isinstance(probs, (list, tuple)):
        n_bad = len(probs)  # scatter.check 自己已经把每条问题打出来了
    else:
        n_bad = int(probs or 0)
    parts = scatter_mod.parse(sc)
    want = [p for p in parts if str(p.get('is_download')).lower() in ('true', '1')]
    log('   会刷 %d 个分区：%s' % (len(want), '、'.join(p['partition_name'] for p in want)))
    return sc, n_bad


def seed_spft_history(scatter_path, spft_exe=None):
    """把 SPFT 的"上次打开目录"改成这次的 scatter，开文件对话框就直接落在刷机目录。"""
    exe = find_spft(spft_exe)
    d = os.path.dirname(exe)
    ini = os.path.join(d, 'history.ini')
    if not os.path.isfile(ini):
        return None
    lines = []
    try:
        with open(ini, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    key = 'lastDir='
    esc = scatter_path.replace('\\', '\\\\')
    out, done = [], False
    for ln in lines:
        if ln.strip().startswith(key):
            out.append(key + esc)
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(key + esc)
    try:
        with open(ini, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
        log('   已把 SPFT 的默认打开目录指到本次刷机目录')
    except Exception as e:
        log('   ! 改 SPFT 配置失败（不影响刷机）：%s' % e)
    return ini


def open_spft(flash_dir, scatter_path, spft_exe=None):
    """拉起 SPFT，并弹出刷机目录（人只需要：Open scatter → Download）。"""
    exe = find_spft(spft_exe)
    seed_spft_history(scatter_path, exe)
    log('■ 拉起 SP Flash Tool：%s' % exe)
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        raise PortError('启动 SPFT 失败：%s' % e)
    try:
        subprocess.Popen(['explorer.exe', flash_dir])
    except Exception:
        pass
    log('   要做的两下：Open 里选 %s → 点 Download（先点后插线）' % os.path.basename(scatter_path))
    return exe


def wait_boot(serial_serial=None, timeout=600, poll=10):
    """刷完后的自检：等设备回到 adb，把新系统的身份念一遍。"""
    from .device import Device
    log('■ 等设备开机（最多 %d 秒）…' % timeout)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            dev = Device.pick(serial_serial, adb_port=5037)
        except PortError:
            time.sleep(poll)
            continue
        props = dev.props(refresh=True)
        log('   ✓ 起来了：%s' % dev.serial)
        log('     型号 %s %s' % (props.get('ro.product.brand', ''),
                               props.get('ro.product.model', '')))
        log('     版本 Android %s（%s）' % (props.get('ro.build.version.release', ''),
                                       props.get('ro.build.version.incremental', '')))
        log('     内核 %s' % dev.kernel()['release'])
        log('     root=%s  ro.debuggable=%s' % (dev.is_root(),
                                              props.get('ro.debuggable', '')))
        return props
    raise PortError('等了 %d 秒没等到设备：可能刷失败、卡在开机动画，或没开 adb（检查 WiFi/USB）'
                    % timeout)
