# -*- coding: utf-8 -*-
"""cpio(newc) 解析 / 序列化 / 目录树互转。逻辑与实测脚本逐字节对齐。

newc 每个条目：
  magic(6='070701') + 13 个 8 位十六进制字段 + 文件名 + '\\0' + 4 字节对齐 + 数据 + 4 字节对齐
字段顺序：ino mode uid gid nlink mtime filesize devmajor devminor rdevmajor rdevminor namesize check
"""
import os

from .util import PortError

FIELDS = ['ino', 'mode', 'uid', 'gid', 'nlink', 'mtime', 'filesize', 'devmajor',
          'devminor', 'rdevmajor', 'rdevminor', 'namesize', 'check']

S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFLNK = 0o120000
S_IFREG = 0o100000


class Entry(object):
    __slots__ = ('magic', 'fields', 'name', 'data')

    def __init__(self, name, data=b'', mode=S_IFREG | 0o644, uid=0, gid=0, nlink=1,
                 mtime=0, magic=b'070701'):
        self.magic = magic
        self.name = name
        self.data = data
        self.fields = {'ino': 0, 'mode': mode, 'uid': uid, 'gid': gid, 'nlink': nlink,
                       'mtime': mtime, 'filesize': len(data), 'devmajor': 0, 'devminor': 0,
                       'rdevmajor': 0, 'rdevminor': 0, 'namesize': len(name.encode('utf-8')) + 1,
                       'check': 0}

    # ---- 便捷属性 ----
    @property
    def is_dir(self):
        return (self.fields['mode'] & S_IFMT) == S_IFDIR

    @property
    def is_link(self):
        return (self.fields['mode'] & S_IFMT) == S_IFLNK

    @property
    def is_reg(self):
        return (self.fields['mode'] & S_IFMT) == S_IFREG

    @property
    def mode(self):
        return self.fields['mode']

    @property
    def perm(self):
        return self.fields['mode'] & 0o7777

    def text(self, enc='utf-8'):
        return self.data.decode(enc, 'replace')

    def set_text(self, s, enc='utf-8'):
        self.set_data(s.encode(enc))

    def set_data(self, b):
        self.data = b
        self.fields['filesize'] = len(b)

    def __repr__(self):
        return '<Entry %s %o %d B>' % (self.name, self.perm, len(self.data))


def parse(raw):
    """解析 cpio 字节流为 Entry 列表；遇到 TRAILER!!! 结束。"""
    ent, pos, n = [], 0, len(raw)
    while pos + 110 <= n:
        if raw[pos:pos + 6] not in (b'070701', b'070702'):
            # 允许前面有垃圾字节（某些 ramdisk 尾部填充）
            pos += 1
            continue
        h = raw[pos:pos + 110]
        v = {}
        try:
            for k, nm in enumerate(FIELDS):
                v[nm] = int(h[6 + k * 8:6 + (k + 1) * 8], 16)
        except ValueError:
            pos += 1
            continue
        ns = v['namesize']
        if ns <= 0 or pos + 110 + ns > n:
            pos += 1
            continue
        name = raw[pos + 110:pos + 110 + ns - 1].decode('utf-8', 'replace')
        dof = pos + 110 + ns
        dof += (-dof) % 4
        data = raw[dof:dof + v['filesize']]
        e = Entry(name, data, magic=h[:6])
        e.fields = v
        ent.append(e)
        if name == 'TRAILER!!!':
            break
        pos = dof + v['filesize']
        pos += (-pos) % 4
    if not ent:
        raise PortError('这段数据里找不到 cpio(newc) 条目，可能不是 cpio 或已损坏')
    return ent


def serialize(ent):
    """Entry 列表 -> cpio 字节流（结尾补 TRAILER 与 256 对齐）。"""
    out = bytearray()
    has_trailer = False
    for e in ent:
        nb = e.name.encode('utf-8') + b'\x00'
        e.fields['namesize'] = len(nb)
        e.fields['filesize'] = len(e.data)
        out += e.magic + b''.join(('%08x' % e.fields[f]).encode() for f in FIELDS) + nb
        out += b'\x00' * ((-len(out)) % 4)
        out += e.data
        out += b'\x00' * ((-len(out)) % 4)
        if e.name == 'TRAILER!!!':
            has_trailer = True
    if not has_trailer:
        t = Entry('TRAILER!!!', b'', mode=0)
        nb = t.name.encode('utf-8') + b'\x00'
        t.fields['namesize'] = len(nb)
        t.fields['filesize'] = 0
        out += t.magic + b''.join(('%08x' % t.fields[f]).encode() for f in FIELDS) + nb
        out += b'\x00' * ((-len(out)) % 4)
    out += b'\x00' * ((-len(out)) % 256)
    return bytes(out)


def to_tree(ent, root):
    """把 cpio 条目铺到目录树（符号链接写成文本目标文件，便于阅读/半自动修改）。"""
    os.makedirs(root, exist_ok=True)
    for e in ent:
        if e.name == 'TRAILER!!!':
            continue
        rel = e.name.lstrip('./').replace('/', os.sep)
        if not rel or rel == '.':
            continue
        p = os.path.join(root, rel)
        if e.is_dir:
            os.makedirs(p, exist_ok=True)
        elif e.is_link:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.lexists(p):
                os.remove(p)
            with open(p, 'wb') as f:
                f.write(b'!<symlink>' + e.data.decode('utf-8', 'replace').encode('utf-16'))
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'wb') as f:
                f.write(e.data)
    return root


def find(ent, name):
    for e in ent:
        if e.name == name or e.name == './' + name:
            return e
    raise PortError('ramdisk 里没有 %s（可用 mtkport ls 看全部条目）' % name)


def from_tree(root, meta_ents=None):
    """把目录树打回 cpio 条目。

    对"树里有的、原 ramdisk 也有的"文件，直接克隆原条目的全部头部字段
    （ino/mode/uid/gid/nlink/mtime/顺序/名字写法），只替换数据 —— 只要内容没改，
    打出来的镜像就和原包逐字节一致；树里新增的文件追加在末尾。
    """
    meta, order = {}, []
    tx = None
    for e in (meta_ents or []):
        if e.name.lstrip('./') == 'TRAILER!!!':
            tx = e
            continue
        k = e.name.lstrip('./')
        meta[k] = e
        order.append(k)
    root = os.path.abspath(root)
    out = []

    def clone(old, name, payload, default_mode):
        if old is not None:
            e = Entry(name, payload, magic=old.magic)
            e.fields = dict(old.fields)
            e.set_data(payload)
            return e
        return Entry(name, payload, mode=default_mode)

    def emit(rel, allow_dir=True):
        fp = os.path.join(root, rel.replace('/', os.sep))
        old = meta.get(rel)
        name = old.name if old is not None else rel
        if allow_dir and os.path.isdir(fp) and not os.path.isfile(fp):
            return clone(old, name, b'', S_IFDIR | 0o755)
        if not os.path.isfile(fp):
            return None
        with open(fp, 'rb') as f:
            data = f.read()
        if data[:10] == b'!<symlink>':
            target = data[10:].decode('utf-16', 'replace').rstrip('\0')
            return clone(old, name, target.encode('utf-8'), S_IFLNK | 0o777)
        perm = 0o755 if ('/bin/' in '/' + rel or '/xbin/' in '/' + rel) else 0o644
        return clone(old, name, data, S_IFREG | perm)

    for rel in order:
        if rel in ('TRAILER!!!', ''):
            continue
        e = emit(rel)
        if e is not None:
            out.append(e)
    done = set(order)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root).replace('\\', '/')
        for d in sorted(dirnames):
            rel = (rel_dir + '/' + d).lstrip('./').replace('\\', '/')
            if rel not in done:
                e = emit(rel)
                if e is not None:
                    out.append(e)
                    done.add(rel)
        for fn in sorted(filenames):
            rel = (rel_dir + '/' + fn).lstrip('./').replace('\\', '/')
            if rel not in done:
                e = emit(rel)
                if e is not None:
                    out.append(e)
                    done.add(rel)
    if tx is not None:
        t = Entry(tx.name, b'', magic=tx.magic)
        t.fields = dict(tx.fields)
        t.set_data(b'')
        out.append(t)
    else:
        out.append(Entry('TRAILER!!!', b'', mode=0))
    return out


def _mk(rel, data, meta, mode, nlink=1, is_link=False):
    """（保留给外部调用）按原条目元数据造一个条目；没有原条目就用默认值。"""
    old = meta.get(rel)
    if old is not None:
        m = mode if is_link else old.fields['mode']
        return Entry(rel, data, mode=m, uid=old.fields['uid'], gid=old.fields['gid'],
                     nlink=nlink, mtime=old.fields['mtime'], magic=old.magic)
    return Entry(rel, data, mode=mode, uid=0, gid=(0 if (mode & S_IFDIR) else 2000),
                 nlink=nlink, mtime=0)
