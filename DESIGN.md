# own-reader 设计

自己的「微信读书」：书在自己的 Calibre 书库里，阅读记录和 AI 问答都存在自己机器的数据库里，各设备用浏览器打开同一个页面（Mac 上另有一个 WKWebView 外壳 App）。

## 为什么是服务器端 AI

- 现成的开源阅读器（ReadAware / Readest / Anx 等）大多把 AI 放在客户端，靠客户端调用 API。
- 这里的关键差别：**AI 在服务器（自己的 Mac）上跑**。服务器离书、本机模型、各家官方 CLI 都近；手机、墨水屏只负责显示和选中文字。
- 不从零写渲染：用 [foliate-js](https://github.com/johnfactotum/foliate-js)（MIT，`vendor/foliate-js`，上游 commit 78914ae）。

## 架构

```
设备浏览器 ──(你自己的反向代理 / VPN)──> 127.0.0.1:8460 server/app.py
                                         ├── Calibre metadata.db（只读）
                                         ├── reader.sqlite（事件库，唯一真值）
                                         └── AI 后端：local(OpenAI 兼容) / free(OpenRouter :free) / claude / grok / kimi / gemini
```

- 只监听 127.0.0.1。单用户，不做登录；要给别的设备用，自己在前面加一层只对自己可达的通道（如 Tailscale、SSH 隧道）。
- 服务端核心是 Python 标准库（http.server + sqlite3）；双语 EPUB 用 lxml，字体列表用 fontTools。

## 数据：只追加的事件库

`events(id, ts, device, book_id, type, cfi, text, payload)`。主要 type：

| type | 含义 | payload |
|---|---|---|
| open | 打开书 | — |
| progress | 翻页后的位置 | fraction, toc 标题 |
| highlight | 划线 | color |
| note | 评注（挂在划线上） | highlight_id |
| ask | 选中提问 | question, answer, backend, model, latency_ms, context_chars |
| feedback | 对回答打分 | ask_id, rating(+1/-1) |
| delete | 删除划线/评注 | target_id |
| search | 搜索词 | tab |
| rec_feedback / rec_comment / rec_memo_edit | 对荐书的反馈 | rec_id, verdict, comment |
| book_feedback | 书架上对某本书的反馈 | calibre_id, verdict, comment, pred |

划线、评注、进度都是从事件推出来的视图，不单独存表。荐书、书库评分直接读这张表。另有派生表：翻译缓存 `translations`、模型调用审计 `llm_calls`（只存长度与元数据，不存 prompt 原文）、书库分类 `book_class` 等。

## 提问的上下文

客户端把「选中文字 + 当前章节里选区前后各约 6000 字」发给服务器；服务器再用 BM25 在全书里检索几段相关原文（`server/book_text.py`），一起拼进 prompt。「答案就在后两句」或「前面某章讲过」的情况都能覆盖。

## 敏感度策略

`server/llm.py` 的 `POLICY_DRAFT` 按数据敏感度限定后端：书的正文（public）可以给任何后端；用户的笔记、提问、阅读记录（personal）默认不给免费档和 gemini；private 只给本机模型。每次调用都记一行审计。
