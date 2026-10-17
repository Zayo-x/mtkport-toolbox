# -*- coding: utf-8 -*-
"""boot.img 解析 / 解包 / 重打包（支持 MTK 3.x 的 ramdisk 前 512 字节标签）。

镜像布局（page 通常 2048）：
  [page 0]  Android boot header（magic 'ANDROID!'）
  [kernel]  kernel_size 字节
  [对齐]    补到 page 边界
  [ramdisk] MTK 前缀（常见 512 B，magic 0x58881688）+ gzip(cpio) + 补零到 ramdisk_size
  [second]  可选 second_size 字节
  [对齐]    补齐到 page 边界
本工具默认用 inplace（原地换 gzip，镜像长度与所有偏移不变，最安全），
只有新 ramdisk 大过槽位时才用 rebuild（重排布局并改写头部）。
"""
import gzip
import io
import json
import os
import struct

from . import cpio
from .util import PortError, align, log, md5, read_all, size, write_all

BOOT_MAGIC = b'ANDROID!'
MTK_RAMDISK_MAGIC = 0x58881688

# 支持的压缩魔数 -> 名字
MAGICS = [
    (b'\x1f\x8b\x08', 'gzip'),
    (b'\x02\x21\x4c\x18', 'lz4'),
    (b'\xfd7zXZ\x00', 'xz'),
    (b'BZh', 'bzip2'),
    (b'\x5d\x00\x00', 'lzma'),
    (b'070701', 'cpio'),
]


def detect_compress(blob):
    for magic, name in MAGICS:
        if blob.startswith(magic):
            return name
    return 'unknown'


def gz_compress_best(cpio_bytes, levels=(9, 6, 5, 1)):
    """和实测脚本一致：mtime=0 固定，多压缩级别取最短的那个（结果可复现）。"""
    best = None
    for lvl in levels:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=lvl, mtime=0) as g:
            g.write(cpio_bytes)
        b = buf.getvalue()
        if best is None or len(b) < len(best[1]):
            best = (lvl, b)
    return best[0], best[1]


def decompress(name, blob):
    if name == 'gzip':
        return gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
    if name == 'cpio':
        return blob
    raise PortError('暂不支持 %s 压缩的 ramdisk（本工具支持 gzip / 未压缩 cpio；'
                    '遇到 lz4/xz 请先用 magiskboot 转换）' % name)


class BootImg(object):
    def __init__(self, path=None):
        self.path = path
        self.raw = b''
        # 头部字段
        self.kernel_size = 0
        self.kernel_addr = 0
        self.ramdisk_size = 0
        self.ramdisk_addr = 0
        self.second_size = 0
        self.second_addr = 0
        self.tags_addr = 0
        self.page_size = 2048
        self.name = ''
        self.cmdline = ''
        self.extra_cmdline = ''
        self.mid = b''
        self.tail_header = b''        # 0x28~0x30 两个 u32，原样保留
        # 派生
        self.kernel = b''
        self.ramdisk_prefix = b''     # MTK 标签
        self.ramdisk_gz = b''         # 压缩后的 ramdisk 原始字节
        self.ramdisk_compress = 'unknown'
        self.cpio_bytes = b''
        self.second = b''
        if path:
            self.load(path)

    # ---------------- 解析 ----------------
    def load(self, path):
        self.raw = read_all(path)
        self.path = path
        if self.raw[:8] != BOOT_MAGIC:
            raise PortError('%s 不是 boot.img（开头不是 ANDROID!，实际 %r）'
                            % (path, self.raw[:8]))
        (self.kernel_size, self.kernel_addr, self.ramdisk_size, self.ramdisk_addr,
         self.second_size, self.second_addr, self.tags_addr, self.page_size) = \
            struct.unpack('<8I', self.raw[8:40])
        self.tail_header = self.raw[0x28:0x30]
        self.name = self.raw[0x30:0x40].split(b'\x00')[0].decode('utf-8', 'replace')
        self.cmdline = self.raw[0x40:0x240].split(b'\x00')[0].decode('utf-8', 'replace')
        self.mid = self.raw[0x240:0x260]
        self.extra_cmdline = self.raw[0x260:0x660].split(b'\x00')[0].decode('utf-8', 'replace')
        if self.page_size not in (512, 1024, 2048, 4096, 8192, 16384):
            raise PortError('page_size=%d 不合理，头部可能已损坏' % self.page_size)

        k_off = self.page_size
        self.kernel = self.raw[k_off:k_off + self.kernel_size]
        r_off = align(k_off + self.kernel_size, self.page_size)
        seg = self.raw[r_off:r_off + self.ramdisk_size]
        if len(seg) < self.ramdisk_size:
            raise PortError('ramdisk 段被截断（头部说 %s B，实际只有 %s B）'
                            % (size(self.ramdisk_size), size(len(seg))))
        # 找压缩流起点（跳过 MTK 标签）
        gz_off = -1
        for off in range(0, min(len(seg), 4096)):
            if detect_compress(seg[off:off + 8]) not in ('unknown',):
                gz_off = off
                break
        if gz_off < 0:
            raise PortError('ramdisk 段里找不到已知压缩魔数（前 16 字节 %r）' % seg[:16])
        self.ramdisk_prefix = seg[:gz_off]
        self.ramdisk_gz = seg[gz_off:]          # 含尾部补零，gzip 解压本身会忽略
        self.ramdisk_compress = detect_compress(self.ramdisk_gz)
        self.cpio_bytes = decompress(self.ramdisk_compress, self.ramdisk_gz)
        s_off = align(r_off + self.ramdisk_size, self.page_size)
        self.second = self.raw[s_off:s_off + self.second_size]
        return self

    # ---------------- 报告 ----------------
    def info(self):
        ram_off = align(self.page_size + self.kernel_size, self.page_size)
        cs = cpio.parse(self.cpio_bytes) if self.cpio_bytes else []
        return {
            'file': self.path,
            'size': len(self.raw),
            'page_size': self.page_size,
            'kernel_size': self.kernel_size,
            'kernel_addr': self.kernel_addr,
            'ramdisk_size': self.ramdisk_size,
            'ramdisk_addr': self.ramdisk_addr,
            'ramdisk_offset': ram_off,
            'mtk_tag_size': len(self.ramdisk_prefix),
            'compress': self.ramdisk_compress,
            'cpio_size': len(self.cpio_bytes),
            'cpio_entries': len(cs),
            'second_size': self.second_size,
            'second_addr': self.second_addr,
            'tags_addr': self.tags_addr,
            'name': self.name,
            'cmdline': self.cmdline,
            'extra_cmdline': self.extra_cmdline,
        }

    def report(self):
        d = self.info()
        log('■ %s' % d['file'])
        log('   镜像 %s B   page %d   kernel %s B @0x%X   ramdisk %s B @0x%X（含 MTK 标签 %d B）'
            % (size(d['size']), d['page_size'], size(d['kernel_size']), d['kernel_addr'],
               size(d['ramdisk_size']), d['ramdisk_addr'], d['mtk_tag_size']))
        log('   ramdisk 压缩 %s，解压后 %s B，%d 个条目'
            % (d['compress'], size(d['cpio_size']), d['cpio_entries']))
        log('   name=%r  cmdline=%r' % (d['name'], d['cmdline']))
        if d['second_size']:
            log('   second %s B @0x%X' % (size(d['second_size']), d['second_addr']))

    # ---------------- 解包 ----------------
    def unpack(self, outdir):
        os.makedirs(outdir, exist_ok=True)
        write_all(os.path.join(outdir, 'kernel'), self.kernel)
        if self.ramdisk_prefix:
            write_all(os.path.join(outdir, 'ramdisk.mtktag'), self.ramdisk_prefix)
        write_all(os.path.join(outdir, 'ramdisk.%s' % ('gz' if self.ramdisk_compress == 'gzip'
                                                      else self.ramdisk_compress)),
                  self.ramdisk_gz)
        write_all(os.path.join(outdir, 'ramdisk.cpio'), self.cpio_bytes)
        ent = cpio.parse(self.cpio_bytes)
        cpio.to_tree(ent, os.path.join(outdir, 'ramdisk-tree'))
        if self.second_size:
            write_all(os.path.join(outdir, 'second'), self.second)
        meta = self.info()
        meta['entries'] = [
            {'name': e.name, 'mode': oct(e.mode), 'perm': oct(e.perm),
             'uid': e.fields['uid'], 'gid': e.fields['gid'],
             'size': len(e.data), 'link': e.text() if e.is_link else None}
            for e in ent]
        write_all(os.path.join(outdir, 'bootimg.json'),
                  json.dumps(meta, indent=2, ensure_ascii=False).encode('utf-8'))
        return outdir

    # ---------------- 重打包 ----------------
    def build(self, cpio_bytes, out, mode='inplace', keep_size=True):
        # 内容一个字都没变 ⇒ 原样输出，连压缩流都不重压（保证可复现）
        if mode == 'inplace' and cpio_bytes == self.cpio_bytes:
            write_all(out, self.raw)
            log('   ramdisk 与原始完全一致，直接原样输出（保留原包 gzip 流）')
            return self.raw
        lvl, new_gz = gz_compress_best(cpio_bytes)
        ram_off = align(self.page_size + self.kernel_size, self.page_size)
        slot = self.ramdisk_size - len(self.ramdisk_prefix)

        if mode == 'inplace':
            if len(new_gz) > slot:
                raise PortError('新 ramdisk（gzip %s B）超出原槽位 %s B，'
                                '请改用 --mode rebuild' % (size(len(new_gz)), size(slot)))
            buf = bytearray(self.raw)
            gz_start = ram_off + len(self.ramdisk_prefix)
            buf[gz_start:gz_start + len(new_gz)] = new_gz
            for i in range(gz_start + len(new_gz), ram_off + self.ramdisk_size):
                buf[i] = 0
            data = bytes(buf)
            assert len(data) == len(self.raw)
            log('   inplace：gzip -%d %s B 塞进槽位 %s B（余 %s B）'
                % (lvl, size(len(new_gz)), size(slot), size(slot - len(new_gz))))
        else:
            data = self._rebuild(new_gz, keep_size)
            log('   rebuild：gzip -%d %s B' % (lvl, size(len(new_gz))))
        write_all(out, data)
        return data

    def _prefix_with_size(self, new_gz_len, new_cpio_len):
        """改写 MTK 标签里的尺寸字段（只在原值与旧长度相等时才动，避免瞎改）。"""
        pre = bytearray(self.ramdisk_prefix)
        for off in (4, 8, 12, 16):
            if off + 4 > len(pre):
                break
            v = struct.unpack('<I', bytes(pre[off:off + 4]))[0]
            if v == len(self.ramdisk_gz):
                pre[off:off + 4] = struct.pack('<I', new_gz_len)
            elif v == len(self.cpio_bytes):
                pre[off:off + 4] = struct.pack('<I', new_cpio_len)
        return bytes(pre)

    def _header_page(self, ramdisk_size):
        """取/造第一页头。有原镜像就改它的头，没有（pack 不带 --ref）就按字段新建。"""
        if self.raw:
            head = bytearray(self.raw[:self.page_size])
        else:
            head = bytearray(self.page_size)
            head[0:8] = BOOT_MAGIC
            if self.tail_header:
                head[0x28:0x30] = self.tail_header[:8].ljust(8, b'\x00')
            for off, s, n in ((0x30, self.name, 16), (0x40, self.cmdline, 512),
                              (0x260, self.extra_cmdline, 1024)):
                b = s.encode('utf-8')[:n - 1]
                head[off:off + len(b)] = b
            if self.mid:
                head[0x240:0x240 + min(len(self.mid), 32)] = self.mid[:32]
        struct.pack_into('<8I', head, 8, self.kernel_size, self.kernel_addr, ramdisk_size,
                         self.ramdisk_addr, self.second_size, self.second_addr,
                         self.tags_addr, self.page_size)
        return head

    def _rebuild(self, new_gz, keep_size):
        pre = self._prefix_with_size(len(new_gz), len(self.cpio_bytes))
        ramdisk_seg = pre + new_gz
        ramdisk_size = max(len(ramdisk_seg), align(len(ramdisk_seg), self.page_size))
        ramdisk_seg = ramdisk_seg + b'\x00' * (ramdisk_size - len(ramdisk_seg))
        head = self._header_page(ramdisk_size)
        body = bytearray()
        body += head
        body += self.kernel
        body += b'\x00' * (align(len(body), self.page_size) - len(body))
        body += ramdisk_seg
        if self.second_size:
            body += b'\x00' * (align(len(body), self.page_size) - len(body))
            body += self.second
        body += b'\x00' * (align(len(body), self.page_size) - len(body))
        data = bytes(body)
        if keep_size and self.raw:
            if len(data) < len(self.raw):
                data += b'\x00' * (len(self.raw) - len(data))
            elif len(data) > len(self.raw):
                raise PortError('rebuild 后镜像 %s B 超过原镜像 %s B；'
                                '请用 --no-keep-size 或换 inplace'
                                % (size(len(data)), size(len(self.raw))))
        return data

    # ---------------- 校验 ----------------
    def verify_against(self, other_path):
        """独立复核：重新解析，比对内核/头部/ramdisk 解压结果。"""
        o = BootImg(other_path)
        problems = []
        if o.kernel != self.kernel:
            problems.append('kernel 不一致')
        if o.cpio_bytes != self.cpio_bytes:
            problems.append('ramdisk 解压后内容不一致')
        if (o.page_size, o.kernel_addr, o.ramdisk_addr, o.tags_addr) != \
                (self.page_size, self.kernel_addr, self.ramdisk_addr, self.tags_addr):
            problems.append('地址字段被改动')
        return problems
