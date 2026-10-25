# paint-gallery-browser · 绘画记录浏览器

本地绘画记录浏览器（找回 · 比较 · 标记 · 复用）。纯 Python 标准库实现，单文件启动，零第三方依赖，自动调起系统默认浏览器。

针对 AI 绘画工作流的痛点而设计：画了几百上千张图，日志散落在 CSV 里，好图找不回、参数对不上。本工具把记录变成可搜索、可比较、可标记、可复用的画廊。

## 功能

- **记录浏览**：解析无表头十列 CSV 绘画日志（UTF-8 BOM），分页 + 懒加载
- **图片四态**：全部存在 / 部分丢失 / 全部丢失 / 无路径，丢失图自动占位
- **搜索过滤**：按提示词关键词、预设、日期区间过滤
- **同题比较**：同一提示词的多次生成并排对比
- **三态标记**：喜欢 / 可用 / 需要重做（独立可并存）+ 备注，原子写独立标记库
- **参数复现**：完整参数卡片 + 一键复制，如实声明「模型与工作流信息未记录，不保证复现」
- **完整校验**：PNG 签名校验、重复记录检测（确定性 SHA-256 身份，免疫转义风格变化）

## 安全设计

- 仅监听 `127.0.0.1`，不暴露局域网
- Host 校验；变更操作仅接受 POST + 每次启动随机令牌；同源、禁跨域
- 图片仅按启动时登记的图片 ID 提供，不接受路径参数
- 文件访问限定在白名单目录内，realpath 规范化并拒绝 `..`
- 标记库损坏时保留原文件并暂停写入（不静默重置）
- 请求体/备注长度上限，无 subprocess

## 快速开始

```bash
# 正式实例（默认读取脚本同目录 prompt_log.csv，http://127.0.0.1:8787/）
python paint_gallery.py

# 测试实例：独立 CSV / 端口 / 标记库 / 图片根目录
python paint_gallery.py --csv test.csv --port 8790 --marks test_marks.json --roots "D:\images"
```

CSV 格式（无表头、UTF-8 BOM、十列）：时间 / 预设 / seed / 宽 / 高 / 步数 / CFG / 中文提示词 / 英文提示词 / 图片路径（多图以 ` | ` 分隔）。

## 测试

```bash
# 21 项自测（自建夹具：四态图片、多图、损坏 JSON、保存失败等）
python paint_gallery_selftest.py

# 真实浏览器验收（Node 24+，零依赖，Edge CDP；需先启动正式服务与调试端口 Edge）
node _gallery_browser.mjs phase1|phase2|corrupt
```

## 文件

| 文件 | 说明 |
|---|---|
| `paint_gallery.py` | 主程序（服务 + 前端，单文件） |
| `paint_gallery_selftest.py` | 独立夹具自测 |
| `paint_log_audit.py` | 日志库常驻审计（四态统计、签名校验、重复检测） |
| `_gallery_browser.mjs` | Edge CDP 真实浏览器验收驱动 |
| `docs/paint_gallery_验收报告.md` | v1.1 验收报告 |

## 设计原则

- **数据零接触**：原始 CSV 全程只读，标记写在完全独立的 JSON 库
- **纯标准库**：Python 3.x 标准库 + 原生 http.server，无任何依赖
- **失败可见**：保存失败保留内存原状态并向页面报错，绝不静默丢标记

## License

[MIT](LICENSE)
