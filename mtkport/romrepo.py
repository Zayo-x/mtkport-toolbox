# -*- coding: utf-8 -*-
"""从 kc.fuquan.moe（OpenList/AList 网盘程序）里找合适的移植包。

站点是 OpenList，公开接口就三个，全部 POST JSON：
    /api/fs/list    {path,password,page,per_page,refresh}  -> data.content[] 目录内容
    /api/fs/get     {path,password}                        -> data.raw_url 临时直链
    /api/fs/search  {parent,keywords,scope,page,per_page}  -> data.content[] 带 parent

移植包目录的组织方式很规整：`<内核版本>   <安卓版本>`，例如
    /5.移植包大全/MT6572&6582&6592&6574&6571&8384/3.4.67     7.1/
这就让"选包"变成一件可以判定的事：先按内核版本锁定目录（移植不换内核），
再按安卓版本挑（要新的就往上挑），最后在同一个目录里给每个包打分。
"""
import json
import os
import re
import urllib.parse
import urllib.request

from .util import PortError, log, human_bytes, size

BASE = 'https://kc.fuquan.moe'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')
ROOT = '/5.移植包大全'

# 安卓版本目录名 -> 版本号
VER_RE = re.compile(r'^(\d+\.\d+)\s*$')
KERNEL_DIR_RE = re.compile(
    r'^(?:(?:x32|x64|arm64|arm|a32|a64)\s+)?(\d+\.\d+(?:\.\d+)?)\s+(\d+\.\d+)', re.I)
UNKNOWN_KERNEL_RE = re.compile(r'^未知内核\s+(\d+\.\d+)')

# 名字里出现这些字样 = 界面重、512MB 内存的机器跑不动
HEAVY = ['miui', 'flyme', 'sense', 'emui', 'oneui', 'coloros', 'funtouch',
         '仿ios', '仿苹果', 'miui10ui']
# 名字里出现这些 = 相对稳、体积小
LIGHT = ['lineage', 'aosp', 'aokp', 'cm1', 'cm-', 'cyanogen', 'slim', 'omni',
         'pac', 'mokee', 'mk.', 'rr-', 'resurrection', 'dot', 'xosp', 'viper']


def _post(path, payload, timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        BASE + path, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': UA,
                 'Referer': BASE + '/'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        raise PortError('访问 %s 失败：%s' % (BASE + path, e))


def _pf(prof, sec, key, default=''):
    """安全取档案字段：prof['sec']['key']，缺了给默认值。"""
    val = (prof or {}).get(sec)
    if isinstance(val, dict):
        val = val.get(key)
    return val if val not in (None, '') else default


class Repo(object):
    def __init__(self, base=BASE, root=ROOT):
        self.base = base.rstrip('/')
        self.root = root

    def ls(self, path, per_page=1000):
        j = _post('/api/fs/list', {'path': path, 'password': '', 'page': 1,
                                   'per_page': per_page, 'refresh': False})
        if j.get('code') != 200:
            raise PortError('列目录失败 %s：%s' % (path, j.get('message')))
        return (j.get('data') or {}).get('content') or []

    def get(self, path):
        j = _post('/api/fs/get', {'path': path, 'password': ''})
        if j.get('code') != 200:
            raise PortError('取文件信息失败 %s：%s' % (path, j.get('message')))
        return j.get('data') or {}

    def search(self, keywords, parent='/'):
        j = _post('/api/fs/search', {'parent': parent, 'keywords': keywords,
                                     'scope': 0, 'page': 1, 'per_page': 100})
        if j.get('code') != 200:
            return []
        return (j.get('data') or {}).get('content') or []

    # ---------------- 找目录 ----------------
    def platform_dirs(self):
        """列出各芯片平台目录（/5.移植包大全 下第一层）。"""
        out = []
        for it in self.ls(self.root):
            if it.get('is_dir'):
                out.append(it['name'])
        return out

    def pick_platform_dir(self, prof):
        """按芯片家族挑平台目录（目录名可能是 MT6572&6582&... 这种多个型号合在一起）。"""
        num = _pf(prof, 'chipset', 'num')
        fam = _pf(prof, 'chipset', 'family')
        dirs = self.platform_dirs()
        if not num:
            return None, dirs
        # 目录名里常常只写数字（MT6575&6577），所以按"纯数字"匹配
        num_d = re.sub(r'\D', '', num or '')
        fam_d = re.sub(r'\D', '', fam or '')
        exact, loose = None, None
        for d in dirs:
            upd = re.sub(r'\D', '', d)
            if num_d and num_d in upd:
                exact = d
                break
            if fam_d and fam_d in upd and loose is None:
                loose = d
        if exact is None and num_d:
            # 退一步：目录里写了同一芯片家族的其它成员（如 6572&6582 里找 6574）
            for d in dirs:
                if fam_d and fam_d in re.sub(r'\D', '', d):
                    loose = d
                    break
        return (exact or loose), dirs

    def kernel_dirs(self, plat_dir):
        """平台目录下的 `<内核>  <安卓>` 子目录，返回 [(dir, kernel, android)]。"""
        out = []
        for it in self.ls(self.root + '/' + plat_dir):
            if not it.get('is_dir'):
                continue
            name = it['name'].strip()
            m = KERNEL_DIR_RE.match(name)
            if m:
                out.append((it['name'], m.group(1), float(m.group(2))))
                continue
            um = UNKNOWN_KERNEL_RE.match(name)
            if um:
                out.append((it['name'], '', float(um.group(1))))
                continue
            else:
                vm = VER_RE.match(it['name'].strip())
                if vm:  # 少数目录只有版本号，没有内核版本
                    out.append((it['name'], '', float(vm.group(1))))
        return out

    # ---------------- 打分的核心 ----------------
    def candidates(self, prof, android=None, limit=40, platform=None,
                   allow_kernel_mismatch=False):
        """返回带分数的候选包列表（分数越高越合适）。

        platform: 强制用某个平台目录（站内没你芯片的目录时手动指一个相近的）。
        allow_kernel_mismatch: 没有同内核版本目录时，就近挑一个并打警告（内核不对
            通常挪不动，这只是"实在没有"的兜底）。
        """
        if platform:
            plat = platform
            all_plats = self.platform_dirs()
            if plat not in all_plats:
                raise PortError('指定的平台目录 %s 在站内不存在（现有：%s）'
                                % (plat, '、'.join(all_plats)))
        else:
            plat, all_plats = self.pick_platform_dir(prof)
            if not plat:
                raise PortError('站内没找到匹配芯片 %s 的平台目录（现有：%s）。'
                                '可以用 --platform 指定一个相近的'
                                % (_pf(prof, 'chipset', 'num'), '、'.join(all_plats)))
        kdirs = self.kernel_dirs(plat)
        if not kdirs:
            raise PortError('平台目录 %s 下没有识别出 <内核 版本> 子目录' % plat)
        krel = _pf(prof, 'kernel', 'release')
        kmm = _pf(prof, 'kernel', 'major_minor') or '.'.join(krel.split('.')[:2])
        rel = _pf(prof, 'android', 'release')
        dev_android = float(rel.rsplit('.', 1)[0]) if rel.count('.') >= 1 else 0.0

        # 内核必须是"同一条"：移植不换内核，内核版本对不上直接挪不了
        same_k = [k for k in kdirs if k[1] == krel or (k[1] and krel.startswith(k[1]))]
        if not same_k:
            same_k = [k for k in kdirs if k[1].startswith(kmm)]
        if not same_k and allow_kernel_mismatch:
            # 就近挑一个内核目录：先比 major.minor 数字差，再比第三位
            def kdist(k):
                a = [int(x) for x in (k[1].split('.') if k[1] else [])[:3]]
                b = [int(x) for x in (krel.split('.'))[:3]]
                while len(a) < 3:
                    a.append(0)
                while len(b) < 3:
                    b.append(0)
                return sum((a[i] - b[i]) * (100 ** (2 - i)) for i in range(3))
            nearest = min(kdirs, key=kdist)
            log('   ! 没有与内核 %s 同版本的目录，就近用 %s（--any-kernel 兜底，'
                '刷前务必确认能开）' % (krel, nearest[1]))
            same_k = [nearest]
        if not same_k:
            raise PortError('没有和本机内核 %s 同版本的目录（站内这台芯片有：%s）。'
                            '加 --any-kernel 可就近挑一个'
                            % (krel, '、'.join('%s/%s' % (k[1], k[2]) for k in kdirs)))

        out = []
        for dname, dkern, dver in same_k:
            if android and abs(dver - float(android)) > 0.01:
                continue
            for it in self.ls(self.root + '/' + plat + '/' + dname):
                if it.get('is_dir'):
                    continue
                nm = it['name']
                low = nm.lower()
                if not low.endswith(('.zip', '.7z', '.rar', '.tar', '.gz')):
                    continue
                sc, why = self._score(nm, prof, dkern, dver, dev_android)
                out.append({'name': nm, 'score': sc, 'why': why, 'size': it.get('size') or 0,
                            'dir': dname, 'path': '%s/%s/%s/%s' % (self.root, plat, dname, nm),
                            'kernel': dkern, 'android': dver})
        out.sort(key=lambda x: (-x['score'], x['size']))
        return out[:limit], plat, [k[1] + '/' + str(k[2]) for k in kdirs]

    @staticmethod
    def _score(name, prof, dkern, dver, dev_android):
        """打分：内核精确 + 设备型号命中 + 安卓版本高 + 轻量 ROM - 重 UI。"""
        low = name.lower()
        sc = 100.0
        why = []
        krel = _pf(prof, 'kernel', 'release')
        if dkern and krel.startswith(dkern):
            if dkern == krel:
                sc += 30
                why.append('内核完全一致(%s)' % dkern)
            else:
                sc += 10
                why.append('内核前缀一致(%s)' % dkern)
        # 型号/代号命中（这台机器是 A102，站内正好有 A102 的包）
        for tag, val in (('设备代号', _pf(prof, 'product', 'device')),
                         ('型号', _pf(prof, 'product', 'model')),
                         ('品牌', _pf(prof, 'product', 'brand'))):
            if val and len(val) >= 3 and val.lower() in low:
                sc += 40
                why.append('%s命中(%s)' % (tag, val))
        # 站内有芯片号写进名字的，也加分
        num = _pf(prof, 'chipset', 'num')
        if num and num in low:
            sc += 12
            why.append('名字含芯片 %s' % num)
        # 安卓版本：比现系统新的更值得，但同代最稳
        if dver > dev_android + 0.05:
            sc += 15
            why.append('安卓 %.1f 比现系统新' % dver)
        elif abs(dver - dev_android) <= 0.05:
            sc += 8
            why.append('安卓 %.1f 与现系统同代' % dver)
        else:
            sc -= 20
            why.append('安卓 %.1f 比现系统旧' % dver)
        for h in HEAVY:
            if h in low:
                sc -= 30
                why.append('重 UI(%s)，%d MB 内存吃力' % (h, prof.get('memory_mb') or 0))
                break
        for lt in LIGHT:
            if lt in low:
                sc += 10
                why.append('轻量类(%s)' % lt)
                break
        mem = prof.get('memory_mb') or 0
        if mem and mem < 800:
            if any(x in low for x in ('fork', '仿', 'ui-')):
                sc -= 5
                why.append('小内存慎选')
        # 明显不是系统包的排除
        if any(x in low for x in ('补丁', 'fix', '汉化', '教程', '驱动', 'twrp', 'recovery.img')):
            sc -= 60
            why.append('不是完整系统包')
        return sc, '；'.join(why)

    # ---------------- 下载 ----------------
    def download(self, path, out_file, expect_size=None, chunk=1 << 20, retries=3):
        """按 /api/fs/get 拿到的直链下载，支持断点续传与大小校验。"""
        info = self.get(path)
        url = info.get('raw_url') or ''
        total = info.get('size') or expect_size or 0
        if not url:
            raise PortError('这个文件拿不到直链：%s' % path)
        os.makedirs(os.path.dirname(os.path.abspath(out_file)) or '.', exist_ok=True)
        got = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        if total and got == total:
            log('   已经下载完整（%s），跳过' % human_bytes(total))
            return out_file
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': UA})
                if got:
                    req.add_header('Range', 'bytes=%d-' % got)
                with urllib.request.urlopen(req, timeout=120) as r:
                    mode = 'ab' if (got and r.status == 206) else 'wb'
                    if mode == 'wb':
                        got = 0
                    t0 = None
                    import time
                    t0 = time.time()
                    with open(out_file, mode) as f:
                        while True:
                            b = r.read(chunk)
                            if not b:
                                break
                            f.write(b)
                            got += len(b)
                            if total and got % (8 << 20) < chunk:
                                dt = max(time.time() - t0, 0.001)
                                log('      %s / %s  (%.1f MB/s)'
                                    % (human_bytes(got), human_bytes(total),
                                       (got / dt) / 1048576.0))
                if total and os.path.getsize(out_file) != total:
                    raise PortError('大小不符：%d / %d' % (os.path.getsize(out_file), total))
                log('   ✓ 下载完成 %s（%s）' % (out_file, human_bytes(os.path.getsize(out_file))))
                return out_file
            except Exception as e:
                log('   ! 第 %d 次下载中断：%s' % (attempt, e))
                got = os.path.getsize(out_file) if os.path.exists(out_file) else 0
                if attempt == retries:
                    raise PortError('下载失败（已重试 %d 次）：%s' % (retries, e))
        return out_file
