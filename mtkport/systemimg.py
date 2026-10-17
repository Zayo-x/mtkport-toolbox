# -*- coding: utf-8 -*-
"""system.img（raw ext4 / sparse）解包、文件树打包、原厂→移植包合并。

Windows 上没有 Linux 的 debugfs，所以：
  解包  用 7z（能直读 ext4 镜像；符号链接会解成"内容是目标路径"的小文件）
  打包  用 make_ext4fs + fs_config + file_contexts（和 mtk-garbage-porttool 一致的配方）
合并  按"从原厂搬哪些路径"的规则做覆盖/删除/保留，避免原厂私有 HAL、字体、
      校准数据被移植包里的通用版本覆盖掉 —— 这正是移植最容易翻车的地方。
"""
import fnmatch
import json
import os
import re
import shutil

from .util import PortError, find_tool, log, read_all, run, size, write_all

SPARSE_MAGIC = 0xED26FF3A
SYMLINK_TAG = b'!<symlink>'


# ---------------- sparse → raw ----------------
def is_sparse(path):
    with open(path, 'rb') as f:
        head = f.read(4)
    return len(head) == 4 and int.from_bytes(head, 'little') == SPARSE_MAGIC


def sparse_to_raw(src, dst=None):
    """把 Android sparse image 展开成 raw。纯 Python，不依赖 simg2img。"""
    dst = dst or (os.path.splitext(src)[0] + '.raw.img')
    import struct
    with open(src, 'rb') as f:
        buf = f.read()
    (magic, _major, _minor, file_hdr_sz, chunk_hdr_sz, blk_sz, _total_blks,
     total_chunks, _cksum) = struct.unpack('<IHHHHIIII', buf[:28])
    if magic != SPARSE_MAGIC:
        raise PortError('%s 不是 sparse 镜像' % src)
    out = bytearray()
    pos = file_hdr_sz
    for _n in range(total_chunks):
        typ, _res, chunk_sz, total_sz = struct.unpack('<HHII', buf[pos:pos + 12])
        body = buf[pos + chunk_hdr_sz:pos + total_sz]
        if typ == 0xCAC1:                      # raw
            out += body
        elif typ == 0xCAC2:                    # fill
            out += body[:4] * (chunk_sz * blk_sz // 4)
        elif typ == 0xCAC3:                    # don't care
            out += b'\x00' * (chunk_sz * blk_sz)
        elif typ == 0xCAC4:                    # crc32
            pass
        else:
            raise PortError('未知 sparse chunk 类型 0x%X' % typ)
        pos += total_sz
    write_all(dst, bytes(out))
    return dst


# ---------------- 解包 ----------------
def extract(img, outdir, force=False):
    if is_sparse(img):
        log('   sparse 镜像，先展开成 raw ...')
        img = sparse_to_raw(img)
    if os.path.isdir(outdir) and os.listdir(outdir) and not force:
        raise PortError('%s 已存在且非空（要覆盖请加 --force）' % outdir)
    os.makedirs(outdir, exist_ok=True)
    seven = find_tool('7z')
    rc, out = run([seven, 'x', '-y', '-o' + outdir, img], quiet=True)
    if rc not in (0, 1):
        raise PortError('7z 解包失败（rc=%d）：%s' % (rc, out.strip()[-500:]))
    n = sum(len(fs) for _, _, fs in os.walk(outdir))
    log('   解出 %d 个文件 → %s' % (n, outdir))
    return outdir


# ---------------- 解析升级脚本（拿精确的 fs_config / file_contexts / 符号链接） ----------------
def _split_args(s):
    out, cur, q, esc = [], '', False, False
    for ch in s:
        if esc:
            cur += ch
            esc = False
            continue
        if ch == '\\':
            cur += ch
            esc = True
            continue
        if ch == '"':
            q = not q
            cur += ch
            continue
        if ch == ',' and not q:
            out.append(cur.strip())
            cur = ''
            continue
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [a.strip().strip('"') for a in out]


def _calls(text, name):
    res, i, key = [], 0, name + '('
    while True:
        i = text.find(key, i)
        if i < 0:
            break
        j, depth, q, esc = i + len(key), 1, False, False
        while j < len(text) and depth:
            ch = text[j]
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                q = not q
            elif not q:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
            j += 1
        res.append(_split_args(text[i + len(key):j - 1]))
        i = j
    return res


def read_updater_script(rom_zip):
    import zipfile
    z = zipfile.ZipFile(rom_zip)
    for cand in ('META-INF/com/google/android/updater-script',
                 'META-INF/com/google/android/update-script'):
        try:
            return z.read(cand).decode('utf-8', 'replace')
        except KeyError:
            continue
    raise PortError('%s 里没有 updater-script' % rom_zip)


def parse_updater(rom_zip):
    """返回 (fs_label, fc_label, symlinks)，与刷机脚本里的 set_metadata 完全一致。"""
    script = read_updater_script(rom_zip)
    fs_label, fc_label = [], []
    for args in _calls(script, 'set_metadata') + _calls(script, 'set_metadata_recursive'):
        if not args:
            continue
        fpath = args[0].replace('+', '\\+').replace('[', '\\[').replace('//', '/')
        fargs = args[1:]
        uid, gid, mode, extra = '0', '0', '644', ''
        selabel = 'u:object_r:system_file:s0'
        for k in range(len(fargs) - 1):
            kk = fargs[k]
            if kk == 'uid':
                uid = fargs[k + 1]
            elif kk == 'gid':
                gid = fargs[k + 1]
            elif kk in ('mode', 'fmode', 'dmode'):
                mode = fargs[k + 1]
            elif kk == 'capabilities':
                if fargs[k + 1] != '0x0':
                    extra = 'capabilities=' + fargs[k + 1]
            elif kk == 'selabel':
                selabel = fargs[k + 1]
        fs_label.append([fpath.lstrip('/'), uid, gid, mode, extra])
        fc_label.append([fpath, selabel])
    symlinks = _calls(script, 'symlink')
    return fs_label, fc_label, symlinks


# ---------------- 打包 ----------------
def _set_system_attr(p):
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(p, 0x4)   # FILE_ATTRIBUTE_SYSTEM
    except Exception:
        pass


def read_symlink_marker(p):
    try:
        with open(p, 'rb') as f:
            if f.read(10) == SYMLINK_TAG:
                return f.read().decode('utf-16').rstrip('\0')
    except Exception:
        return None
    return None


def write_symlink_marker(p, target):
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    if os.path.isfile(p):
        os.remove(p)
    elif os.path.isdir(p):
        shutil.rmtree(p)
    with open(p, 'wb') as f:
        f.write(SYMLINK_TAG + target.encode('utf-16') + b'\0\0')
    _set_system_attr(p)


def build_fs_config(tree, fs_label, skip=('install',)):
    """把升级脚本给的元数据 + 目录树里其余文件 合成 make_ext4fs 的 -C 文件。"""
    have = set(x[0] for x in fs_label)
    rows = list(fs_label)
    nd = nf = nl = 0
    for root, dirs, files in os.walk(tree):
        rel_root = os.path.relpath(root, tree)
        if rel_root != '.' and rel_root.split(os.sep)[0] in skip:
            continue
        for d in dirs:
            up = os.path.relpath(os.path.join(root, d), tree).replace('\\', '/')
            if up.split('/')[0] in skip:
                continue
            if up not in have:
                rows.append([up, '0', '0', '0755'])
                have.add(up)
                nd += 1
        for fn in files:
            fp = os.path.join(root, fn)
            up = os.path.relpath(fp, tree).replace('\\', '/')
            if up.split('/')[0] in skip or up in have:
                continue
            link = read_symlink_marker(fp)
            if link:
                rows.append([up, '0', '2000', '0755', link])
                nl += 1
            else:
                mode = '0755' if '/bin/' in '/' + up or '/xbin/' in '/' + up else '0644'
                rows.append([up, '0', '2000', mode])
                nf += 1
            have.add(up)
    rows.sort(key=lambda x: x[0])
    return rows, (nd, nf, nl)


def build_file_contexts(fc_label):
    rows, seen = [['/', 'u:object_r:system_file:s0'],
                  ['/system(/.*)?', 'u:object_r:system_file:s0']], set()
    out = []
    for p, c in rows + fc_label:
        k = (p, c)
        if k not in seen:
            seen.add(k)
            out.append('%s %s' % (p, c))
    return out


def pack(tree, out, part_size, rom_zip=None, label='system', mount_point='system',
         workdir=None):
    """把文件树打成 raw ext4 镜像（尺寸严格等于 part_size）。"""
    workdir = workdir or (os.path.dirname(os.path.abspath(out)) or '.')
    os.makedirs(workdir, exist_ok=True)
    fs_label, fc_label, symlinks = ([], [], [])
    if rom_zip:
        fs_label, fc_label, symlinks = parse_updater(rom_zip)
        log('   升级脚本：set_metadata %d 条、symlink %d 条' % (len(fs_label), len(symlinks)))

    made = skipped = 0
    for args in symlinks:
        if len(args) < 2:
            continue
        src, targets = args[0], args[1:]
        for t in targets:
            dest = os.path.join(tree, t.lstrip('/').replace('/', os.sep))
            if os.path.isfile(dest) and read_symlink_marker(dest):
                skipped += 1
                continue
            write_symlink_marker(dest, src)
            made += 1
    if made or skipped:
        log('   符号链接标记：新建 %d，已存在 %d' % (made, skipped))

    rows, (nd, nf, nl) = build_fs_config(tree, fs_label)
    cfg_path = os.path.join(workdir, '%s_fs_config.txt' % mount_point)
    with open(cfg_path, 'w', encoding='utf-8', newline='\n') as f:
        for r in rows:
            f.write(' '.join(r) + '\n')
    log('   fs_config %d 行（目录 +%d 文件 +%d 链接 +%d）' % (len(rows), nd, nf, nl))

    fc_path = os.path.join(workdir, '%s_file_contexts.txt' % mount_point)
    fc_rows = build_file_contexts(fc_label)
    with open(fc_path, 'w', encoding='utf-8', newline='\n') as f:
        for line in fc_rows:
            f.write(line + '\n')
    log('   file_contexts %d 行' % len(fc_rows))

    tool = find_tool('make_ext4fs')
    cmd = [tool, '-J', '-T', '1', '-l', str(part_size), '-C', cfg_path, '-S', fc_path,
           '-L', label, '-a', mount_point, out, tree]
    if os.path.exists(out):
        os.remove(out)
    rc, o = run(cmd, timeout=7200, quiet=True)
    if rc != 0 or not os.path.exists(out):
        raise PortError('make_ext4fs 失败（rc=%d）：\n%s' % (rc, o.strip()[-1500:]))
    got = os.path.getsize(out)
    log('   make_ext4fs 完成：%s B（目标 %s B%s）'
        % (size(got), size(part_size), ' 一致 ✓' if got == part_size else ' ★不一致'))
    if got != part_size:
        raise PortError('镜像尺寸 %d ≠ 分区尺寸 %d' % (got, part_size))
    return out


# ---------------- 原厂 → 移植包 合并 ----------------
def merge(stock_dir, donor_dir, out_dir, spec):
    """spec: {"copy_from_stock": [glob...], "delete": [glob...], "keep": [glob...]}
    先整体拷 donor，再按 copy_from_stock 从原厂覆盖，最后按 delete 删除。
    """
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise PortError('%s 已存在且非空' % out_dir)
    shutil.copytree(donor_dir, out_dir)
    st = stock_dir.rstrip('\\/')

    def hit(rel, glob):
        """glob 支持：`a/b.so` 精确、`etc/firmware/*` 一层、`etc/firmware/` 或 `/**` 整个目录树。"""
        g = glob.replace('\\', '/')
        if g.endswith('/') or g.endswith('/**'):
            pre = g.rstrip('*').rstrip('/')
            return rel.startswith(pre + '/')
        return fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip('/') + '/*')

    def rel_paths(base):
        for root, _dirs, files in os.walk(base):
            for fn in files:
                fp = os.path.join(root, fn)
                yield fp, os.path.relpath(fp, base).replace('\\', '/')

    copied = 0
    unmatched = []
    for glob in spec.get('copy_from_stock', []):
        n = 0
        for fp, rel in rel_paths(st):
            if hit(rel, glob):
                dst = os.path.join(out_dir, rel.replace('/', os.sep))
                os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
                shutil.copy2(fp, dst)
                copied += 1
                n += 1
        if n == 0:
            unmatched.append(glob)
    log('   从原厂搬入 %d 个文件（%d 条规则）' % (copied, len(spec.get('copy_from_stock', []))))
    for g in unmatched:
        log('   ! 这条搬运规则一个文件都没匹配上：%s（原厂树里没有？拼写不对？）' % g)

    deleted = 0
    for glob in spec.get('delete', []):
        n = 0
        for root, dirs, files in os.walk(out_dir):
            for fn in files:
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, out_dir).replace('\\', '/')
                if hit(rel, glob):
                    os.remove(fp)
                    deleted += 1
                    n += 1
        if n == 0:
            log('   ! 这条删除规则没删到任何文件：%s' % glob)
    log('   删除 %d 个文件（%d 条规则）' % (deleted, len(spec.get('delete', []))))
    return out_dir


# ---------------- 打包结果的内容复核 ----------------
def verify_tree(image, tree, workdir=None):
    """把打好的镜像再解一遍，和源目录逐个比 md5。

    注意：7z 在 Windows 上解不出 ext4 里的符号链接（会报 Unsupported Method），
    所以符号链接只统计数量、不比对内容 —— 这是 7z 的限制，不是镜像的问题。
    """
    import hashlib
    import tempfile
    tmp = tempfile.mkdtemp(prefix='mtkport-verify-', dir=workdir)
    try:
        seven = find_tool('7z')
        rc, out = run([seven, 'x', '-y', '-o' + tmp, image], quiet=True)
        n0 = sum(len(fs) for _, _, fs in os.walk(tmp))
        # 7z 解 ext4 里的符号链接会报错并返回 2，但只要普通文件解出来了就算成功
        if n0 == 0:
            raise PortError('7z 解镜像失败（rc=%d）：%s' % (rc, out.strip()[-300:]))

        def h(p):
            m = hashlib.md5()
            with open(p, 'rb') as f:
                for c in iter(lambda: f.read(1 << 20), b''):
                    m.update(c)
            return m.hexdigest()

        same = diff = missing = links = 0
        bad = []
        for root, _dirs, files in os.walk(tree):
            for fn in files:
                a = os.path.join(root, fn)
                rel = os.path.relpath(a, tree)
                try:
                    with open(a, 'rb') as f:
                        head = f.read(10)
                except OSError:
                    continue
                if head == SYMLINK_TAG:
                    links += 1
                    continue
                b = os.path.join(tmp, rel)
                if not os.path.isfile(b):
                    missing += 1
                    bad.append(('镜像里缺', rel))
                elif h(a) == h(b):
                    same += 1
                else:
                    diff += 1
                    bad.append(('内容不同', rel))
        n_img = sum(len(fs) for _, _, fs in os.walk(tmp))
        res = {'same': same, 'diff': diff, 'missing': missing, 'links': links,
               'image_files': n_img, 'bad': bad}
        log('   ✓ 内容一致 %d 个 / 不同 %d / 缺失 %d；软链接标记 %d 个（7z 解不出，正常）'
            % (same, diff, missing, links))
        log('   镜像里共 %d 个文件' % n_img)
        for k, v in bad[:10]:
            log('   ✗ %s %s' % (k, v))
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
