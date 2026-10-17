# -*- coding: utf-8 -*-
"""命令行界面：把 boot.img / system.img / scatter 的移植操作都收成一条命令。"""
import argparse
import io
import json
import os
import sys

from . import bootimg, cpio, rules as rules_mod, scatter as scatter_mod, systemimg
from .util import PortError, log, md5, size


def _rules_path(p):
    if not os.path.isfile(p):
        raise PortError('规则文件不存在：%s' % p)
    return p


def cmd_info(a):
    for p in a.images:
        bootimg.BootImg(p).report()
        log('   md5 = %s' % md5(p))
        log('')
    return 0


def cmd_unpack(a):
    b = bootimg.BootImg(a.image)
    out = b.unpack(a.out)
    b.report()
    log('   解包 → %s' % out)
    log('   kernel / ramdisk.cpio / ramdisk.mtktag / ramdisk-tree/ / bootimg.json')
    return 0


def cmd_ls(a):
    b = bootimg.BootImg(a.image)
    ent = cpio.parse(b.cpio_bytes)
    import re
    pat = re.compile(a.pattern) if a.pattern else None
    n = 0
    for e in ent:
        if pat and not pat.search(e.name):
            continue
        n += 1
        kind = 'd' if e.is_dir else ('l' if e.is_link else '-')
        extra = ('-> ' + e.text()[:60]) if e.is_link else ''
        log('   %s %s %8d %s %s' % (kind, oct(e.perm)[2:].rjust(4), len(e.data), e.name, extra))
    log('   共 %d 个条目（全 ramdisk %d）' % (n, len(ent)))
    return 0


def cmd_cat(a):
    b = bootimg.BootImg(a.image)
    e = cpio.find(cpio.parse(b.cpio_bytes), a.entry)
    sys.stdout.write(e.text())
    if not e.text().endswith('\n'):
        sys.stdout.write('\n')
    return 0


def cmd_patch(a):
    b = bootimg.BootImg(a.image)
    rules = rules_mod.load_rules(_rules_path(a.rules))
    base_dir = os.path.dirname(os.path.abspath(a.rules))
    ent = cpio.parse(b.cpio_bytes)
    before = {e.name: e.data for e in ent}
    ent, rep = rules_mod.apply_to_ramdisk(ent, rules, base_dir)
    log('■ 应用规则 %s' % a.rules)
    rep.dump()
    changed = [e.name for e in ent if before.get(e.name) != e.data]
    log('   内容变化的条目：%s' % ('、'.join(changed) if changed else '（无）'))
    if a.dry_run:
        log('   --dry-run：只检查规则，不写镜像')
        return 0 if rep.fail == 0 else 2
    if rep.fail and not a.force:
        raise PortError('有 %d 条规则没生效，已中止（确认没问题可加 --force）' % rep.fail)
    if not changed and not a.force:
        raise PortError('规则跑完但没有任何条目变化 —— 多半是"以为改了其实没改"，'
                        '请核对上面的报告（要硬来加 --force）')
    mode = a.mode or (rules.get('boot', {}).get('mode') if isinstance(rules, dict) else None) \
        or 'inplace'
    new_cpio = cpio.serialize(ent)
    # 自检：重新解析必须干净
    again = cpio.parse(new_cpio)
    if len(again) < 2:
        raise PortError('生成的 cpio 反常（解析出 %d 条）' % len(again))
    data = b.build(new_cpio, a.out, mode=mode, keep_size=not a.no_keep_size)
    log('   写出 %s（%s B）md5=%s' % (a.out, size(len(data)), md5(data)))
    # 独立复核：把刚写出的文件重新解析一遍，逐项核对
    nb = bootimg.BootImg(a.out)
    probs = []
    if nb.cpio_bytes != new_cpio:
        probs.append('ramdisk 解压结果和预期 cpio 不一致')
    if nb.kernel != b.kernel:
        probs.append('kernel 被改动')
    if (nb.page_size, nb.kernel_addr, nb.ramdisk_addr, nb.tags_addr, nb.second_addr) != \
            (b.page_size, b.kernel_addr, b.ramdisk_addr, b.tags_addr, b.second_addr):
        probs.append('头部地址字段被改动')
    if mode == 'inplace' and len(nb.raw) != len(b.raw):
        probs.append('inplace 模式下镜像长度变了（%d → %d）' % (len(b.raw), len(nb.raw)))
    if probs:
        log('   ✗ 复核发现问题：%s' % '；'.join(probs))
        return 3
    log('   ✓ 复核：内核未变、地址未变、ramdisk 解压结果与预期一致')
    return 0


def cmd_pack(a):
    """把一个已解包的目录重新打回 boot.img（不依赖 --ref 时用 bootimg.json 里的头部）。"""
    d = a.dir
    kern = os.path.join(d, 'kernel')
    if not os.path.isfile(kern):
        raise PortError('%s 里没有 kernel' % d)
    cpio_path = os.path.join(d, 'ramdisk.cpio')
    tree = os.path.join(d, 'ramdisk-tree')
    if a.from_tree:
        if not os.path.isdir(tree):
            raise PortError('%s 里没有 ramdisk-tree' % d)
        if not a.ref:
            raise PortError('--from-tree 需要 --ref：新文件要借原 ramdisk 的属主/权限/顺序')
        ref = bootimg.BootImg(a.ref)
        ent_meta = cpio.parse(ref.cpio_bytes)
        ent = cpio.from_tree(tree, ent_meta)
        raw = cpio.serialize(ent)
        log('   从目录树重建 cpio：%d 个条目（原 %d）' % (len(ent), len(ent_meta)))
    elif os.path.isfile(cpio_path):
        from .util import read_all
        raw = read_all(cpio_path)
        cpio.parse(raw)          # 校验一下格式
    else:
        raise PortError('%s 里既没有 ramdisk.cpio 也没有 ramdisk-tree' % d)
    if a.ref:
        b = bootimg.BootImg(a.ref)
        data = b.build(raw, a.out, mode=a.mode or 'inplace', keep_size=not a.no_keep_size)
    else:
        b = _stub_from_json(d)
        data = b._rebuild_from_file(raw, a.out)
    log('   写出 %s（%s B）md5=%s' % (a.out, size(len(data)), md5(data)))
    nb = bootimg.BootImg(a.out)
    if nb.cpio_bytes != raw:
        log('   ✗ 复核失败：打出来的镜像里 cpio 内容和输入不一致')
        return 3
    log('   ✓ 复核：cpio 条目数 %d，内容与输入逐字节一致'
        % len(cpio.parse(nb.cpio_bytes)))
    return 0


def _stub_from_json(d):
    """用 bootimg.json 造一个"空壳"镜像对象，只用来重打包。"""
    jp = os.path.join(d, 'bootimg.json')
    if not os.path.isfile(jp):
        raise PortError('%s 里没有 bootimg.json，请用 --ref 指定原镜像' % d)
    with open(jp, 'rb') as f:
        meta = json.loads(f.read().decode('utf-8-sig'))
    b = bootimg.BootImg()
    b.path = None
    for k in ('kernel_size', 'kernel_addr', 'ramdisk_addr', 'second_size', 'second_addr',
              'tags_addr', 'page_size', 'name', 'cmdline', 'extra_cmdline'):
        if k in meta:
            setattr(b, k, meta[k])
    b.kernel = bootimg.read_all(os.path.join(d, 'kernel'))
    b.kernel_size = len(b.kernel)
    tag = os.path.join(d, 'ramdisk.mtktag')
    b.ramdisk_prefix = bootimg.read_all(tag) if os.path.isfile(tag) else b''
    b.ramdisk_gz = b''
    b.cpio_bytes = b''
    b.ramdisk_size = 0
    sec = os.path.join(d, 'second')
    b.second = bootimg.read_all(sec) if os.path.isfile(sec) else b''
    b.second_size = len(b.second)
    return b


def _rebuild_from_file(self, cpio_bytes, out):
    lvl, gz = bootimg.gz_compress_best(cpio_bytes)
    self.cpio_bytes = cpio_bytes
    self.ramdisk_gz = gz
    data = self._rebuild(gz, keep_size=False)
    bootimg.write_all(out, data)
    log('   rebuild（无参考镜像）：gzip -%d %s B，镜像 %s B'
        % (lvl, size(len(gz)), size(len(data))))
    return data


bootimg.BootImg._rebuild_from_file = _rebuild_from_file


def cmd_sys_extract(a):
    systemimg.extract(a.image, a.out, force=a.force)
    return 0


def cmd_sys_pack(a):
    systemimg.pack(a.dir, a.out, a.size, rom_zip=a.rom, label=a.label,
                   mount_point=a.mount_point)
    log('   md5 = %s' % md5(a.out))
    return 0


def cmd_sys_verify(a):
    res = systemimg.verify_tree(a.image, a.tree)
    return 0 if (res['diff'] == 0 and res['missing'] == 0) else 2


def cmd_sys_merge(a):
    with open(a.rules, 'rb') as f:
        spec = json.loads(f.read().decode('utf-8-sig'))
    spec = spec.get('system', spec)
    log('■ 合并：原厂 %s + 移植包 %s' % (a.stock, a.donor))
    systemimg.merge(a.stock, a.donor, a.out, spec)
    log('   结果 → %s' % a.out)
    return 0


def cmd_scatter_summary(a):
    scatter_mod.summary(a.scatter)
    return 0


def cmd_scatter_flash(a):
    images = {}
    for item in a.images or []:
        if '=' not in item:
            raise PortError('--images 要写成 分区名=文件路径，例如 BOOTIMG=boot.img')
        k, v = item.split('=', 1)
        images[k.upper()] = v
    scatter_mod.make_flash_dir(a.scatter, a.only, a.out, images)
    probs = scatter_mod.check(os.path.join(a.out, os.path.basename(a.scatter)), a.out)
    return 2 if probs else 0


def cmd_scatter_check(a):
    probs = scatter_mod.check(a.scatter, a.dir)
    return 2 if probs else 0


def cmd_dump_landmarks(a):
    from .util import read_all
    text = read_all(a.dumchar).decode('utf-8', 'replace')
    rows = scatter_mod.dump_landmarks(a.dump, text)
    log('■ 在各分区起址上找特征魔数（StartAddr 当 mmcblk0 字节偏移用）')
    scatter_mod.report_landmarks(rows)
    bad = [r for r in rows if r['name'].lower() in ('bootimg', 'recovery')
           and r['magic'] != 'boot.img']
    if bad:
        log('   ✗ %d 个 boot 类分区偏移对不上，说明这份 dumchar 不属于该 dump'
            % len(bad))
        return 2
    return 0


def cmd_scan(a):
    from . import device as dev_mod
    dev = dev_mod.Device.pick(a.serial, a.adb_port)
    prof = dev.scan()
    dev_mod.Device.report(prof)
    if a.save:
        dev_mod.Device.save(prof, a.save)
        log('   档案已存 %s' % a.save)
    if not prof['root']:
        log('   ! 没有 root：分区表读不到，备份也用不了（先 adb root 或刷带 root 的包）')
    return 0


def cmd_roms(a):
    from . import device as dev_mod, romrepo
    if a.profile:
        with open(a.profile, encoding='utf-8') as f:
            prof = json.load(f)
    else:
        prof = dev_mod.Device.pick(a.serial, a.adb_port).scan()
    repo = romrepo.Repo()
    cands, plat, kdirs = repo.candidates(prof, android=a.android, limit=a.limit,
                                           platform=getattr(a, 'platform', None),
                                           allow_kernel_mismatch=getattr(a, 'any_kernel', False))
    log('■ 平台目录：%s' % plat)
    log('   站内该芯片的内核/版本：%s' % '、'.join(kdirs))
    log('   本机：内核 %s  Android %s  %d MB 内存  型号 %s'
        % (prof['kernel']['release'], prof['android']['release'],
           prof['memory_mb'], prof['product']['model']))
    log('')
    log('   推荐顺序（分数越高越合适）：')
    for i, c in enumerate(cands, 1):
        log('   %2d. %-6.1f %8s  %s' % (i, c['score'],
                                        '%.0fMB' % (c['size'] / 1048576.0), c['name']))
        log('        理由：%s' % c['why'])
        log('        路径：%s' % c['path'])
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(cands, f, ensure_ascii=False, indent=2)
        log('   候选已存 %s' % a.json)
    return 0


def cmd_fetch(a):
    from . import romrepo
    repo = romrepo.Repo()
    best = a.path
    if not best:
        from . import device as dev_mod
        if a.profile:
            with open(a.profile, encoding='utf-8') as f:
                prof = json.load(f)
        else:
            prof = dev_mod.Device.pick(a.serial, a.adb_port).scan()
        cands, _plat, _kd = repo.candidates(
            prof, android=a.android, limit=1,
            platform=getattr(a, 'platform', None),
            allow_kernel_mismatch=getattr(a, 'any_kernel', False))
        if not cands:
            raise PortError('没有候选包可下')
        best = cands[0]['path']
        log('■ 自动选中：%s' % best)
    out = a.out or os.path.join('downloads', os.path.basename(best))
    repo.download(best, out)
    return 0


def cmd_backup(a):
    from . import backup as bk, device as dev_mod
    dev = dev_mod.Device.pick(a.serial, a.adb_port)
    if not dev.is_root():
        log('   想拿 root（adbd 会重启一下）…')
        if not dev.root():
            raise PortError('拿不到 root，读不了分区（先 adb root，或用带 root 的包）')
    prof = dev.scan()
    want = [x.strip() for x in a.parts.split(',')] if a.parts else None
    bk.backup(dev, prof, a.out, want=want, include_system=not a.no_system,
              include_userdata=a.with_userdata, verify=not a.no_verify)
    return 0


def cmd_backup_verify(a):
    from . import backup as bk
    return 0 if bk.verify_backup(a.dir) == 0 else 2


def cmd_auto(a):
    from . import device as dev_mod, pipeline
    if a.profile:
        with open(a.profile, encoding='utf-8') as f:
            prof = json.load(f)
    else:
        dev = dev_mod.Device.pick(a.serial, a.adb_port)
        prof = dev.scan()
        dev_mod.Device.report(prof)
    s = pipeline.run(prof, work=a.work, donor=a.donor, android=a.android,
                     stock_system=a.stock_system, scatter=a.scatter, only=a.only,
                     skip_backup=a.skip_backup, skip_fetch=a.skip_fetch,
                     usb_vid=a.usb_vid, usb_pid=a.usb_pid,
                     flash_mode=a.flash)
    return 0 if s.get('boot') else 2


def cmd_flash_check(a):
    from . import flash
    flash.check_dir(a.dir)
    return 0


def cmd_flash_open(a):
    from . import flash
    sc, n_bad = flash.check_dir(a.dir)
    if n_bad:
        raise PortError('自检没过（%d 处问题），不拉起刷机工具' % n_bad)
    flash.open_spft(a.dir, sc, a.spft)
    return 0


def cmd_flash_wait(a):
    from . import flash
    flash.wait_boot(a.serial, timeout=a.timeout)
    return 0


def cmd_scatter_gen(a):
    from . import scattergen
    from . import scatter as scatter_mod
    images = {}
    for item in (a.images or []):
        if '=' not in item:
            raise SystemExit('--images 要写成 分区名=镜像路径，比如 bootimg=boot.img')
        k, v = item.split('=', 1)
        images[k] = v
    if getattr(a, 'profile', None):
        with open(a.profile, encoding='utf-8') as f:
            prof = json.load(f)
        parts = prof.get('partitions') or []
        log('■ 数据来源：设备档案 %s 的分区表（%d 项）' % (a.profile, len(parts)))
        project = a.project
        if project == 'auto':
            project = (prof.get('product', {}) or {}).get('device') or 'auto'
        out_text = scattergen.gen_from_parts(parts, images=images,
                                             platform=a.platform, project=project)
    else:
        if not a.dumchar:
            raise SystemExit('要么给 /proc/dumchar_info 文件，要么用 --profile 指定设备档案')
        text = io.open(a.dumchar, encoding='utf-8', errors='replace').read()
        out_text = scattergen.gen_from_dumchar(text, images=images,
                                               platform=a.platform, project=a.project)
    scattergen.write_scatter(out_text, a.out)
    rows = scatter_mod.parse(a.out)
    log('■ 生成了 %d 个分区的 scatter：%s' % (len(rows), a.out))
    n_dl = 0
    for r in rows:
        flag = ''
        if str(r['is_download']).lower() == 'true':
            n_dl += 1
            flag = '  ← 会刷 ' + r['file_name']
        log('   %-10s %-12s %-12s %s%s' % (r['partition_name'], r['linear_start_addr'],
                                           r['partition_size'], r['type'], flag))
    log('   勾选要刷的分区：%d 个' % n_dl)
    if a.compare:
        ok, diffs = scattergen.compare(scatter_mod.parse(a.compare), rows)
        if ok:
            log('   ✓ 与 %s 的分区名/偏移/大小完全一致' % os.path.basename(a.compare))
        else:
            log('   ✗ 与参照 scatter 有 %d 处不同：' % len(diffs))
            for d in diffs[:12]:
                log('      %s' % d)
            return 2
    if not a.yes:
        log('\n   ！这是按 /proc/dumchar_info 推出来的表，刷前请用 scatter-summary 再核一眼。')
        log('     确认要用于刷机时，把这条命令加上 --yes 才会写文件。')
        return 0
    return 0


def cmd_doctor(a):
    from .doctor import doctor
    return doctor()


def cmd_flash_mtk(a):
    from . import mtkflash
    mtkflash.write(a.dir, mtk_dir=a.mtk, do_reset=not a.no_reset,
                   wait_prompt=not a.now)
    if not a.no_wait:
        mtkflash.wait_boot(a.serial, timeout=a.timeout)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog='mtkport',
        description='MTK(4.x/5.x) 系统移植工具箱：boot.img 解包重打包、声明式补丁、'
                    'system.img 合并重打包、scatter 生成与校验。',
        epilog='详细用法见同目录《用法说明.txt》；命令后面都可以加 -h 看单条说明。')
    p.add_argument('--version', action='version', version='mtkport 1.0')
    sub = p.add_subparsers(dest='cmd', metavar='命令')

    s = sub.add_parser('info', help='看 boot.img 结构（不解包）')
    s.add_argument('images', nargs='+')
    s.set_defaults(func=cmd_info)

    s = sub.add_parser('unpack', help='解包 boot.img 到目录')
    s.add_argument('image')
    s.add_argument('-o', '--out', required=True)
    s.set_defaults(func=cmd_unpack)

    s = sub.add_parser('ls', help='列 ramdisk 里的文件')
    s.add_argument('image')
    s.add_argument('pattern', nargs='?')
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser('cat', help='把 ramdisk 里某个文件打印出来')
    s.add_argument('image')
    s.add_argument('entry')
    s.set_defaults(func=cmd_cat)

    s = sub.add_parser('patch', help='用规则文件打补丁（最常用：改 fstab/props/rc）')
    s.add_argument('image')
    s.add_argument('--rules', required=True)
    s.add_argument('-o', '--out', required=True)
    s.add_argument('--mode', choices=['inplace', 'rebuild'])
    s.add_argument('--no-keep-size', action='store_true')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--force', action='store_true')
    s.set_defaults(func=cmd_patch)

    s = sub.add_parser('pack', help='把 unpack 出来的目录重新打包成 boot.img')
    s.add_argument('dir')
    s.add_argument('-o', '--out', required=True)
    s.add_argument('--ref', help='原镜像（推荐给：头部的 page/地址/MTK 标签都照它来）')
    s.add_argument('--from-tree', action='store_true',
                   help='从 ramdisk-tree 目录重建 cpio（顺手改了目录树里的文件时用这个）')
    s.add_argument('--mode', choices=['inplace', 'rebuild'])
    s.add_argument('--no-keep-size', action='store_true')
    s.set_defaults(func=cmd_pack)

    s = sub.add_parser('sys-extract', help='解包 system.img（sparse 也行）')
    s.add_argument('image')
    s.add_argument('-o', '--out', required=True)
    s.add_argument('--force', action='store_true')
    s.set_defaults(func=cmd_sys_extract)

    s = sub.add_parser('sys-pack', help='把文件树打包成 raw ext4 system.img')
    s.add_argument('dir')
    s.add_argument('-o', '--out', required=True)
    s.add_argument('--size', type=lambda x: int(x, 0), required=True,
                   help='分区字节数，和 scatter 的 partition_size 一致（可写 0x32000000）')
    s.add_argument('--rom', help='卡刷包 zip：从 updater-script 取精确的权限/SELinux/软链接')
    s.add_argument('--label', default='system')
    s.add_argument('--mount-point', default='system')
    s.set_defaults(func=cmd_sys_pack)

    s = sub.add_parser('sys-verify', help='把打好的 system.img 解出来，和源目录逐个比内容')
    s.add_argument('image')
    s.add_argument('--tree', required=True, help='打包时用的那个文件树')
    s.set_defaults(func=cmd_sys_verify)

    s = sub.add_parser('sys-merge', help='原厂 system 树 + 移植包 system 树 合并')
    s.add_argument('--stock', required=True)
    s.add_argument('--donor', required=True)
    s.add_argument('--rules', required=True)
    s.add_argument('-o', '--out', required=True)
    s.set_defaults(func=cmd_sys_merge)

    s = sub.add_parser('scatter-summary', help='列出 scatter 的分区表')
    s.add_argument('scatter')
    s.set_defaults(func=cmd_scatter_summary)

    s = sub.add_parser('scatter-flash', help='生成只刷某几个分区的刷机文件夹')
    s.add_argument('scatter')
    s.add_argument('--only', nargs='+', required=True, help='要刷的分区名，如 BOOTIMG')
    s.add_argument('--images', nargs='*', help='分区名=镜像路径')
    s.add_argument('-o', '--out', required=True)
    s.set_defaults(func=cmd_scatter_flash)

    s = sub.add_parser('scatter-check', help='核对文件夹里的镜像有没有超出分区大小')
    s.add_argument('scatter')
    s.add_argument('--dir', required=True)
    s.set_defaults(func=cmd_scatter_check)

    s = sub.add_parser('dump-landmarks', help='用 dumchar 的 StartAddr 在整盘 dump 里验偏移')
    s.add_argument('dump')
    s.add_argument('--dumchar', required=True, help='/proc/dumchar_info 的文本')
    s.set_defaults(func=cmd_dump_landmarks)

    s = sub.add_parser('scan', help='扫描连着 adb 的设备，生成设备档案')
    s.add_argument('--serial', help='指定设备序列号（不给就自动挑）')
    s.add_argument('--adb-port', type=int, default=5037)
    s.add_argument('--save', help='把档案存成 json')
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser('roms', help='从 kc.fuquan.moe 找合适的移植包并打分排序')
    s.add_argument('--profile', help='用已存的设备档案 json（不给就现场扫描）')
    s.add_argument('--serial')
    s.add_argument('--adb-port', type=int, default=5037)
    s.add_argument('--android', help='只要这个安卓版本，例如 7.1')
    s.add_argument('--limit', type=int, default=15)
    s.add_argument('--platform', help='强制用某个平台目录（站内没你芯片时指一个相近的）')
    s.add_argument('--any-kernel', action='store_true', help='没有同内核版本目录时就近挑一个（兜底）')
    s.add_argument('--json', help='把候选存成 json')
    s.set_defaults(func=cmd_roms)

    s = sub.add_parser('fetch', help='下载移植包（不给 --path 就自动选第一名）')
    s.add_argument('--path', help='站内路径，例如 /5.移植包大全/.../x.zip')
    s.add_argument('--profile')
    s.add_argument('--serial')
    s.add_argument('--adb-port', type=int, default=5037)
    s.add_argument('--android')
    s.add_argument('--platform', help='强制用某个平台目录')
    s.add_argument('--any-kernel', action='store_true', help='没有同内核版本目录时就近挑一个')
    s.add_argument('-o', '--out')
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser('backup', help='把设备上的分区原样备份到本地（带双端 md5 校验）')
    s.add_argument('-o', '--out', required=True, help='备份目录')
    s.add_argument('--parts', help='只备份这些分区，逗号分隔（默认一套骨架 + system）')
    s.add_argument('--no-system', action='store_true', help='不要 system（省 800MB）')
    s.add_argument('--with-userdata', action='store_true', help='连 userdata/fat 一起备（很大）')
    s.add_argument('--no-verify', action='store_true', help='跳过一次设备端 md5 复核（快）')
    s.add_argument('--serial')
    s.add_argument('--adb-port', type=int, default=5037)
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser('backup-verify', help='复核一个备份目录（清单 vs 实际文件）')
    s.add_argument('dir')
    s.set_defaults(func=cmd_backup_verify)

    s = sub.add_parser('auto', help='一键：扫描→选包→下载→备份→移植→出刷机目录')
    s.add_argument('--work', default=None, help='工作目录（默认 工具箱\\_工作区\\auto）')
    s.add_argument('--profile', help='用已存的设备档案（不给就现场扫描）')
    s.add_argument('--serial')
    s.add_argument('--adb-port', type=int, default=5037)
    s.add_argument('--donor', help='手动指定移植包（给了就不下载）')
    s.add_argument('--android', help='只要这个安卓版本，例如 7.1')
    s.add_argument('--stock-system', help='原厂 system.img（不给就用备份里的）')
    s.add_argument('--scatter', help='原厂 scatter 文件（给了才生成刷机目录）')
    s.add_argument('--only', default='BOOTIMG,ANDROID')
    s.add_argument('--flash', choices=['mtk', 'spft'],
                   help='生成后直接刷：mtk=命令行全自动，spft=拉起图形工具')
    s.add_argument('--skip-backup', action='store_true')
    s.add_argument('--skip-fetch', action='store_true')
    s.add_argument('--usb-vid', help='改 USB vid，例如 18D1（Windows 免驱动）')
    s.add_argument('--usb-pid', help='改 USB pid，例如 D001')
    s.set_defaults(func=cmd_auto)

    s = sub.add_parser('flash-check', help='刷机前自检（镜像有没有超分区、要刷哪几个）')
    s.add_argument('dir', help='刷机目录')
    s.set_defaults(func=cmd_flash_check)

    s = sub.add_parser('flash-open', help='自检通过后拉起 SP Flash Tool 并弹出刷机目录')
    s.add_argument('dir')
    s.add_argument('--spft', help='flash_tool.exe 的路径')
    s.set_defaults(func=cmd_flash_open)

    s = sub.add_parser('flash-wait', help='刷完之后等开机，把新系统的身份念出来')
    s.add_argument('--serial')
    s.add_argument('--timeout', type=int, default=600)
    s.set_defaults(func=cmd_flash_wait)

    s = sub.add_parser('flash-mtk', help='用 mtkclient 直写（BROM 模式，可无人值守）')
    s.add_argument('dir', help='刷机目录')
    s.add_argument('--mtk', help='mtkclient 根目录（含 runtime\\python.exe）')
    s.add_argument('--no-reset', action='store_true', help='写完不自动重启')
    s.add_argument('--now', action='store_true', help='设备已经在 BROM 里了，别等我提示')
    s.add_argument('--no-wait', action='store_true', help='写完不等开机验证')
    s.add_argument('--serial')
    s.add_argument('--timeout', type=int, default=600)
    s.set_defaults(func=cmd_flash_mtk)

    s = sub.add_parser('doctor', help='体检：这台电脑和设备够不够跑（缺什么直接说）')
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser('scatter-gen', help='没有原厂 scatter 时，按 /proc/dumchar_info 反推一张')
    s.add_argument('dumchar', nargs='?', help='设备的 /proc/dumchar_info 文本（用 --profile 可省）')
    s.add_argument('--profile', help='设备档案 json（里面有分区表，带 sysfs 交叉校验）')
    s.add_argument('-o', '--out', required=True, help='输出的 scatter 文件')
    s.add_argument('--images', nargs='*', help='分区名=镜像路径（写进去就勾选要刷）')
    s.add_argument('--platform', default='MT6572')
    s.add_argument('--project', default='auto')
    s.add_argument('--compare', help='和一张已知正确的 scatter 对比（验证推导对不对）')
    s.add_argument('--yes', action='store_true', help='确认可用于刷机')
    s.set_defaults(func=cmd_scatter_gen)
    return p


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, 'cmd', None):
        ap.print_help()
        return 1
    try:
        return a.func(a) or 0
    except PortError as e:
        log('')
        log('✗ ' + str(e))
        return 2
    except KeyboardInterrupt:
        log('\n已中断')
        return 130
