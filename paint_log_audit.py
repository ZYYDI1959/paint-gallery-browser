# -*- coding: utf-8 -*-
"""
paint_log_audit.py — prompt_log.csv 只读审计（常驻工具，运行时数据输出）

用法:
    python tools/paint_log_audit.py            # 审计 tools/prompt_log.csv
    python tools/paint_log_audit.py --csv <路径>

只输出当前数据的事实，不内置任何"通过条件"。
"""
import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def canon_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def main():
    ap = argparse.ArgumentParser(description="prompt_log.csv 只读审计")
    ap.add_argument("--csv", default=os.path.join(APP_DIR, "prompt_log.csv"))
    args = ap.parse_args()
    path = args.csv
    if not os.path.isfile(path):
        print(f"[错误] 文件不存在: {path}")
        sys.exit(1)

    raw = open(path, "rb").read()
    bom = raw[:3] == b"\xef\xbb\xbf"
    size_kb = len(raw) / 1024

    rows = []
    malformed = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or all(not c.strip() for c in row):
                continue
            if len(row) != 10:
                malformed += 1
                continue
            rows.append(row)

    ids = Counter(hashlib.sha256(canon_json(list(r))).hexdigest() for r in rows)
    dup_rows = len(rows) - len(ids)

    cls = Counter()
    ref_total = ref_unique = ref_exist = ref_missing = 0
    sig_ok = 0
    unique_paths = set()
    dates = []
    presets = Counter()
    sizes = Counter()
    multi = 0
    for r in rows:
        dates.append(r[0][:10])
        presets[r[1] or "(空)"] += 1
        sizes[f"{r[3]}x{r[4]}"] += 1
        paths = [p.strip() for p in r[9].split(" | ") if p.strip()]
        if len(paths) > 1:
            multi += 1
        if not paths:
            cls["无路径"] += 1
            continue
        st = []
        for p in paths:
            ref_total += 1
            unique_paths.add(p)
            if os.path.isfile(p):
                ref_exist += 1
                st.append(True)
                with open(p, "rb") as fh:
                    if fh.read(8) == PNG_SIG:
                        sig_ok += 1
            else:
                ref_missing += 1
                st.append(False)
        if all(st):
            cls["全部存在"] += 1
        elif any(st):
            cls["部分丢失"] += 1
        else:
            cls["全部丢失"] += 1
    ref_unique = len(unique_paths)

    print("=" * 62)
    print(f"prompt_log.csv 只读审计 · {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 62)
    print(f"文件       : {path}")
    print(f"大小       : {size_kb:.1f} KB | UTF-8 BOM: {'有(读取用 utf-8-sig)' if bom else '无'}")
    print(f"记录       : {len(rows)} 条 | 列数异常 {malformed} | 完全重复组 {dup_rows} | 稳定ID唯一 {len(ids)}")
    print(f"记录四态   : 全部存在 {cls['全部存在']} / 部分丢失 {cls['部分丢失']} / "
          f"全部丢失 {cls['全部丢失']} / 无路径 {cls['无路径']}")
    print(f"路径引用   : 总数 {ref_total} | 去重 {ref_unique} | 存在 {ref_exist} | 缺失 {ref_missing}")
    print(f"PNG 签名   : {sig_ok} 个存在文件通过 8 字节签名检查")
    print(f"           ⚠ 完整解码情况尚未全部验证（签名正确≠内容完整可解码）")
    print(f"多图记录   : {multi} 条")
    if dates:
        print(f"日期范围   : {min(dates)} ~ {max(dates)}")
    print(f"预设分布   : {dict(presets.most_common())}")
    print(f"尺寸Top    : {dict(sizes.most_common(6))}")
    print("=" * 62)
    print("未记录字段（历史原因）: 模型、LoRA、采样器、调度器、负向词、图生图参数")
    sys.exit(0)


if __name__ == "__main__":
    main()
