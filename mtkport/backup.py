# -*- coding: utf-8 -*-
"""把设备上的分区原样备份到本地，带校验和清单。

为什么用 dd 读分区而不是"复制文件"：备份要的是**能刷回去的原始镜像**。
所以按分区表（MTK 的 /proc/dumchar_info）逐块读：
    · MapTo 是 /dev/block/mmcblk0pN  -> 直接 dd 这个节点
    · MapTo 是 /dev/block/mmcblk0    -> dd 整盘 + skip/count（StartAddr 就是字节偏移）
读完在本地算 md5，同时让设备再算一遍同一区间的 md5，两边对上才算备份成功。

清单（manifest.json）里记下：设备档案、每个分区的起址/大小/md5/文件路径，
以及 dumchar_info 原文 —— 以后要还原，靠这份清单就能对上号。
"""
import json
import os
import subprocess
import time

from .util import PortError, log, md5, human_bytes, size

# 默认备份哪些：能还原一台机器的"骨架"，但不碰 userdata（几个 G，且没意义）
DEFAULT_PARTS = ['pro_info', 'nvram', 'protect_f', 'protect_s', 'seccfg', 'uboot',
                 'bootimg', 'recovery', 'logo', 'expdb', 'misc', 'metadata', 'cache',
                 'system']
# 大分区：默认不备（system 例外，见上面；userdata 一定要显式要）
HUGE_PARTS = ['usrdata', 'userdata', 'internal_sd', 'fat']


def _part_size(p):
    return p.get('size') or 0


def pick_parts(prof, want=None, include_system=True, include_userdata=False):
    parts = {p['name']: p for p in prof.get('partitions') or []}
    if not parts:
        raise PortError('拿不到分区表（需要 root；MTK 用 /proc/dumchar_info）')
    names = list(want) if want else list(DEFAULT_PARTS)
    if not include_system:
        names = [n for n in names if n not in ('system', 'android')]
    if include_userdata:
        names += HUGE_PARTS
    out = []
    for n in names:
        for pname, p in parts.items():
            if pname.lower() == n.lower() or (n == 'system' and pname.lower() == 'android'):
                out.append(p)
                break
    if not out:
        raise PortError('这些分区一个都没找到：%s' % '、'.join(names))
    return out


def _dd_cmd(dev, part):
    """拼出读这个分区的 dd 命令（在设备上跑，输出到 stdout）。"""
    mp = (part.get('map_to') or '').strip()
    bs = 4096
    start = part.get('start') or 0
    n = _part_size(part)
    if mp and mp.startswith('/dev/block/') and not mp.endswith('/mmcblk0'):
        # 独立分区节点，大小由节点自身决定
        return 'dd if=%s bs=%d 2>/dev/null' % (mp, bs)
    if not n:
        raise PortError('分区 %s 没有大小信息，没法按偏移读' % part['name'])
    if start % bs or n % bs:
        # 极端情况：起址/大小不是 4K 对齐，改用 512
        bs = 512
    return ('dd if=/dev/block/mmcblk0 bs=%d skip=%d count=%d 2>/dev/null'
            % (bs, start // bs, n // bs))


def backup(dev, prof, out_dir, want=None, include_system=True, include_userdata=False,
           verify=True):
    parts = pick_parts(prof, want, include_system, include_userdata)
    os.makedirs(out_dir, exist_ok=True)
    total = sum(_part_size(p) for p in parts)
    log('■ 备份 %d 个分区，共约 %s → %s' % (len(parts), human_bytes(total), out_dir))
    rows = []
    for p in parts:
        name = p['name']
        fname = name + '.img'
        fpath = os.path.join(out_dir, fname)
        cmd = _dd_cmd(dev, p)
        log('   %-10s %10s B  ← %s' % (name, size(_part_size(p)), cmd))
        t0 = time.time()
        rc, out = _stream(dev, cmd, fpath)
        if rc != 0:
            raise PortError('读 %s 失败（rc=%d）：%s' % (name, rc, out.strip()[-200:]))
        got = os.path.getsize(fpath)
        dt = max(time.time() - t0, 0.001)
        m = md5(fpath)
        log('      %s  %s  (%.1f MB/s)  md5=%s' % (human_bytes(got), '%.0fs' % dt,
                                                   got / dt / 1048576.0, m))
        if _part_size(p) and got != _part_size(p):
            raise PortError('%s 大小不对：备份到 %d B，分区表说 %d B'
                            % (name, got, _part_size(p)))
        if verify:
            drc, dmd5 = dev.sh(cmd + ' | md5sum', timeout=1800)
            tok = dmd5.split()[0] if dmd5.strip() else ''
            if drc == 0 and tok and tok != m:
                raise PortError('%s 校验失败：设备端 %s，本地 %s（读的时候可能被改动了）'
                                % (name, tok, m))
            log('      ✓ 设备端 md5 %s 与本地一致' % tok if tok == m else '      ! 设备端没给出 md5，跳过校验')
        rows.append({'name': name, 'file': fname, 'size': got, 'md5': m,
                     'partition_size': _part_size(p), 'start': p.get('start'),
                     'map_to': p.get('map_to'), 'cmd': cmd})
    manifest = {'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                'device': {k: prof.get(k) for k in ('serial', 'product', 'chipset',
                                                    'kernel', 'android', 'display',
                                                    'memory_mb')},
                'files': rows}
    mpath = os.path.join(out_dir, 'manifest.json')
    with open(mpath, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    # 分区表原文也存一份，还原时能对上
    rc, dc = dev.sh('cat /proc/dumchar_info')
    if rc == 0:
        with open(os.path.join(out_dir, 'dumchar_info.txt'), 'w', encoding='utf-8') as f:
            f.write(dc)
    rc, bp = dev.sh('cat /system/build.prop')
    if rc == 0:
        with open(os.path.join(out_dir, 'build.prop.txt'), 'w', encoding='utf-8') as f:
            f.write(bp)
    log('   ✓ 备份完成，清单 %s' % mpath)
    log('   总计 %s' % human_bytes(sum(r['size'] for r in rows)))
    return manifest


def _stream(dev, shell_cmd, out_file):
    """把设备上的命令输出（二进制）直接落盘，走 adb exec-out 避免换行被改。"""
    adb = dev.adb_path
    cmd = [adb, '-P', str(dev.adb_port), '-s', dev.serial, 'exec-out', shell_cmd]
    with open(out_file, 'wb') as f:
        pr = subprocess.Popen(cmd, stdout=f, stderr=subprocess.PIPE)
        err = pr.stderr.read() if pr.stderr else b''
        rc = pr.wait()
    return rc, err.decode('utf-8', 'replace')


def verify_backup(out_dir):
    """复核一个备份目录：清单 vs 实际文件。"""
    mpath = os.path.join(out_dir, 'manifest.json')
    if not os.path.isfile(mpath):
        raise PortError('%s 里没有 manifest.json' % out_dir)
    with open(mpath, encoding='utf-8') as f:
        man = json.load(f)
    bad = 0
    for r in man['files']:
        fp = os.path.join(out_dir, r['file'])
        if not os.path.isfile(fp):
            log('   ✗ 缺文件 %s' % r['file'])
            bad += 1
            continue
        if os.path.getsize(fp) != r['size']:
            log('   ✗ 大小变了 %s' % r['file'])
            bad += 1
            continue
        if md5(fp) != r['md5']:
            log('   ✗ md5 变了 %s' % r['file'])
            bad += 1
            continue
        log('   ✓ %-12s %10s B  %s' % (r['name'], size(r['size']), r['md5']))
    log('   %s' % ('全部一致' if bad == 0 else '%d 个文件有问题' % bad))
    return bad
