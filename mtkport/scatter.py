# -*- coding: utf-8 -*-
"""MTK 分区表(scatter)读写，以及整盘 dump 的分区偏移核对。

两个关键事实（本工具把它们固化下来，避免再踩坑）：
 1. /proc/dumchar_info 里的 StartAddr 就是 /dev/block/mmcblk0 的**字节偏移**，
    可以直接拿来做 dd / 十六进制核对；而 scatter 里的 linear_start_addr
    属于"flasher 空间"，和 dumchar 差一个常量（本机实测 0xC00000）。
 2. scatter 是 GBK 编码、CRLF 换行；改写必须逐行原地改，不要重新序列化，
    否则 SPFlashTool 会认不出来。
"""
import os
import re

from .util import PortError, log, read_all, size, write_all

PART_KEYS = ('partition_index', 'partition_name', 'file_name', 'is_download', 'type',
             'linear_start_addr', 'physical_start_addr', 'partition_size', 'region',
             'storage', 'boundary_check', 'is_reserved', 'operation_type', 'reserve')


def load(path):
    raw = read_all(path)
    for enc in ('gbk', 'utf-8-sig', 'utf-8'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise PortError('%s 编码无法识别' % path)
    newline = '\r\n' if '\r\n' in text else '\n'
    return text, enc, newline


def parse(path):
    """返回 partition 字典列表，每项含 value 与 line（该分区块起始行号）。"""
    text, _enc, _nl = load(path)
    lines = text.split('\n')
    parts, cur = [], None
    for i, line in enumerate(lines):
        m = re.match(r'\s*-\s*partition_index:\s*(\S+)', line)
        if m:
            cur = {'partition_index': m.group(1), 'line': i, '_lines': {}}
            parts.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r'\s*(\w+):\s*(.*?)\s*$', line)
        if m and m.group(1) in PART_KEYS:
            cur[m.group(1)] = m.group(2)
            cur['_lines'][m.group(1)] = i
    return parts


def summary(path):
    parts = parse(path)
    log('■ %s  共 %d 个分区' % (path, len(parts)))
    log('   %-10s %-4s %-8s %-18s %-10s %s'
        % ('名称', '下载', '大小', '起址(scatter)', 'storage', '文件名'))
    for p in parts:
        log('   %-10s %-4s %-9s %-18s %-10s %s'
            % (p.get('partition_name', '?'),
               '✓' if p.get('is_download', '').lower() == 'true' else '',
               p.get('partition_size', '?'),
               p.get('linear_start_addr', '?'),
               p.get('storage', '').replace('HW_STORAGE_', ''),
               p.get('file_name', '')))
    return parts


def make_flash_dir(scatter_path, only, out_dir, images=None):
    """生成"只刷某几个分区"的刷机文件夹。

    only   : 要勾选下载的分区名（如 ['BOOTIMG']），其余强制 is_download: false
    images : {分区名: 镜像文件路径}，会把镜像复制进 out_dir 并改 file_name
    """
    text, enc, nl = load(scatter_path)
    lines = text.split('\r\n') if nl == '\r\n' else text.split('\n')
    only = [o.upper() for o in only]
    images = images or {}
    cur = None
    for i, line in enumerate(lines):
        m = re.search(r'partition_name:\s*(\S+)', line)
        if m:
            cur = m.group(1).upper()
            continue
        if cur is None:
            continue
        if re.match(r'\s*is_download:', line):
            want = 'true' if cur in only else 'false'
            lines[i] = re.sub(r'(is_download:\s*)\S+', lambda _m: _m.group(1) + want, line)
        elif re.match(r'\s*file_name:', line) and cur in images:
            base = os.path.basename(images[cur])
            lines[i] = re.sub(r'(file_name:\s*)\S+', lambda _m: _m.group(1) + base, line)
    new_text = nl.join(lines)
    os.makedirs(out_dir, exist_ok=True)
    out_scat = os.path.join(out_dir, os.path.basename(scatter_path))
    write_all(out_scat, new_text.encode(enc, 'replace'))
    for name, src in images.items():
        if not os.path.isfile(src):
            raise PortError('镜像不存在：%s' % src)
        dst = os.path.join(out_dir, os.path.basename(src))
        write_all(dst, read_all(src))
        log('   放入 %s（%s B）' % (os.path.basename(dst), size(os.path.getsize(dst))))
    log('   scatter → %s（勾选：%s）' % (out_scat, '、'.join(only) or '无'))
    return out_dir


def check(scatter_path, folder):
    """核对：文件夹里每个被勾选的镜像 ≤ 对应分区大小，未勾选的分区不应放文件。"""
    parts = parse(scatter_path)
    by_name = {p['partition_name'].upper(): p for p in parts}
    problems, checked = [], 0
    for f in sorted(os.listdir(folder)):
        fp = os.path.join(folder, f)
        if not os.path.isfile(fp) or f.lower() in ('mtk_allinone_da.bin', 'da_6735_all.bin'):
            continue
        owner = None
        for p in parts:
            if p.get('file_name') and os.path.basename(p['file_name']).lower() == f.lower():
                owner = p
                break
        if owner is None:
            continue
        checked += 1
        want = int(owner['partition_size'], 16) if owner.get('partition_size', '').startswith('0x') \
            else None
        got = os.path.getsize(fp)
        dl = owner.get('is_download', '').lower() == 'true'
        if want and got > want:
            problems.append('%s: 镜像 %s B 超过分区 %s B' % (f, size(got), size(want)))
        log('   %-28s %-9s 分区 %-10s %s'
            % (f, size(got), owner['partition_name'], '✓ 会刷' if dl else '· 不刷'))
    if not checked:
        problems.append('文件夹里没有与 scatter 对应的镜像')
    for p in problems:
        log('   ✗ ' + p)
    return problems


# ---------------- 整盘 dump 的偏移核对 ----------------
def _num(s):
    """dumchar 里的数字都可能是 0x 开头的十六进制，也可能是十进制。"""
    s = s.strip()
    try:
        return int(s, 16) if s.lower().startswith('0x') else int(s)
    except ValueError:
        return None


def parse_dumchar(text):
    """解析 /proc/dumchar_info：name size(hex) StartAddr(hex) type ...

    真实长这样（Size 与 StartAddr 都是 0x 开头的十六进制）：
        preloader   0x0000000000040000   0x0000000000000000   ssd
        bootimg     0x0000000000600000   0x0000000001D80000   ssd
    注意：StartAddr 就是 /dev/block/mmcblk0 的字节偏移（已用 MBR/ANDROID!/ext4 三处标志物验证）。
    """
    rows = []
    for line in text.replace('\r\n', '\n').split('\n'):
        f = line.split()
        if len(f) < 4:
            continue
        name, size_s, addr_s, typ = f[0], f[1], f[2], f[3]
        if not addr_s.lower().startswith('0x'):
            continue
        sz, st = _num(size_s), _num(addr_s)
        if sz is None or st is None:
            continue
        rows.append({'name': name, 'size': sz, 'start': st, 'type': typ})
    return rows


def dump_landmarks(dump_path, dumchar_text):
    """在整盘 dump 里按 dumchar 的 StartAddr 找各分区特征魔数，验证偏移理论。"""
    rows = parse_dumchar(dumchar_text)
    out = []
    with open(dump_path, 'rb') as f:
        for r in rows:
            f.seek(r['start'])
            head = f.read(0x500)
            tag = '?'
            why = ''
            if head[:8] == b'ANDROID!':
                tag, why = 'boot.img', 'ANDROID! 在偏移上'
            elif len(head) >= 0x43A and head[0x438:0x43A] == b'\x53\xef':
                tag, why = 'ext4', 'ext4 超级块魔数在 +0x438'
            elif head[0x1FE:0x200] == b'\x55\xAA':
                tag, why = 'fat/mbr', '55AA 在 +0x1FE'
            elif head[:4] == b'\x88\x16\x88\x58':
                tag, why = 'mtk-tag', 'MTK 标签 0x58881688'
            r = dict(r)
            r.update({'magic': tag, 'why': why})
            out.append(r)
    return out


def report_landmarks(rows):
    STARTED = ('bootimg', 'recovery')
    for r in sorted(rows, key=lambda x: x['start']):
        nm = r['name'].lower()
        hit = r['magic'] == 'boot.img'
        if nm in STARTED:
            log('   %-10s @0x%-9X %-9s %s' % (r['name'], r['start'], r['magic'],
                                              '✓' if hit else '✗ 期望 boot.img'))
        elif r['magic'] != '?':
            log('   %-10s @0x%-9X %-9s %s' % (r['name'], r['start'], r['magic'], r['why']))
