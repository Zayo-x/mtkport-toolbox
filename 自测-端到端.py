# -*- coding: utf-8 -*-
"""端到端自测：全部拿真实文件跑，并和已知正确的 md5 对齐。

跑法：
    python 自测-端到端.py            # 快（≈10 秒）
    python 自测-端到端.py --full     # 加上 system.img 解包/打包（几分钟）

自测通过的标准不是"没报错"，而是：
    · unpack→pack 往返，md5 与原镜像一模一样
    · 用配置打出来的 boot.img，md5 与当初手工验证过的成品一模一样
这两条一旦成立，就说明 cpio 读写、gzip 复现、原地替换、规则命中判定全都是对的。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mtkport import bootimg, cpio, rules, scatter as scatter_mod  # noqa: E402
from mtkport.util import md5  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

ROM_ZIP = r'D:\deepseek-harness\spflash\B-再刷系统-lineage-14.1-P100-port.zip'
SCATTER = r'D:\deepseek-harness\spflash\p100-flash\MT6572_Android_scatter.txt'
STOCK_SYSTEM = (r'D:\deepseek-harness\安卓折腾\博阅P100\01-原厂固件-整机恢复'
                r'\原厂固件\system.img')
DONOR_TREE = r'D:\deepseek-harness\安卓折腾\博阅P100\build\system'
CFG = os.path.join(HERE, 'configs', 'p100-lineage14.json')
WORK = os.path.join(HERE, '_自测')

MD5_DONOR = '1310d377553c6e42f3318a8a60b3fb86'   # 移植包里的 boot.img
MD5_V6 = 'ee0cf8cf39e9a3dbca3ee32f825ee63e'      # 当初手工验证过的成品
SIZE_V6 = 4747264
SYSTEM_SIZE = 838860800

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(('  ✓ ' if cond else '  ✗ ') + name + (('   —— ' + detail) if detail else ''),
          flush=True)
    return bool(cond)


def section(t):
    print('\n■ ' + t, flush=True)


def cli(*args):
    """跑一条 CLI 命令，返回 (rc, 输出)。"""
    cmd = [sys.executable, os.path.join(HERE, 'mtkport.py')] + [str(a) for a in args]
    r = subprocess.run(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=7200)
    return r.returncode, r.stdout.decode('utf-8', 'replace')


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # ---------- 0. 准备输入 ----------
    section('0. 准备输入文件')
    boot_src = os.path.join(WORK, 'boot.img')
    if not os.path.isfile(boot_src):
        if not os.path.isfile(ROM_ZIP):
            print('  ! 缺少移植包 zip，无法自测：%s' % ROM_ZIP)
            return 2
        with zipfile.ZipFile(ROM_ZIP) as z:
            open(boot_src, 'wb').write(z.read('boot.img'))
    check('从移植包取出 boot.img', os.path.isfile(boot_src),
          '%d B' % os.path.getsize(boot_src))
    check('输入 boot.img 的 md5 与记录一致', md5(boot_src) == MD5_DONOR, md5(boot_src))

    # ---------- 1. 解析 ----------
    section('1. 解析 boot.img（info）')
    b = bootimg.BootImg(boot_src)
    check('page_size = 2048', b.page_size == 2048, str(b.page_size))
    check('kernel_size = 3356832', b.kernel_size == 3356832, str(b.kernel_size))
    check('MTK 标签 512 B', len(b.ramdisk_prefix) == 512, '%d B' % len(b.ramdisk_prefix))
    check('ramdisk 是 gzip', b.ramdisk_compress == 'gzip', b.ramdisk_compress)
    ent = cpio.parse(b.cpio_bytes)
    check('cpio 条目 52 个', len(ent) == 52, '%d 个' % len(ent))
    rc, out = cli('info', boot_src)
    check('CLI info 正常退出', rc == 0 and 'ANDROID' not in out.splitlines()[0][:20])

    # ---------- 2. cpio 往返 ----------
    section('2. cpio 读写是否逐字节可逆')
    raw = b.cpio_bytes
    check('serialize(parse(x)) == x', cpio.serialize(cpio.parse(raw)) == raw)

    # ---------- 3. 解包→打包 往返 ----------
    section('3. unpack → pack 往返（md5 必须回到原值）')
    d1 = os.path.join(WORK, 'boot-rt')
    rc, out = cli('unpack', boot_src, '-o', d1)
    check('unpack 成功', rc == 0, out.strip().splitlines()[-1][:60] if out.strip() else '')
    out1 = os.path.join(WORK, 'rt.img')
    rc, out = cli('pack', d1, '--ref', boot_src, '-o', out1)
    check('pack 成功', rc == 0)
    check('往返 md5 == 原镜像', os.path.isfile(out1) and md5(out1) == MD5_DONOR,
          md5(out1) if os.path.isfile(out1) else '没生成')

    # ---------- 4. 从目录树打包 ----------
    section('4. 从 ramdisk-tree 打包（--from-tree，等价性）')
    out2 = os.path.join(WORK, 'tree.img')
    rc, out = cli('pack', d1, '--ref', boot_src, '--from-tree', '-o', out2)
    check('--from-tree 打包成功', rc == 0)
    check('树往返 md5 == 原镜像', os.path.isfile(out2) and md5(out2) == MD5_DONOR,
          md5(out2) if os.path.isfile(out2) else '没生成')

    # ---------- 4b. 不带 --ref（靠 bootimg.json 造头部） ----------
    section('4b. 不带 --ref 从 bootimg.json 重打包')
    out2b = os.path.join(WORK, 'noref.img')
    rc, out = cli('pack', d1, '-o', out2b)
    nb2 = bootimg.BootImg(out2b) if os.path.isfile(out2b) else None
    check('无 --ref 打包成功', rc == 0 and nb2 is not None, 'rc=%d' % rc)
    check('内核 / ramdisk / 头地址 与原镜像一致',
          nb2 is not None and nb2.kernel == b.kernel
          and nb2.cpio_bytes == b.cpio_bytes
          and (nb2.page_size, nb2.kernel_addr, nb2.ramdisk_addr)
          == (b.page_size, b.kernel_addr, b.ramdisk_addr))

    # ---------- 5. 规则打补丁 ----------
    section('5. 用 P100 配置打补丁（必须复现当初验证过的成品）')
    out3 = os.path.join(WORK, 'boot-v6.img')
    rc, out = cli('patch', boot_src, '--rules', CFG, '-o', out3)
    check('patch 成功且复核通过', rc == 0 and '复核' in out)
    got = md5(out3) if os.path.isfile(out3) else ''
    check('md5 == 当初成品 ee0cf8cf…', got == MD5_V6, got)
    check('镜像尺寸 == 4,747,264 B',
          os.path.isfile(out3) and os.path.getsize(out3) == SIZE_V6)

    nb = bootimg.BootImg(out3)
    fstab = cpio.find(cpio.parse(nb.cpio_bytes), 'fstab.mt6572').text()
    dprop = cpio.find(cpio.parse(nb.cpio_bytes), 'default.prop').text()
    usb = cpio.find(cpio.parse(nb.cpio_bytes), 'init.mt6572.usb.rc').text()
    swap = cpio.find(cpio.parse(nb.cpio_bytes), 'enableswap.sh').text()
    check('fstab 已改成 sdcard0:7,nonremovable,noemulatedsd',
          'voldmanaged=sdcard0:7,nonremovable,noemulatedsd' in fstab)
    check('default.prop 已关 adb 授权且默认 mtp,adb',
          'ro.adb.secure=0' in dprop and 'persist.sys.usb.config=mtp,adb' in dprop)
    check('USB VID/PID 已改成 18D1:D001/4E12',
          'sys.usb.vid 18D1' in usb and 'idProduct D001' in usb and 'idProduct 4E12' in usb)
    check('enableswap 已改用 toybox mkswap/swapon',
          '/system/bin/mkswap' in swap and 'tiny_mkswap' not in swap)
    check('内核与原镜像逐字节相同', nb.kernel == b.kernel)

    # ---------- 6. 规则没命中时必须报错 ----------
    section('6. 安全性：规则对不上要拒绝出镜像')
    bad = os.path.join(WORK, 'bad-rules.json')
    open(bad, 'w', encoding='utf-8').write(
        '{"boot":{"edits":[{"file":"default.prop","type":"replace",'
        '"old":"这个字符串肯定不存在__XYZ","new":"x"}]}}')
    rc, out = cli('patch', boot_src, '--rules', bad, '-o', os.path.join(WORK, 'bad.img'))
    check('未命中的 replace 被判失败（rc!=0）', rc != 0, 'rc=%d' % rc)
    check('并且没有生成镜像', not os.path.isfile(os.path.join(WORK, 'bad.img')))

    # ---------- 7. scatter ----------
    section('7. scatter 解析与"只刷 boot"')
    if os.path.isfile(SCATTER):
        parts = scatter_mod.parse(SCATTER)
        check('scatter 解析出 20 个分区', len(parts) == 20, '%d 个' % len(parts))
        has_dl = [p['partition_name'] for p in parts
                  if p.get('is_download', '').lower() == 'true']
        check('原表里有 4 个分区默认要刷', len(has_dl) == 4, '、'.join(has_dl))
        fdir = os.path.join(WORK, 'flash-boot')
        main_img = os.path.join(WORK, 'boot.img')
        shutil.copy2(out3, main_img)
        rc, out = cli('scatter-flash', SCATTER, '--only', 'BOOTIMG',
                      '--images', 'BOOTIMG=' + main_img, '-o', fdir)
        check('scatter-flash 生成成功', rc == 0, 'rc=%d' % rc)
        new_parts = scatter_mod.parse(os.path.join(fdir, 'MT6572_Android_scatter.txt'))
        dl = [p['partition_name'] for p in new_parts
              if p.get('is_download', '').lower() == 'true']
        check('只勾选 BOOTIMG', dl == ['BOOTIMG'], '、'.join(dl) or '无')
        check('镜像已复制进刷机文件夹', len([f for f in os.listdir(fdir)
                                            if f.lower().endswith('.img')]) == 1)
    else:
        print('  ! 没找到 scatter，跳过：%s' % SCATTER)

    # ---------- 8. dump 偏移核对 ----------
    section('8. 用 dumchar 的 StartAddr 在 dump 里定位分区（dump-landmarks）')
    dump = os.path.join(WORK, 'fake-mmcblk0.img')
    DC_BOOT, DC_REC = 0x1d80000, 0x2380000
    with open(dump, 'wb') as f:
        f.truncate(64 * 1024 * 1024)
        with open(boot_src, 'rb') as g:
            img = g.read()
        f.seek(DC_BOOT)
        f.write(img)
        f.seek(DC_REC)
        f.write(img[:2048])
    dcf = os.path.join(WORK, 'dumchar_info.txt')
    open(dcf, 'w', encoding='utf-8').write(
        'bootimg 0x600000 0x%X ssd\nrecovery 0x600000 0x%X ssd\n'
        'android 0x32000000 0x3740000 ssd\n' % (DC_BOOT, DC_REC))
    rc, out = cli('dump-landmarks', dump, '--dumchar', dcf)
    check('在偏移上认出了 bootimg 与 recovery',
          rc == 0 and out.count('boot.img') >= 2, 'rc=%d' % rc)
    # 故意把偏移写错，工具必须能发现
    dcf_bad = os.path.join(WORK, 'dumchar-bad.txt')
    open(dcf_bad, 'w', encoding='utf-8').write('bootimg 0x600000 0x1d81000 ssd\n')
    rc2, out2 = cli('dump-landmarks', dump, '--dumchar', dcf_bad)
    check('偏移写错时能报错（rc!=0）', rc2 != 0, 'rc=%d' % rc2)

    # ---------- 9. system（可选，慢） ----------
    if '--full' in sys.argv:
        section('9. system.img 解包 + 打包（--full，耗时几分钟）')
        exec_dir = os.path.join(WORK, 'system-stock')
        rc, out = cli('sys-extract', STOCK_SYSTEM, '-o', exec_dir, '--force')
        nf = sum(len(f) for _, _, f in os.walk(exec_dir)) if os.path.isdir(exec_dir) else 0
        check('原厂 system.img 解包成功', rc == 0 and nf > 500, '%d 个文件' % nf)
        out_img = os.path.join(WORK, 'system-new.img')
        rc, out = cli('sys-pack', DONOR_TREE, '-o', out_img, '--size', hex(SYSTEM_SIZE),
                      '--rom', ROM_ZIP)
        check('移植包 system 树打包成功', rc == 0, out.strip().splitlines()[-1][:80]
              if out.strip() else '')
        ok = os.path.isfile(out_img) and os.path.getsize(out_img) == SYSTEM_SIZE
        check('system 镜像尺寸 == 838,860,800 B', ok,
              '%d B' % os.path.getsize(out_img) if os.path.isfile(out_img) else '没生成')
        if ok:
            with open(out_img, 'rb') as f:
                f.seek(0x438)
                check('ext4 超级块魔数 0xEF53', f.read(2) == b'\x53\xef')

    # ---------- 汇总 ----------
    bad_n = [r for r in results if not r[1]]
    print('\n' + '=' * 70)
    print('自测结果：%d 项通过，%d 项失败' % (len(results) - len(bad_n), len(bad_n)))
    for n, _, d in bad_n:
        print('   失败：%s  %s' % (n, d))
    print('=' * 70)
    return 0 if not bad_n else 1


if __name__ == '__main__':
    sys.exit(main())
