# -*- coding: utf-8 -*-
"""体检：这台电脑 + 这台设备，够不够跑移植和刷机。

新手最容易卡住的不是命令写错，而是"少了个工具/驱动/连接"，报错还看不懂。
doctor 把这些前置条件一次列出来，缺什么直说怎么补。
"""
import os
import platform
import sys

from .util import PortError, find_tool, log, size

OK = '✓'
NO = '✗'
OPT = '·'


def _check(tag, ok, detail='', hint=''):
    line = ' %s %-22s %s' % (OK if ok == 1 else (OPT if ok == 0 else NO), tag, detail)
    log(line.rstrip())
    if not ok and hint:
        log('      → %s' % hint)
    return ok == 1


def doctor(profile=None, adb_serial=None):
    from . import autoport, flash, mtkflash

    need_fail = 0
    log('■ 运行环境')
    log('  Python %s (%s / %s)' % (sys.version.split()[0], platform.system(),
                                  platform.machine()))
    if sys.version_info < (3, 8):
        log('  ! 建议 Python 3.8 以上')
        need_fail += 1

    log('\n■ 打包/解包工具')
    for tag, key, required, hint in [
        ('7z', '7z', True, '解 system.img / 移植包都要它；装 7-Zip 或用 MTKPORT_7Z 指路'),
        ('make_ext4fs', 'make_ext4fs', True, '打 system.img 用；MTK 移植工具包里带'),
        ('mke2fs', 'mke2fs', False, '可选：造空 data/cache 镜像'),
        ('magiskboot', 'magiskboot', False, '可选：lz4/xz 的 ramdisk 要靠它'),
    ]:
        p = find_tool(key)
        if not _check(tag, 1 if p else (0 if not required else -1),
                      os.path.basename(p) + ' (' + size(os.path.getsize(p)) + ')' if p
                      else '没找到', hint):
            need_fail += 1

    log('\n■ 刷机工具')
    spft = None
    for c in flash.SPFT_CANDIDATES:
        if os.path.isfile(c):
            spft = c
            break
    _check('SP Flash Tool', 1 if spft else 0, spft or '没找到（只影响图形界面刷机）',
           '给 --spft 或装 SP_Flash_Tool_v5.1812')

    try:
        py, script, root = mtkflash.find_mtkclient()
        _check('mtkclient', 1, root)
        have, why = mtkflash.usbdk_state()
        if not _check('UsbDk 驱动', 1 if have else -1, why,
                      '右键管理员运行 工具\\装UsbDk驱动-右键管理员运行.bat'):
            need_fail += 1
    except PortError as e:
        _check('mtkclient', 0, str(e), '全自动刷机才需要它')

    log('\n■ 设备连接')
    try:
        from .device import Device
        devs = Device.list_devices(None, 5037)
        if not devs:
            _check('adb 设备', -1, '一台都没连上',
                   'USB 插好或 adb connect <ip>:5555')
            need_fail += 1
        for d in devs:
            _check('  ' + d['serial'], 1, ('无线' if d.get('wifi') else
                                          ('模拟器' if d.get('emulator') else 'USB')))
        dev = Device.pick(None, adb_port=5037) if devs else None
        if dev:
            prof = dev.scan() if profile is None else profile
            log('      %s %s / %s / 内核 %s / Android %s'
                % (prof['product']['brand'], prof['product']['model'],
                   prof['chipset']['family'] or prof['chipset']['raw'],
                   prof['kernel']['release'], prof['android']['release']))
            log('      root=%s  分区 %d 个' % (dev.is_root(), len(prof['partitions'])))
    except Exception as e:
        _check('adb 设备', -1, '读设备失败：%s' % e, '先 adb connect')

    log('\n■ 机型预设')
    pres = autoport.presets()
    log('  找到 %d 个预设：%s' % (len(pres), '、'.join(p['file'] for p in pres) or '无'))
    import json as _json
    for p in pres:
        try:
            d = _json.load(open(p['path'], encoding='utf-8'))
        except Exception as e:
            log('   ✗ %s 读不出来：%s' % (p['file'], e))
            need_fail += 1
            continue
        miss = [k for k in ('name', 'boot', 'system') if k not in d]
        if miss:
            log('   ✗ %s 缺字段 %s' % (p['file'], '、'.join(miss)))
            need_fail += 1
        else:
            log('   ✓ %s  %d 条 boot 规则 / %d 条 system 规则'
                % (p['file'], len(d['boot'].get('edits', [])),
                   len(d['system'].get('copy_from_stock', []))))

    log('\n' + ('✓ 环境齐了，可以开跑' if not need_fail
                else '✗ 还差 %d 项，按上面的 → 补齐' % need_fail))
    return 0 if not need_fail else 2
