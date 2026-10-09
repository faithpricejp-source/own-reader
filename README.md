# own-reader（讀書）

在浏览器里读自己的 Calibre 书库：划线、评注、选中一段直接问 AI，所有阅读行为写进本机的事件库。服务端跑在自己的 Mac 上，手机、平板、墨水屏用浏览器打开同一个页面；Mac 上另有一个 WKWebView 外壳 App「讀書」。

设计说明见 [DESIGN.md](DESIGN.md)。

## 为什么做这个

这是「个人操作系统」系列中的一个 App。这个系列只有一个目标：把我和外界之间每一次信息往来的动作和历史都留在自己手里——看了什么、在哪里停留了多久、点了什么、买了什么、卖了什么、看完之后做了什么。

普通人的行为受各种已知和未知的条件、环境与刺激影响，处在一种伪随机的状态里，就像别人在我们身上装了开关：这个开关一拨、那根弦一拨，我们就做出别人预期中的动作。自己感觉像随机，在别人眼里却像提线木偶。平台和机构握着我们的行为数据，比我们更了解自己。这一整套东西，就是为了改变这个状况。

背后的想法是：个人对外部世界的理解好比一滴水去理解大海，几乎不可能做到。但一滴水向内看，看清自己的每个分子怎么动，是做得到的——什么温度下我会怎么动，遇到什么潮汐、什么洋流又会怎么动。把这些搞清楚，就能为自己争取更好的生存条件。这比理解整个大海现实得多。

这滴水自己的状态也要记下来。同一个人，前一晚没睡好、当天和家人吵了架、身体不舒服的时候，重大决定出错的概率明显更高，很多人的回忆录里都写到过这样的时刻。所以除了记录动作，还要记录当时的身体、情绪和外部环境（天气、行情、日程），之后才看得出「在什么状态下，我会怎么做」。

这些 App 的后台最终要打通、互相共享信息。大型金融集团早就这样对待每一个客户：把他在存款、贷款、保险、证券上的行为合在一起看，再做交叉销售。有了 AI，个人没有理由不能对自己做同样的事——区别是这一次，数据和分析只为自己服务。目前每个 App 各用一个本机 SQLite 库，打通是下一步。

理想的最终状态是：所有能接触到我的信息，都要先经过我自己做的过滤网和记录器才能进来；我的反馈和行为，也要先经过我自己的过滤网和保护器才能发出去。

所以这个系列的共同约定是：所有行为记录写进本机数据库，不上传任何第三方；AI 分析在本机或用户自己选择的服务上运行。

## 功能

- **书架**：直接读 Calibre 的 `metadata.db`（只读），按书名/作者搜索，最近入库；EPUB 优先，其次 PDF。
- **阅读页**（foliate-js 渲染）：点左右翻页、目录、划线（多色）、评注、删除；进度跨设备同步；可选服务器上的字体；发光屏「桌面」主题与墨水屏黑白主题自动切换。
- **选中提问**：选中文字后提问，服务器把选段前后各约 6000 字原文 + 全书 BM25 检索到的相关段落拼进 prompt；回答可打分、可换别的模型重问。
- **对照翻译**：外文书读到哪翻到哪，译文插在原文段落下面，段落级缓存永不重翻；也能导出整本双语 EPUB 给别的阅读器用（`/api/books/<id>/bilingual`）。
- **在读 / 读过 / 笔记**：合并本 App 的阅读事件与（可选）导入的微信读书笔记、进度。
- **荐书**：根据阅读史、笔记、提问和你对过往推荐的反馈，让 AI 每批推荐 5–10 本，并到 Calibre / 微信读书书城 / Open Library / 国立国会図書館查证书确实存在。
- **书库评分**：给整个 Calibre 书库分类，并打「想读 / 用得上 / 应该读」三个只面向你本人的分；书架上逐本反馈、读完 / 搁置等行为会回流，每晚增量重算。
- **Mac App**：⌘H 书架、⌘[ 返回、⌘R 重载、⌘E 切换墨水屏模式、⌘+/⌘-/⌘0 缩放；服务没响应时先 `launchctl kickstart` 再重试。

## 依赖

- macOS（Mac App 与 launchd 部分；服务端本身是普通 Python，理论上别的系统也能跑，未测试）
- Python 3（作者在 3.14 下开发和测试，更低版本未验证）；服务端核心只用标准库，`lxml`（双语 EPUB）、`fonttools`（字体列表）、`pytest`（测试）见 `requirements.txt`
- Calibre 书库
- 至少一个 AI 后端：本机任意 OpenAI 兼容服务（llama.cpp / LM Studio / Ollama …），或 OpenRouter key，或已登录的 `claude` / `grok` / `kimi` / `agy` 官方 CLI
- 编译 Mac App：Xcode Command Line Tools（`swiftc`）
- PDF 支持：需要 pdf.js（约 13MB，未入库）。从上游 foliate-js commit 78914ae 的 `vendor/pdfjs/` 复制到本仓库 `vendor/foliate-js/vendor/pdfjs/`；不放则只能读 EPUB。

## 安装与运行

```sh
git clone <this repo> own-reader && cd own-reader
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.conf config.conf      # 按需改：至少确认 OWN_READER_CALIBRE 和 AI 后端
.venv/bin/python server/app.py          # 打开 http://127.0.0.1:8460/
.venv/bin/python -m pytest -q           # 测试（全部离线，用临时目录）
```

- 常驻：按 `launchd/com.example.ownreader.plist.example` 里的说明替换占位符，放进 `~/Library/LaunchAgents/`。
- 其他设备访问：服务只绑 127.0.0.1、没有登录，请自己在前面加一层只对自己可达的通道（例如 `tailscale serve --bg --https=8445 http://127.0.0.1:8460`）。不要直接暴露到公网。
- Mac App：`macapp/build.sh`，产物在 `build/讀書.app`，`ditto build/讀書.app /Applications/讀書.app`。改了 launchd label 或端口，同步改 `macapp/Info.plist` 的 `OwnReaderLaunchdLabel` / `OwnReaderURL`。
- 后台任务（可选，均可手动跑或另建 launchd）：
  - `server/import_history.py` 导入微信读书笔记（需要 key，见下）
  - `server/recommend.py generate` 出一批荐书
  - `server/library_class.py pilot | run` 书库首轮分类评分；`server/library_feedback.py refresh` 夜间增量

## 配置

全部通过环境变量，或仓库根目录的 `config.conf`（KEY=VALUE；已加入 .gitignore；`OWN_READER_CONFIG` 可指向别处）。完整列表与默认值见 [config.example.conf](config.example.conf)。常用的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `OWN_READER_CALIBRE` | `~/Calibre Library` | Calibre 书库目录 |
| `OWN_READER_DATA` | `~/Library/Application Support/own-reader` | 数据目录 |
| `OWN_READER_DEFAULT_BACKEND` | `local` | 选中提问用的后端 |
| `OWN_READER_FALLBACK_BACKEND` | 空 | 默认后端失败时改用的后端 |
| `OWN_READER_LOCAL_LLM_URL` | `http://127.0.0.1:8080/v1` | 本机 OpenAI 兼容服务 |
| `OWN_READER_RECS_BACKEND` / `OWN_READER_LIBRARY_BACKEND` | `claude` | 荐书 / 书库评分用的后端 |
| `OWN_READER_TRANSLATE_CHAIN` | `mt_free,mt_local` | 翻译后端顺序 |
| `OWN_READER_TAXONOMY` | 内置通用类目 | 自定义类目表，格式见 `taxonomy.example.json` |
| `OWN_READER_WEREAD_KEY_FILE` | `~/.config/weread/api-key.txt` | 微信读书导入（可选） |
| `OWN_READER_HOMECINEMA_DB` / `OWN_READER_NEWS_API` | 空 | 荐书的辅助信号（可选） |

仓库里没有任何密钥。各后端用它自己 CLI 已登录的凭据，或你指定的 key 文件。

`server/llm.py` 里的 `POLICY_DRAFT` 按数据敏感度限定后端：你的笔记、提问、阅读记录默认**不会**发给 `free`（OpenRouter 免费档）和 `gemini`；书的正文（翻译用）可以发给任何后端。按自己的信任边界改这张表。

## 数据存在哪

- `<数据目录>/reader.sqlite`：事件库（唯一真值），以及派生表：翻译缓存、书库分类评分、荐书、导入的外部笔记、模型调用审计（`llm_calls`，只存长度与元数据，不存 prompt 原文）。
- `<数据目录>/booktext/`、`bilingual/`、`import/`：全文段落缓存、双语 EPUB、微信读书原始 JSON 缓存。
- `<数据目录>/profile.md`、`needs.md`、`should.md`、`positions.md`、`rec_memo.md`：荐书与评分读的「关于你」的文字，首次运行生成模板，随便改。
- `<数据目录>/fonts/`：放进去的 ttf/otf 会出现在阅读页的字体选单里（也会读 `~/Library/Fonts`）。
- Calibre 书库只读，不会写。

## 局限

- 这是作者在自己机器上为自己开发的，公开版把私有集成都改成了配置项或删掉了，部分功能需要自行配置才能用：AI 后端、微信读书 key、翻译模型、荐书的辅助信号。
- 单用户、无登录、无权限控制，只适合放在只有自己能访问的网络里。
- 微信读书导入用的是它的 agent gateway 接口，接口变动随时可能失效。
- 荐书与书库评分的 prompt 是中文写的，针对中文读者。
- `claude` / `grok` / `kimi` / `gemini` 后端是通过各家官方 CLI 的非交互模式调用订阅；是否符合你所在地区、你的订阅条款，请自行确认。
- PDF 需要自行放入 pdf.js（见「依赖」）。

## English (short)

own-reader is a single-user, self-hosted reading app: it serves your Calibre library to any browser on your own network, renders EPUB/PDF with foliate-js, and records every reading action (progress, highlights, notes, AI questions) in a local SQLite event log. Select a passage to ask an AI backend of your choice (a local OpenAI-compatible server, OpenRouter, or the official Claude / Grok / Kimi / Gemini CLIs); foreign-language books get on-demand interlinear translation. It also generates book recommendations and scores your whole library against your own reading history. Configuration is by environment variables or `config.conf` (see `config.example.conf`); no keys are stored in the repo. The UI and prompts are in Chinese. Developed on the author's own Mac; some integrations need your own setup. License: GPL-3.0 (vendored libraries keep their own licenses).

## 许可证

GPL-3.0，见 [LICENSE](LICENSE)。`vendor/foliate-js` 为 MIT（见其目录下 LICENSE），`vendor/marked.min.js` 为 MIT（见文件头）。
