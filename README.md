[English](README.en.md) | 中文

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
- **对照翻译**：外文书读到哪翻到哪，译文插在原文段落下面，段落级缓存永不重翻；也能导出整本双语 EPUB 给别的阅读器用（`/api/books/<id>/bilingual`）。可选整本批量翻译（Claude API Message Batches，见下）。
- **地理批注**：选中一段点「地理」，出地形图 + 地名事实表 + 形势解说（见下）。
- **把书读厚**（需要 Claude API）：逐段精读批注、每章导读、全书导读（见下）。
- **在读 / 读过 / 笔记**：合并本 App 的阅读事件与（可选）导入的微信读书笔记、进度。
- **荐书**：根据阅读史、笔记、提问和你对过往推荐的反馈，让 AI 每批推荐 5–10 本，并到 Calibre / 微信读书书城 / Open Library / 国立国会図書館查证书确实存在。
- **书库评分**：给整个 Calibre 书库分类，并打「想读 / 用得上 / 应该读」三个只面向你本人的分；书架上逐本反馈、读完 / 搁置等行为会回流，每晚增量重算。
- **Mac App**：⌘H 书架、⌘[ 返回、⌘R 重载、⌘E 切换墨水屏模式、⌘+/⌘-/⌘0 缩放（书架等页面整页缩放；阅读页固定 100% 改调字号，因为整页缩放会让 foliate 的分栏错开半页）；服务没响应时先 `launchctl kickstart` 再重试。

## 把书读厚：精读批注、每章导读、全书导读

三层都由 AI 写，原则相同：书外事实不能编，书内引用必须是原文。都需要 Claude API（见下一节），只支持 EPUB。

- **精读批注**（顶栏「批」）：按读者画像，只批这位读者可能看不懂或会错过的地方，分五类——**背景**（文化习俗、典故、制度、词在当时的含义）、**释义**（术语、专名、理工书里缺的前置知识或跳掉的推导）、**存疑**（作者可能说错或有争议，必须写依据和把握，标「模型判断」）、**双关**、**言外**（阴阳话、明褒实贬、话里有话）。批注引用的原文必须**逐字**出现在该段里（只容忍空白与弯直引号差异），否则整条丢弃；没把握就不批。批注插在段落下方，不改原文节点，划线位置不受影响。
  - 每本书单独开关，读到哪批到哪并预取下一节；段落级缓存，批过的段永不重批。也可「整本预跑」，走 Message Batches 一次提交全书。
  - 读者画像存在 `<数据目录>/reader_profile.md`，首次运行生成一个示例，**请在「批」面板里改成你自己的**（母语、外语水平、专业背景、熟悉和不熟悉的领域）。
  - **墨水屏折叠模式**：墨水屏主题和窄屏默认把批注折叠成段末一个小标记（如〔批：背景 言外2〕），点开在底部面板看；面板里可切换「折叠 / 展开在段落下」。
- **每章导读**（「导读」面板里生成）：按 EPUB 目录切章，目录对不上时让 Sonnet 从「像标题的短行」里挑章首（只认候选里的行号）；每章一个请求走 Message Batches，写「这一章在干什么 / 要点 / 跳步与存疑 / 留给你的」，打开「批」后显示在章首。
- **全书导读**（顶栏「导读」）：整本书（带章节名）放进 Opus 的 1M 上下文，开联网搜索核书外事实，按固定小节写：一句话、来龙去脉、想干嘛、结构与论证路线、独特之处、偏颇盲区与争议、后来的反响、和你的关系、留给你的（AI 替你总结不了、需要自己读原文判断的问题）。书外事实只认联网搜索结果并附来源链接，查不到写「未核」。一本书生成一次并缓存；作者实测一本 36 万 token 的英文书约 2.7 美元、6 分钟。

## Claude API 与 Message Batches

`claude_api` 后端用官方 `anthropic` SDK 直连 Claude API（`pip install anthropic`），按量付费，**需要自备 API key**：放进一个只有 key 一行的文件，用 `OWN_READER_ANTHROPIC_KEY_FILE` 指过去（默认 `~/.config/anthropic/api-key.txt`）。用它的功能：精读批注（默认后端，可用 `OWN_READER_GLOSS_BACKEND` 换成别的后端；失败时回落 `OWN_READER_FALLBACK_BACKEND`）、每章导读、全书导读、整本批量翻译。

- **额度周期上限**：每次调用按挂牌价把估算花费记进本地账本（`reader.sqlite` 的 `api_spend` 表），本额度周期累计到 `OWN_READER_API_MONTHLY_CAP`（默认 20 美元）就拒绝调用；整本批量任务、全书导读在提交前先估算，会超上限就不提交。周期起点是每月 `OWN_READER_CREDIT_DAY` 日 0 点（UTC），默认 `1`，即自然月。账本是本地估算，不是官方账单——请同时在 Anthropic Console 设好消费限额。
- **Message Batches**：整本预跑（精读批注、每章导读、整本翻译）走批量接口，价格五折，通常一小时内出结果（最长 24 小时）。批次与每个请求的业务数据存在 `batch_job` 表，服务器重启后自动续收；批次在途的段落，阅读页翻到那里不会再重复实时请求。
- **整本批量翻译**：「批」面板里「整本批量翻译」，用 Sonnet 把全书没翻过的段落一次提交成 Batch，结果写进同一份段落翻译缓存（实时翻译与双语 EPUB 共用）。

## 导读版 EPUB、安卓原生事件、手写批注

安卓 App 不在本仓库。下面只描述讀書在本仓里收下的事件和网页上的显示。

- **导读版 EPUB**：在已有的全书导读、本章导读、精读批注之上，生成一本给墨水屏离线读的 EPUB（卷首全书导读、章首本章导读、段下批注）。在仓库根目录运行：

  ```
  uv run --no-project --with markdown --with lxml python -I server/annotated_epub.py <calibre_id> [输出路径]
  ```

  不传路径时写到 `<数据目录>/annotated/<书号> 导读版 [or<书号>].epub`。文件名里的 `[or<书号>]` 给外部阅读器认书。`markdown` 不写入 `requirements.txt`，只在这条命令里用 `uv run --with` 带上。
- **安卓原生位置**：网页版用 EPUB CFI。原生阅读器另有两种位置，和 CFI 进度互不覆盖，都放在 `progress_xp`，进度里带 `device`。
  - `pos_kind=crengine`：EPUB 的 xpointer，字段是 `pos` / `pos_end`。评注和删除不带服务器事件号，按这两个位置挂回或删掉当时那条划线。
  - `pos_kind=pdf`：`pos` 形如 `pdfpage:页码`，划线另带 `quads`。评注和删除按 `pos` + `pos_end` + `quads` 对回；和 crengine 互不命中。
  网页版在对应节或 PDF 页按原文找到位置画出来，笔记列表可以跳过去。
- **手写批注**：`ink` 事件保存笔画，网页在对应段落下画出笔迹。识别引擎 `OWN_READER_INK_ENGINE`：
  - `off`（默认）：只保存和显示，不调用模型。
  - `openai`：`OWN_READER_INK_URL` 与 `OWN_READER_INK_MODEL`，向任意 OpenAI 兼容接口发 `chat/completions`，图片用 data URI，只用标准库。
  - `claude`：走现有 Claude API（`llm.api_call`），模型 `OWN_READER_INK_CLAUDE_MODEL`（默认 `claude-sonnet-5-5`），花费计入上面的额度周期。
- **跨站防护**：POST 只收 `Content-Type: application/json`，请求体必须是 JSON 对象，`Content-Length` 不得超过 50MB。带 `Origin` 时必须和 `Host` 同源。`Host` 只认 `127.0.0.1`、`localhost`、`[::1]` 和 `*.ts.net`。本服务不回答 CORS 预检，所以浏览器里的跨站 JSON 请求会被拦下。

## 地理批注

选中一段 → 底栏「地理」→ 程序画地形图（OpenTopoMap 等高线 / Esri 晕渲两种底图，Leaflet）+ 地名事实表 + 一段「形势」解说。原则：**精度可以一层层往后退，但不能编**。模型只做两件事——从原文抽地名（附年代和它认为的今地），以及最后写解说；事实表（今地、年代、来源）由程序从地名库直接生成，不经模型。每个地名标可信等级：

1. **地名库定位**：CHGIS V6（秦至清的府县治所，带「今地」考释）或 Pleiades（地中海古代地名）里有，且**事件年份落在该记录的起止年内**（同名不同地的侨置郡县不会张冠李戴）→ 实心点；该年代有府界时画虚线框。
2. **模型给出今地**：模型说「在今某县」，点位取 1990 年该县的中心（中国以外用 OpenStreetMap Nominatim 查）→ 空心虚线点，明示不是遗址位置。
3. **只有文字**：只有方位，或同名多处分不清 → 不上图。
4. **未能定位**。

解说只能用表里的地名和程序算出的距离，推断句首标【推断】；含 ② 点的距离标「粗略」。结果存成一条提问记录，点划线可重看地图。

**地名数据要自己下载，本仓库不包含任何 CHGIS / Pleiades 数据文件，也不包含建好的库。** CHGIS 的最终用户许可（EULA）只允许非商业的学术 / 教育用途，禁止再分发，使用时须引用 "CHGIS Version 6."（完整引用："CHGIS Version 6." (c) Fairbank Center for Chinese Studies and the Institute for Chinese Historical Geography at Fudan University, Dec 2016.，地理批注的事实表末尾会自动带上）。是否符合你的用途请自行判断。建库步骤：

1. 从 Harvard Dataverse 下载 CHGIS V6（https://dataverse.harvard.edu/dataverse/chgis_v6 ），需要府级多边形、府级治所点、县级治所点、1990 年县界四个图层，解压到 `<地名库目录>/raw/` 下的 `pref_pgn/`、`pref_pts/`、`cnty_pts/`、`citas90_cnty_pgn/`（具体文件名见 `server/geo_build.py` 文件头）。
2. 从 Pleiades 的数据下载页（`https://atlantides.org/downloads/pleiades/dumps/`）下载 `pleiades-places-latest.csv.gz` 和 `pleiades-names-latest.csv.gz`，改名为 `pleiades-places.csv.gz`、`pleiades-names.csv.gz` 放进 `raw/`（Pleiades 为 CC-BY）。
3. `.venv/bin/pip install pyshp pyproj && .venv/bin/python server/geo_build.py`，生成 `<地名库目录>/geo.sqlite`。

`<地名库目录>` 默认 `<数据目录>/geo`，可用 `OWN_READER_GEO_DIR` 改。没建库时只有地理批注不能用，会提示先建库。

## 依赖

- macOS（Mac App 与 launchd 部分；服务端本身是普通 Python，理论上别的系统也能跑，未测试）
- Python 3（作者在 3.14 下开发和测试，更低版本未验证）；服务端核心只用标准库，`lxml`（双语 EPUB）、`fonttools`（字体列表）、`pytest`（测试）见 `requirements.txt`
- Calibre 书库
- 至少一个 AI 后端：本机任意 OpenAI 兼容服务（llama.cpp / LM Studio / Ollama …），或 OpenRouter key，或已登录的 `claude` / `grok` / `kimi` / `agy` 官方 CLI
- 可选：Anthropic API key 与 `anthropic` SDK（精读批注、导读、整本批量翻译）；CHGIS V6 / Pleiades 数据与 `pyshp`、`pyproj`（只在建地理批注的地名库时用）
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
| `OWN_READER_ANTHROPIC_KEY_FILE` | `~/.config/anthropic/api-key.txt` | Claude API key 文件（`claude_api` 后端） |
| `OWN_READER_API_MONTHLY_CAP` | `20` | Claude API 额度周期花费上限（美元，本地估算） |
| `OWN_READER_CREDIT_DAY` | `1` | 额度周期起点（每月几号，1–28；1 = 自然月） |
| `OWN_READER_INK_ENGINE` | `off` | 手写识别：`off` / `openai` / `claude` |
| `OWN_READER_INK_URL` / `OWN_READER_INK_MODEL` | 空 | `openai` 引擎的兼容接口与模型 |
| `OWN_READER_INK_API_KEY` | 空 | 托管的兼容接口要密钥时填（Bearer），本机服务不用 |
| `OWN_READER_INK_CLAUDE_MODEL` | `claude-sonnet-5-5` | `claude` 引擎用的模型 |
| `OWN_READER_GLOSS_BACKEND` | `claude_api` | 精读批注用的后端 |
| `OWN_READER_GEO_DIR` | `<数据目录>/geo` | 地理批注的地名库目录 |
| `OWN_READER_TAXONOMY` | 内置通用类目 | 自定义类目表，格式见 `taxonomy.example.json` |
| `OWN_READER_WEREAD_KEY_FILE` | `~/.config/weread/api-key.txt` | 微信读书导入（可选） |
| `OWN_READER_HOMECINEMA_DB` / `OWN_READER_NEWS_API` | 空 | 荐书的辅助信号（可选） |

仓库里没有任何密钥。各后端用它自己 CLI 已登录的凭据，或你指定的 key 文件。

**哪些东西会离开本机**：阅读行为本身（打开、翻页、停留、划线、评注）只写本机 SQLite。但荐书、书库评分、中英文版配对会把阅读史、划线、想法和提问拼进 prompt，发给你配置的 AI 后端（默认 claude，即第三方服务器）；翻译会把书的正文发给配置的翻译后端；荐书结果会去微信读书书城、Open Library、国立国会図書館查书。**Claude API**（精读批注、每章导读、全书导读、整本批量翻译）会收到书的正文（全书导读是整本书）和你的读者画像；全书导读还会带上你读过、做过笔记的书名，并让 Anthropic 代为联网搜索。**地理批注**会把选段及前后文发给提问用的后端，今地名查询会发给 OpenStreetMap Nominatim，地图瓦片从 OpenTopoMap / Esri 加载。**手写识别**在 `OWN_READER_INK_ENGINE` 为 `openai` 或 `claude` 时，会把笔迹图和旁边的原文发给该接口；`off` 时不外发。只想完全本机，就把所有后端都配成 `local`，并且不用 Claude API 相关功能和地理批注。

`server/llm.py` 里的 `POLICY_DRAFT` 按数据敏感度限定后端：你的笔记、提问、阅读记录、读者画像默认**不会**发给 `free`（OpenRouter 免费档）和 `gemini`；书的正文（翻译用）可以发给任何后端。按自己的信任边界改这张表。

## 数据存在哪

- `<数据目录>/reader.sqlite`：事件库（唯一真值），以及派生表：翻译缓存、精读批注缓存（`gloss_*`）、全书导读（`book_dossier`）、章节切分（`chapter_seg`）、Batch 作业（`batch_job`）、Claude API 花费账本（`api_spend`）、书库分类评分、荐书、导入的外部笔记、模型调用审计（`llm_calls`，只存长度与元数据，不存 prompt 原文）。
- `<数据目录>/reader_profile.md`：精读批注与导读用的读者画像。
- `<数据目录>/geo/`：地理批注的原始数据与 `geo.sqlite`（自行下载生成，见「地理批注」）。
- `<数据目录>/booktext/`、`bilingual/`、`annotated/`、`import/`：全文段落缓存、双语 EPUB、导读版 EPUB、微信读书原始 JSON 缓存。
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
- 精读批注、导读、整本批量翻译只支持 EPUB，且依赖 Claude API（按量付费）；模型 ID 与价格表写在 `server/llm.py`，官方改价或换代时要手动更新。批注的逐字引文门槛只保证「引的是原文」，不保证解释正确。
- 地理批注：先秦没有开放的带坐标地名库，多数只能到 ②③；CHGIS 府界多边形覆盖稀疏；模型对同一地名的类别判断可能前后不一。地名数据需自行下载（许可限制，见上）。

## English (short)

own-reader is a single-user, self-hosted reading app: it serves your Calibre library to any browser on your own network, renders EPUB/PDF with foliate-js, and records every reading action (progress, highlights, notes, AI questions) in a local SQLite event log. Select a passage to ask an AI backend of your choice (a local OpenAI-compatible server, OpenRouter, or the official Claude / Grok / Kimi / Gemini CLIs); foreign-language books get on-demand interlinear translation. Optional Claude API features (bring your own key, with a local monthly spend cap; whole-book jobs use Message Batches): close-reading glosses whose quotes must match the source verbatim, per-chapter guides, and a whole-book dossier with web-sourced citations. A geography mode maps place names in a passage via the CHGIS V6 / Pleiades gazetteers; CHGIS data cannot be redistributed, so you download it yourself and run `server/geo_build.py`. It also generates book recommendations and scores your whole library against your own reading history. Configuration is by environment variables or `config.conf` (see `config.example.conf`); no keys are stored in the repo. The UI and prompts are in Chinese. Developed on the author's own Mac; some integrations need your own setup. License: GPL-3.0 (vendored libraries keep their own licenses).

## 许可证

GPL-3.0，见 [LICENSE](LICENSE)。`vendor/foliate-js` 为 MIT（见其目录下 LICENSE），`vendor/marked.min.js` 为 MIT（见文件头），`vendor/leaflet` 为 BSD-2-Clause（见其目录下 LICENSE）。地图瓦片来自 OpenStreetMap / OpenTopoMap（CC-BY-SA）与 Esri，按其各自条款使用。CHGIS、Pleiades 数据不在本仓库内，各按其许可。
