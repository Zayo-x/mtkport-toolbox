# -*- coding: utf-8 -*-
"""一键流水线：扫描设备 → 选包 → 下载 → 备份 → 移植打包 → 出刷机目录。

每一步的产物都落在工作目录里，重跑会跳过已完成的部分（能断点续做）：
    <work>/device.json           设备档案
    <work>/rules.json            这次用的规则（预设或自动草稿）
    <work>/backup/               原机分区备份 + manifest.json
    <work>/downloads/            下载的移植包
    <work>/donor/                移植包解出来的东西（boot.img / system 树）
    <work>/stock/                原厂 system 树
    <work>/build/system/         合并后的 system 树
    <work>/out/boot.img          移植后的 boot
    <work>/out/system.img        移植后的 system
    <work>/flash/                给 SP Flash Tool 的刷机目录 + 说明
"""
import json
import os
import shutil

from . import autoport, backup as backup_mod, bootimg, cpio, romrepo, rules as rules_mod
from . import scatter as scatter_mod, systemimg
from .util import PortError, log, md5, human_bytes, size, find_tool, write_all
from .util import run as run_cmd

DEFAULT_WORK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            '_工作区', 'auto')


def _need(path):
    return os.path.exists(path)


def _find(root, names):
    """在目录里按文件名找（递归），返回第一个。"""
    names = [n.lower() for n in names]
    for base, _dirs, files in os.walk(root):
        for f in files:
            if f.lower() in names:
                return os.path.join(base, f)
    return None


def _unzip(archive, outdir):
    seven = find_tool('7z')
    os.makedirs(outdir, exist_ok=True)
    rc, out = run_cmd([seven, 'x', archive, '-o' + outdir, '-y'], timeout=3600, quiet=True)
    if rc not in (0, 2):  # 2 = 有警告（比如解不出符号链接），内容通常没问题
        raise PortError('解压失败（rc=%d）：%s' % (rc, out.strip()[-300:]))


def run(prof, work=DEFAULT_WORK, donor=None, android=None, stock_system=None,
        scatter=None, only='BOOTIMG,ANDROID', skip_backup=False, skip_fetch=False,
        rules_out=None, usb_vid=None, usb_pid=None, flash_mode=None):
    work = os.path.abspath(work)
    for d in ('downloads', 'donor', 'stock', 'build', 'out', 'backup', 'flash'):
        os.makedirs(os.path.join(work, d), exist_ok=True)
    dev = None

    # ---------- 1. 规则 ----------
    log('\n■ 第 1 步：定规则（有预设用预设，没有就自动推导草稿）')
    preset = autoport.match_preset(prof)
    rules = None
    if preset:
        with open(preset['info']['path'], encoding='utf-8') as f:
            rules = json.load(f)
        log('   ✓ 命中机型预设：%s' % preset['info']['name'])
        for w in preset['why']:
            log('     · %s' % w)
    else:
        log('   没有匹配的预设，先推一份草稿（原厂树到位后再补 system 部分）')
        for near in autoport.near_presets(prof)[:2]:
            log('   · 提示：站内有个相近预设《%s》（%s），但型号对不上，没有直接套用'
                % (near['info']['name'], '；'.join(near['why'])))
        rules, _notes = autoport.derive_boot_rules(prof, usb_vid=usb_vid, usb_pid=usb_pid)
    rules_path = rules_out or os.path.join(work, 'rules.json')
    with open(rules_path, 'w', encoding='utf-8') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    log('   规则文件：%s' % rules_path)

    # ---------- 2. 备份 ----------
    man = None
    if skip_backup:
        log('\n■ 第 2 步：跳过备份（--skip-backup）')
    else:
        log('\n■ 第 2 步：备份原机分区')
        from .device import Device
        dev = Device.pick(prof['serial'])
        if not dev.is_root() and not dev.root():
            raise PortError('备份需要 root：请先 adb root，或用带 root 的包')
        man = backup_mod.backup(dev, prof, os.path.join(work, 'backup'))

    # ---------- 3. 选包 / 下载 ----------
    log('\n■ 第 3 步：选移植包')
    if donor and os.path.isfile(donor):
        log('   用指定的包：%s' % donor)
        donor_zip = donor
    elif skip_fetch:
        cand = sorted(os.listdir(os.path.join(work, 'downloads')))
        if not cand:
            raise PortError('--skip-fetch 但 downloads 里没有包')
        donor_zip = os.path.join(work, 'downloads', cand[0])
        log('   用已有下载：%s' % donor_zip)
    else:
        repo = romrepo.Repo()
        cands, plat, _kd = repo.candidates(prof, android=android, limit=3)
        if not cands:
            raise PortError('站内没有合适候选')
        log('   平台目录 %s，前 3 名：' % plat)
        for i, c in enumerate(cands, 1):
            log('     %d. %6.1f 分  %s' % (i, c['score'], c['name']))
        best = cands[0]
        log('   → 选 %s' % best['name'])
        log('     理由：%s' % best['why'])
        donor_zip = repo.download(best['path'], os.path.join(work, 'downloads',
                                                             os.path.basename(best['name'])))

    # ---------- 4. 解开移植包 ----------
    log('\n■ 第 4 步：解开移植包')
    donor_dir = os.path.join(work, 'donor')
    boot_src = _find(donor_dir, ['boot.img']) if _need(donor_dir) else None
    if not boot_src:
        _unzip(donor_zip, donor_dir)
        boot_src = _find(donor_dir, ['boot.img'])
    if not boot_src:
        raise PortError('移植包里没找到 boot.img：%s' % donor_zip)
    log('   boot.img：%s（%s B）' % (boot_src, size(os.path.getsize(boot_src))))
    donor_sys = _find_dir(donor_dir, 'system')
    if not donor_sys:
        sys_img = _find(donor_dir, ['system.img'])
        if sys_img:
            log('   移植包给的是 system.img，解包…')
            donor_sys = os.path.join(donor_dir, 'system-unpacked')
            systemimg.extract(sys_img, donor_sys, force=True)
        else:
            raise PortError('移植包里既没有 system/ 目录也没有 system.img')
    log('   移植包 system 树：%s' % donor_sys)

    # ---------- 5. 原厂 system 树 ----------
    log('\n■ 第 5 步：准备原厂 system')
    stock_dir = os.path.join(work, 'stock')
    if not (os.path.isdir(stock_dir) and os.listdir(stock_dir)):
        src = stock_system or (os.path.join(work, 'backup', 'system.img')
                               if _need(os.path.join(work, 'backup', 'system.img')) else None)
        if not src:
            raise PortError('没有原厂 system.img：用 --stock-system 指定，或让第 2 步把 system 备份下来')
        log('   从 %s 解包…' % src)
        systemimg.extract(src, stock_dir, force=True)
    log('   原厂树：%s（%d 项）' % (stock_dir, len(os.listdir(stock_dir))))

    # 如果用的是自动草稿，现在把 system 那部分补进规则
    if not preset:
        sys_rules, notes = autoport.derive_system_rules(stock_dir, donor_sys,
                                                        chip=prof['chipset']['family'])
        rules.update(sys_rules)
        with open(rules_path, 'w', encoding='utf-8') as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        for n in notes:
            log('   · ' + n)

    # ---------- 6. 合并 + 打包 system ----------
    log('\n■ 第 6 步：合并 system 并打包')
    merged = os.path.join(work, 'build', 'system')
    if os.path.isdir(merged) and os.listdir(merged):
        log('   已有合并树，跳过合并（要重来就删掉 %s）' % merged)
    else:
        spec = (rules.get('system') or {})
        systemimg.merge(stock_dir, donor_sys, merged, spec)
    part = _part_size(prof, ('android', 'system'))
    out_sys = os.path.join(work, 'out', 'system.img')
    if part:
        # 只有包里真带 updater-script 时才拿它当权限/ SELinux 标签的准绳
        rom_for_meta = donor_zip if _has_updater(donor_zip) else None
        if not rom_for_meta:
            log('   ! 这个包里没有 updater-script，权限标签按默认规则生成')
        systemimg.pack(merged, out_sys, part, rom_zip=rom_for_meta)
        log('   ✓ system.img %s B  md5=%s' % (size(os.path.getsize(out_sys)), md5(out_sys)))
    else:
        raise PortError('分区表里没有 system 的大小，打不了包（先 root 扫描一次）')

    # ---------- 7. boot ----------
    log('\n■ 第 7 步：打 boot 补丁')
    out_boot = os.path.join(work, 'out', 'boot.img')
    b = bootimg.BootImg(boot_src)
    ent = cpio.parse(b.cpio_bytes)
    boot_sec = (rules.get('boot') or {}) if isinstance(rules, dict) else {}
    ent, rep = rules_mod.apply_to_ramdisk(ent, boot_sec, os.path.dirname(rules_path))
    rep.dump()
    n_edit = len(boot_sec.get('edits') or [])
    n_hit = len(rep.changed)
    if n_edit and not n_hit:
        log('   ! 警告：%d 条 boot 规则一条都没命中 —— 这个包的 ramdisk 与你预期的不同，'
            '刷进去也不会改 USB/存储/zram' % n_edit)
    elif n_edit:
        log('   boot 规则：%d 条，改到 %d 个文件（%s）'
            % (n_edit, n_hit, '、'.join(sorted(rep.changed)) or '无'))
    if rep.fail and not boot_sec.get('force_all_fail'):
        log('   ! 有 %d 条规则没命中（不会中止：移植包版本不同属常见）' % rep.fail)
    new_cpio = cpio.serialize(ent)
    b.build(new_cpio, out_boot, mode=(rules.get('boot') or {}).get('mode', 'inplace'))
    log('   ✓ boot.img %s B  md5=%s' % (size(os.path.getsize(out_boot)), md5(out_boot)))

    # ---------- 8. 刷机目录 ----------
    log('\n■ 第 8 步：生成刷机目录')
    flash_dir = os.path.join(work, 'flash')
    boot_p = _part_name(prof, ('bootimg', 'boot', 'kernel'), 'bootimg')
    sys_p = _part_name(prof, ('android', 'system'), 'android')
    if not scatter and (prof.get('dumchar_raw') or '').strip():
        # 没有原厂 scatter 也能出刷机目录：按设备自己的分区表反推一张
        from . import scattergen
        plat = (prof.get('chipset', {}).get('family') or 'mt6572').upper()
        text = scattergen.gen_from_dumchar(
            prof['dumchar_raw'],
            images={boot_p.lower(): out_boot, sys_p.lower(): out_sys},
            platform=plat, project=prof['product'].get('device') or 'auto')
        scatter = os.path.join(work, 'scatter-自动推导.txt')
        scattergen.write_scatter(text, scatter)
        log('   没给 --scatter：已按设备的 /proc/dumchar_info 自动推导一张')
        log('   （偏移 = preloader 起按分区顺序累加，本机实测与原厂 scatter 逐项一致）')
    if not scatter:
        log('   ! 没有 --scatter，设备档案里也没有分区表，跳过')
        log('     可用 scatter-gen 按 /proc/dumchar_info 自己生成一张，再用 scatter-flash 出目录')
        flash = None
    else:
        imgs, want = {}, []
        if 'BOOTIMG' in only.upper() or 'BOOT' in only.upper():
            imgs[boot_p] = out_boot
            want.append(boot_p)
        if 'ANDROID' in only.upper() or 'SYSTEM' in only.upper():
            imgs[sys_p] = out_sys
            want.append(sys_p)
        scatter_mod.make_flash_dir(scatter, want, flash_dir, images=imgs)
        _write_flash_readme(flash_dir, prof, out_boot, out_sys, rules_path)
        flash = flash_dir

    summary = {
        'device': {'model': prof['product']['model'], 'device': prof['product']['device'],
                   'kernel': prof['kernel']['release'], 'android': prof['android']['release']},
        'donor': os.path.basename(donor_zip),
        'rules': rules_path,
        'boot': out_boot, 'system': out_sys,
        'backup': os.path.join(work, 'backup') if man else None,
        'flash': flash,
        'md5': {'boot': md5(out_boot), 'system': md5(out_sys)},
    }
    with open(os.path.join(work, 'pipeline.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


    if flash and flash_mode == 'mtk':
        from . import mtkflash
        log('\n■ 第 9 步：用 mtkclient 写入（会一直等设备进 BROM）')
        mtkflash.write(flash)
    elif flash and flash_mode == 'spft':
        from . import flash as flash_mod
        sc, n_bad = flash_mod.check_dir(flash)
        if not n_bad:
            flash_mod.open_spft(flash, sc)

    log('\n■ 完成')
    log('   boot.img   %s  md5=%s' % (out_boot, summary['md5']['boot']))
    log('   system.img %s  md5=%s' % (out_sys, summary['md5']['system']))
    if flash:
        log('   刷机目录   %s' % flash)
        log('   下一步：打开 SP Flash Tool，选 %s 里的 scatter，Download Only，'
            '只勾 BOOTIMG/ANDROID，点 Download 再插 USB' % flash)
    else:
        log('   下一步：用原厂 scatter + scatter-flash 命令生成刷机目录')
    return summary


def _has_updater(zip_path):
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as z:
            return any(n in z.namelist() for n in
                       ('META-INF/com/google/android/updater-script',
                        'META-INF/com/google/android/update-script'))
    except Exception:
        return False


def _find_dir(root, name):
    for base, dirs, _files in os.walk(root):
        for d in dirs:
            if d.lower() == name:
                return os.path.join(base, d)
    return None


def _part_name(prof, names, default=None):
    """按别名找设备上真实的分区名（不同机型叫 bootimg/boot、android/system）。"""
    for p in prof.get('partitions') or []:
        if (p.get('name') or '').lower() in names:
            return p['name']
    return default


def _part_size(prof, names):
    for p in prof.get('partitions') or []:
        if p['name'].lower() in names:
            return p['size']
    return 0


def _write_flash_readme(flash_dir, prof, boot, sysimg, rules_path):
    txt = []
    txt.append('这台机器的地址（SP Flash Tool 点 Download 之前先核对）')
    txt.append('  型号：%s %s' % (prof['product']['brand'], prof['product']['model']))
    txt.append('  芯片：%s   内核：%s   系统基座：Android %s'
               % (prof['chipset']['family'], prof['kernel']['release'],
                  prof['android']['release']))
    txt.append('')
    txt.append('要刷的两个文件')
    txt.append('  BOOTIMG  %s  %s' % (os.path.basename(boot), size(os.path.getsize(boot))))
    txt.append('  ANDROID  %s  %s' % (os.path.basename(sysimg),
                                      size(os.path.getsize(sysimg))))
    txt.append('')
    txt.append('步骤')
    txt.append('  1. SP Flash Tool 里选本目录的 scatter 文件')
    txt.append('  2. 下拉框选 Download Only，只勾 BOOTIMG 和 ANDROID')
    txt.append('  3. 点 Download，然后插 USB（先点后插）')
    txt.append('  4. 等绿色对勾，拔线开机')
    txt.append('')
    txt.append('出问题怎么退回去')
    txt.append('  用同一份 scatter，把 BOOTIMG/ANDROID 换成备份目录里的 bootimg.img / system.img')
    txt.append('  规则文件：%s' % rules_path)
    import io as _io
    with _io.open(os.path.join(flash_dir, '刷机说明.txt'), 'w', encoding='utf-8',
                  newline='') as f:
        f.write('\r\n'.join(txt) + '\r\n')
