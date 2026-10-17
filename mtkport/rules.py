# -*- coding: utf-8 -*-
"""声明式补丁规则引擎（ramdisk / 一般文本树通用）。

规则文件是 JSON，结构见 配置文件说明。支持的操作：

  {"file": "default.prop", "type": "prop",    "key": "ro.adb.secure", "value": "0"}
  {"file": "fstab.mt6572", "type": "replace", "old": "emmc@fat", "new": "7"}
  {"file": "xxx.rc",       "type": "regex",   "pattern": "voldmanaged=...", "repl": "..."}
  {"file": "xxx.rc",       "type": "append",  "text": "on property:sys.boot_completed=1\\n    write ..."}
  {"file": "init.d.sh",    "type": "add",     "source": "files/init.d.sh", "mode": "0755"}
  {"file": "旧的.rc",       "type": "delete"}
  {"file": "bin/sh",       "type": "chmod",   "mode": "0755"}

每个操作都可以加：
  "count": N       要求恰好出现 N 次（默认：replace/regex 至少要 1 次，否则整份规则失败）
  "optional": true  允许找不到（只提示，不失败）
  "why": "人话理由" 记入报告，方便以后看懂当初为什么这么改
  "expect": "正则"  改完必须能匹配到，否则失败（防"以为改了其实没改"）
"""
import json
import os
import re

from . import cpio
from .util import PortError, log

OPS = ('prop', 'replace', 'regex', 'append', 'add', 'delete', 'chmod')


class Report(object):
    def __init__(self):
        self.lines = []
        self.changed = set()
        self.fail = 0

    def ok(self, s):
        self.lines.append('   ✓ ' + s)

    def warn(self, s):
        self.lines.append('   ! ' + s)

    def bad(self, s):
        self.lines.append('   ✗ ' + s)
        self.fail += 1

    def dump(self):
        for l in self.lines:
            log(l)


def load_rules(path):
    with open(path, 'rb') as f:
        data = json.loads(f.read().decode('utf-8-sig'))
    rules = data.get('boot', data.get('ramdisk', data))
    edits = rules.get('edits', []) if isinstance(rules, dict) else rules
    if not isinstance(edits, list):
        raise PortError('规则文件里 edits 应该是列表')
    for i, e in enumerate(edits):
        if 'type' not in e or 'file' not in e:
            raise PortError('第 %d 条规则缺 file/type：%r' % (i + 1, e))
        if e['type'] not in OPS:
            raise PortError('第 %d 条规则 type=%r 不认识（支持 %s）'
                            % (i + 1, e['type'], '/'.join(OPS)))
    return rules


# ---------------- 单文件操作 ----------------
def _prop_set(text, key, value):
    """把 key=value 设成新值：有该键就替换整行，没有就追加到末尾。"""
    pat = re.compile(r'(?m)^\s*%s\s*=.*$' % re.escape(key))
    newline = key + '=' + value
    if pat.search(text):
        return pat.sub(newline, text), 'replaced'
    if text and not text.endswith('\n'):
        text += '\n'
    return text + newline + '\n', 'appended'


def apply_to_text(text, op, base_dir, rep, name):
    t = op['type']
    why = op.get('why', '')

    if t == 'prop':
        new, how = _prop_set(text, op['key'], op['value'])
        rep.ok('%s: %s=%s（%s）%s%s' % (name, op['key'], op['value'], how,
                                       '  ← ' + why if why else '',
                                       '  [%s]' % op['expect'] if op.get('expect') else ''))
        return new

    if t == 'replace':
        old, new = op['old'], op['new']
        c = text.count(old)
        want = op.get('count')
        if c == 0:
            if op.get('optional'):
                rep.warn('%s: 没找到 %r，跳过（optional）' % (name, old[:60]))
                return text
            rep.bad('%s: 没找到要替换的 %r —— 文件版本可能不同' % (name, old[:80]))
            return text
        if want is not None and c != want:
            rep.bad('%s: %r 出现 %d 次，规则要求 %d 次' % (name, old[:60], c, want))
            return text
        rep.ok('%s: %r → %r ×%d%s' % (name, old[:52], new[:44], c,
                                      '  ← ' + why if why else ''))
        return text.replace(old, new)

    if t == 'regex':
        pat = re.compile(op['pattern'], re.M)
        hits = pat.findall(text)
        if not hits:
            if op.get('optional'):
                rep.warn('%s: 正则 %r 没匹配，跳过（optional）' % (name, op['pattern']))
                return text
            rep.bad('%s: 正则 %r 没匹配 —— 文件版本可能不同' % (name, op['pattern']))
            return text
        rep.ok('%s: 正则 %r → %r ×%d%s' % (name, op['pattern'][:48],
                                          (op.get('repl') or '')[:44], len(hits),
                                          '  ← ' + why if why else ''))
        return pat.sub(op.get('repl', ''), text)

    if t == 'append':
        add = op['text'].replace('\\n', '\n')
        body = text if text.endswith('\n') or not text else text + '\n'
        rep.ok('%s: 末尾追加 %d 行%s' % (name, add.count('\n') + 1,
                                        '  ← ' + why if why else ''))
        return body + add + ('' if add.endswith('\n') else '\n')

    raise PortError('内部错误：apply_to_text 不支持 %s' % t)


# ---------------- 作用于 cpio ramdisk ----------------
def apply_to_ramdisk(ent, rules, base_dir='.'):
    """对 cpio Entry 列表应用规则，返回 (新 Entry 列表, Report)。"""
    rep = Report()
    if isinstance(rules, dict):
        if 'edits' not in rules and isinstance(rules.get('boot'), dict):
            # 传进来的是整个规则文件（带 boot/system 段）
            rules = rules.get('boot') or {}
        edits = rules.get('edits', [])
        required = rules.get('required', [])
    else:
        edits, required = rules, []
    names = set(e.name.lstrip('./') for e in ent)

    for req in required:
        r = req.lstrip('./')
        if r not in names:
            rep.bad('必需条目 %s 不存在于 ramdisk' % r)

    by_name = {}
    for e in ent:
        by_name[e.name.lstrip('./')] = e

    for op in edits:
        name = op['file'].lstrip('./')
        t = op['type']

        if t == 'add':
            src = op['source']
            if not os.path.isabs(src):
                src = os.path.join(base_dir, src)
            if not os.path.isfile(src):
                rep.bad('%s: 源文件不存在 %s' % (name, src))
                continue
            with open(src, 'rb') as f:
                data = f.read()
            mode = int(op.get('mode', '0644'), 8)
            uid = int(op.get('uid', 0))
            gid = int(op.get('gid', 0))
            if name in by_name:
                e = by_name[name]
                e.set_data(data)
                e.fields['mode'] = cpio.S_IFREG | mode
                e.fields['uid'], e.fields['gid'] = uid, gid
                rep.ok('%s: 用本地文件覆盖（%d B）' % (name, len(data)))
            else:
                # 保证父目录条目存在
                _ensure_parents(ent, name)
                e = cpio.Entry(name, data, mode=cpio.S_IFREG | mode, uid=uid, gid=gid)
                ent.append(e)
                by_name[name] = e
                rep.ok('%s: 新增条目（%d B，权限 %s）%s'
                       % (name, len(data), op.get('mode', '0644'),
                          '  ← ' + op.get('why', '') if op.get('why') else ''))
            continue

        e = by_name.get(name)
        if e is None:
            if op.get('optional'):
                rep.warn('%s 不在 ramdisk 里，跳过（optional）' % name)
                continue
            rep.bad('%s 不在 ramdisk 里' % name)
            continue

        if t == 'delete':
            ent.remove(e)
            del by_name[name]
            rep.ok('%s: 删除' % name)
            continue

        if t == 'chmod':
            e.fields['mode'] = (e.fields['mode'] & ~0o7777) | int(op['mode'], 8)
            rep.ok('%s: 权限 → %s' % (name, op['mode']))
            continue

        # 文本类操作
        text = e.text()
        new = apply_to_text(text, op, base_dir, rep, name)
        if op.get('expect') and not re.search(op['expect'], new, re.M):
            rep.bad('%s: 改完了但 expect 正则 %r 仍不匹配' % (name, op['expect']))
            continue
        if new != text:
            e.set_text(new)
            rep.changed.add(name)

    return ent, rep


def _ensure_parents(ent, name):
    have = set(e.name.lstrip('./') for e in ent)
    parts = name.split('/')[:-1]
    cur = ''
    for p in parts:
        cur = p if not cur else cur + '/' + p
        if cur and cur not in have:
            ent.append(cpio.Entry(cur, b'', mode=cpio.S_IFDIR | 0o755, nlink=2))
            have.add(cur)
