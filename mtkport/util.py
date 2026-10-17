# -*- coding: utf-8 -*-
"""通用小工具：日志、md5、找外部程序、跑子进程、十六进制块打印。"""
import hashlib
import os
import shutil
import subprocess
import sys

# 让 Windows 控制台 / 后台重定向都能正确输出中文
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


class PortError(Exception):
    """工具可预期的失败（给出人话原因），CLI 捕获后只打印这一行。"""


def log(msg=''):
    print(msg, flush=True)


def md5(data):
    if isinstance(data, str):
        with open(data, 'rb') as f:
            h = hashlib.md5()
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
            return h.hexdigest()
    return hashlib.md5(data).hexdigest()


def size(n):
    return format(n, ',')


def run(cmd, timeout=3600, cwd=None, quiet=False):
    """跑外部程序，返回 (rc, 合并输出)。"""
    if not quiet:
        log('   $ ' + ' '.join('"%s"' % c if ' ' in str(c) else str(c) for c in cmd))
    try:
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise PortError('找不到程序：%s' % cmd[0])
    out = r.stdout.decode('utf-8', 'replace')
    return r.returncode, out


# 随包自带的 bin 目录（放 7z / make_ext4fs / adb 等，开箱即用）
_BUNDLE_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin')

# 外部程序搜索表：名字 -> 候选路径（含环境变量 MTKPORT_BIN 目录）
_TOOLS = {
    '7z': [
        r'D:\MTK工具\rom制作工具破解版\7z.exe',
        r'C:\Program Files\7-Zip\7z.exe',
        r'C:\Program Files (x86)\7-Zip\7z.exe',
    ],
    'make_ext4fs': [
        r'D:\MTK工具\mtk-garbage-porttool\bin\win\x86_64\make_ext4fs.exe',
    ],
    'magiskboot': [
        r'D:\MTK工具\mtk-garbage-porttool\bin\win\x86_64\magiskboot.exe',
    ],
    'mke2fs': [
        r'D:\deepseek-harness\安卓折腾\tools\platform-tools\mke2fs.exe',
    ],
    'adb': [
        r'D:\deepseek-harness\安卓折腾\tools\platform-tools\adb.exe',
    ],
}


def find_tool(name, required=True):
    """按 环境变量 MTKPORT_BIN -> 随包 bin/ -> 内置候选 -> PATH 的顺序找外部程序。"""
    env_dir = os.environ.get('MTKPORT_BIN')
    cands = []
    if env_dir:
        cands += [os.path.join(env_dir, name + '.exe'), os.path.join(env_dir, name)]
    cands += [os.path.join(_BUNDLE_BIN, name + '.exe'), os.path.join(_BUNDLE_BIN, name)]
    cands += _TOOLS.get(name, [])
    for c in cands:
        if os.path.isfile(c):
            return c
    p = shutil.which(name)
    if p:
        return p
    if required:
        raise PortError('缺少外部程序 %s：请装到 PATH，或设环境变量 MTKPORT_BIN 指向目录' % name)
    return None


def read_all(path):
    with open(path, 'rb') as f:
        return f.read()


def write_all(path, data):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def human_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit) if unit != 'B' else '%d B' % n
        n /= 1024.0


def align(n, page):
    return n + (-n) % page
