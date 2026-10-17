# -*- coding: utf-8 -*-
"""自动推导移植规则：把"这台机器长什么样"翻译成 boot / system 的改动清单。

两层策略：
  1. **已知机型走预设**（configs 里以设备代号命名的 json）——这类规则是实测过的，
     可以全自动跑，结果能和当年手工做出来的镜像逐字节对上。
  2. **没见过的机型出草稿**——按通用规律推导（fstab 的 emmc@fat 换成真实分区号、
     精简分区的 vold 参数、USB/root/交换分区这些），并逐条打印"依据"和"要人确认的地方"。

自动推导能可靠拿到的几件事：
    · FAT/内置存储在后备分区表里是第几个（从 /proc/dumchar_info 的 mmcblk0pN 里数出来）
    · 本机是不是 MTK、内核版本、安卓版本（决定换不换得动）
    · 原厂 system 里有哪些带本机芯片名的库/固件（这些必须从原厂搬，不能留移植包的）
"""
import json
import os
import re

from .util import PortError, log, size

# 这些目录下的文件"带机型特征"，移植时必须用原厂的，不能留移植包的
VENDOR_DIRS = ['etc/firmware/', 'etc/.tp/', 'vendor/', 'lib/modules/', 'etc/wifi/']
VENDOR_FILES = ['lib/libaudio.primary.default.so', 'lib/libaudio.a2dp.default.so',
                'lib/libbluetooth.default.so', 'lib/libbt_vendor.so']

PRESET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'configs')


def fat_index(prof):
    """数出 FAT/内置存储是 mmcblk0p 几号 —— fstab 的 voldmanaged 要用这个数字。"""
    fat = None
    for p in prof.get('partitions') or []:
        nm = p['name'].lower()
        mp = (p.get('map_to') or '')
        m = re.search(r'mmcblk0p(\d+)$', mp)
        if nm in ('fat', 'usrdata', 'userdata', 'internal_sd') and m:
            fat = int(m.group(1))
            if nm == 'fat':
                return fat, '分区表里 FAT 就是 ' + mp
    if fat:
        return fat, '分区表里没单独叫 fat 的，用 usrdata 的 ' + str(fat)
    return None, '分区表里没找到 FAT/usrdata 的 mmcblk0pN'


def presets():
    """列出可用的机型预设。"""
    if not os.path.isdir(PRESET_DIR):
        return []
    out = []
    for f in sorted(os.listdir(PRESET_DIR)):
        if not f.endswith('.json'):
            continue
        if f.startswith(('模板', '_', 'tpl', 'template')):
            continue  # 模板不是预设，别让它参与匹配
        try:
            with open(os.path.join(PRESET_DIR, f), encoding='utf-8') as fh:
                d = json.load(fh)
        except Exception:
            continue
        out.append({'file': f, 'path': os.path.join(PRESET_DIR, f),
                    'name': d.get('name', f), 'device': d.get('device', ''),
                    'kernel': d.get('kernel', ''), 'chipset': d.get('chipset', '')})
    return out


def match_preset(prof, strict=True):
    """按 设备代号 / 内核 / 芯片 三个线索找预设。

    strict=True（默认）只认"设备代号或型号对得上"的预设——预设里的
    USB vid/pid、温控阈值、传感器 HAL 都是按某块板子实测出来的，
    内核和芯片碰巧一样不代表能直接套（那是两台不同的机器）。
    内核/芯片对得上但型号不对的，只作为"相近预设"提示给人看。
    """
    codename = (prof['product']['device'] or '').lower()
    model = (prof['product']['model'] or '').lower()
    kern = prof['kernel']['release'] or ''
    fam = prof['chipset']['family'] or ''
    best = None
    for p in presets():
        score = 0
        why = []
        dev_hit = False
        dev = (p['device'] or '').lower()
        mdl = (p.get('model') or '').lower()
        if dev and (dev == codename or dev == model or (mdl and mdl == model)):
            score += 100
            dev_hit = True
            why.append('设备代号/型号 %s 一致' % (p['device'] or p.get('model')))
        elif dev and (dev in codename or codename in dev):
            score += 40
            dev_hit = True
            why.append('设备代号相近(%s)' % p['device'])
        if p['kernel'] and p['kernel'] == kern:
            score += 30
            why.append('内核 %s 一致' % kern)
        if p['chipset'] and p['chipset'].lower() in (fam, prof['chipset']['num']):
            score += 20
            why.append('芯片 %s 一致' % p['chipset'])
        if strict and not dev_hit:
            continue
        if score and (best is None or score > best['score']):
            best = {'score': score, 'why': why, 'info': p}
    return best


def near_presets(prof):
    """内核/芯片对得上、但型号对不上的预设（只用来提示，不自动套用）。"""
    out = []
    for p in presets():
        s = 0
        why = []
        if p['kernel'] and p['kernel'] == (prof['kernel']['release'] or ''):
            s += 30
            why.append('内核 %s 一致' % p['kernel'])
        if p['chipset'] and p['chipset'].lower() in (prof['chipset']['family'] or '',
                                                    prof['chipset']['num'] or ''):
            s += 20
            why.append('芯片 %s 一致' % p['chipset'])
        if s:
            out.append({'score': s, 'why': why, 'info': p})
    out.sort(key=lambda x: -x['score'])
    return out


def derive_boot_rules(prof, fat=None, usb_vid=None, usb_pid=None, adb_secure_off=True,
                      usb_config='mtp,adb', fix_swap=True, driver=None):
    """推导 boot 的改动（只写"有把握"的几条；不做猜测）。"""
    fat, why_fat = (fat, '') if fat else fat_index(prof)
    edits = []
    if fat:
        # fstab 里 emmc@fat 这种写法在 7.x 的 vold 上解析不了，必须换成真实分区号
        edits.append({'file': driver or 'fstab*', 'type': 'replace',
                      'old': 'voldmanaged=sdcard0:emmc@fat',
                      'new': 'voldmanaged=sdcard0:%d,nonremovable,noemulatedsd' % fat,
                      'optional': True})
        edits.append({'file': driver or 'fstab*', 'type': 'replace',
                      'old': 'voldmanaged=sdcard0:auto',
                      'new': 'voldmanaged=sdcard0:%d,nonremovable,noemulatedsd' % fat,
                      'optional': True})
    if usb_vid:
        edits.append({'file': 'default.prop', 'type': 'prop', 'key': 'sys.usb.vid',
                      'value': usb_vid, 'optional': True})
    if usb_pid:
        edits.append({'file': 'init*.rc', 'type': 'replace', 'old': 'idProduct 7495',
                      'new': 'idProduct %s' % usb_pid, 'optional': True})
    if adb_secure_off:
        edits.append({'file': 'default.prop', 'type': 'prop', 'key': 'ro.adb.secure',
                      'value': '0', 'optional': True})
        edits.append({'file': 'default.prop', 'type': 'prop', 'key': 'ro.debuggable',
                      'value': '1', 'optional': True})
    if usb_config:
        edits.append({'file': 'default.prop', 'type': 'prop',
                      'key': 'persist.sys.usb.config', 'value': usb_config,
                      'optional': True})
    if fix_swap:
        # 有些移植包的 enableswap.sh 调用 toybox 里不存在的 tiny_mkswap/tiny_swapon
        edits.append({'file': 'enableswap.sh', 'type': 'replace',
                      'old': 'tiny_mkswap', 'new': 'mkswap', 'optional': True})
        edits.append({'file': 'enableswap.sh', 'type': 'replace',
                      'old': 'tiny_swapon', 'new': 'swapon', 'optional': True})
    rules = {'boot': {'mode': 'inplace', 'edits': edits}}
    notes = []
    if fat:
        notes.append('FAT/内置存储 = mmcblk0p%d（%s）' % (fat, why_fat))
    else:
        notes.append('⚠ 没数出 FAT 分区号，fstab 那条没生成，需要手工确认')
    return rules, notes


def derive_system_rules(stock_tree, donor_tree, chip=None):
    """推导 system 的搬运：原厂独有的机型库/固件必须搬过来。

    输出的键名与 `sys-merge` 用的完全一致：copy_from_stock / delete / keep。
    glob 三种写法：`a/b.so` 精确、`etc/firmware/` 整棵子树、`lib/*mt6572*.so` 通配。
    """
    copies = []
    for d in VENDOR_DIRS:
        if os.path.isdir(os.path.join(stock_tree, d.rstrip('/'))):
            copies.append(d)
    for f in VENDOR_FILES:
        if os.path.isfile(os.path.join(stock_tree, f)):
            copies.append(f)
    # 原厂 lib/hw 下的 HAL 一律用原厂的（移植包的 HAL 常带别的机型参数）
    if os.path.isdir(os.path.join(stock_tree, 'lib', 'hw')):
        copies.append('lib/hw/')
    # 带本机芯片名的库（libcamera.mt6572.so 这类）
    if chip:
        for root in ('lib', 'vendor/lib'):
            d = os.path.join(stock_tree, root)
            if os.path.isdir(d) and any(chip.lower() in f.lower()
                                        for f in os.listdir(d) if f.endswith('.so')):
                copies.append('%s/*%s*.so' % (root, chip.lower()))
    rules = {'system': {'copy_from_stock': copies,
                        'delete': ['etc/install-recovery.sh'],
                        'note': ['自动推导：固件/温控/HAL/带芯片名的库都按原厂覆盖']}}
    notes = ['从原厂搬 %d 条规则（%s）' % (len(copies), '、'.join(copies))]
    return rules, notes


def draft(prof, stock_tree, donor_tree, out_path, fat=None, usb_vid=None, usb_pid=None):
    """生成一份"草稿规则"，写盘并打印需要人确认的地方。"""
    rules, n1 = derive_boot_rules(prof, fat=fat, usb_vid=usb_vid, usb_pid=usb_pid)
    sys_rules, n2 = derive_system_rules(stock_tree, donor_tree,
                                        chip=prof['chipset']['family'])
    rules.update(sys_rules)
    rules['_说明'] = ('这是按设备档案自动推导出来的草稿：boot 只填了有把握的几条，'
                      'system 只搬原厂独有的机型文件。生成后请把 expect 补上，'
                      '第一次刷前务必先备份。')
    rules['_设备'] = {'serial': prof.get('serial'), 'model': prof['product']['model'],
                      'device': prof['product']['device'],
                      'kernel': prof['kernel']['release'],
                      'android': prof['android']['release']}
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    log('■ 自动推导的规则已写到 %s' % out_path)
    for n in n1 + n2:
        log('   · ' + n)
    log('   需要人工确认的：')
    log('     · 上面 fstab 那条的 old 写法是不是移植包里真实存在的（不是就改成实际那行）')
    log('     · USB 的 vid/pid 要不要改（不改就用移植包默认的，Windows 可能要装驱动）')
    log('     · 屏幕方向 ro.sf.hwrotation 要不要跟着原厂（本机原厂是 %s）'
        % (prof['props'].get('ro.sf.hwrotation') or '0'))
    return rules
