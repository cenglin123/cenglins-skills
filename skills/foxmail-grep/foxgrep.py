#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
foxgrep.py — Foxmail 邮箱 grep 式检索工具 (供 agent / CLI 使用)

三种数据源:
  语料库模式 (默认): corpus/*.eml — 由 Foxmail 批量导出的标准 MIME 邮件,
                      含完整正文+附件内容, 可 extract 提取附件文件。
  实时模式 (--live): 直接解析 Foxmail 7.2 自维护的搜索索引 (Indexes/),
                      已有对应 EML 的邮件可按 Foxmail 原始编号提取附件。
  6.x 模式 (--v6 ROOT): 只读扫描 Foxmail 6.x 的 mail/<账号>/*.BOX,
                      元数据缓存于 v6cache/ (sqlite), 支持全部检索与附件提取。

用法:
  foxgrep.py list                       邮件列表
  foxgrep.py grep <关键词> [-i]         全文检索 (主题+正文+收发件人+附件名)
  foxgrep.py show <编号>                显示邮件全文
  foxgrep.py attach                     附件清单
  foxgrep.py extract <编号> [--out 目录] 提取该邮件全部附件 (本地→渲染缓存→POP3 逐级兜底)
通用: --json 输出 JSON; --live 实时索引; --v6 <Foxmail6根目录> 扫描 6.x 备份

附件取数链 (extract 自动逐级兜底): 语料库 .eml → BOX 原位 → 语料副本匹配
→ Foxmail 渲染缓存 (Roaming/Foxmail7/Temp-*/Attach/, 渲染即解密) → POP3 服务器。
全部未命中时按 refs/render-harvest-recipe.md 触发一次渲染即可提取。
"""
import os, re, sys, json, glob, argparse, datetime, email
from email import policy
from email.header import decode_header, make_header

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(TOOL_DIR, 'corpus')

# ==================== 语料库模式: 解析 .eml ====================

def _dh(s):
    if not s:
        return ''
    s = str(s)
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s

def _addr(s):
    m = re.search(r'[\w.%+-]+@[\w.-]+', s or '')
    return m.group(0) if m else (s or '')

def _payload_text(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ''
    cs = part.get_content_charset() or ''
    for enc in ([cs] if cs else []) + ['utf-8', 'gb18030']:
        try:
            return payload.decode(enc)
        except Exception:
            continue
    return payload.decode('utf-8', 'replace')

_HTML_TAG = re.compile(r'<[^>]+>')

def parse_eml(path):
    with open(path, 'rb') as f:
        raw = f.read()
    m = _parse_eml_bytes_full(raw)
    m['_src'] = path
    return m

def _parse_eml_bytes_full(raw):
    """从 RFC822 字节解析完整邮件 (附件含 _data)"""
    import io as _io
    msg = email.message_from_binary_file(_io.BytesIO(raw), policy=policy.compat32)
    body_plain, body_html, atts = '', '', []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition') or '')
        fn = part.get_filename()
        if fn:
            fn = _dh(fn)
        if 'attachment' in cd.lower() or fn:
            data = part.get_payload(decode=True)
            atts.append({'name': fn or 'unnamed', 'size': len(data or b''),
                         '_data': data, 'pos': None})
        elif part.get_content_type() == 'text/plain' and not body_plain:
            body_plain = _payload_text(part)
        elif part.get_content_type() == 'text/html' and not body_html:
            body_html = _payload_text(part)
    body = body_plain.strip() or _HTML_TAG.sub('', body_html).strip()
    try:
        dt = email.utils.parsedate_to_datetime(msg.get('Date'))
    except Exception:
        dt = None
    frm = _dh(msg.get('From', ''))
    return {'from': _addr(frm),
            'from_name': (lambda n: '' if n == _addr(frm) else n)(re.sub(r'<[^>]*>', '', frm).strip().strip('"')),
            'to': [_addr(x) for x in re.split(r'[,;]', _dh(msg.get('To', ''))) if _addr(x)],
            'subject': _dh(msg.get('Subject', '')), 'date': dt,
            'body': body, 'attachments': atts}


def _dt_sort_key(m):
    """排序键: None 排最前; naive 视为 UTC, 避免与 aware 混比报错"""
    dt = m.get('date')
    if dt is None:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)

def load_corpus():
    mails = []
    for p in glob.glob(os.path.join(CORPUS, '*.eml')):
        try:
            mails.append(parse_eml(p))
        except Exception as e:
            print(f"警告: 解析失败 {p}: {e}", file=sys.stderr)
    mails.sort(key=_dt_sort_key)
    for i, m in enumerate(mails, 1):
        m['id'] = i
    return mails

# ==================== 实时模式: 解析 Foxmail 索引 ====================

_STORE_ROOT_CACHE = None

def _resolve_store_root(path):
    """安装根/环境变量路径 -> 存储根。识别两种布局:
    - 7.2 多账号: <root>\\Storage\\<email>\\Mails\\Index, 返回 <root>\\Storage
    - 7.0/7.1 扁平: <root>\\Mails\\Index 直接位于数据目录 (如 Data\\), 返回数据目录本身
    数据目录名优先取 FMStorage.list 首行 (UTF-16), 否则回退 Data\\。无法识别返回 None。"""
    if os.path.isfile(os.path.join(path, 'Mails', 'Index')):
        return path
    st = os.path.join(path, 'Storage')
    if os.path.isdir(st):
        return st
    data_dir = None
    lst = os.path.join(path, 'FMStorage.list')
    if os.path.isfile(lst):
        try:
            with open(lst, encoding='utf-16') as f:
                data_dir = f.readline().strip().strip('\\/')
        except (OSError, UnicodeError):
            data_dir = None
    for name in (data_dir, 'Data'):
        if name and os.path.isfile(os.path.join(path, name, 'Mails', 'Index')):
            return os.path.join(path, name)
    return None

def store_root():
    """定位 Foxmail 存储根 (Storage\\ 或扁平数据目录): 环境变量 FOXGREP_STORE > 注册表安装路径 > 常见安装路径扫描"""
    global _STORE_ROOT_CACHE
    if _STORE_ROOT_CACHE:
        return _STORE_ROOT_CACHE
    env = os.environ.get('FOXGREP_STORE')
    if env and os.path.isdir(env):
        _STORE_ROOT_CACHE = _resolve_store_root(env) or env
        return _STORE_ROOT_CACHE
    # 注册表卸载项找安装根目录
    try:
        import winreg
        for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
                           (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
                           (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')):
            try:
                k = winreg.OpenKey(hive, path)
                for i in range(winreg.QueryInfoKey(k)[0]):
                    sk = winreg.OpenKey(k, winreg.EnumKey(k, i))
                    try:
                        if 'foxmail' in winreg.QueryValueEx(sk, 'DisplayName')[0].lower():
                            for val in ('InstallLocation', 'UninstallString', 'DisplayIcon'):
                                try:
                                    v = winreg.QueryValueEx(sk, val)[0]
                                    m = re.search(r'^(.+?\\[^\\]+)$', v.strip('"'))
                                    root = m.group(1) if val == 'InstallLocation' else os.path.dirname(m.group(1)) if m else None
                                    sr = _resolve_store_root(root) if root else None
                                    if sr:
                                        _STORE_ROOT_CACHE = sr
                                        return _STORE_ROOT_CACHE
                                except FileNotFoundError:
                                    pass
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass
    except ImportError:
        pass
    # 常见路径扫描 (7.2 Storage\\ / 7.0-7.1 扁平 Data\\)
    for pat in (r'C:\Program Files*\Foxmail*\Storage', r'D:\Program Files*\Foxmail*\Storage'):
        hits = glob.glob(pat)
        if hits:
            _STORE_ROOT_CACHE = hits[0]
            return hits[0]
    for pat in (r'C:\Foxmail*\Data', r'D:\Foxmail*\Data',
                r'C:\Program Files*\Foxmail*\Data', r'D:\Program Files*\Foxmail*\Data'):
        for hit in glob.glob(pat):
            if os.path.isfile(os.path.join(hit, 'Mails', 'Index')):
                _STORE_ROOT_CACHE = hit
                return hit
    sys.exit('找不到 Foxmail Storage/Data 目录, 请设环境变量 FOXGREP_STORE 指向它')

TLDS = ('com.cn', 'com', 'cn', 'net', 'org', 'edu', 'gov', 'vip')
_LOCAL_RE = re.compile(r'[A-Za-z0-9._%+-]+@')

def _split_emails(text):
    people, spans = [], []
    i = 0
    while True:
        m = _LOCAL_RE.search(text, i)
        if not m:
            break
        local = m.group(0)[:-1]
        j = m.end()
        ds = j
        while j < len(text) and text[j].isascii() and (text[j].isalnum() or text[j] in '.-'):
            j += 1
        domain_full = text[ds:j]
        best_k = None
        for tld in TLDS:
            k = domain_full.find(tld)
            if k >= 0 and (best_k is None or k < best_k[0] or (k == best_k[0] and len(tld) > best_k[1])):
                best_k = (k, len(tld))
        cut = best_k[0] + best_k[1] if best_k else len(domain_full)
        domain = domain_full[:cut]
        j = ds + cut
        h = len(local) // 2
        name_hint = ''
        if h > 1 and local[:h] == local[h:]:
            name_hint = local[:h]
            local = local[:h]
            m_start = m.start() + h
        else:
            m_start = m.start()
        if not domain or '.' not in domain:
            i = m.end()
            continue
        people.append({'addr': f'{local}@{domain}', 'name': name_hint})
        spans.append((m_start, j))
        i = j
    dedup_p, dedup_s = [], []
    for p, sp in zip(people, spans):
        if dedup_p and dedup_p[-1]['addr'] == p['addr']:
            continue
        dedup_p.append(p); dedup_s.append(sp)
    prev_end = 0
    for p, (s, e) in zip(dedup_p, dedup_s):
        if not p['name']:
            p['name'] = text[prev_end:s].strip()
        prev_end = e
    subject = text[spans[-1][1]:].strip() if spans else text.strip()
    return dedup_p, subject

def _fxis_string_table(rec):
    """FXIS 定长记录 (0x200) 内的字符串表: 0x2d..0x31 五个 u8 段长
    (from_name, from, to_name, to, subject), 0x33 起为连续 UTF-8 字符串, 之后补零。
    校验 (补零 + UTF-8) 不通过返回 None, 交回启发式解析 (跨版本安全回退)。"""
    lens = list(rec[0x2d:0x32])
    total = sum(lens)
    if total > 0x200 - 0x33 or rec[0x33 + total:].strip(b'\x00'):
        return None
    ss, pos = [], 0x33
    for L in lens:
        try:
            ss.append(rec[pos:pos + L].decode('utf-8'))
        except UnicodeDecodeError:
            return None
        pos += L
    return ss

def _fxis_recv_date(rec):
    """FXIS 记录偏移 0x10 的 OLE 日期 (1899-12-30 纪元) 为收件时间 (0x08 为发送时间),
    抽样与 recvdate.ind/senddate.ind 逐一相等。索引缺失的邮件由此回退补日期。"""
    import struct
    try:
        d = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=struct.unpack('<d', rec[16:24])[0])
    except (struct.error, OverflowError, ValueError):
        return None
    return d if 1990 <= d.year <= 2040 else None

def _split_to(raw):
    """to 段拆为地址列表: 按中英文逗号/分号切分; 单段含多个 @ 时再按空白切。
    组名类 to (无 @) 原样保留单元素。"""
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r'[,;，；]+', raw) if p.strip()]
    if len(parts) == 1 and parts[0].count('@') > 1:
        parts = [p for p in re.split(r'\s+', parts[0]) if p]
    return parts

def _parse_fxis_mails(path):
    data = open(path, 'rb').read()
    if data[:4] != b'FXIS':
        return {}
    mails = {}
    for i in range((len(data) - 0x200) // 0x200):
        rec = data[0x200 + i * 0x200: 0x200 + (i + 1) * 0x200]
        mid = int.from_bytes(rec[0:4], 'little')
        if mid == 0:
            continue
        date = _fxis_recv_date(rec)
        ss = _fxis_string_table(rec)
        if ss is not None:
            fn, frm, _tn, to, sub = ss
            addr = frm or fn
            mails[mid] = {'from': addr,
                          'from_name': fn if (frm and fn != frm) else '',
                          'to': _split_to(to), 'subject': sub, 'date': date}
            continue
        best = ''
        for r in re.findall(rb'[\x20-\x7e\x80-\xff]{3,}', rec):
            s = None
            for enc in ('utf-8', 'gbk'):
                try:
                    s = r.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if s and '@' in s and len(s) > len(best):
                best = s
        people, subject = _split_emails(best)
        mails[mid] = {'from': people[0]['addr'] if people else '',
                      'from_name': people[0]['name'] if people else '',
                      'to': [p['addr'] for p in people[1:]], 'subject': subject, 'date': date}
    return mails

def _parse_kvls(path):
    d = open(path, 'rb').read()
    if d[:4] != b'KVLS':
        return []
    out = []
    for i in range(0x20, len(d) - 15, 16):
        import struct
        tag, key, v1, v2 = struct.unpack('>4I', d[i:i + 16])
        if key == 0 or key > 100000:
            continue
        out.append((key, v1, v2))
    return out

def _parse_bodies(map_path, rec_path):
    import struct
    bodies = {}
    if not (os.path.exists(map_path) and os.path.exists(rec_path)):
        return bodies
    rec = open(rec_path, 'rb').read()
    for mid, off, ln in _parse_kvls(map_path):
        if 0 <= off and off + 12 + ln <= len(rec) and ln < 10_000_000:
            bodies[mid] = rec[off + 12: off + 12 + ln].decode('utf-16-le', 'replace')
    return bodies

def _parse_dates(path):
    out = {}
    if not os.path.exists(path):
        return out
    for mid, v1, v2 in _parse_kvls(path):
        try:
            d = datetime.date.fromordinal(v1)
            t = datetime.time(v2 // 3600000 % 24, v2 // 60000 % 60, v2 // 1000 % 60)
            out[mid] = datetime.datetime.combine(d, t)
        except (ValueError, OverflowError):
            pass
    return out

def _parse_attachments(rec_path):
    import struct
    if not os.path.exists(rec_path):
        return []
    d = open(rec_path, 'rb').read()
    atts = []
    marks = [m.start() for m in re.finditer(rb'[\xf0-\xff]\xea[\x00-\xff]{2}\x00{8}', d)]
    marks.append(len(d))
    for a, b in zip(marks, marks[1:]):
        blk = d[a:b]
        rec = {}
        m = re.search(rb'name\x08\x00\x00\x00(....)', blk, re.S)
        if not m:
            continue
        n = struct.unpack('<I', m.group(1))[0]
        rec['name'] = blk[m.end(): m.end() + n * 2].decode('utf-16-le', 'replace')
        for f in (b'index', b'size', b'pos'):
            m2 = re.search(f + rb'\x03\x00\x00\x00(....)', blk, re.S)
            if m2:
                rec[f.decode()] = struct.unpack('<I', m2.group(1))[0]
        if rec.get('name') and 'size' in rec and 'pos' in rec:
            atts.append(rec)
    return atts

def _attach_stem(name):
    return name.rsplit('.', 1)[0] if '.' in name else name

def _mail_file_path(root, mid):
    """邮件数据文件定位 (尝试两种布局, 不存在返回 None):
    - 7.2: Mails/<mid>/0/<mid>
    - 7.0/7.1 扁平: Mails/<mid%32>/((mid//32)%32)/<mid> 两级桶
    (mid<32 时两布局路径相同。)"""
    p = os.path.join(root, 'Mails', str(mid), '0', str(mid))
    if os.path.isfile(p):
        return p
    p = os.path.join(root, 'Mails', str(mid % 32), str((mid // 32) % 32), str(mid))
    return p if os.path.isfile(p) else None

def _build_stem_index(texts, atts):
    """stem -> set(mid): 附件名主干出现在邮件 (主题+正文) 中的全部邮件。
    与逐对 `stem in text` 语义逐位等价, 但用 前缀桶+位置扫描 把复杂度
    从 O(邮件数×附件数) 降为 O(文本总长 + 短主干数×邮件数):
    46773 主干 x 3200 万字符文本的实测全量扫描约 20 秒 (原交叉匹配外推 >2 小时)。"""
    stems = set()
    for a in atts:
        s = _attach_stem(a.get('name') or '')
        if s:  # 空名附件不入索引: 与原算法 name_match 为 False 走全体回退的语义一致
            stems.add(s)
    short = sorted(s for s in stems if len(s) < 4)
    buckets = {}
    for s in stems:
        if len(s) >= 4:
            buckets.setdefault(s[:4], []).append(s)
    out = {s: set() for s in stems}
    for mid, t in texts:
        for pos in range(len(t) - 3):
            for s in buckets.get(t[pos:pos + 4], ()):
                if t.startswith(s, pos):
                    out[s].add(mid)
        for s in short:
            if s in t:
                out[s].add(mid)
    return out

def _assign_cache_file(root):
    import hashlib
    h = hashlib.md5(os.path.normcase(os.path.abspath(root)).encode('utf-8')).hexdigest()[:12]
    return os.path.join(TOOL_DIR, 'assigncache', f'att-{h}.json')

def _assign_cache_stat(root):
    """缓存失效键: rec0 与 FXIS 的 size+mtime (Foxmail 重写索引时自动失效)"""
    rec0 = os.path.join(root, 'Indexes', 'attach', 'attachInfo.rec0')
    fxis = os.path.join(root, 'Mails', 'Index')
    try:
        return [os.path.getsize(rec0), int(os.path.getmtime(rec0)),
                os.path.getsize(fxis), int(os.path.getmtime(fxis))]
    except OSError:
        return None

def _assign_cache_load(root, n_mails, n_atts):
    path = _assign_cache_file(root)
    stat = _assign_cache_stat(root)
    if stat is None:
        return None
    try:
        with open(path, encoding='utf-8') as f:
            c = json.load(f)
        if (c.get('stat') == stat and c.get('n_mails') == n_mails
                and c.get('n_atts') == n_atts):
            return {int(k): v for k, v in c['map'].items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None

def _assign_cache_save(root, result, n_mails, n_atts):
    stat = _assign_cache_stat(root)
    if stat is None:
        return
    path = _assign_cache_file(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'stat': stat, 'n_mails': n_mails, 'n_atts': n_atts,
                       'map': {str(k): v for k, v in result.items()}}, f)
        os.replace(tmp, path)
    except OSError:
        pass

def _assign_attachments(root, mails, atts):
    cached = _assign_cache_load(root, len(mails), len(atts))
    if cached is not None:
        return cached
    import bisect
    sizes = {}
    for mid in mails:
        p = _mail_file_path(root, mid)
        sizes[mid] = os.path.getsize(p) if p else 0
    texts = [(mid, (m.get('subject') or '') + m.get('body', '')) for mid, m in mails.items()]
    smap = _build_stem_index(texts, atts)
    # 按 (size, mid) 排序: 回退候选按 slack 升序 = 从 bisect 位置起顺序产出 (惰性, 与原全排序等价)
    filed = sorted((sizes[mid], mid) for mid in mails if sizes[mid] > 0)
    assigned = {mid: [] for mid in mails}
    result = {mid: [] for mid in mails}

    def overlaps(mid, pos, end):
        for p, e in assigned[mid]:
            if not (end <= p or pos >= e):
                return True
        return False

    def named_cands(i):
        rec = atts[i]
        end = rec['pos'] + rec['size']
        nm = smap.get(_attach_stem(rec.get('name') or ''), set())
        if nm:
            ok = [(sizes[mid] - end, mid) for mid in nm if sizes[mid] >= end]
            ok.sort()
            return ok
        return None  # 名称未命中 -> 全体 (有文件) 邮件按 slack 回退

    def fallback_first(i):
        end = atts[i]['pos'] + atts[i]['size']
        j = bisect.bisect_left(filed, (end, -1))
        return (filed[j][0] - end, filed[j][1]) if j < len(filed) else None

    def fallback_iter(end):
        j = bisect.bisect_left(filed, (end, -1))
        while j < len(filed):
            size, mid = filed[j]
            yield size - end, mid
            j += 1

    pre = []
    for i in range(len(atts)):
        ok = named_cands(i)
        if ok is None:
            first = fallback_first(i)
            pre.append((1, first[0] if first else 1 << 60, ok))
        else:
            pre.append((0, ok[0][0] if ok else 1 << 60, ok))
    order = sorted(range(len(atts)), key=lambda i: (pre[i][0], pre[i][1]))
    for i in order:
        rec = atts[i]
        end = rec['pos'] + rec['size']
        ok = pre[i][2]
        cands_iter = iter(ok) if ok is not None else fallback_iter(end)
        for slack, mid in cands_iter:
            if not overlaps(mid, rec['pos'], end):
                assigned[mid].append((rec['pos'], end))
                result[mid].append(i)
                break
    _assign_cache_save(root, result, len(mails), len(atts))
    return result

def _match_corpus_by_size(root, mails):
    corpus_by_size = {}
    for path in glob.glob(os.path.join(CORPUS, '*.eml')):
        corpus_by_size.setdefault(os.path.getsize(path), []).append(path)
    mails_by_size = {}
    for mid in mails:
        path = _mail_file_path(root, mid)
        if path:
            mails_by_size.setdefault(os.path.getsize(path) - 512, []).append(mid)
    return {mids[0]: corpus_by_size[size][0]
            for size, mids in mails_by_size.items()
            if len(mids) == 1 and len(corpus_by_size.get(size, [])) == 1}

def default_account_root():
    """返回最近使用账号的存储目录 (Storage/<email>/); 扁平布局时存储根即账号根"""
    sr = store_root()
    if os.path.isfile(os.path.join(sr, 'Mails', 'Index')):
        return sr
    cands = [d for d in os.listdir(sr) if '@' in d]
    cands.sort(key=lambda d: os.path.getmtime(os.path.join(sr, d)), reverse=True)
    if not cands:
        sys.exit('存储根下未找到含 @ 的账号目录, 请设 FOXGREP_STORE 指向账号数据目录')
    return os.path.join(sr, cands[0])

def load_live():
    root = default_account_root()
    idx = os.path.join(root, 'Indexes')
    mails = _parse_fxis_mails(os.path.join(root, 'Mails', 'Index'))
    bodies = _parse_bodies(os.path.join(idx, 'msgBody', 'bodytxt_txt.map'),
                           os.path.join(idx, 'msgBody', 'bodytxt_txt.rec0'))
    dates = _parse_dates(os.path.join(idx, 'recvdate.ind'))
    atts = _parse_attachments(os.path.join(idx, 'attach', 'attachInfo.rec0'))
    for mid, m in mails.items():
        m['id'] = mid
        m['body'] = bodies.get(mid, '')
        m['date'] = dates.get(mid) or m.get('date')
    amap = _assign_attachments(root, mails, atts)
    matched = _match_corpus_by_size(root, mails)
    for mid, m in mails.items():
        m['attachments'] = [{**atts[i], '_data': None} for i in amap.get(mid, [])]
        m['_src'] = matched.get(mid)
        if m['_src']:
            parsed = parse_eml(m['_src'])
            m['attachments'] = [{'name': a['name'], 'size': a['size'], '_data': None}
                                for a in parsed['attachments']]
    return sorted(mails.values(), key=lambda m: m['date'] or datetime.datetime.min)

# ==================== fetch: POP3 拉取新邮件到 corpus ====================

def _read_env_password():
    env = os.path.join(TOOL_DIR, '.env')
    if os.path.exists(env):
        for line in open(env, encoding='utf-8'):
            if line.startswith('MAIL_PASSWORD='):
                return line.split('=', 1)[1].strip()
    return None

def _extract_password_from_rec0(rec0_path):
    """从 Account.rec0 提取 Foxmail 本地保存的邮箱密码 (V7 算法)"""
    key = b'~F@7%m$~'
    d = open(rec0_path, 'rb').read()
    for m in re.finditer(rb'\x00Password', d):
        i = m.start() + 1
        off = i + 8 + 4
        plen = int.from_bytes(d[off:off + 4], 'little')
        if not (0 < plen < 200):
            continue
        try:
            b = bytes(int(d[off + 4 + j: off + 6 + j], 16) for j in range(0, plen, 2))
        except ValueError:
            continue
        c = bytearray(b)
        c[0] ^= sum(key) % 255
        dd = bytes(b[k + 1] ^ key[k % len(key)] for k in range(len(b) - 1))
        e = bytes((dd[k] - c[k]) % 256 if dd[k] >= c[k] else (0xFF - c[k] + dd[k]) % 256
                  for k in range(len(dd)))
        if all(32 <= ch < 127 for ch in e):
            return e.decode()
    return None

def _read_account_fields(rec0_path):
    """从 Account.rec0 读取账号配置字段 (IncomingServer/IncomingPort/Email 等)。
    KV 布局: [namelen u32][name][type u32][value]; type 3=u32, 8=UTF-16 字符串, 0x100=长度前缀 ASCII"""
    import struct
    d = open(rec0_path, 'rb').read()
    out = {}
    for field in (b'IncomingServer', b'IncomingPort', b'Email'):
        idx = d.find(field)
        while idx >= 0:
            if idx >= 4 and struct.unpack('<I', d[idx - 4:idx])[0] == len(field):
                j = idx + len(field)
                tp = struct.unpack('<I', d[j:j + 4])[0]
                j += 4
                if tp == 3:
                    out[field.decode()] = struct.unpack('<I', d[j:j + 4])[0]
                elif tp == 8:
                    n = struct.unpack('<I', d[j:j + 4])[0]
                    out[field.decode()] = d[j + 4:j + 4 + n * 2].decode('utf-16-le', 'replace')
                elif tp == 0x100:
                    n = struct.unpack('<I', d[j:j + 4])[0]
                    out[field.decode()] = d[j + 4:j + 4 + n].decode('ascii', 'replace')
                break
            idx = d.find(field, idx + 1)
    return out

# ==================== Foxmail 6.x 模式: 扫描 .BOX ====================
# 6.x 结构: <root>/mail/<账号>/<文件夹>.BOX + .IND
# BOX = 若干 RFC822 原文, 以 b'\x10'*7+b'\x11'*6+b'S\r\n' 分隔; 数据未加密。
# IND 为变长记录索引, 本工具不依赖 —— 直接扫 BOX 分隔符, 元数据缓存进 sqlite。
# 只读扫描, 不复制邮件数据 (适配只读备份盘)。

V6_DELIM = b'\x10' * 7 + b'\x11' * 6 + b'S\r\n'

def _v6_cache_db(root):
    import hashlib
    h = hashlib.md5(os.path.normcase(os.path.abspath(root)).encode('utf-8')).hexdigest()[:12]
    d = os.path.join(TOOL_DIR, 'v6cache')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'v6-{h}.db')

def _v6_boxes(root):
    """产出 (账号, 文件夹, box路径); root 可为 Foxmail6 安装根或其下的 mail 目录"""
    maildir = os.path.join(root, 'mail')
    if not os.path.isdir(maildir):
        maildir = root
    for acc in sorted(os.listdir(maildir)):
        adir = os.path.join(maildir, acc)
        if not os.path.isdir(adir):
            continue
        for fn in sorted(os.listdir(adir)):
            if fn.lower().endswith('.box'):
                yield acc, os.path.splitext(fn)[0], os.path.join(adir, fn)

def _v6_parse_mail(raw):
    """轻量解析一封 BOX 邮件: 正文解码, 附件只记名字+估算大小 (不解码数据)"""
    import io as _io
    msg = email.message_from_binary_file(_io.BytesIO(raw), policy=policy.compat32)
    body_plain, body_html, atts = '', '', []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition') or '')
        fn = part.get_filename()
        if fn:
            fn = _dh(fn)
        if 'attachment' in cd.lower() or fn:
            payload = part.get_payload(decode=False)
            approx = len(payload) * 3 // 4 if isinstance(payload, str) else 0
            atts.append({'name': fn or 'unnamed', 'size': approx})
        elif part.get_content_type() == 'text/plain' and not body_plain:
            body_plain = _payload_text(part)
        elif part.get_content_type() == 'text/html' and not body_html:
            body_html = _payload_text(part)
    body = body_plain.strip() or _HTML_TAG.sub('', body_html).strip()
    try:
        dt = email.utils.parsedate_to_datetime(msg.get('Date'))
    except Exception:
        dt = None
    frm = _dh(msg.get('From', ''))
    return {'from': _addr(frm),
            'from_name': (lambda n: '' if n == _addr(frm) else n)(re.sub(r'<[^>]*>', '', frm).strip().strip('"')),
            'to': [_addr(x) for x in re.split(r'[,;]', _dh(msg.get('To', ''))) if _addr(x)],
            'subject': _dh(msg.get('Subject', '')), 'date': dt,
            'body': body, 'attachments': atts}

def _v6_scan_box(conn, box_path, account, folder):
    """流式扫描一个 BOX, 邮件元数据写入 db"""
    import mmap
    if os.path.getsize(box_path) == 0:
        return 0
    f = open(box_path, 'rb')
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        offs, start = [], 0
        while True:
            i = mm.find(V6_DELIM, start)
            if i < 0:
                break
            offs.append(i)
            start = i + 1
        n, bad = 0, 0
        for k, o in enumerate(offs):
            end = offs[k + 1] if k + 1 < len(offs) else len(mm)
            cstart = o + len(V6_DELIM)
            raw = mm[cstart:end]
            try:
                m = _v6_parse_mail(raw)
            except Exception:
                bad += 1
                continue
            dt = m['date']
            conn.execute(
                'INSERT INTO mails VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (box_path, cstart, len(raw), account, folder, m['subject'], m['from'],
                 m['from_name'], ','.join(m['to']),
                 dt.isoformat() if dt else '', m['body'],
                 json.dumps(m['attachments'], ensure_ascii=False)))
            n += 1
        if bad:
            print(f'  警告: {bad} 封解析失败已跳过', file=sys.stderr)
        return n
    finally:
        mm.close()
        f.close()

def load_v6(root):
    """扫描/增量更新 v6 缓存, 返回邮件列表 (编号按日期升序)"""
    import sqlite3
    db = _v6_cache_db(root)
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE IF NOT EXISTS boxes(path TEXT PRIMARY KEY, size INTEGER, mtime REAL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS mails(
        box TEXT, offset INTEGER, size INTEGER, account TEXT, folder TEXT,
        subject TEXT, sender TEXT, from_name TEXT, rcpts TEXT, date TEXT,
        body TEXT, atts TEXT)''')
    on_disk = {}
    for acc, folder, path in _v6_boxes(root):
        st = os.stat(path)
        on_disk[path] = (acc, folder, st.st_size, st.st_mtime)
    stale_db = {r[0] for r in conn.execute('SELECT path FROM boxes')} - set(on_disk)
    for p in stale_db:
        conn.execute('DELETE FROM boxes WHERE path=?', (p,))
        conn.execute('DELETE FROM mails WHERE box=?', (p,))
    for path, (acc, folder, size, mtime) in on_disk.items():
        row = conn.execute('SELECT size, mtime FROM boxes WHERE path=?', (path,)).fetchone()
        if row and row[0] == size and abs(row[1] - mtime) < 1e-6:
            continue
        print(f'扫描 {acc}/{folder}.Box ({size / 1e9:.2f} GB)...', file=sys.stderr)
        conn.execute('DELETE FROM mails WHERE box=?', (path,))
        n = _v6_scan_box(conn, path, acc, folder)
        conn.execute('INSERT OR REPLACE INTO boxes VALUES (?,?,?)', (path, size, mtime))
        print(f'  -> {n} 封', file=sys.stderr)
    conn.commit()
    mails = []
    for r in conn.execute('SELECT box, offset, size, account, folder, subject, sender,'
                          ' from_name, rcpts, date, body, atts FROM mails'):
        box, off, size, acc, folder, subj, sender, fname, rcpts, dts, body, atts = r
        try:
            dt = datetime.datetime.fromisoformat(dts) if dts else None
        except Exception:
            dt = None
        mails.append({'from': sender, 'from_name': fname,
                      'to': [x for x in rcpts.split(',') if x],
                      'subject': subj, 'date': dt, 'body': body,
                      'attachments': json.loads(atts),
                      'account': acc, 'folder': folder,
                      '_v6': (box, off, size)})
    conn.close()
    mails.sort(key=_dt_sort_key)
    for i, m in enumerate(mails, 1):
        m['id'] = i
    return mails

def _temp_cache_find(m):
    """扫描 Foxmail 渲染缓存 (~/AppData/Roaming/Foxmail7/Temp-*/Attach/) 中已解密的附件明文。
    Foxmail 渲染（打开/预览）邮件时会自行把附件解密到该缓存——命中即可直接收割, 无需解密。
    返回 {附件名: 缓存文件路径}。"""
    found = {}
    base = os.path.join(os.environ.get('APPDATA', ''), 'Foxmail7')
    roots = glob.glob(os.path.join(glob.escape(base), 'Temp-*', 'Attach'))
    for a in m['attachments']:
        name = re.split(r'[\\/]', a['name'])[-1].rstrip(' .')
        for d in roots:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                found[a['name']] = p
                break
    return found

def _pop3_login():
    import poplib
    rec0 = os.path.join(default_account_root(), 'Accounts', 'Account.rec0')
    cfg = _read_account_fields(rec0)
    pw = _read_env_password()
    if not pw:
        pw = _extract_password_from_rec0(rec0)
    if not pw:
        sys.exit('无密码: 请创建 .env (MAIL_PASSWORD=...) 或确认 Account.rec0 可读')
    server = cfg.get('IncomingServer')
    if not server:
        sys.exit('无法从 Account.rec0 读取 IncomingServer')
    conn = poplib.POP3(server, cfg.get('IncomingPort', 110), timeout=15)
    conn.user(cfg.get('Email', ''))
    conn.pass_(pw)
    return conn

def fetch_corpus():
    """POP3 拉取服务器邮件 (只读 RETR, 不删除), 按 UIDL 去重入库 corpus/"""
    conn = _pop3_login()
    try:
        n, _ = conn.stat()
        print(f'服务器邮件数: {n}')
        if n == 0:
            return 0
        seen_file = os.path.join(CORPUS, '.uidl_seen')
        seen = set()
        if os.path.exists(seen_file):
            seen = set(open(seen_file, encoding='utf-8').read().split())
        uidls = conn.uidl()[1]
        added = 0
        os.makedirs(CORPUS, exist_ok=True)
        for entry in uidls:
            parts = entry.decode().split(None, 1)
            if len(parts) < 2:
                continue
            num, uidl = parts
            if uidl in seen:
                continue
            lines, _ = conn.retr(int(num))
            raw = b'\r\n'.join(lines) + b'\r\n'
            m = parse_eml_bytes(raw)
            subj = re.sub(r'[\\/:*?"<>|\r\n]', '_', m['subject'])[:80] or 'no-subject'
            ds = m['date'].strftime('%Y%m%d-%H%M%S') if m['date'] else 'nodate'
            dst = os.path.join(CORPUS, f'{ds} {subj}.eml')
            i = 1
            while os.path.exists(dst):
                dst = os.path.join(CORPUS, f'{ds} {subj}_{i}.eml')
                i += 1
            open(dst, 'wb').write(raw)
            print(f'  + {os.path.basename(dst)}')
            seen.add(uidl)
            added += 1
        open(seen_file, 'w', encoding='utf-8').write('\n'.join(sorted(seen)))
        print(f'fetch 完成: 新增 {added} 封')
        return 0
    finally:
        conn.quit()

def _pop3_retrieve(target):
    """在 POP3 服务器上按 主题+日期 匹配并 RETR 一封邮件, 返回完整解析 (含附件数据) 或 None。
    只读: 不删除服务器邮件。服务器邮件少 (收后即删场景) 时逐封 TOP 取头比对即可。"""
    conn = _pop3_login()
    try:
        n, _ = conn.stat()
        if n == 0:
            return None
        tsubj = target.get('subject') or ''
        tdt = target.get('date')
        for i in range(1, n + 1):
            try:
                _, lines, _ = conn.top(i, 30)
                h = parse_eml_bytes(b'\r\n'.join(lines))
            except Exception:
                continue
            if h['subject'] != tsubj:
                continue
            if tdt and h['date'] and abs((h['date'] - tdt).total_seconds()) > 120:
                continue
            _, lines, _ = conn.retr(i)
            return _parse_eml_bytes_full(b'\r\n'.join(lines) + b'\r\n')
        return None
    finally:
        conn.quit()

def parse_eml_bytes(raw):
    import io as _io
    msg = email.message_from_binary_file(_io.BytesIO(raw), policy=policy.compat32)
    try:
        dt = email.utils.parsedate_to_datetime(msg.get('Date'))
    except Exception:
        dt = None
    return {'subject': _dh(msg.get('Subject', '')), 'date': dt}
# ==================== 输出 ====================

def mail_dict(m, with_body=False):
    dt = m.get('date')
    d = {'id': m['id'],
         'date': dt.strftime('%Y-%m-%d %H:%M:%S') if dt else None,
         'from': m['from'], 'from_name': m.get('from_name', ''), 'to': m['to'],
         'subject': m['subject'],
         'attachments': [{'name': a['name'], 'size': a['size']} for a in m['attachments']]}
    if with_body:
        d['body'] = m.get('body', '')
    if m.get('folder'):
        d['account'], d['folder'] = m['account'], m['folder']
    return d

def fmt_mail_head(m):
    dt = m.get('date')
    d = dt.strftime('%Y-%m-%d %H:%M') if dt else '????-??-??'
    to = ','.join(m['to'])
    att = f" [附件x{len(m['attachments'])}]" if m['attachments'] else ''
    fn = f"{m.get('from_name')}<{m['from']}>" if m.get('from_name') else m['from']
    loc = f"  [{m['account']}/{m['folder']}]" if m.get('folder') else ''
    return f"#{m['id']:<3} {d}  {fn} -> {to}{att}{loc}\n      主题: {m['subject']}"

def main():
    ap = argparse.ArgumentParser(prog='foxgrep')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--live', action='store_true', help='实时读 Foxmail 索引而非 .eml 语料库')
    ap.add_argument('--v6', metavar='ROOT', help='Foxmail 6.x 模式: 扫描 ROOT/mail/*/ *.BOX (只读, 元数据缓存于 v6cache/)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    g = sub.add_parser('grep'); g.add_argument('pattern'); g.add_argument('-i', action='store_true')
    s = sub.add_parser('show'); s.add_argument('mailid', type=int)
    sub.add_parser('attach')
    sub.add_parser('fetch', help='POP3 拉取服务器邮件进 corpus (只读不删, 按 UIDL 去重)')
    x = sub.add_parser('extract'); x.add_argument('mailid', type=int)
    x.add_argument('--out', help='输出目录 (默认 corpus/att-<编号>/)')
    args = ap.parse_args()

    if args.cmd == 'fetch':
        sys.exit(fetch_corpus())

    if args.v6:
        ms = load_v6(args.v6)
    else:
        ms = load_live() if args.live else load_corpus()
    if not ms:
        sys.exit('无邮件数据: 请先用 Foxmail 批量导出 .eml 到 corpus/, 或用 --live 读实时索引, 或用 --v6 扫描 6.x 备份')

    if args.cmd == 'list':
        if args.json:
            print(json.dumps([mail_dict(m) for m in ms], ensure_ascii=False, indent=1))
        else:
            for m in ms:
                print(fmt_mail_head(m))
    elif args.cmd == 'grep':
        pat = re.compile(re.escape(args.pattern), re.I if args.i else 0)
        hits = [m for m in ms if any(pat.search(f or '') for f in
                [m['subject'], m.get('body', ''), m['from'], m.get('from_name', ''),
                 ' '.join(m['to'])] + [a['name'] for a in m['attachments']])]
        if args.json:
            print(json.dumps([mail_dict(m) for m in hits], ensure_ascii=False, indent=1))
        else:
            for m in hits:
                print(fmt_mail_head(m))
                for mt in list(pat.finditer(m.get('body', '')))[:3]:
                    s = max(0, mt.start() - 40)
                    snip = re.sub(r'\s+', ' ', m['body'][s: mt.end() + 40])
                    print(f"      …{snip}…")
            print(f"\n共 {len(hits)} 封命中")
    elif args.cmd == 'show':
        m = next((m for m in ms if m['id'] == args.mailid), None)
        if not m:
            sys.exit(f"无此邮件: {args.mailid}")
        if args.json:
            print(json.dumps(mail_dict(m, True), ensure_ascii=False, indent=1))
        else:
            print(fmt_mail_head(m))
            for a in m['attachments']:
                print(f"      附件: {a['name']} ({a['size']:,} bytes)")
            print('-' * 60)
            print(m.get('body', ''))
    elif args.cmd == 'attach':
        rows = [{'mailid': m['id'], 'name': a['name'], 'size': a['size']}
                for m in ms for a in m['attachments']]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(f"[邮件#{r['mailid']:>2}] {r['name']}  ({r['size']:,} bytes)")
            print(f"\n共 {len(rows)} 个附件")
    elif args.cmd == 'extract':
        m = next((m for m in ms if m['id'] == args.mailid), None)
        if not m:
            sys.exit(f"无此邮件: {args.mailid}")
        if args.live and m.get('_src'):
            m = parse_eml(m['_src'])
            m['id'] = args.mailid
        if m.get('_v6'):
            box, off, size = m['_v6']
            with open(box, 'rb') as f:
                f.seek(off)
                full = _parse_eml_bytes_full(f.read(size))
            m['attachments'] = full['attachments']
        if not m['attachments']:
            sys.exit('该邮件无附件')
        if any(a.get('_data') is None for a in m['attachments']):
            # 本地无副本 → 依次兜底: Foxmail 渲染缓存收割 → POP3 服务器拉取
            cached = _temp_cache_find(m)
            for a in m['attachments']:
                if a.get('_data') is None and a['name'] in cached:
                    a['_data'] = open(cached[a['name']], 'rb').read()
                    print(f"缓存收割: {a['name']}", file=sys.stderr)
        if any(a.get('_data') is None for a in m['attachments']):
            print('缓存未命中，尝试从 POP3 服务器拉取该邮件...', file=sys.stderr)
            full = _pop3_retrieve(m)
            if full:
                by_name = {x['name']: x for x in full['attachments']}
                for a in m['attachments']:
                    x = by_name.get(a['name'])
                    if a.get('_data') is None and x and x.get('_data') is not None:
                        a['_data'] = x['_data']
                print('已从服务器取回邮件原文', file=sys.stderr)
        if all(a.get('_data') is None for a in m['attachments']):
            sys.exit('提取失败: 本地无副本, 渲染缓存未命中, 服务器上也未找到。\n'
                     '解决: 按 refs/render-harvest-recipe.md 触发 Foxmail 渲染该邮件\n'
                     '(附件会随之进入渲染缓存), 然后重试 extract。')
        for a in m['attachments']:
            if a.get('_data') is None:
                print(f"警告: 附件未取得: {a['name']}", file=sys.stderr)
        out = args.out or os.path.join(CORPUS, f"att-{m['id']}")
        os.makedirs(out, exist_ok=True)
        for a in m['attachments']:
            name = re.split(r'[\\/]', a['name'])[-1].rstrip(' .') or 'unnamed'
            stem, ext = os.path.splitext(name)
            p = os.path.join(out, name)
            suffix = 1
            while os.path.exists(p):
                p = os.path.join(out, f'{stem}_{suffix}{ext}')
                suffix += 1
            with open(p, 'wb') as f:
                f.write(a['_data'])
            print(p)

if __name__ == '__main__':
    main()
