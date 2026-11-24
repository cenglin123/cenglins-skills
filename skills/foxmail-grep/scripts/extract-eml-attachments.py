#!/usr/bin/env python3
"""foxgrep 附加脚本: 把已导出的 .eml 批量拆成「附件目录 + 邮件索引.csv」。

用途
----
Foxmail 7.1+/7.2 的本地邮件库是**全库加密**的，`--live extract` 在
「无语料库 + 无渲染缓存 + POP3 无副本」时四级兜底必然全空。唯一稳妥出路是
让 Foxmail 自己导出 `.eml`（列表多选 → 右键「导出邮件」），之后用本脚本解附件。

用法
----
    python extract-eml-attachments.py <eml目录> <附件输出目录> [--index 索引csv路径]

输出
----
- `<附件输出目录>/<日期>_<主题>/<附件名>`   （同名自动加 _1/_2 后缀）
- `<索引csv路径>`（默认 <eml目录的上级>/邮件索引.csv）:
  日期, 发件人, 收件人, 主题, 附件数, 附件清单

注意
----
- 只读 .eml，不修改邮件；不联网。
- 主题里的 `\\ / : * ? " < > |` 会被替换为 `_`，目录名结尾的空格/点会被去掉
  （否则 Windows 路径失效）。
- 依赖同目录上一级（技能根）的 foxgrep.py —— 因此本脚本应放在
  `<skill>/scripts/` 下，或把 foxgrep.py 所在目录加入 sys.path。
"""
import csv
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
import foxgrep  # noqa: E402


def safe_name(s, n=60):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', s or '').strip(' .')
    return s[:n] or 'no-subject'


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    src, out = argv[1], argv[2]
    index_path = None
    if '--index' in argv:
        index_path = argv[argv.index('--index') + 1]
    if not index_path:
        index_path = os.path.join(os.path.dirname(os.path.abspath(src.rstrip('\\/'))), '邮件索引.csv')
    if not os.path.isdir(src):
        print('目录不存在: %s' % src, file=sys.stderr)
        return 2
    os.makedirs(out, exist_ok=True)

    rows, n_att, n_bytes, fails = [], 0, 0, []
    files = sorted(f for f in os.listdir(src) if f.lower().endswith('.eml'))
    for f in files:
        p = os.path.join(src, f)
        try:
            m = foxgrep.parse_eml(p)
        except Exception as e:
            fails.append((f, 'parse: %s' % e))
            continue
        dt = m['date'].strftime('%Y-%m-%d') if m['date'] else 'nodate'
        sub = safe_name(m['subject'])
        atts = m['attachments']
        rows.append({
            'file': f, 'date': dt, 'from': m['from'], 'to': ';'.join(m['to']),
            'subject': m['subject'], 'n_att': len(atts),
            'attachments': ' | '.join('%s(%d)' % (a['name'], a['size']) for a in atts),
        })
        if not atts:
            continue
        d = os.path.join(out, '%s_%s' % (dt, sub))
        os.makedirs(d, exist_ok=True)
        for a in atts:
            name = re.split(r'[\\/]', a['name'])[-1].rstrip(' .') or 'unnamed'
            stem, ext = os.path.splitext(name)
            q = os.path.join(d, name)
            k = 1
            while os.path.exists(q):
                q = os.path.join(d, '%s_%d%s' % (stem, k, ext))
                k += 1
            try:
                with open(q, 'wb') as fh:
                    fh.write(a['_data'] or b'')
                n_att += 1
                n_bytes += os.path.getsize(q)
            except Exception as e:
                fails.append((f, 'write %s: %s' % (name, e)))

    with open(index_path, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['file', 'date', 'from', 'to', 'subject', 'n_att', 'attachments'])
        w.writeheader()
        w.writerows(rows)

    print('eml: %d  邮件行: %d  附件: %d  %.1f MB  索引: %s'
          % (len(files), len(rows), n_att, n_bytes / 1024 / 1024, index_path))
    if fails:
        print('失败 %d:' % len(fails), file=sys.stderr)
        for x in fails[:10]:
            print('  ', x, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
