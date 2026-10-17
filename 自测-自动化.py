# -*- coding: utf-8 -*-
"""自动化这一层的自测（不需要设备，不需要网络 —— 除了最后一组可跳过的联网用例）。

覆盖：
    1. 预设匹配必须"型号对得上"，认错机器会误套规则
    2. FAT 分区号能从分区表数出来（fstab 修存储靠它）
    3. boot 规则草稿：fstab / USB / adb / 交换分区四类都生成
    4. system 规则草稿：固件目录、HAL、带芯片名的库
    5. 草稿落盘的结构（boot/system/_说明/_设备）
    6. 刷机目录自检：镜像大小 / 要刷哪几个分区 / 超了要报错
    7. （可选 --net）站点目录规律与打分排序

用法： python 自测-自动化.py [--net]
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from mtkport import autoport, flash, scatter as scatter_mod  # noqa: E402
from mtkport.util import PortError  # noqa: E402

PASS = []
FAIL = []


def ok(name, cond, detail=''):
    if cond:
        PASS.append(name)
        print('  ✓ %s%s' % (name, ('  —— ' + detail) if detail else ''))
    else:
        FAIL.append(name)
        print('  ✗ %s%s' % (name, ('  —— ' + detail) if detail else ''))


def check(no, name, fn):
    print('\n[%d] %s' % (no, name))
    try:
        fn()
    except Exception as e:
        FAIL.append(name)
        print('  ✗ 抛异常：%s: %s' % (type(e).__name__, e))


def fake_profile(device='A102', model='P100SC-HELOWIN', brand='Boyue'):
    """造一份设备档案：真的分区表（20 项、FAT=p7），但型号可换。"""
    parts = [
        {'name': 'preloader', 'size': 0xC00000, 'start': 0x0, 'type': 'SV5_BL_BIN',
         'map_to': '/dev/block/mmcblk0', 'format': 'mtk-dumchar'},
        {'name': 'pro_info', 'size': 0x300000, 'start': 0x100000, 'type': 'NORMAL_ROM',
         'map_to': '/dev/block/mmcblk0', 'format': 'mtk-dumchar'},
        {'name': 'nvram', 'size': 0x500000, 'start': 0x500000, 'type': 'NORMAL_ROM',
         'map_to': '/dev/block/mmcblk0', 'format': 'mtk-dumchar'},
        {'name': 'protect_f', 'size': 0xA00000, 'start': 0xA00000, 'type': 'EXT4',
         'map_to': '/dev/block/mmcblk0p2', 'format': 'mtk-dumchar'},
        {'name': 'bootimg', 'size': 0x600000, 'start': 0x1D80000, 'type': 'NORMAL_ROM',
         'map_to': '/dev/block/mmcblk0', 'format': 'mtk-dumchar'},
        {'name': 'recovery', 'size': 0x600000, 'start': 0x2380000, 'type': 'NORMAL_ROM',
         'map_to': '/dev/block/mmcblk0', 'format': 'mtk-dumchar'},
        {'name': 'fat', 'size': 0x4A000000, 'start': 0x37E0000, 'type': 'FAT',
         'map_to': '/dev/block/mmcblk0p7', 'format': 'mtk-dumchar'},
    ]
    return {
        'serial': 'TEST-0001', 'via': 'usb', 'root': True,
        'props': {'ro.sf.hwrotation': '180'},
        'kernel': {'release': '3.4.67', 'version': '#1', 'arch': 'armv7l',
                   'major_minor': '3.4'},
        'chipset': {'raw': 'mt6572', 'family': 'mt6572', 'num': '6572', 'mtk': True},
        'android': {'release': '7.1.2', 'sdk': 25, 'incremental': 'x', 'display_id': 'y',
                    'fingerprint': 'z'},
        'product': {'brand': brand, 'model': model, 'device': device, 'name': 'n',
                    'manufacturer': brand, 'board': 'MT6572'},
        'display': {'width': 480, 'height': 800, 'density': 220, 'hwrotation': '180'},
        'memory_mb': 463,
        'partitions': parts,
    }


def main():
    print('=' * 66)
    print('自动化层自测')
    print('=' * 66)

    # 1 预设必须按型号严格匹配
    def t1():
        me = fake_profile()
        hit = autoport.match_preset(me)
        ok('本机（A102/内核/芯片都对）能命中预设', hit is not None,
           hit['info']['name'][:24] if hit else '')
        other = fake_profile(device='X9000', model='SomePhone-X9', brand='NoName')
        ok('陌生机型不套预设', autoport.match_preset(other) is None)
        near = autoport.near_presets(other)
        ok('但会给出"相近预设"提示', len(near) >= 1,
           '；'.join(near[0]['why']) if near else '')
        ok('strict=False 时能放宽（给人工参考用）',
           autoport.match_preset(other, strict=False) is not None)
    check(1, '预设匹配：认型号，不认"碰巧同内核"', t1)

    # 2 FAT 分区号
    def t2():
        fat, why = autoport.fat_index(fake_profile())
        ok('从 mmcblk0pN 数出 FAT 号', fat == 7, 'p%d / %s' % (fat, why))
        broken = fake_profile()
        for p in broken['partitions']:
            if p['name'] == 'fat':
                p['map_to'] = '/dev/block/mmcblk0'
        fat2, _ = autoport.fat_index(broken)
        ok('数不出来时返回 None（不乱猜）', fat2 is None)
    check(2, 'FAT 分区号：给 fstab 用的那个数字', t2)

    # 3 boot 规则草稿
    def t3():
        rules, notes = autoport.derive_boot_rules(fake_profile(), usb_vid='18D1',
                                                 usb_pid='D001')
        edits = rules['boot']['edits']
        joined = json.dumps(edits, ensure_ascii=False)
        ok('fstab 换成真实分区号 + noemulatedsd',
           'voldmanaged=sdcard0:7,nonremovable,noemulatedsd' in joined)
        ok('emmc@fat 那条也在（老移植包的写法）',
           any(e.get('old') == 'voldmanaged=sdcard0:emmc@fat' for e in edits))
        ok('USB vid/pid 有对应规则',
           any('sys.usb.vid' == e.get('key') for e in edits)
           and any('idProduct D001' == e.get('new') for e in edits))
        ok('adb 免授权与 usb 配置有规则',
           any(e.get('key') == 'ro.adb.secure' for e in edits)
           and any(e.get('key') == 'persist.sys.usb.config' for e in edits))
        ok('交换分区修复有规则（tiny_mkswap）',
           any(e.get('old') == 'tiny_mkswap' for e in edits))
        ok('每条都标 optional（原包版本不同不至于卡住）',
           all(e.get('optional') for e in edits))
    check(3, 'boot 规则草稿', t3)

    # 4 system 规则草稿
    def t4():
        tmp = tempfile.mkdtemp(prefix='mtkport-autotest-')
        try:
            stock = os.path.join(tmp, 'stock')
            donor = os.path.join(tmp, 'donor')
            for d in ('etc/firmware/mt6627', 'etc/.tp', 'lib/hw', 'vendor/lib', 'lib'):
                os.makedirs(os.path.join(stock, d), exist_ok=True)
            for d in ('etc', 'lib'):
                os.makedirs(os.path.join(donor, d), exist_ok=True)
            open(os.path.join(stock, 'lib/hw/gralloc.mt6572.so'), 'wb').write(b'x')
            open(os.path.join(stock, 'lib/libcamera.mt6572.so'), 'wb').write(b'x')
            open(os.path.join(stock, 'lib/libaudio.primary.default.so'), 'wb').write(b'x')
            rules, notes = autoport.derive_system_rules(stock, donor, chip='mt6572')
            copies = rules['system']['copy_from_stock']
            ok('固件目录整棵搬（etc/firmware/）', 'etc/firmware/' in copies)
            ok('温控目录整棵搬（etc/.tp/）', 'etc/.tp/' in copies)
            ok('HAL 目录整棵搬（lib/hw/）', 'lib/hw/' in copies)
            ok('带芯片名的库有通配规则',
               any('*mt6572*.so' in c for c in copies), '；'.join(c for c in copies
                                                                if 'mt6572' in c))
            ok('删掉 install-recovery.sh（免得刷回 recovery）',
               'etc/install-recovery.sh' in rules['system']['delete'])
            ok('输出的键名与 sys-merge 一致',
               set(rules['system']) >= {'copy_from_stock', 'delete'})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    check(4, 'system 规则草稿', t4)

    # 5 草稿落盘结构
    def t5():
        tmp = tempfile.mkdtemp(prefix='mtkport-autotest-')
        try:
            stock = os.path.join(tmp, 'stock')
            donor = os.path.join(tmp, 'donor')
            os.makedirs(os.path.join(stock, 'etc/firmware'), exist_ok=True)
            os.makedirs(donor, exist_ok=True)
            out = os.path.join(tmp, 'rules.json')
            autoport.draft(fake_profile(), stock, donor, out, usb_vid='18D1')
            d = json.load(io.open(out, encoding='utf-8'))
            ok('顶层是 boot/system/_说明/_设备', set(d) ==
               {'boot', 'system', '_说明', '_设备'}, '、'.join(d))
            ok('_设备 里记了型号与内核',
               d['_设备']['kernel'] == '3.4.67' and d['_设备']['device'] == 'A102')
            ok('草稿能直接被 json 读回（没写坏）', isinstance(d['boot']['edits'], list))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    check(5, '草稿落盘', t5)

    # 6 刷机目录自检
    def t6():
        tmp = tempfile.mkdtemp(prefix='mtkport-autotest-')
        try:
            sc = write_fake_scatter(tmp)
            img = os.path.join(tmp, 'boot.img')
            open(img, 'wb').write(b'\0' * 1024)
            # 让 scatter 里 BOOTIMG 指向一个 1024 字节的分区
            rows = scatter_mod.parse(sc)
            ok('scatter 能解析', len(rows) == 2, '%d 个分区' % len(rows))
            got, bad = flash.check_dir(tmp)
            ok('自检通过（镜像没超分区）', bad == 0, os.path.basename(got))
            # 再放一个超过分区大小的镜像，应该被查出来
            open(os.path.join(tmp, 'system.img'), 'wb').write(b'\0' * 4096)
            got2, bad2 = flash.check_dir(tmp)
            ok('超分区会被查出来', bad2 >= 1, '问题数 %d' % bad2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    check(6, '刷机目录自检', t6)

    # 8 没有原厂 scatter 也能反推分区表（拿真机 dump 的分区表对照原厂 scatter）
    def t8():
        from mtkport import scatter as scatter_mod
        from mtkport import scattergen
        dum = (r'D:\deepseek-harness\安卓折腾\博阅P100\03-真机备份-20261007'
               r'\dumchar_info.txt')
        fac = (r'D:\deepseek-harness\安卓折腾\博阅P100\01-原厂固件-整机恢复'
               r'\原厂固件\MT6572_Android_scatter.txt')
        if not (os.path.isfile(dum) and os.path.isfile(fac)):
            ok('跳过（本机没留真机 dumchar / 原厂 scatter）', True)
            return
        text = io.open(dum, encoding='utf-8', errors='replace').read()
        gen = scattergen.gen_from_dumchar(text)
        tmpd = tempfile.mkdtemp(prefix='mtkport-scatter-')
        out = os.path.join(tmpd, 'gen-scatter.txt')
        scattergen.write_scatter(gen, out)
        rows = scatter_mod.parse(out)
        ok('反推的分区表能解析', len(rows) >= 10, '%d 个分区' % len(rows))
        good, diffs = scattergen.compare(scatter_mod.parse(fac), rows)
        ok('分区名/偏移/大小与原厂 scatter 完全一致', good, '；'.join(diffs[:3]))
        ok('默认一个都不勾（不会误刷）',
           all(str(r['is_download']).lower() != 'true' for r in rows))
        broken = gen.replace('partition_size: 0x600000', 'partition_size: 0x601000', 1)
        scattergen.write_scatter(broken, out)
        good2, _ = scattergen.compare(scatter_mod.parse(fac),
                                      scatter_mod.parse(out))
        ok('故意写错偏移能查出来', not good2)
        shutil.rmtree(tmpd, ignore_errors=True)
    check(8, '没有原厂 scatter 时按分区表反推', t8)

    def t9():
        from mtkport import cpio, rules as rules_mod, scattergen
        tmpd = tempfile.mkdtemp(prefix='mtkport-t9-')
        try:
            tree = os.path.join(tmpd, 'rd')
            os.makedirs(tree)
            with io.open(os.path.join(tree, 'fstab.mt6572'), 'w', encoding='utf-8',
                         newline='') as f:
                f.write('/devices/platform/mtk-msdc.0/mmc_host* auto auto defaults '
                        'voldmanaged=sdcard0:emmc@fat\n')
            with io.open(os.path.join(tree, 'default.prop'), 'w', encoding='utf-8',
                         newline='') as f:
                f.write('ro.adb.secure=1\n')
            ent = cpio.from_tree(tree)
            whole = {'boot': {'mode': 'inplace',
                              'edits': [
                                  {'file': 'fstab.mt6572', 'type': 'regex',
                                   'pattern': 'voldmanaged=sdcard0:\\S+',
                                   'repl': 'voldmanaged=sdcard0:7,nonremovable,noemulatedsd'},
                                  {'file': 'default.prop', 'type': 'prop',
                                   'key': 'ro.adb.secure', 'value': '0'}]}}
            ent2, rep = rules_mod.apply_to_ramdisk(ent, whole, tmpd)
            data = {e.name.lstrip('./'): e.data for e in ent2}
            ok('整份规则文件（带 boot 段）也能应用',
               b'noemulatedsd' in data.get('fstab.mt6572', b'')
               and b'ro.adb.secure=0' in data.get('default.prop', b''),
               '改到 %d 条' % len(rep.changed))

            parts = [{'name': 'preloader', 'size': 0x1000},
                     {'name': 'mbr', 'size': 0x1000},
                     {'name': 'bootimg', 'size': 0x2000, 'sysfs_start': 0x0}]
            txt = scattergen.gen_from_parts(parts)
            scattergen.write_scatter(txt, os.path.join(tmpd, 'sc.txt'))
            rows = scatter_mod.parse(os.path.join(tmpd, 'sc.txt'))
            by = {r['partition_name'].upper(): int(r['linear_start_addr'], 16) for r in rows}
            ok('偏移按 preloader 顺序累加（sysfs 只作校验）',
               by.get('PRELOADER') == 0 and by.get('MBR') == 0x1000
               and by.get('BOOTIMG') == 0x2000,
               'PRELOADER=0x%X MBR=0x%X BOOTIMG=0x%X'
               % (by.get('PRELOADER', -1), by.get('MBR', -1), by.get('BOOTIMG', -1)))
            parts2 = [{'name': 'preloader', 'size': 0x1000},
                      {'name': 'bootimg', 'size': 0x2000, 'sysfs_start': 0x5000}]
            txt2 = scattergen.gen_from_parts(parts2)   # 故意给错的 sysfs 起址
            ok('sysfs 与累加不符时只警告不崩', 'BOOTIMG' in txt2)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
    check(9, '规则文件整份传入 + scatter 偏移算法', t9)

    def t10():
        from mtkport import romrepo
        ok('_pf 缺字段给默认', romrepo._pf({}, 'chipset', 'num') == '')
        ok('_pf 正常取值', romrepo._pf({'a': {'b': 1}}, 'a', 'b') == 1)
        m = romrepo.KERNEL_DIR_RE.match('x32 3.18.19   7.1')
        ok('版本目录能认出 x32 前缀', bool(m) and m.group(1) == '3.18.19'
           and m.group(2) == '7.1', str(m.groups()) if m else 'None')
        um = romrepo.UNKNOWN_KERNEL_RE.match('未知内核   5.1')
        ok('能认出"未知内核"目录', bool(um) and um.group(1) == '5.1')

        class R(romrepo.Repo):
            def platform_dirs(self):
                return ['1.BUG修复', 'MT6572&6582&6592&6574&6571&8384',
                        'MT6575&6577', 'MT6580&8321']

        r = R()
        plat, _ = r.pick_platform_dir({'chipset': {'num': 'mt6577', 'family': 'mt6577'}})
        ok('按纯数字匹配平台目录（MT6577→MT6575&6577）', plat == 'MT6575&6577', plat)
        plat, _ = r.pick_platform_dir({'chipset': {'num': 'mt6592', 'family': 'mt6592'}})
        ok('MT6592 归入 6572&6582&6592 目录',
           plat == 'MT6572&6582&6592&6574&6571&8384', plat)
    check(10, '老 MTK 平台/内核目录兜底（离线）', t10)

    if '--net' in sys.argv:
        def t7():
            from mtkport import romrepo
            repo = romrepo.Repo()
            cands, plat, kdirs = repo.candidates(fake_profile(), limit=5)
            ok('能列出平台目录', bool(plat), plat)
            ok('能认出 <内核 版本> 子目录', len(kdirs) >= 3, '、'.join(kdirs[:4]))
            ok('候选按分数降序', [c['score'] for c in cands] ==
               sorted([c['score'] for c in cands], reverse=True))
            top = cands[0]
            ok('第一名是 A102 的包', 'A102' in top['name'], top['name'])
            ok('每条候选都有理由', all(c['why'] for c in cands))
        check(7, '站点选包（联网）', t7)

    print('\n' + '=' * 66)
    print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print('  ✗ ' + f)
        return 1
    print('全绿')
    return 0


def write_fake_scatter(d):
    """造一个两分区的 scatter（BOOTIMG 1024 字节、ANDROID 2048 字节）。"""
    p = os.path.join(d, 'MT6572_Android_scatter.txt')
    def block(idx, name, fname, dl, typ, start, size, op='UPDATE'):
        return ('- partition_index: %s\r\n'
                '  partition_name: %s\r\n'
                '  file_name: %s\r\n'
                '  is_download: %s\r\n'
                '  type: %s\r\n'
                '  linear_start_addr: 0x%X\r\n'
                '  physical_start_addr: 0x%X\r\n'
                '  partition_size: 0x%X\r\n'
                '  region: EMMC\r\n'
                '  storage: HW_STORAGE_EMMC\r\n'
                '  boundary_check: true\r\n'
                '  is_reserved: false\r\n'
                '  operation_type: %s\r\n'
                '  reserve: 0x00\r\n\r\n'
                % (idx, name, fname, dl, typ, start, start, size, op))
    with io.open(p, 'w', encoding='gbk', errors='replace', newline='') as f:
        f.write('################################################################\r\n')
        f.write('# 通用 scatter（自测用）\r\n')
        f.write('################################################################\r\n\r\n')
        f.write(block('SYS0', 'BOOTIMG', 'boot.img', 'true', 'NORMAL_ROM', 0x0, 1024))
        f.write(block('SYS1', 'ANDROID', 'system.img', 'true', 'EXT4_IMG', 0x1000, 2048))
    return p


if __name__ == '__main__':
    sys.exit(main())
