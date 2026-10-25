# -*- coding: utf-8 -*-
"""
paint_gallery_selftest.py — paint_gallery v1.1 独立故障测试（不触碰正式数据）

范围: 独立夹具目录 + 独立端口(8788/8789) + 独立标记库
覆盖: 四态记录/多图/重复识别/安全头(Host/令牌/POST-only/体积/长度)/
      保存失败内存保持/并发写锁/重启持久/损坏JSON暂停写入/跨进程独占锁/
      截断PNG按字节提供(解码失败由页面处理)/缺失与未知图片ID
用法: python tools/paint_gallery_selftest.py
"""
import csv as _csv
import http.client
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

APP_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
GALLERY = str(APP_DIR / "paint_gallery.py")
FIX = APP_DIR / "paint_gallery_test"
PORT_A, PORT_B, PORT_C = 8788, 8789, 8790
ALLOWED_LABELS = ("喜欢", "可用", "需要重做")

sys.stdout.reconfigure(encoding="utf-8")

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, ""))
        print(f"  ✓ {name}" + (f" — {detail}" if detail else ""))
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print(f"  ✗ {name} — {e}")
    except Exception as e:
        RESULTS.append((name, False, f"异常: {e}"))
        print(f"  ✗ {name} — 异常: {e}")


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    return msg


# ---------------------------------------------------------------- 夹具 -------

def make_png(w=3, h=3, rgb=(200, 60, 60)):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def build_fixtures():
    shutil.rmtree(str(FIX), ignore_errors=True)
    FIX.mkdir(parents=True, exist_ok=True)
    pngs = {}
    for name in ("fx_ok1.png", "fx_m1.png", "fx_m2.png", "fx_m3.png", "fx_partial.png"):
        pngs[name] = make_png()
        (FIX / name).write_bytes(pngs[name])
    full = make_png(8, 8, (30, 120, 220))
    pngs["fx_trunc.png"] = full[:40]  # 签名+IHDR 完整、IDAT 截断 → 浏览器必解码失败
    (FIX / "fx_trunc.png").write_bytes(pngs["fx_trunc.png"])
    miss = str(FIX / "__missing_1__.png")

    rows = [
        ["2026-10-04 10:00:00", "anime", "111", "832", "1216", "26", "5",
         '测试一 <a> & "引号" \'单引号\', 逗号', 'test one <b> & "q" \'s\', comma',
         str(FIX / "fx_ok1.png")],
        ["2026-10-04 10:01:00", "anime", "222", "1024", "1024", "26", "5",
         "测试二 多图", "test two multi",
         " | ".join([str(FIX / "fx_m1.png"), str(FIX / "fx_m2.png"),
                     str(FIX / "fx_m3.png")])],
        ["2026-10-04 10:02:00", "real", "333", "832", "1216", "26", "5",
         "测试三 部分丢失", "test three partial",
         str(FIX / "fx_partial.png") + " | " + miss],
        ["2026-10-04 10:03:00", "real", "444", "512", "512", "8", "5",
         "测试四 全丢失", "test four none", miss],
        ["2026-10-04 10:04:00", "zphoto", "555", "1024", "1024", "4", "1",
         "测试五 无路径", "test five no path", ""],
        ["2026-10-04 10:05:00", "anime", "666", "8", "8", "26", "5",
         "测试六 截断图", "test six truncated", str(FIX / "fx_trunc.png")],
    ]
    rows.append(list(rows[0]))  # 完全重复行 → 重复组 1
    buf = io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerows(rows)
    (FIX / "test_log.csv").write_text(buf.getvalue(), encoding="utf-8")
    (FIX / "test_marks_corrupt.json").write_text(
        '{"version": 1, "marks": {BROKEN', encoding="utf-8")
    return pngs, miss


# ---------------------------------------------------------------- HTTP -------

def req(method, port, path, body=None, headers=None, timeout=15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        return r.status, data
    finally:
        conn.close()


def post_mark(port, token, iid, labels, note="", host="127.0.0.1"):
    payload = json.dumps({"image_id": iid, "labels": labels, "note": note}).encode("utf-8")
    return req("POST", port, "/api/mark", body=payload, headers={
        "Content-Type": "application/json", "X-Token": token,
        "Content-Length": str(len(payload)), "Host": f"{host}:{port}"})


def wait_port(port, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def start_instance(port, marks, tag):
    log = (FIX / f"inst_{tag}.log").open("w", encoding="utf-8")
    p = subprocess.Popen(
        [sys.executable, GALLERY, "--csv", str(FIX / "test_log.csv"),
         "--marks", str(marks), "--port", str(port), "--roots", str(FIX), "--no-browser"],
        stdout=log, stderr=subprocess.STDOUT)
    if not wait_port(port):
        p.terminate()
        raise RuntimeError(f"实例 {tag} 未在 20s 内就绪")
    return p


def get_json(port, path):
    st, data = req("GET", port, path)
    expect(st == 200, f"{path} → {st}")
    return json.loads(data.decode("utf-8"))


def read_marks(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def by_seed(payload, seed):
    for r in payload["records"]:
        if r["seed"] == seed:
            return r
    raise AssertionError(f"找不到 seed={seed}")


def main():
    pngs, miss_path = build_fixtures()
    marks_a = FIX / "test_marks.json"
    proc_a = proc_b = None
    try:
        print("— 启动测试实例 A (8788) —")
        proc_a = start_instance(PORT_A, marks_a, "A")
        st, home = req("GET", PORT_A, "/")
        check("首页 200 且含令牌", lambda: expect(
            st == 200 and b"paint_gallery" in home, f"status={st}"))
        token = re.search(rb'const TOKEN="([^"]+)"', home).group(1).decode()

        data = get_json(PORT_A, "/data")

        def _counts():
            expect(len(data["records"]) == 6, f"records={len(data['records'])}")
            expect(data["dup_rows"] == 1, f"dup={data['dup_rows']}")
            expect(data["malformed"] == 0, f"malformed={data['malformed']}")
            return "6 条记录 / 重复组 1 / 列异常 0"
        check("记录/重复/列异常计数", _counts)

        def _states():
            expect(by_seed(data, "111")["state"] == "all", "111 应为 all")
            r2 = by_seed(data, "222")
            expect(r2["state"] == "all" and len(r2["images"]) == 3, "222 应为 all 且 3 图")
            expect(by_seed(data, "333")["state"] == "partial", "333 应为 partial")
            expect(by_seed(data, "444")["state"] == "none", "444 应为 none")
            expect(by_seed(data, "555")["state"] == "no_path", "555 应为 no_path")
            r6 = by_seed(data, "666")
            expect(r6["state"] == "all" and r6["images"][0]["sig"] is True,
                   "666 应为 all 且签名通过")
            return "四态 + 多图 + 签名 全部正确"
        check("记录四态/多图/签名分类", _states)

        iid_r1 = by_seed(data, "111")["images"][0]["id"]
        iid_r6 = by_seed(data, "666")["images"][0]["id"]
        img_r3 = by_seed(data, "333")["images"]
        token_holder = {"t": token}  # 重启后令牌会变, 统一经此处取用

        check("GET /api/mark → 405", lambda: expect(
            req("GET", PORT_A, "/api/mark")[0] == 405, "应拒绝 GET 变更"))
        check("无令牌 POST → 403", lambda: expect(
            req("POST", PORT_A, "/api/mark", body=b"{}",
                headers={"Content-Type": "application/json"})[0] == 403, "缺令牌应 403"))
        check("错误令牌 POST → 403", lambda: expect(
            req("POST", PORT_A, "/api/mark", body=b"{}",
                headers={"Content-Type": "application/json", "X-Token": "wrong"})[0] == 403,
            "错令牌应 403"))
        check("伪造 Host → 403", lambda: expect(
            req("GET", PORT_A, "/data", headers={"Host": "evil.com:8788"})[0] == 403,
            "非法 Host 应 403"))

        def _big_body():
            st2, _ = req("POST", PORT_A, "/api/mark", body=b"x" * 70000,
                         headers={"Content-Type": "application/json", "X-Token": token,
                                  "Content-Length": "70000"})
            return expect(st2 == 413, f"status={st2}")
        check("超大请求体 → 413", _big_body)

        def _long_note():
            st2, d2 = post_mark(PORT_A, token, iid_r1, [], "长" * 600)
            return expect(st2 == 400, f"status={st2} body={d2[:80]}")
        check("超长备注 → 400", _long_note)

        check("未知图片 ID → 404", lambda: expect(
            post_mark(PORT_A, token, "deadbeef" * 8, ["喜欢"])[0] == 404, "未知 ID 应 404"))

        def _origin():
            payload = json.dumps({"image_id": iid_r1, "labels": ["喜欢"], "note": ""}).encode("utf-8")
            s1, _d1 = req("POST", PORT_A, "/api/mark", body=payload, headers={
                "Content-Type": "application/json", "X-Token": token,
                "Origin": f"http://127.0.0.1:{PORT_A}", "Content-Length": str(len(payload))})
            expect(s1 == 200, f"正常本地 Origin 应 200, 实为 {s1}")
            s2, _d2 = req("POST", PORT_A, "/api/mark", body=payload, headers={
                "Content-Type": "application/json", "X-Token": token,
                "Origin": "http://evil.com", "Content-Length": str(len(payload))})
            expect(s2 == 403, f"外站 Origin 应 403, 实为 {s2}")
            s3, _d3 = req("POST", PORT_A, "/api/mark", body=payload, headers={
                "Content-Type": "application/json", "X-Token": token,
                "Origin": "null", "Content-Length": str(len(payload))})
            expect(s3 == 403, f"null Origin 应 403, 实为 {s3}")
            # 缺失 Origin（上面其余全部请求均不带）→ 放行, 由令牌兜底
            return "本地 200 / 外站 403 / null 403 / 缺失放行（令牌兜底）"
        check("Origin 同源校验四态", _origin)

        def _valid_save():
            st2, d2 = post_mark(PORT_A, token, iid_r1, ["喜欢", "可用"], "好的那张")
            expect(st2 == 200, f"status={st2} {d2[:80]}")
            ent = read_marks(marks_a)["marks"].get(iid_r1)
            expect(ent and ent["labels"] == ["喜欢", "可用"] and ent["note"] == "好的那张",
                   "落盘内容不一致")
            return "落盘与响应一致"
        check("合法标记保存 + 落盘一致", _valid_save)

        def _concurrent():
            def one(i):
                lab = [ALLOWED_LABELS[i % 3]]
                s, _d = post_mark(PORT_A, token, iid_r1, lab)
                return s == 200
            with ThreadPoolExecutor(max_workers=8) as ex:
                ok = list(ex.map(one, range(8)))
            expect(all(ok), "并发请求有失败")
            ent = read_marks(marks_a)["marks"].get(iid_r1)
            expect(ent is not None and all(x in ALLOWED_LABELS for x in ent["labels"]),
                   "并发后文件损坏或标签非法")
            return "8 并发后文件仍为合法 JSON"
        check("并发写（锁串行）", _concurrent)

        final_labels = read_marks(marks_a)["marks"][iid_r1]["labels"]

        def _restart():
            nonlocal proc_a
            proc_a.terminate()
            proc_a.wait(timeout=10)
            proc_a = start_instance(PORT_A, marks_a, "A2")
            _st, _home = req("GET", PORT_A, "/")
            token_holder["t"] = re.search(rb'const TOKEN="([^"]+)"',
                                          _home).group(1).decode()
            m = get_json(PORT_A, "/marks")
            ent = m["marks"].get(iid_r1)
            expect(ent and ent["labels"] == final_labels, f"重启后标记丢失: {ent}")
            return "重启后标记保留"
        check("停止并重启 → 标记持久", _restart)

        def _save_fail():
            tk = token_holder["t"]
            os.chmod(str(marks_a), 0o444)  # 只读 → os.replace 必失败
            try:
                st2, d2 = post_mark(PORT_A, tk, iid_r1, final_labels + ["需要重做"])
                expect(st2 == 500, f"应 500 实为 {st2}")
                now = get_json(PORT_A, "/marks")["marks"].get(iid_r1, {}).get("labels", [])
                expect(now == final_labels, f"内存状态被污染: {now}")
                disk = read_marks(marks_a)["marks"][iid_r1]["labels"]
                expect(disk == final_labels, f"磁盘状态被污染: {disk}")
            finally:
                os.chmod(str(marks_a), 0o644)
            st3, _d3 = post_mark(PORT_A, tk, iid_r1, final_labels + ["需要重做"])
            expect(st3 == 200, f"恢复后应可写: {st3}")
            leftover = [x.name for x in FIX.iterdir() if x.name.startswith(".paint_marks_")]
            expect(not leftover, f"残留临时文件: {leftover}")
            return "500 / 内存与磁盘均保持原状态 / 恢复后可写 / 无残留"
        check("保存失败 → 内存不保留未保存修改", _save_fail)

        def _trunc_bytes():
            st2, d2 = req("GET", PORT_A, "/img?id=" + iid_r6)
            expect(st2 == 200, f"status={st2}")
            expect(d2 == pngs["fx_trunc.png"], "字节与夹具不一致")
            return f"{len(d2)} 字节（签名正确、解码失败由页面占位处理）"
        check("截断 PNG 按字节提供", _trunc_bytes)

        def _missing_img():
            iid_miss = img_r3[1]["id"]
            st2, d2 = req("GET", PORT_A, "/img?id=" + iid_miss)
            return expect(st2 == 404 and b"file missing" in d2, f"{st2} {d2[:60]}")
        check("缺失文件 ID → 404", _missing_img)

        check("未知图片请求 → 404", lambda: expect(
            req("GET", PORT_A, "/img?id=" + "0" * 64)[0] == 404, "应 404"))

        st, _d = req("GET", PORT_A, "/img?id=" + img_r3[0]["id"])
        check("正常图片 → 200", lambda: expect(st == 200, f"status={st}"))

        # ---- 损坏标记库（实例 B）----
        print("— 启动测试实例 B (8789, 损坏标记库) —")
        corrupt_path = FIX / "test_marks_corrupt.json"
        before = corrupt_path.read_bytes()
        proc_b = start_instance(PORT_B, corrupt_path, "B")

        def _corrupt_mode():
            m = get_json(PORT_B, "/marks")
            expect(m["corrupt"] is True, f"corrupt={m.get('corrupt')}")
            st2, _d2 = req("GET", PORT_B, "/data")
            expect(st2 == 200, "损坏模式下浏览应可用")
            data_b = get_json(PORT_B, "/data")
            iid_b = by_seed(data_b, "111")["images"][0]["id"]
            tok_b = re.search(rb'const TOKEN="([^"]+)"',
                              req("GET", PORT_B, "/")[1]).group(1).decode()
            st3, d3 = post_mark(PORT_B, tok_b, iid_b, ["喜欢"])
            expect(st3 == 409, f"损坏模式应 409, 实为 {st3}")
            expect(corrupt_path.read_bytes() == before, "损坏文件被改动")
            return "corrupt=true / 浏览可用 / 写入 409 / 原文件未动"
        check("损坏 JSON → 暂停写入且保留原文件", _corrupt_mode)

        # ---- 跨进程独占锁（A 仍在跑, 同一标记库再起实例 C）----
        def _lock():
            r = subprocess.run(
                [sys.executable, GALLERY, "--csv", str(FIX / "test_log.csv"),
                 "--marks", str(marks_a), "--port", str(PORT_C), "--roots", str(FIX),
                 "--no-browser"],
                capture_output=True, timeout=30)
            out = (r.stdout + r.stderr).decode("utf-8", "replace")
            return expect(r.returncode != 0 and "占用" in out,
                          f"rc={r.returncode} out={out[-200:]}")
        check("同标记库第二实例 → 拒绝启动", _lock)

    finally:
        for p in (proc_a, proc_b):
            if p is not None:
                try:
                    p.terminate()
                    p.wait(timeout=10)
                except Exception:
                    pass

    fails = [x for x in RESULTS if not x[1]]
    print("=" * 62)
    print(f"自测结果: {len(RESULTS) - len(fails)}/{len(RESULTS)} 通过")
    for name, ok, detail in fails:
        print(f"  ✗ 未通过: {name} — {detail}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
