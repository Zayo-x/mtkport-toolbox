# -*- coding: utf-8 -*-
"""从 /proc/dumchar_info 反推 scatter（没有原厂 scatter 的设备也能出刷机目录）。

原理（已用真机验证）：SP Flash Tool 的 scatter 里
    linear_start_addr = 从 preloader 开始按 dumchar 顺序累加各分区大小的结果
也就是"flasher 空间的线性偏移"。本机实测：preloader 0xC00000 → MBR 0xC00000 →
EBR1 0xC80000 → PRO_INFO 0xD00000 … ANDROID 0x4340000 → CACHE 0x36340000 →
USRDATA 0x44540000 → FAT 0x97140000 → BMTPOOL 0xE8700000，与原厂 scatter 完全一致。

⚠ 这是"能不用原厂 scatter"的兜底方案，生成后务必用 scatter-summary 与原厂对照；
   刷错分区表会砖，工具会强制要求显式 --yes 才写文件。
"""
import io
import os

from .util import PortError, log, md5

# 名字 → (type, region, operation_type)，取自原厂 MT6572 scatter 的真实取值
GEN_TABLE = {
    'preloader': ('SV5_BL_BIN', 'EMMC_BOOT_1', 'BOOTLOADERS'),
    'mbr': ('NORMAL_ROM', 'EMMC_USER', 'UPDATE'),
    'ebr1': ('NORMAL_ROM', 'EMMC_USER', 'UPDATE'),
    'pro_info': ('NORMAL_ROM', 'EMMC_USER', 'PROTECTED'),
    'nvram': ('NORMAL_ROM', 'EMMC_USER', 'BINREGION'),
    'protect_f': ('EXT4_IMG', 'EMMC_USER', 'PROTECTED'),
    'protect_s': ('EXT4_IMG', 'EMMC_USER', 'PROTECTED'),
    'seccfg': ('NORMAL_ROM', 'EMMC_USER', 'INVISIBLE'),
    'sec_ro': ('NORMAL_ROM', 'EMMC_USER', 'UPDATE'),
    'uboot': ('NORMAL_ROM', 'EMMC_USER', 'UPDATE'),
    'bootimg': ('NORMAL_ROM', 'EMMC_USER', 'UPDATE'),
    'recovery': ('NORMAL_ROM', 'EMMC_USER', 'UPDATE'),
    'misc': ('NORMAL_ROM', 'EMMC_USER', 'INVISIBLE'),
    'logo': ('NORMAL_ROM', 'EMMC_USER', 'UPDATE'),
    'expdb': ('NORMAL_ROM', 'EMMC_USER', 'INVISIBLE'),
    'android': ('EXT4_IMG', 'EMMC_USER', 'UPDATE'),
    'cache': ('EXT4_IMG', 'EMMC_USER', 'UPDATE'),
    'usrdata': ('EXT4_IMG', 'EMMC_USER', 'UPDATE'),
    'fat': ('NORMAL_ROM', 'EMMC_USER', 'INVISIBLE'),
    'bmtpool': ('NORMAL_ROM', 'EMMC_USER', 'RESERVED'),
}

# 陌生设备上按名字猜（先查表，查不到再按关键字猜）
GUESS = [
    ('preloader', ('SV5_BL_BIN', 'EMMC_BOOT_1', 'BOOTLOADERS')),
    ('nvram', ('NORMAL_ROM', 'EMMC_USER', 'BINREGION')),
    ('protect', ('EXT4_IMG', 'EMMC_USER', 'PROTECTED')),
    ('seccfg', ('NORMAL_ROM', 'EMMC_USER', 'INVISIBLE')),
    ('system', ('EXT4_IMG', 'EMMC_USER', 'UPDATE')),
    ('android', ('EXT4_IMG', 'EMMC_USER', 'UPDATE')),
    ('userdata', ('EXT4_IMG', 'EMMC_USER', 'UPDATE')),
    ('cache', ('EXT4_IMG', 'EMMC_USER', 'UPDATE')),
    ('logo', ('NORMAL_ROM', 'EMMC_USER', 'UPDATE')),
    ('boot', ('NORMAL_ROM', 'EMMC_USER', 'UPDATE')),
]


def classify(name):
    low = name.lower()
    if low in GEN_TABLE:
        return GEN_TABLE[low]
    for key, val in GUESS:
        if key in low:
            return val
    return ('NORMAL_ROM', 'EMMC_USER', 'UPDATE')


def gen_from_parts(parts, images=None, platform='MT6572', project='auto',
                   storage='EMMC', boot_channel='MSDC_0'):
    """按设备档案里的分区表生成 scatter 文本。

    偏移一律按"从 preloader 顺序累加"算（这是 flasher 线性坐标，真机逐项验证过与原厂
    scatter 一致）；分区若带 sysfs 真实起址，只用来交叉校验并在不一致时警告 ——
    sysfs 是用户区坐标（不含 EMMC_BOOT_1 里的 preloader），两者差一个 preloader 大小。
    """
    rows = []
    for p in parts or []:
        name = (p.get('name') or '').strip()
        try:
            sz = int(p.get('size') or 0)
        except (TypeError, ValueError):
            sz = 0
        if not name or sz <= 0:
            continue
        rows.append({'name': name, 'size': sz, 'start': p.get('sysfs_start')})
    if not rows:
        raise PortError('分区表是空的，生成不了 scatter')

    pre = 0
    for r in rows:
        if r['name'].lower() == 'preloader':
            pre = r['size']
            break
    acc, n_ok, n_bad = 0, 0, []
    for r in rows:
        if r['start'] is not None and r['name'].lower() != 'bmtpool':
            if r['start'] + pre == acc:
                n_ok += 1
            else:
                n_bad.append('%s(实际 0x%X / 累加 0x%X)' % (r['name'], r['start'] + pre, acc))
        acc += r['size']
    if n_ok or n_bad:
        log('   · 偏移校验：%d 项与 sysfs 一致%s'
            % (n_ok, '，%d 项不一致：%s' % (len(n_bad), '、'.join(n_bad)) if n_bad else ''))
    log('   · 偏移 = 从 preloader 起按分区顺序累加（与原厂 scatter 逐项一致的算法）')
    return _render(rows, images, platform, project, storage, boot_channel, False)


def gen_from_dumchar(dumchar_text, images=None, platform='MT6572',
                     project='auto', storage='EMMC', boot_channel='MSDC_0'):
    """返回 scatter 文本。images = {分区名小写: 本地镜像路径}（决定 file_name/is_download）。"""
    from .scatter import parse_dumchar

    rows = parse_dumchar(dumchar_text)
    if not rows:
        raise PortError('dumchar 里一个分区都没解析出来')
    for r in rows:
        r.setdefault('start', None)
    return _render(rows, images, platform, project, storage, boot_channel, False)


def _render(rows, images=None, platform='MT6572', project='auto',
            storage='EMMC', boot_channel='MSDC_0', use_real_start=False):
    """真正写 scatter 文本。rows: [{'name','size','start'}]。"""
    images = {k.lower(): v for k, v in (images or {}).items()}
    lines = ['#########################################__mtkport__###################################################',
             '#',
             '#  General Setting',
             '#',
             '#########################################__mtkport__###################################################',
             '- general: MTK_PLATFORM_CFG',
             '  info: ',
             '    - config_version: V1.1.1',
             '      platform: %s' % platform,
             '      project: %s' % project,
             '      storage: %s' % storage,
             '      boot_channel: %s' % boot_channel,
             '      block_size: 0x20000',
             '############################################################################################################',
             '#',
             '#  Layout Setting',
             '#',
             '############################################################################################################']
    acc = 0
    used = set()
    for i, r in enumerate(rows):
        name = r['name']
        typ, region, op = classify(name)
        img = images.get(name.lower())
        used.add(name.lower())
        if use_real_start and r.get('start') is not None:
            off = int(r['start'])
            if off != acc:
                log('   ! %s 真实起址 0x%X 与累加值 0x%X 不一致，按真实起址写'
                    % (name.upper(), off, acc))
        else:
            off = acc
        lines += [
            '- partition_index: SYS%d' % i,
            '  partition_name: %s' % name.upper(),
            '  file_name: %s' % (os.path.basename(img) if img else 'NONE'),
            '  is_download: %s' % ('true' if img else 'false'),
            '  type: %s' % typ,
            '  linear_start_addr: 0x%X' % off,
            '  physical_start_addr: 0x%X' % off,
            '  partition_size: 0x%X' % r['size'],
            '  region: %s' % region,
            '  storage: HW_STORAGE_%s' % storage,
            '  boundary_check: true',
            '  is_reserved: %s' % ('true' if op == 'RESERVED' else 'false'),
            '  operation_type: %s' % op,
        ]
        if name.lower() == 'preloader':
            lines += ['  is_upgradable: true', '  empty_boot_needed: false']
        lines += ['  reserve: 0x00', '']
        acc = off + r['size']
    for k in images:
        if k not in used:
            log('   ! 镜像对应的分区 %s 在 dumchar 里不存在，忽略' % k)
    return '\n'.join(lines) + '\n'


def write_scatter(text, out_path):
    """scatter 用 GBK 写（SPFT 只认 ANSI）。"""
    data = text.encode('gbk', 'replace')
    io.open(out_path, 'wb').write(data)
    return out_path


def compare(a_rows, b_rows):
    """对比两张分区表：名字、起址、大小、勾选。返回 (ok, 差异列表)。"""
    da = {r['partition_name'].lower(): r for r in a_rows}
    db = {r['partition_name'].lower(): r for r in b_rows}
    diffs = []
    for k in sorted(set(da) | set(db)):
        ra, rb = da.get(k), db.get(k)
        if ra is None:
            diffs.append('%s 只在第二张表里' % k)
            continue
        if rb is None:
            diffs.append('%s 只在第一张表里' % k)
            continue
        if (int(ra['linear_start_addr'], 16) != int(rb['linear_start_addr'], 16)
                or int(ra['partition_size'], 16) != int(rb['partition_size'], 16)):
            diffs.append('%s 偏移/大小不同：%s/%s vs %s/%s' % (
                k, ra['linear_start_addr'], ra['partition_size'],
                rb['linear_start_addr'], rb['partition_size']))
    return (not diffs), diffs
