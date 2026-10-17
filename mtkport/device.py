# -*- coding: utf-8 -*-
"""扫描连着的安卓设备：把"这台机器是什么"变成一份能拿去选包的档案。

只用 adb（不需要 root 也能读到大部分东西；有 root 才能读分区表/做备份）。

档案里最要紧的四项，正好是选移植包的四把尺子：
    kernel.release   例如 3.4.67   —— 移植包必须同内核才能直接换（内核不换）
    chipset.family   例如 mt6572   —— 决定厂商库/固件从哪来
    android.release  例如 7.1.2    —— 原系统版本，决定"基础包"是哪一代
    partitions       bootimg/recovery/system 的起址与大小 —— 备份与刷机要用
"""
import json
import os
import re
import time

from .util import PortError, log, size, find_tool, run

# 想读的 prop（只挑有用的，避免上千行）
PROPS = [
    'ro.product.model', 'ro.product.brand', 'ro.product.manufacturer',
    'ro.product.device', 'ro.product.name', 'ro.product.board', 'ro.product.cpu.abi',
    'ro.product.locale', 'ro.board.platform', 'ro.hardware', 'ro.boot.hardware',
    'ro.build.version.release', 'ro.build.version.sdk', 'ro.build.version.incremental',
    'ro.build.version.security_patch', 'ro.build.display.id', 'ro.build.type',
    'ro.build.tags', 'ro.build.fingerprint', 'ro.build.date.utc',
    'ro.sf.lcd_density', 'ro.sf.hwrotation', 'ro.opengles.version',
    'ro.mediatek.platform', 'ro.mediatek.version.release', 'ro.mediatek.version.branch',
    'persist.sys.usb.config', 'ro.adb.secure', 'ro.debuggable', 'ro.secure',
    'dalvik.vm.heapsize', 'ro.config.low_ram', 'ro.zygote',
    'gsm.version.baseband', 'ro.telephony.default_network',
]

CHIP_RE = re.compile(r'(mt\d{4}|msm\d{4}|sdm\d{3}|apq\d{4}|rk\d{4}|exynos\d{4}|t\d{3})', re.I)


class Device(object):
    def __init__(self, serial=None, adb_port=5037, adb=None):
        self.adb_path = adb or find_tool('adb')
        self.adb_port = adb_port
        self.serial = serial
        self._props = None

    # ---------------- adb 基础 ----------------
    def _cmd(self, *args):
        c = [self.adb_path, '-P', str(self.adb_port)]
        if self.serial:
            c += ['-s', self.serial]
        c += [str(a) for a in args]
        return c

    def adb_run(self, *args, **kw):
        return run(self._cmd(*args), timeout=kw.get('timeout', 120), quiet=kw.get('quiet', True))

    def sh(self, cmd, timeout=120):
        rc, out = run(self._cmd('shell', cmd), timeout=timeout, quiet=True)
        return rc, out

    # ---------------- 设备枚举 ----------------
    @staticmethod
    def list_devices(adb=None, adb_port=5037):
        adb = adb or find_tool('adb')
        rc, out = run([adb, '-P', str(adb_port), 'devices'], quiet=True)
        devs = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or '\t' not in line:
                continue
            serial, state = line.split('\t', 1)
            if state.strip() != 'device':
                continue
            devs.append({'serial': serial.strip(),
                         'state': state.strip(),
                         'emulator': serial.startswith('emulator-'),
                         'wifi': ':' in serial})
        return devs

    @classmethod
    def connect(cls, host='192.168.0.47:5555', adb_port=5037, adb=None):
        adb = adb or find_tool('adb')
        return run([adb, '-P', str(adb_port), 'connect', host], quiet=True)

    @classmethod
    def pick(cls, serial=None, adb_port=5037, adb=None):
        """挑设备：指定优先；否则优先 WiFi 串（不受 USB 句柄争抢），再非模拟器。"""
        adb = adb or find_tool('adb')
        devs = cls.list_devices(adb, adb_port)
        real = [d for d in devs if not d['emulator']]
        if serial:
            for d in real:
                if d['serial'] == serial:
                    return cls(serial, adb_port, adb)
            raise PortError('没找到设备 %s（当前可用：%s）'
                            % (serial, '、'.join(d['serial'] for d in real) or '无'))
        if not real:
            raise PortError('没有在线的安卓设备：请先插 USB 或 adb connect <ip>:5555')
        real.sort(key=lambda d: (not d['wifi'], d['serial']))
        return cls(real[0]['serial'], adb_port, adb)

    # ---------------- 读数 ----------------
    def props(self, refresh=False):
        if self._props is not None and not refresh:
            return self._props
        rc, out = self.sh('getprop')
        if rc != 0:
            raise PortError('读不到设备属性（adb shell getprop 失败）：%s' % out.strip()[:200])
        p = {}
        for line in out.splitlines():
            m = re.match(r'\[([^\]]+)\]:\s*\[(.*)\]\s*$', line.strip())
            if m:
                p[m.group(1)] = m.group(2)
        self._props = p
        return p

    def prop(self, key, default=''):
        return self.props().get(key, default)

    def is_root(self):
        rc, out = self.sh('id')
        return 'uid=0' in out

    def root(self):
        """尝试拿 root（adbd 重启后会断一下，属正常）。"""
        if self.is_root():
            return True
        run(self._cmd('root'), quiet=True)
        time.sleep(3)
        if ':' in (self.serial or ''):
            run([self.adb_path, '-P', str(self.adb_port), 'connect', self.serial], quiet=True)
        else:
            run(self._cmd('wait-for-device'), quiet=True)
        time.sleep(2)
        return self.is_root()

    def kernel(self):
        rc, a = self.sh('uname -r')
        release = a.strip().splitlines()[0].strip() if rc == 0 and a.strip() else ''
        rc2, b = self.sh('uname -v')
        version = b.strip().splitlines()[0].strip() if rc2 == 0 and b.strip() else ''
        rc3, c = self.sh('uname -m')
        arch = c.strip().splitlines()[0].strip() if rc3 == 0 and c.strip() else ''
        mm = '.'.join(release.split('.')[:2]) if release else ''
        return {'release': release, 'version': version, 'arch': arch, 'major_minor': mm}

    def screen(self):
        w = h = d = 0
        rc, out = self.sh('wm size')
        m = re.search(r'(\d+)x(\d+)', out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        rc, out = self.sh('wm density')
        m = re.search(r'(\d+)', out)
        if m:
            d = int(m.group(1))
        return {'width': w, 'height': h, 'density': d,
                'hwrotation': self.prop('ro.sf.hwrotation') or '0'}

    def memory_mb(self):
        rc, out = self.sh('cat /proc/meminfo')
        m = re.search(r'MemTotal:\s+(\d+)\s*kB', out)
        return int(m.group(1)) // 1024 if m else 0

    def sysfs_map(self):
        """从 /sys/class/block 拿真实扇区偏移：{节点名: (start_bytes, size_bytes, partname)}。

        dumchar 的 StartAddr 是分区表算出来的，sysfs 是内核视图，两者能互相校验；
        对没有 dumchar 的机器（非 MTK 或老内核）sysfs 还能直接给出偏移。
        """
        nodes = self.sh('ls /sys/class/block')[1].split()
        cmd = 'for n in %s; do echo "@@$n"; cat /sys/class/block/$n/start; ' \
              'cat /sys/class/block/$n/size; cat /sys/class/block/$n/uevent; done' % ' '.join(nodes)
        rc, out = self.sh(cmd)
        res = {}
        if rc != 0:
            return res
        cur, buf = None, []
        for line in out.splitlines():
            if line.startswith('@@'):
                if cur:
                    res[cur] = self._sysfs_entry(buf)
                cur, buf = line[2:], []
            else:
                buf.append(line)
        if cur:
            res[cur] = self._sysfs_entry(buf)
        return {k: v for k, v in res.items() if v}

    @staticmethod
    def _sysfs_entry(buf):
        start = size = None
        partname = ''
        for line in buf:
            m = re.match(r'^PARTNAME=(.+)$', line.strip())
            if m:
                partname = m.group(1)
            elif line.strip().isdigit():
                if start is None:
                    start = int(line.strip())
                elif size is None:
                    size = int(line.strip())
        if start is None or size is None:
            return None
        return (start * 512, size * 512, partname)

    def partitions(self):
        """解析分区表：MTK 用 /proc/dumchar_info，别的用 /proc/partitions。

        两条路都会再用 /sys/class/block 的真实偏移交叉校验一遍（字段 sysfs_start /
        sysfs_size / offset_ok），这样"分区偏移到底对不对"有第二个来源作证。
        """
        sysfs = self.sysfs_map()
        rc, out = self.sh('cat /proc/dumchar_info')
        parts = []
        if rc == 0 and 'Part_Name' in out:
            for line in out.splitlines():
                f = line.split()
                if len(f) < 5 or f[0] == 'Part_Name':
                    continue
                try:
                    sz = int(f[1], 16)
                    st = int(f[2], 16)
                except ValueError:
                    continue
                parts.append({'name': f[0], 'size': sz, 'start': st,
                              'type': f[3], 'map_to': f[4], 'format': 'mtk-dumchar'})
            self._cross_check(parts, sysfs)
            return parts

        rc, out = self.sh('cat /proc/partitions')
        for line in out.splitlines():
            f = line.split()
            if len(f) < 4 or not f[0].isdigit():
                continue
            name = f[3]
            item = {'name': name, 'size': int(f[2]) * 1024, 'start': None,
                    'type': '', 'map_to': '/dev/block/' + name,
                    'format': 'proc-partitions'}
            node = name.split('/')[-1]
            if node in sysfs:
                item['start'] = sysfs[node][0]
                item['size'] = sysfs[node][1]
                item['format'] = 'sysfs'
                if sysfs[node][2]:
                    item['name'] = sysfs[node][2]
            parts.append(item)
        self._cross_check(parts, sysfs)
        return parts

    @staticmethod
    def _cross_check(parts, sysfs):
        """把 sysfs 的真实偏移填进分区，并标出 dumchar 与 sysfs 不一致的项。"""
        for p in parts:
            node = (p.get('map_to') or '').split('/')[-1]
            if node in sysfs:
                s, z, _pn = sysfs[node]
                p['sysfs_start'] = s
                p['sysfs_size'] = z
                p['offset_ok'] = (p.get('start') is None or p['start'] == s)
                if p.get('size') and z and p['size'] != z:
                    p['size_ok'] = (p['size'] == z)

    def find_partition(self, parts, want):
        """按名字找分区（bootimg/boot 都算 boot）。"""
        alias = {'boot': ('bootimg', 'boot', 'kernel'), 'recovery': ('recovery',),
                 'system': ('android', 'system'), 'userdata': ('usrdata', 'userdata'),
                 'cache': ('cache',), 'nvram': ('nvram',), 'metadata': ('metadata',)}
        names = alias.get(want, (want,))
        low = [n.lower() for n in names]
        for p in parts:
            if p['name'].lower() in low:
                return p
        return None

    def scan(self):
        p = self.props()
        kern = self.kernel()
        scr = self.screen()
        parts = self.partitions() if self.is_root() else []
        dumchar_raw = ''
        if parts and parts[0].get('format') == 'mtk-dumchar':
            rc, out = self.sh('cat /proc/dumchar_info')
            if rc == 0:
                dumchar_raw = out

        raw_parts = []
        for k in ('ro.board.platform', 'ro.hardware', 'ro.boot.hardware',
                  'ro.mediatek.platform', 'ro.product.board'):
            v = (p.get(k) or '').strip()
            if v and v.lower() not in [x.lower() for x in raw_parts]:
                raw_parts.append(v)
        raw_chip = ' / '.join(raw_parts)
        m = CHIP_RE.search(' '.join(raw_parts))
        family = m.group(1).lower() if m else ''
        num = re.sub(r'\D', '', family)
        prof = {
            'serial': self.serial,
            'via': 'wifi' if ':' in (self.serial or '') else 'usb',
            'root': self.is_root(),
            'dumchar_raw': dumchar_raw,
            'captured': time.strftime('%Y-%m-%d %H:%M:%S'),
            'props': {k: p.get(k, '') for k in PROPS if k in p},
            'kernel': kern,
            'chipset': {'raw': raw_chip.strip(), 'family': family, 'num': num,
                        'mtk': family.startswith('mt')},
            'android': {
                'release': p.get('ro.build.version.release', ''),
                'sdk': int(p.get('ro.build.version.sdk') or 0),
                'incremental': p.get('ro.build.version.incremental', ''),
                'display_id': p.get('ro.build.display.id', ''),
                'fingerprint': p.get('ro.build.fingerprint', ''),
            },
            'product': {k: p.get('ro.product.' + k, '') for k in
                        ('brand', 'model', 'device', 'name', 'manufacturer', 'board')},
            'display': scr,
            'memory_mb': self.memory_mb(),
            'partitions': parts,
        }
        return prof

    # ---------------- 给人看的报告 ----------------
    @staticmethod
    def report(prof):
        log('■ 设备档案')
        log('   序列号 %s（%s）   root=%s'
            % (prof['serial'], '无线' if prof['via'] == 'wifi' else 'USB', prof['root']))
        pr = prof['product']
        log('   型号 %s %s（device=%s board=%s）'
            % (pr['brand'], pr['model'], pr['device'], pr['board']))
        log('   芯片 %s（家族 %s）' % (prof['chipset']['raw'], prof['chipset']['family']))
        log('   内核 %s  %s  %s' % (prof['kernel']['release'], prof['kernel']['arch'],
                                 prof['kernel']['version'][:40]))
        log('   系统 Android %s（SDK %s）%s'
            % (prof['android']['release'], prof['android']['sdk'],
               prof['android']['display_id']))
        d = prof['display']
        log('   屏幕 %dx%d 密度 %d hwrotation=%s' % (d['width'], d['height'], d['density'],
                                                d['hwrotation']))
        log('   内存 %d MB' % prof['memory_mb'])
        if prof['partitions']:
            log('   分区表 %d 项（%s）：' % (len(prof['partitions']),
                                        prof['partitions'][0]['format']))
            for want in ('boot', 'recovery', 'system', 'userdata', 'cache'):
                pp = None
                alias = {'boot': ('bootimg', 'boot'), 'recovery': ('recovery',),
                         'system': ('android', 'system'), 'userdata': ('usrdata', 'userdata'),
                         'cache': ('cache',)}
                for q in prof['partitions']:
                    if q['name'].lower() in alias[want]:
                        pp = q
                        break
                if pp:
                    log('      %-9s %-10s %10s B  起址 0x%X'
                        % (want, pp['name'], size(pp['size']), pp['start'] or 0))
        else:
            log('   分区表：没读到（需要 root）')

    # ---------------- 落盘 ----------------
    @staticmethod
    def save(prof, path):
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(prof, f, ensure_ascii=False, indent=2)
        return path
