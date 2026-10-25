# -*- coding: utf-8 -*-
"""
paint_gallery v1.1 — 本地绘画记录浏览器（找回 · 比较 · 标记 · 复用）

数据源: tools/prompt_log.csv（无表头十列, UTF-8 BOM, 全程只读）
标记库: tools/paint_marks.json（与 CSV 完全分离, 原子写, 跨进程独占锁）

用法:
    python tools/paint_gallery.py               # 正式实例, http://127.0.0.1:8787/
    python tools/paint_gallery.py --csv <文件> --port N --marks <文件> --roots <目录,目录>
                                                # 测试实例（测试夹具目录必须进 --roots）

安全: 仅监听 127.0.0.1; Host 校验; 变更仅 POST + 每次启动随机令牌;
      图片仅按启动时登记的图片 ID 提供, 不接受路径参数; 不开放跨域;
      不使用 subprocess; 所有文件打开限定在允许目录(roots)内, 一律先
      realpath 规范化并拒绝含 ".." 的路径; 写入经 mkstemp 临时文件后原子替换。
标记: 喜欢/可用/需要重做 三个独立标签 + 备注, 绑定图片 ID(完整 SHA-256)。
      保存失败时内存保持原状态并向页面报错; 标记库损坏时保留原文件并暂停写入。
"""
import argparse
import copy
import csv
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sys
import tempfile
import threading
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOWED_LABELS = ("喜欢", "可用", "需要重做")
MAX_BODY = 64 * 1024
MAX_NOTE = 500
DEFAULT_PORT = 8787
PNG_SIG = b"\x89PNG\r\n\x1a\n"
STATE_TXT = {"all": "全部存在", "partial": "部分丢失", "none": "全部丢失", "no_path": "无路径"}
DEFAULT_IMAGE_ROOT = r"D:\AI\ComfyUI\output"

REPRO_NOTICE = ("已记录：seed、尺寸、步数、CFG、预设和提示词。"
                "历史模型与工作流信息未记录，因此不保证复现。本工具支持参数查阅与复制。")
SNAPSHOT_NOTICE = "数据为启动时快照；生成新记录后请重启服务刷新。"

ROOTS = []          # 允许打开文件的目录白名单（启动时构建, 全小写规范化）


def norm_lower(p):
    return os.path.normpath(os.path.abspath(p)).lower().rstrip(os.sep)


def build_roots(extra):
    roots = {norm_lower(APP_DIR), norm_lower(DEFAULT_IMAGE_ROOT)}
    for e in extra or []:
        if e:
            roots.add(norm_lower(e))
    return sorted(roots)


def add_root_dir_of(file_path):
    """把某文件所在目录并入白名单（CSV / 标记库自身目录）"""
    d = norm_lower(os.path.dirname(os.path.abspath(file_path)))
    if d not in ROOTS:
        ROOTS.append(d)


# ---------------------------------------------------------------- CSV 解析 ---

def canon_json(obj):
    """确定性 JSON 序列化（身份计算用）"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def record_id_of(fields10):
    return hashlib.sha256(canon_json(list(fields10))).hexdigest()


def image_id_of(rid, raw_path):
    return hashlib.sha256(canon_json([rid, raw_path])).hexdigest()


def load_csv(path):
    """返回 (records, dup_rows, malformed, outside)。
    完全重复记录共享身份并计数; 白名单外路径登记但永不打开。"""
    real = os.path.realpath(path)
    low = real.lower()
    if ".." in real.split(os.sep) or not any(
            low == r or low.startswith(r + os.sep) for r in ROOTS):
        sys.exit("[退出] CSV 路径被拒绝（不在允许目录内）: " + str(path))
    records = {}
    dup_rows = 0
    malformed = 0
    outside = 0
    with open(real, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or all(not c.strip() for c in row):
                continue
            if len(row) != 10:
                malformed += 1
                continue
            rid = record_id_of(row)
            if rid in records:
                dup_rows += 1
                continue
            paths = [s for s in (p.strip() for p in row[9].split(" | ")) if s]
            images = []
            for raw in paths:
                fp = os.path.normpath(raw)   # 显示用规范化路径; 身份按日志原始字符串另行计算
                rimg = os.path.realpath(fp)
                lowimg = rimg.lower()
                exists = False
                sig = False
                if (".." not in rimg.split(os.sep)
                        and any(lowimg == r or lowimg.startswith(r + os.sep) for r in ROOTS)):
                    try:
                        if os.path.isfile(rimg):
                            exists = True
                            with open(rimg, "rb") as fh:
                                sig = fh.read(8) == PNG_SIG
                    except OSError:
                        pass
                else:
                    outside += 1
                images.append({"id": image_id_of(rid, raw), "path": fp,
                               "exists": exists, "sig": sig})
            states = [im["exists"] for im in images]
            if not images:
                state = "no_path"
            elif all(states):
                state = "all"
            elif any(states):
                state = "partial"
            else:
                state = "none"
            records[rid] = {"id": rid, "ts": row[0], "preset": row[1], "seed": row[2],
                            "w": row[3], "h": row[4], "steps": row[5], "cfg": row[6],
                            "zh": row[7], "en": row[8], "images": images, "state": state}
    recs = sorted(records.values(), key=lambda r: r["ts"], reverse=True)
    return recs, dup_rows, malformed, outside


# ---------------------------------------------------------------- 标记库 -----

def load_marks(path):
    """返回 (data, corrupt_reason)。损坏时保留原文件, 返回 (None, 原因)。"""
    if not os.path.exists(path):
        return {"version": 1, "marks": {}}, None
    real = os.path.realpath(path)
    low = real.lower()
    if ".." in real.split(os.sep) or not any(
            low == r or low.startswith(r + os.sep) for r in ROOTS):
        return None, "标记库路径不在允许目录内"
    try:
        with open(real, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("marks"), dict):
            raise ValueError("顶层结构必须为 {version, marks}")
        for k, v in data["marks"].items():
            if not isinstance(k, str) or not isinstance(v, dict):
                raise ValueError("标记项结构不合法")
            labs = v.get("labels", [])
            if not isinstance(labs, list) or any(x not in ALLOWED_LABELS for x in labs):
                raise ValueError("labels 含未定义标签")
            if not isinstance(v.get("note", ""), str):
                raise ValueError("note 必须为字符串")
        return data, None
    except Exception as e:
        return None, str(e)


def save_marks(path, data):
    """原子写: mkstemp 临时文件 → fsync → os.replace。失败时不动原文件。"""
    real = os.path.realpath(path)
    low = real.lower()
    if ".." in real.split(os.sep) or not any(
            low == r or low.startswith(r + os.sep) for r in ROOTS):
        raise PermissionError("标记库路径不在允许目录内")
    fd, tmpname = tempfile.mkstemp(dir=os.path.dirname(real) or ".",
                                   prefix=".paint_marks_", suffix=".tmp")
    fobj = None
    try:
        fobj = os.fdopen(fd, "w", encoding="utf-8")
        json.dump(data, fobj, ensure_ascii=False, indent=1)
        fobj.flush()
        os.fsync(fobj.fileno())
        fobj.close()
        fobj = None
        os.replace(tmpname, real)
    except Exception:
        if fobj is not None:
            try:
                fobj.close()
            except OSError:
                pass
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise


def acquire_marks_lock(marks_path):
    """跨进程独占锁: 同一标记文件只允许一个实例。返回 (fd|None, 错误|None)。"""
    lock_path = marks_path + ".lock"
    d = os.path.dirname(lock_path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        os.write(fd, b"\x00")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        try:
            os.close(fd)
        except OSError:
            pass
        return None, ("标记库正被另一个实例占用（同一标记文件只允许一个服务实例）: "
                      f"{lock_path} ({e})")
    return fd, None


# ---------------------------------------------------------------- 应用状态 ---

class App:
    def __init__(self, csv_path, marks_path):
        self.csv_path = csv_path
        self.marks_path = marks_path
        self.store_lock = threading.Lock()
        self.token = secrets.token_urlsafe(16)
        self.records = []
        self.image_index = {}   # iid -> (path, exists)
        self.dup_rows = 0
        self.malformed = 0
        self.outside = 0
        self.marks = None
        self.corrupt_reason = None

    def load_all(self):
        if not os.path.isfile(self.csv_path):
            sys.exit(f"[退出] CSV 不存在: {self.csv_path}")
        (self.records, self.dup_rows,
         self.malformed, self.outside) = load_csv(self.csv_path)
        for r in self.records:
            for im in r["images"]:
                self.image_index[im["id"]] = (im["path"], im["exists"])
        self.marks, self.corrupt_reason = load_marks(self.marks_path)

    def snapshot_payload(self):
        cnt = {"all": 0, "partial": 0, "none": 0, "no_path": 0}
        for r in self.records:
            cnt[r["state"]] += 1
        return {"records": self.records, "dup_rows": self.dup_rows,
                "malformed": self.malformed, "csv": self.csv_path,
                "state_counts": cnt,
                "repro_notice": REPRO_NOTICE, "snapshot_notice": SNAPSHOT_NOTICE}

    def marks_payload(self):
        if self.corrupt_reason:
            return {"corrupt": True, "error": self.corrupt_reason,
                    "path": self.marks_path, "marks": {}}
        return {"corrupt": False, "path": self.marks_path, "marks": self.marks.get("marks", {})}


APP = None  # type: App
PORT = DEFAULT_PORT
HOSTS_OK = set()
ORIGINS_OK = set()  # 同源校验: Origin 头存在时必须命中; 缺失则由随机令牌兜底


# ---------------------------------------------------------------- HTTP -------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "paint_gallery/1.1"

    def log_message(self, *a):  # 控制台只留启动/错误信息
        pass

    # ---- 基础发送 ----
    def _send(self, code, body: bytes, ctype: str, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---- 路由 ----
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        try:
            host = self.headers.get("Host", "")
            if host not in HOSTS_OK:
                self.send_json(403, {"error": "Host 校验失败"})
                return
            parsed = urllib.parse.urlparse(self.path)
            path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
            if method == "GET":
                if path == "/api/mark":
                    self.send_json(405, {"error": "变更仅接受 POST"})
                elif path == "/":
                    self._send(200, PAGE_HTML.replace("__TOKEN__", APP.token)
                               .replace("__PORT__", str(PORT)).encode("utf-8"),
                               "text/html; charset=utf-8")
                elif path == "/data":
                    self.send_json(200, APP.snapshot_payload())
                elif path == "/marks":
                    self.send_json(200, APP.marks_payload())
                elif path == "/img":
                    self.send_image(qs.get("id", [""])[0])
                else:
                    self.send_json(404, {"error": "not found"})
            else:
                if path == "/api/mark":
                    self.handle_mark()
                else:
                    self.send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json(500, {"error": "internal error"})
            except Exception:
                pass

    # ---- 图片 ----
    def send_image(self, iid):
        info = APP.image_index.get(iid)
        if info is None:
            self.send_json(404, {"error": "unknown image id"})
            return
        p, exists = info
        real = os.path.realpath(p)
        low = real.lower()
        if (not exists or ".." in real.split(os.sep)
                or not any(low == r or low.startswith(r + os.sep) for r in ROOTS)):
            self.send_json(404, {"error": "file missing"})
            return
        try:
            with open(real, "rb") as f:
                data = f.read()
        except OSError as e:
            self.send_json(404, {"error": f"file unreadable: {e}"})
            return
        ctype = "image/png" if real.lower().endswith(".png") else (
            mimetypes.guess_type(real)[0] or "application/octet-stream")
        self._send(200, data, ctype, cache="private, max-age=3600")

    # ---- 标记变更 ----
    def handle_mark(self):
        tok = self.headers.get("X-Token", "")
        if not hmac.compare_digest(tok, APP.token):
            self.send_json(403, {"error": "令牌校验失败"})
            return
        origin = self.headers.get("Origin", "")
        if origin and origin not in ORIGINS_OK:
            self.send_json(403, {"error": "Origin 校验失败"})
            return
        # Origin 缺失 = 非浏览器客户端（脚本/curl）: 放行, 令牌校验已兜底
        try:
            cl = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            cl = 0
        if cl > MAX_BODY:
            self.send_json(413, {"error": "请求体过大"})
            return
        raw = self.rfile.read(cl) if cl else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_json(400, {"error": "请求体不是合法 JSON"})
            return
        iid = body.get("image_id")
        if not isinstance(iid, str) or iid not in APP.image_index:
            self.send_json(404, {"error": "unknown image id"})
            return
        labels = body.get("labels", [])
        if (not isinstance(labels, list)
                or any(x not in ALLOWED_LABELS for x in labels)
                or len(set(labels)) != len(labels)):
            self.send_json(400, {"error": "labels 不合法"})
            return
        note = body.get("note", "")
        if not isinstance(note, str):
            self.send_json(400, {"error": "note 不合法"})
            return
        if len(note) > MAX_NOTE:
            self.send_json(400, {"error": f"备注超长（上限 {MAX_NOTE} 字符）"})
            return
        if APP.corrupt_reason:
            self.send_json(409, {"error": f"标记库损坏，写入已暂停: {APP.corrupt_reason}"})
            return
        with APP.store_lock:
            candidate = copy.deepcopy(APP.marks)
            if labels or note:
                candidate.setdefault("marks", {})[iid] = {
                    "labels": labels, "note": note,
                    "updated_at": datetime.now().isoformat(timespec="seconds")}
            else:
                candidate.get("marks", {}).pop(iid, None)
            try:
                save_marks(APP.marks_path, candidate)
            except Exception as e:
                # 候选数据仅在写盘成功后替换内存; 失败则内存保持原状态
                self.send_json(500, {"error": f"保存失败，本次更改未生效: {e}"})
                return
            APP.marks = candidate
        self.send_json(200, {"ok": True,
                             "marks_count": len(APP.marks.get("marks", {}))})


# ---------------------------------------------------------------- 页面 -------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>绘画记录浏览器 · paint_gallery</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#14161a;color:#e2e6ea;font-family:"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px}
header{padding:10px 20px;background:#1b1e24;border-bottom:1px solid #2a2f38;position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0 0 6px}
.notice{font-size:12px;color:#9aa4b0;margin:2px 0}
.banner{background:#5c1d1d;border:1px solid #a33;color:#ffd9d9;padding:10px 14px;margin:8px 20px;border-radius:6px;font-size:13px;white-space:pre-wrap}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;align-items:center}
.controls input,.controls select{background:#12151a;color:#e2e6ea;border:1px solid #333a45;border-radius:5px;padding:5px 8px;font-size:13px}
.controls input#q{width:260px}
.controls input#seed{width:120px}
.controls input#f-date{width:150px}
button{cursor:pointer;background:#262c36;color:#e2e6ea;border:1px solid #3a424f;border-radius:5px;padding:4px 10px;font-size:13px}
button:hover{background:#31394a}
#meta{padding:8px 20px;color:#8f99a6;font-size:12px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px;padding:6px 20px 90px}
.card{background:#1b1e24;border:1px solid #262c36;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:8px}
.chead{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.ts{color:#9aa4b0;font-size:12px}
.badge{font-size:11px;border-radius:10px;padding:2px 8px;background:#262c36}
.st-all{background:#1d3a25;color:#8ee6a1}.st-partial{background:#4a3413;color:#ffce7a}
.st-none{background:#4a1717;color:#ff9d9d}.st-no_path{background:#2a2f38;color:#aab4c0}
.prompt{font-size:12px;line-height:1.5;color:#cfd6de;cursor:pointer;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-all}
.prompt.open{-webkit-line-clamp:unset}
.prompt .lab{color:#7d8794;margin-right:4px}
.params{font-size:12px;color:#9aa4b0;word-break:break-all}
.params code{color:#d8dee6}
.imgs{display:flex;flex-direction:column;gap:8px}
.imgblk{border:1px solid #262c36;border-radius:6px;padding:8px;display:flex;flex-direction:column;gap:6px}
.imgbox{background:#0e1013;border-radius:4px;min-height:60px;display:flex;align-items:center;justify-content:center}
.imgbox img{max-width:100%;max-height:230px;object-fit:contain;cursor:zoom-in;display:block}
.miss{color:#8f99a6;font-size:12px;padding:26px 10px;text-align:center;border:1px dashed #3a424f;border-radius:4px;width:100%}
.markrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.chip{border-radius:12px;padding:2px 10px;font-size:12px;background:#20242c;color:#aeb8c4}
.chip.on{background:#3b5bdb;color:#fff;border-color:#3b5bdb}
.chip.on[data-label="喜欢"]{background:#c2364f;border-color:#c2364f}
.chip.on[data-label="可用"]{background:#2f9e5d;border-color:#2f9e5d}
.chip.on[data-label="需要重做"]{background:#c2762c;border-color:#c2762c}
.cmpwrap{font-size:12px;color:#9aa4b0;display:flex;align-items:center;gap:4px}
.note{background:#12151a;border:1px solid #333a45;border-radius:5px;color:#e2e6ea;padding:4px 8px;font-size:12px;width:100%}
.pathline{font-size:11px;color:#6f7986;word-break:break-all}
.copyrow{display:flex;gap:6px;flex-wrap:wrap}
.copyrow button{font-size:12px;padding:3px 8px}
#pager{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:#1b1e24ee;border:1px solid #2a2f38;border-radius:20px;padding:6px 14px;display:flex;gap:10px;align-items:center;z-index:6}
#cmpbar{position:fixed;bottom:14px;right:16px;background:#1b1e24ee;border:1px solid #2a2f38;border-radius:10px;padding:8px 12px;display:none;gap:8px;align-items:center;z-index:6}
#overlay{position:fixed;inset:0;background:#000d;display:none;z-index:10;overflow:auto;padding:20px}
#overlay .inner{max-width:1400px;margin:0 auto}
.ovcols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.ovpanel{background:#1b1e24;border-radius:8px;padding:12px}
.ovpanel table{width:100%;font-size:12px;border-collapse:collapse;margin-top:8px}
.ovpanel td{padding:3px 6px;border-bottom:1px solid #262c36;word-break:break-all}
.ovpanel td.k{color:#8f99a6;width:70px;white-space:nowrap}
#lightbox{position:fixed;inset:0;background:#000e;display:none;z-index:12;align-items:center;justify-content:center;flex-direction:column;gap:8px}
#lightbox img{max-width:96vw;max-height:88vh;object-fit:contain}
#lightbox .cap{color:#9aa4b0;font-size:12px;max-width:90vw;word-break:break-all;text-align:center}
#copymodal{position:fixed;inset:0;background:#000d;display:none;z-index:14;align-items:center;justify-content:center}
#copymodal>div{background:#1b1e24;border:1px solid #2a2f38;border-radius:8px;padding:16px;width:min(680px,92vw)}
#copymodal textarea{width:100%;height:120px;background:#12151a;color:#e2e6ea;border:1px solid #333a45;border-radius:5px;padding:8px;font-size:12px}
.unset{color:#8f99a6}
#toast{position:fixed;top:14px;right:14px;z-index:20;display:none;max-width:420px;padding:10px 14px;border-radius:8px;font-size:13px}
#toast.err{display:block;background:#5c1d1d;border:1px solid #a33;color:#ffd9d9}
#toast.ok{display:block;background:#1d3a25;border:1px solid #2f9e5d;color:#c8f0d4}
</style></head><body>
<header>
  <h1>🖼 绘画记录浏览器 <span style="font-size:11px;color:#7d8794">paint_gallery v1.1</span></h1>
  <div class="notice" id="snapnotice"></div>
  <div class="notice" id="repro"></div>
  <div class="controls">
    <input id="q" placeholder="搜索提示词（中/英）">
    <input id="seed" placeholder="seed 精确">
    <select id="f-preset"><option value="">预设：全部</option></select>
    <select id="f-size"><option value="">尺寸：全部</option></select>
    <select id="f-state">
      <option value="">状态：全部</option>
      <option value="all">全部存在</option><option value="partial">部分丢失</option>
      <option value="none">全部丢失</option><option value="no_path">无路径</option>
    </select>
    <input id="f-date" placeholder="日期 2026-09 或 2026-09-27">
    <label class="cmpwrap"><input type="checkbox" id="f-liked">只看“喜欢”</label>
    <button id="btn-reset">重置</button>
  </div>
</header>
<div id="corrupt-banner" class="banner" style="display:none"></div>
<div id="meta"></div>
<div id="grid"></div>
<div id="pager">
  <button id="pg-prev">‹ 上一页</button>
  <span id="pg-info"></span>
  <button id="pg-next">下一页 ›</button>
</div>
<div id="cmpbar">
  <span id="cmp-info"></span>
  <button id="cmp-go">对比</button>
  <button id="cmp-clear">清空</button>
</div>
<div id="overlay"><div class="inner">
  <button id="ov-close">关闭对比 (Esc)</button>
  <div class="ovcols" id="ov-cols"></div>
</div></div>
<div id="lightbox"><img id="lb-img" alt=""><div class="cap" id="lb-cap"></div><button id="lb-close">关闭</button></div>
<div id="copymodal"><div>
  <div style="margin-bottom:8px">剪贴板接口不可用，请手动复制（文本已选中，按 Ctrl+C）：</div>
  <textarea id="copyta" readonly></textarea>
  <div style="margin-top:8px;text-align:right"><button id="copyclose">关闭</button></div>
</div></div>
<div id="toast"></div>
<script>
const TOKEN="__TOKEN__";
const PAGE_SIZE=40;
const STATE_TXT={all:"全部存在",partial:"部分丢失",none:"全部丢失",no_path:"无路径"};
const LABS=["喜欢","可用","需要重做"];
let DATA={records:[]},MARKS={corrupt:false,marks:{},path:""},IMG_IDX=new Map(),REC_IDX=new Map();
let filtered=[],page=0,cmp=[];
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function markOf(iid){return MARKS.marks[iid]||{labels:[],note:""};}
function toast(msg,cls){const t=$("#toast");t.textContent=msg;t.className=cls;clearTimeout(t._h);t._h=setTimeout(()=>{t.className="";},cls==="err"?6000:2000);}
function paramStr(r){return "seed="+r.seed+" | "+r.w+"x"+r.h+" | steps="+r.steps+" | cfg="+r.cfg+" | preset="+(r.preset||"(空)");}
/* ---------- 标记 API ---------- */
async function apiMark(iid,labels,note){
  try{
    const r=await fetch("/api/mark",{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},
      body:JSON.stringify({image_id:iid,labels,note})});
    if(r.ok)return true;
    const e=await r.json().catch(()=>({}));
    toast("保存失败，已撤回更改："+(e.error||("HTTP "+r.status)),"err");
    return false;
  }catch(err){toast("保存失败，已撤回更改："+err,"err");return false;}
}
/* 同图修改按 ID 串行排队: 每次都从上一次已确认的服务端状态计算下一次操作,
   快速连点/双击/备注与标签并发不再互相覆盖 */
const markQueues=new Map();
function enqueueMark(iid,mutator){
  const job=(markQueues.get(iid)||Promise.resolve()).catch(()=>{}).then(()=>doMark(iid,mutator));
  markQueues.set(iid,job);
  return job;
}
async function doMark(iid,mutator){
  if(MARKS.corrupt){toast("标记库损坏，写入已暂停（"+MARKS.path+"）","err");syncImgUI(iid);return false;}
  const {labels,note}=mutator(markOf(iid));
  const ok=await apiMark(iid,labels,note);
  if(ok){
    if(labels.length||note)MARKS.marks[iid]={labels,note};else delete MARKS.marks[iid];
  }
  syncImgUI(iid);  // 成功=刷新为确认态; 失败=还原为确认态
  return ok;
}
function syncImgUI(iid){
  const cur=markOf(iid);
  document.querySelectorAll('.chip[data-iid="'+iid+'"]').forEach(b=>{
    b.classList.toggle("on",cur.labels.includes(b.dataset.label));});
  document.querySelectorAll('input.note[data-iid="'+iid+'"]').forEach(inp=>{
    inp.value=cur.note;inp.dataset.prev=cur.note;});
}
async function toggleLabel(btn){
  const iid=btn.dataset.iid,lab=btn.dataset.label;
  await enqueueMark(iid,cur=>{
    const labels=cur.labels.includes(lab)?cur.labels.filter(x=>x!==lab):[...cur.labels,lab];
    return {labels,note:cur.note};
  });
}
async function saveNote(inp){
  const iid=inp.dataset.iid;
  await enqueueMark(iid,cur=>({labels:cur.labels,note:inp.value.slice(0,500)}));
}
/* ---------- 复制 ---------- */
async function copyText(t){
  try{await navigator.clipboard.writeText(t);toast("已复制","ok");}
  catch(e){
    const m=$("#copymodal"),ta=$("#copyta");ta.value=t;m.style.display="flex";
    ta.focus();ta.select();try{document.execCommand("copy");}catch(_){}
  }
}
document.addEventListener("click",e=>{
  const b=e.target.closest("[data-copy]");if(!b)return;
  const r=REC_IDX.get(b.dataset.rid);if(!r)return;
  const k=b.dataset.copy;
  copyText(k==="zh"?r.zh:k==="en"?r.en:k==="seed"?r.seed:paramStr(r));
});
/* ---------- 渲染 ---------- */
function imgInner(im){
  if(!im.exists)return '<div class="miss">文件已丢失</div>';
  return '<img loading="lazy" src="/img?id='+esc(im.id)+'" data-iid="'+esc(im.id)+'" title="'+esc(im.path)+'" onerror="imgFail(this)">';
}
function imgFail(el){el.parentElement.innerHTML='<div class="miss">图片无法加载</div>';}
function chipHtml(im){
  const cur=markOf(im.id);
  return LABS.map(l=>'<button class="chip'+(cur.labels.includes(l)?" on":"")+
    '" data-iid="'+esc(im.id)+'" data-label="'+l+'" onclick="toggleLabel(this)">'+l+"</button>").join("");
}
function imgblkHtml(im){
  return '<div class="imgblk"><div class="imgbox" data-iid="'+esc(im.id)+'">'+imgInner(im)+"</div>"+
    '<div class="markrow"><label class="cmpwrap"><input type="checkbox" class="cmp" data-iid="'+esc(im.id)+
    '" onchange="toggleCompare(this)">对比</label>'+chipHtml(im)+"</div>"+
    '<input class="note" data-iid="'+esc(im.id)+'" data-prev="'+esc(markOf(im.id).note)+
    '" value="'+esc(markOf(im.id).note)+'" placeholder="备注…" onchange="saveNote(this)">'+
    '<div class="pathline">'+esc(im.path)+(im.exists&&!im.sig?"（⚠ 非PNG签名）":"")+"</div></div>";
}
function cardHtml(r){
  return '<div class="card" data-rid="'+esc(r.id)+'">'+
    '<div class="chead"><span class="ts">'+esc(r.ts)+'</span><span class="badge">'+esc(r.preset||"(空)")+
    '</span><span class="badge st-'+r.state+'">'+STATE_TXT[r.state]+"</span></div>"+
    '<div class="prompt" onclick="this.classList.toggle(\'open\')"><span class="lab">中</span>'+esc(r.zh)+"</div>"+
    '<div class="prompt" onclick="this.classList.toggle(\'open\')"><span class="lab">EN</span>'+esc(r.en)+"</div>"+
    '<div class="params">'+esc(r.w)+"×"+esc(r.h)+" · steps "+esc(r.steps)+" · cfg "+esc(r.cfg)+
    ' · seed <code>'+esc(r.seed)+"</code> · 模型<span class='unset'>未记录</span> · LoRA<span class='unset'>未记录</span></div>"+
    '<div class="imgs">'+r.images.map(imgblkHtml).join("")+"</div>"+
    '<div class="copyrow"><button data-copy="zh" data-rid="'+esc(r.id)+'">复制中文</button>'+
    '<button data-copy="en" data-rid="'+esc(r.id)+'">复制EN</button>'+
    '<button data-copy="seed" data-rid="'+esc(r.id)+'">复制seed</button>'+
    '<button data-copy="params" data-rid="'+esc(r.id)+'">复制参数</button></div></div>';
}
function render(){
  const slice=filtered.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE);
  $("#grid").innerHTML=slice.map(cardHtml).join("");
  const tp=Math.max(1,Math.ceil(filtered.length/PAGE_SIZE));
  $("#pg-info").textContent="第 "+(page+1)+"/"+tp+" 页 · 筛选 "+filtered.length+" 条";
  $("#meta").textContent="共 "+DATA.records.length+" 条记录（重复组 "+DATA.dup_rows+"，列异常 "+DATA.malformed+
    "）· 记录状态: 全部存在 "+DATA.state_counts.all+" / 部分丢失 "+DATA.state_counts.partial+
    " / 全部丢失 "+DATA.state_counts.none+" / 无路径 "+DATA.state_counts.no_path;
}
/* ---------- 筛选 ---------- */
function applyFilter(){
  const q=$("#q").value.trim().toLowerCase();
  const seed=$("#seed").value.trim();
  const preset=$("#f-preset").value;
  const size=$("#f-size").value;
  const st=$("#f-state").value;
  const date=$("#f-date").value.trim();
  const liked=$("#f-liked").checked;
  filtered=DATA.records.filter(r=>{
    if(q&&!(r.zh.toLowerCase().includes(q)||r.en.toLowerCase().includes(q)))return false;
    if(seed&&r.seed!==seed)return false;
    if(preset&&r.preset!==preset)return false;
    if(size&&(r.w+"x"+r.h)!==size)return false;
    if(st&&r.state!==st)return false;
    if(date&&!r.ts.startsWith(date))return false;
    if(liked&&!r.images.some(im=>markOf(im.id).labels.includes("喜欢")))return false;
    return true;
  });
  page=0;render();
}
function initFilters(){
  const ps=[...new Set(DATA.records.map(r=>r.preset))].sort();
  $("#f-preset").innerHTML='<option value="">预设：全部</option>'+ps.map(p=>'<option value="'+esc(p)+'">'+esc(p||"(空)")+"</option>").join("");
  const ss=[...new Set(DATA.records.map(r=>r.w+"x"+r.h))];
  $("#f-size").innerHTML='<option value="">尺寸：全部</option>'+ss.map(s=>'<option value="'+esc(s)+'">'+esc(s)+"</option>").join("");
  ["q","seed","f-preset","f-size","f-state","f-date"].forEach(id=>{
    const el=document.getElementById(id);
    el.addEventListener(el.tagName==="SELECT"?"change":"input",debounce(applyFilter,250));
  });
  $("#f-liked").addEventListener("change",applyFilter);
  $("#btn-reset").addEventListener("click",()=>{
    ["q","seed","f-date"].forEach(i=>$("#"+i).value="");
    ["f-preset","f-size","f-state"].forEach(i=>$("#"+i).value="");
    $("#f-liked").checked=false;applyFilter();
  });
}
function debounce(fn,ms){let h;return(...a)=>{clearTimeout(h);h=setTimeout(()=>fn(...a),ms);};}
/* ---------- 分页 ---------- */
$("#pg-prev").addEventListener("click",()=>{if(page>0){page--;render();window.scrollTo(0,0);}});
$("#pg-next").addEventListener("click",()=>{
  if((page+1)*PAGE_SIZE<filtered.length){page++;render();window.scrollTo(0,0);}});
/* ---------- 对比 ---------- */
function toggleCompare(cb){
  const iid=cb.dataset.iid;
  if(cb.checked){
    if(cmp.length>=2){cb.checked=false;toast("最多选择 2 张","err");return;}
    cmp.push(iid);
  }else cmp=cmp.filter(x=>x!==iid);
  $("#cmp-info").textContent="已选 "+cmp.length+"/2";
  $("#cmpbar").style.display=cmp.length?"flex":"none";
}
$("#cmp-clear").addEventListener("click",()=>{
  cmp=[];document.querySelectorAll(".cmp").forEach(c=>c.checked=false);
  $("#cmpbar").style.display="none";
});
$("#cmp-go").addEventListener("click",()=>{
  if(cmp.length!==2){toast("请先勾选 2 张图片","err");return;}
  const cols=cmp.map(iid=>{
    const o=IMG_IDX.get(iid);if(!o)return "";
    const im=o.im,r=o.rec,cur=markOf(iid);
    const rows=[["时间",r.ts],["预设",r.preset||"(空)"],["seed",r.seed],["尺寸",r.w+"×"+r.h],
      ["步数",r.steps],["CFG",r.cfg],["模型","未记录"],["LoRA","未记录"],["采样器","未记录"],["路径",im.path]];
    return '<div class="ovpanel"><div class="imgbox" style="min-height:120px">'+imgInner(im)+"</div>"+
      '<div class="markrow">'+chipHtml(im)+"</div>"+
      '<input class="note" data-iid="'+esc(iid)+'" data-prev="'+esc(cur.note)+'" value="'+esc(cur.note)+
      '" placeholder="备注…" onchange="saveNote(this)">'+
      "<table>"+rows.map(x=>"<tr><td class='k'>"+x[0]+"</td><td>"+esc(x[1])+"</td></tr>").join("")+"</table></div>";
  });
  $("#ov-cols").innerHTML=cols.join("");
  $("#overlay").style.display="block";
});
$("#ov-close").addEventListener("click",closeOverlay);
function closeOverlay(){$("#overlay").style.display="none";}
/* ---------- 原图 / 其他 ---------- */
document.addEventListener("click",e=>{
  const img=e.target.closest(".imgbox img");
  if(!img)return;
  const iid=img.dataset.iid,o=IMG_IDX.get(iid);
  $("#lb-img").src="/img?id="+encodeURIComponent(iid);
  $("#lb-cap").textContent=o?o.im.path:"";
  $("#lightbox").style.display="flex";
});
$("#lb-close").addEventListener("click",()=>$("#lightbox").style.display="none");
$("#lightbox").addEventListener("click",e=>{if(e.target.id==="lightbox")$("#lightbox").style.display="none";});
$("#copyclose").addEventListener("click",()=>$("#copymodal").style.display="none");
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){$("#overlay").style.display="none";$("#lightbox").style.display="none";$("#copymodal").style.display="none";}
});
function showCorrupt(){
  const b=$("#corrupt-banner");
  b.style.display="block";
  b.textContent="⚠ 标记库损坏，已暂停标记写入（浏览与搜索不受影响）。原文件未做任何改动："+
    MARKS.path+"\n原因："+MARKS.error+
    "\n恢复方法：先将该文件复制一份备份，再手工修复 JSON 结构（顶层 {\"version\":1,\"marks\":{}}），修好后重启服务即可恢复写入。工具不会自动覆盖或重置该文件。";
}
/* ---------- 启动 ---------- */
(async function boot(){
  const [d,m]=await Promise.all([fetch("/data").then(r=>r.json()),fetch("/marks").then(r=>r.json())]);
  DATA=d;MARKS=m;
  $("#snapnotice").textContent="📌 "+DATA.snapshot_notice;
  $("#repro").textContent="ℹ "+DATA.repro_notice;
  DATA.records.forEach(r=>{REC_IDX.set(r.id,r);r.images.forEach(im=>IMG_IDX.set(im.id,{im,rec:r}));});
  if(MARKS.corrupt)showCorrupt();
  initFilters();applyFilter();
})();
</script></body></html>"""


# ---------------------------------------------------------------- 启动 -------

def main():
    global APP, PORT, HOSTS_OK, ORIGINS_OK, ROOTS
    ap = argparse.ArgumentParser(description="paint_gallery v1.1 本地绘画记录浏览器")
    ap.add_argument("--csv", default=os.path.join(APP_DIR, "prompt_log.csv"))
    ap.add_argument("--marks", default=os.path.join(APP_DIR, "paint_marks.json"))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--roots", default="",
                    help="额外的允许目录（逗号分隔）。测试实例必须把测试夹具目录列入")
    ap.add_argument("--no-browser", action="store_true",
                    help="保留兼容; 本版始终只打印 URL, 不自动打开浏览器")
    args = ap.parse_args()
    PORT = args.port
    HOSTS_OK = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    ORIGINS_OK = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}

    print("paint_gallery v1.1")
    print(f"Python : {sys.version.split()[0]} ({sys.executable})")

    ROOTS = build_roots([s.strip() for s in args.roots.split(",") if s.strip()])
    add_root_dir_of(args.csv)
    add_root_dir_of(args.marks)
    print(f"允许目录: {ROOTS}")
    print(f"CSV    : {args.csv}（全程只读）")
    print(f"标记库 : {args.marks}")

    lock_fd, lock_err = acquire_marks_lock(args.marks)
    if lock_fd is None:
        print(f"[拒绝启动] {lock_err}")
        sys.exit(2)

    APP = App(args.csv, args.marks)
    APP.load_all()
    cnt = APP.snapshot_payload()["state_counts"]
    img_exist = sum(1 for p, e in APP.image_index.values() if e)
    img_all = len(APP.image_index)
    print(f"记录   : {len(APP.records)} 条（完全重复组 {APP.dup_rows}，列异常 {APP.malformed}）")
    print(f"图片   : 登记 {img_all}，文件在 {img_exist}，丢失 {img_all - img_exist}"
          f"（全部存在 {cnt['all']} / 部分丢失 {cnt['partial']} / 全部丢失 {cnt['none']} / 无路径 {cnt['no_path']}）"
          + (f"，白名单外拒开 {APP.outside}" if APP.outside else ""))
    if APP.corrupt_reason:
        print("=" * 60)
        print(f"⚠⚠ 标记库损坏，写入已暂停: {APP.corrupt_reason}")
        print(f"⚠⚠ 原文件保留未动: {args.marks}")
        print("=" * 60)
    else:
        print(f"标记   : {len(APP.marks.get('marks', {}))} 条")

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"[退出] 端口 {PORT} 不可用: {e}")
        sys.exit(2)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % PORT
    print(f"服务   : {url} （仅本机）  在浏览器打开此地址; Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    main()
