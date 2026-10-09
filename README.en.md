[中文](README.md) | English

# own-reader (讀書)

Read your own Calibre library in the browser: highlight, annotate, select a passage and ask an AI about it directly, with every reading action written to an event store on your own machine. The server runs on your own Mac; phones, tablets and e-ink devices open the same page in a browser. On the Mac there is also a WKWebView wrapper app, 「讀書」.

For design notes see [DESIGN.md](DESIGN.md).

## Why I built this

This is one app in a "personal operating system" series. The series has a single goal: to keep in my own hands the actions and the history of every exchange of information between me and the outside world — what I looked at, where I lingered and for how long, what I clicked, what I bought, what I sold, and what I did after seeing it.

An ordinary person's behavior is shaped by all kinds of known and unknown conditions, environments and stimuli, leaving them in a pseudo-random state — as if someone had installed switches on us: flip this switch, pluck that string, and we perform exactly the action someone else expected. To ourselves it feels random; to others we look like puppets on strings. Platforms and institutions hold our behavioral data and understand us better than we understand ourselves. This whole set of tools exists to change that.

The idea behind it: for an individual to understand the outside world is like a drop of water trying to understand the ocean — almost impossible. But a drop of water looking inward, seeing clearly how each of its own molecules moves, is achievable — how I move at a given temperature, how I move when I meet a given tide or current. Once that is clear, I can secure better conditions for my own survival. That is far more realistic than understanding the whole ocean.

The drop's own state needs to be recorded too. The same person, after a bad night's sleep, after a fight with family that day, or while feeling unwell, is noticeably more likely to get a major decision wrong; plenty of memoirs describe moments like that. So besides recording actions, we also record the body, emotions and external environment at the time (weather, markets, schedule), so that later it becomes visible "in what state, I do what."

The back ends of these apps are ultimately meant to be connected and to share information with one another. Large financial groups have long treated every customer this way: they look at the customer's behavior across deposits, loans, insurance and securities together, then cross-sell. With AI, there is no reason an individual cannot do the same thing to themselves — the difference being that this time, the data and the analysis serve only you. For now each app uses its own local SQLite database; connecting them is the next step.

The ideal end state: all information that can reach me must first pass through a filter and recorder I built myself before it gets in; my feedback and behavior must likewise pass through my own filter and protector before going out.

So the shared convention across this series is: all behavior records are written to a local database and never uploaded to any third party; AI analysis runs locally or on a service the user chooses.

## Features

- **Bookshelf**: reads Calibre's `metadata.db` directly (read-only); search by title/author, recently added; EPUB preferred, PDF otherwise.
- **Reading page** (rendered by foliate-js): tap left/right to turn pages, table of contents, highlights (multiple colors), notes, deletion; progress syncs across devices; fonts on the server are selectable; automatic switching between a "desktop" theme for emissive screens and a black-and-white theme for e-ink.
- **Ask about a selection**: select text and ask a question; the server splices into the prompt about 6,000 characters of original text before and after the selection plus related passages retrieved from the whole book via BM25; answers can be rated, and you can re-ask with a different model.
- **Parallel translation**: for foreign-language books, translation follows wherever you are reading, with the translation inserted below each original paragraph; a paragraph-level cache means nothing is ever retranslated. You can also export the whole book as a bilingual EPUB for other readers (`/api/books/<id>/bilingual`). Optional whole-book batch translation (Claude API Message Batches, see below).
- **Geographic annotation**: select a passage and tap "地理" (Geography) to get a terrain map + a place-name fact table + a commentary on the strategic situation (see below).
- **Reading the book "thick"** (requires the Claude API): paragraph-by-paragraph close-reading glosses, per-chapter guides, a whole-book guide (see below).
- **Reading / Read / Notes**: merges this app's reading events with (optionally) imported WeChat Read (微信读书) notes and progress.
- **Book recommendations**: based on your reading history, notes, questions and your feedback on past recommendations, the AI recommends 5–10 books per batch, and verifies that each book really exists against Calibre / the WeChat Read store / Open Library / the National Diet Library (国立国会図書館).
- **Library scoring**: classifies your entire Calibre library and gives three scores aimed only at you personally — "want to read / useful to me / ought to read"; per-book feedback on the shelf and actions such as finished / shelved flow back in, with incremental recalculation every night.
- **Mac App**: ⌘H bookshelf, ⌘[ back, ⌘R reload, ⌘E toggle e-ink mode, ⌘+/⌘-/⌘0 zoom (pages such as the bookshelf zoom as a whole page; the reading page stays fixed at 100% and adjusts the font size instead, because whole-page zoom makes foliate's columns shift by half a page); when the service doesn't respond, it first runs `launchctl kickstart` and then retries.

## Reading the book "thick": close-reading glosses, per-chapter guides, whole-book guide

All three layers are written by AI under the same principles: facts from outside the book must not be made up, and quotations from inside the book must be the original text. All of them require the Claude API (see the next section) and support EPUB only.

- **Close-reading glosses** (top bar "批"): based on a reader profile, it annotates only the places this particular reader is likely not to understand or to miss, in five categories — **background** (cultural customs, allusions, institutions, what a word meant at the time), **explanation** (terms, proper names, prerequisite knowledge missing from technical books or skipped derivations), **doubtful** (where the author may be wrong or the point is contested; it must state its basis and confidence, and is marked "model judgment"), **pun**, and **subtext** (sarcasm, apparent praise that is really criticism, words with hidden meaning). The original text quoted in a gloss must appear **verbatim** in that paragraph (only differences in whitespace and curly vs. straight quotes are tolerated), otherwise the whole gloss is discarded; when unsure, it doesn't gloss. Glosses are inserted below the paragraph without altering the original text nodes, so highlight positions are unaffected.
  - Toggled per book; it glosses wherever you are reading and prefetches the next section; paragraph-level cache, so a glossed paragraph is never glossed again. You can also "pre-run the whole book," which submits the entire book at once via Message Batches.
  - The reader profile is stored in `<data dir>/reader_profile.md`; an example is generated on first run — **please change it to your own in the "批" panel** (native language, foreign-language proficiency, professional background, fields you are and aren't familiar with).
  - **E-ink collapsed mode**: the e-ink theme and narrow screens by default collapse glosses into a small marker at the end of the paragraph (e.g. 〔批：背景 言外2〕); tap it to view in the bottom panel; in the panel you can switch between "collapsed / expanded below paragraph."
- **Per-chapter guides** (generated in the "导读" (Guide) panel): chapters are split according to the EPUB table of contents; when the TOC doesn't match, Sonnet picks chapter starts from "short lines that look like headings" (only line numbers among the candidates are accepted); each chapter is one request via Message Batches, writing "what this chapter is doing / key points / skipped steps and doubtful points / what's left for you"; once "批" is turned on, it is shown at the start of the chapter.
- **Whole-book guide** (top bar "导读"): the entire book (with chapter titles) is put into Opus's 1M context, with web search turned on to check facts from outside the book, and written in fixed sections: one-sentence summary, background and origins, what it is trying to do, structure and line of argument, what is distinctive, biases, blind spots and controversies, later reception, its relation to you, and what's left for you (questions the AI can't summarize for you and that you must judge by reading the original). Facts from outside the book are accepted only from web search results and come with source links; anything that can't be found is marked "unverified." Each book is generated once and cached; in the author's own test, an English book of 360,000 tokens cost about US$2.70 and took 6 minutes.

## Claude API and Message Batches

The `claude_api` backend connects directly to the Claude API using the official `anthropic` SDK (`pip install anthropic`), pay-as-you-go, and **you must supply your own API key**: put it in a file containing only the key on one line, and point `OWN_READER_ANTHROPIC_KEY_FILE` at it (default `~/.config/anthropic/api-key.txt`). Features that use it: close-reading glosses (the default backend; can be switched to another backend with `OWN_READER_GLOSS_BACKEND`; on failure it falls back to `OWN_READER_FALLBACK_BACKEND`), per-chapter guides, the whole-book guide, and whole-book batch translation.

- **Credit-period cap**: every call records its estimated cost at list price in a local ledger (the `api_spend` table in `reader.sqlite`); once the period's total reaches `OWN_READER_API_MONTHLY_CAP` (default US$20), calls are refused; whole-book batch jobs and the whole-book guide are estimated before submission and not submitted if they would exceed the cap. The period starts at 00:00 UTC on day `OWN_READER_CREDIT_DAY` of each month; the default is `1`, which is the calendar month. The ledger is a local estimate, not the official bill — please also set a spend limit in the Anthropic Console.
- **Message Batches**: whole-book pre-runs (close-reading glosses, per-chapter guides, whole-book translation) use the batch API at half price, usually with results within an hour (at most 24 hours). Batches and each request's business data are stored in the `batch_job` table, and collection resumes automatically after a server restart; for paragraphs whose batch is in flight, turning to them on the reading page will not trigger a duplicate real-time request.
- **Whole-book batch translation**: "整本批量翻译" (whole-book batch translation) in the "批" panel uses Sonnet to submit all of the book's not-yet-translated paragraphs as one Batch; results are written into the same paragraph translation cache (shared by real-time translation and the bilingual EPUB).

## Annotated EPUB, Android native events, handwriting

The Android app is not in this repository. own-reader only stores the events that app writes back, and shows them on the web reader.

- **Annotated EPUB**: on top of the whole-book guide, the per-chapter guides and the close-reading glosses, this builds an offline EPUB (guide at the front, chapter guide at each chapter start, glosses under paragraphs). From the repository root:

  ```
  uv run --no-project --with markdown --with lxml python -I server/annotated_epub.py <calibre_id> [output path]
  ```

  With no output path it writes `<data dir>/annotated/<id> 导读版 [or<id>].epub`. The `[or<id>]` marker lets an external reader recognize the book. `markdown` is not added to `requirements.txt`; the command above pulls it with `uv run --with`.
- **Android native positions**: the web reader uses EPUB CFI. The native reader uses two other position kinds. They do not overwrite CFI progress; both land in `progress_xp`, and progress carries `device`.
  - `pos_kind=crengine`: an EPUB xpointer in `pos` / `pos_end`. Notes and deletes do not carry a server event id; they attach to or remove the highlight with that same position.
  - `pos_kind=pdf`: `pos` looks like `pdfpage:<page>`, and a highlight also carries `quads`. Notes and deletes match `pos` + `pos_end` + `quads`, and never match a crengine highlight.
  The web reader finds the quoted text in the section or on the PDF page and draws it. The notes list can jump there.
- **Handwriting**: an `ink` event stores the strokes, and the web reader draws them under the matching paragraph. `OWN_READER_INK_ENGINE`:
  - `off` (default): store and display only; no model is called.
  - `openai`: `OWN_READER_INK_URL` and `OWN_READER_INK_MODEL`. own-reader POSTs `chat/completions` to any OpenAI-compatible endpoint, with the image as a data URI, using the standard library.
  - `claude`: the existing Claude API (`llm.api_call`), model `OWN_READER_INK_CLAUDE_MODEL` (default `claude-sonnet-5-5`). The cost counts toward the credit-period cap above.
- **Cross-site protection**: POST accepts only `Content-Type: application/json`, the body must be a JSON object, and `Content-Length` may not exceed 50MB. If `Origin` is present it must match `Host`. `Host` may only be `127.0.0.1`, `localhost`, `[::1]`, or a `*.ts.net` name. This server does not answer a CORS preflight, so a browser blocks cross-site JSON requests.

## Geographic annotation

Select a passage → bottom bar "地理" → the program draws a terrain map (two base maps, OpenTopoMap contours / Esri hillshade, with Leaflet) + a place-name fact table + a "situation" commentary. Principle: **precision may fall back level by level, but nothing may be made up**. The model does only two things — extracting place names from the original text (with the period and what it believes is the modern location), and writing the commentary at the end; the fact table (modern location, period, source) is generated by the program directly from the gazetteer, without going through the model. Each place name is marked with a confidence level:

1. **Located in the gazetteer**: present in CHGIS V6 (seats of prefectures and counties from the Qin through the Qing, with research notes on their "modern location") or Pleiades (ancient Mediterranean place names), and **the event year falls within that record's start and end years** (so displaced "émigré" commanderies and counties sharing a name with a different place won't be confused) → solid dot; if prefecture boundaries exist for that period, a dashed outline is drawn.
2. **Modern location given by the model**: the model says "in present-day such-and-such county"; the point is taken as the center of that county in 1990 (outside China, looked up with OpenStreetMap Nominatim) → hollow dashed dot, explicitly not the site's location.
3. **Text only**: only a direction is given, or multiple places with the same name can't be told apart → not put on the map.
4. **Could not be located**.

The commentary may only use place names from the table and distances computed by the program; inferred sentences start with 【推断】 (inference); distances involving a ② point are marked "rough." The result is saved as a question record, and tapping the highlight lets you view the map again.

**You must download the place-name data yourself; this repository contains no CHGIS / Pleiades data files, and no pre-built database.** The CHGIS end-user license agreement (EULA) permits only non-commercial academic / educational use, prohibits redistribution, and requires citing "CHGIS Version 6." when used (full citation: "CHGIS Version 6." (c) Fairbank Center for Chinese Studies and the Institute for Chinese Historical Geography at Fudan University, Dec 2016.; it is automatically appended to the end of the geographic annotation's fact table). Please judge for yourself whether your use complies. Steps to build the database:

1. Download CHGIS V6 from Harvard Dataverse (https://dataverse.harvard.edu/dataverse/chgis_v6 ); you need four layers — prefecture polygons, prefecture seat points, county seat points, and 1990 county boundaries — unzipped under `<gazetteer dir>/raw/` into `pref_pgn/`, `pref_pts/`, `cnty_pts/`, `citas90_cnty_pgn/` (see the header of `server/geo_build.py` for the exact file names).
2. From the Pleiades data download page (`https://atlantides.org/downloads/pleiades/dumps/`) download `pleiades-places-latest.csv.gz` and `pleiades-names-latest.csv.gz`, rename them to `pleiades-places.csv.gz` and `pleiades-names.csv.gz`, and put them in `raw/` (Pleiades is CC-BY).
3. `.venv/bin/pip install pyshp pyproj && .venv/bin/python server/geo_build.py`, which generates `<gazetteer dir>/geo.sqlite`.

`<gazetteer dir>` defaults to `<data dir>/geo` and can be changed with `OWN_READER_GEO_DIR`. Without the database, only geographic annotation is unavailable, and it will prompt you to build the database first.

## Dependencies

- macOS (for the Mac App and the launchd parts; the server itself is plain Python and in theory runs on other systems too, untested)
- Python 3 (the author develops and tests on 3.14; lower versions are unverified); the server core uses only the standard library; `lxml` (bilingual EPUB), `fonttools` (font list) and `pytest` (tests) are listed in `requirements.txt`
- A Calibre library
- At least one AI backend: any local OpenAI-compatible service (llama.cpp / LM Studio / Ollama …), or an OpenRouter key, or the logged-in official `claude` / `grok` / `kimi` / `agy` CLIs
- Optional: an Anthropic API key and the `anthropic` SDK (close-reading glosses, guides, whole-book batch translation); CHGIS V6 / Pleiades data and `pyshp`, `pyproj` (only used when building the gazetteer for geographic annotation)
- Building the Mac App: Xcode Command Line Tools (`swiftc`)
- PDF support: requires pdf.js (about 13MB, not checked in). Copy `vendor/pdfjs/` from upstream foliate-js commit 78914ae to `vendor/foliate-js/vendor/pdfjs/` in this repository; without it only EPUB can be read.

## Installation and running

```sh
git clone <this repo> own-reader && cd own-reader
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.conf config.conf      # edit as needed: at least check OWN_READER_CALIBRE and the AI backend
.venv/bin/python server/app.py          # open http://127.0.0.1:8460/
.venv/bin/python -m pytest -q           # tests (all offline, using temporary directories)
```

- Running persistently: replace the placeholders following the instructions in `launchd/com.example.ownreader.plist.example`, and put it in `~/Library/LaunchAgents/`.
- Access from other devices: the service binds only to 127.0.0.1 and has no login; please put in front of it your own channel that only you can reach (for example `tailscale serve --bg --https=8445 http://127.0.0.1:8460`). Do not expose it directly to the public internet.
- Mac App: `macapp/build.sh`; the output is at `build/讀書.app`, then `ditto build/讀書.app /Applications/讀書.app`. If you change the launchd label or the port, update `OwnReaderLaunchdLabel` / `OwnReaderURL` in `macapp/Info.plist` accordingly.
- Background jobs (optional; all can be run manually or via separate launchd jobs):
  - `server/import_history.py` imports WeChat Read notes (requires a key, see below)
  - `server/recommend.py generate` produces a batch of book recommendations
  - `server/library_class.py pilot | run` does the first round of library classification and scoring; `server/library_feedback.py refresh` does the nightly incremental update

## Configuration

Everything is via environment variables, or `config.conf` in the repository root (KEY=VALUE; already in .gitignore; `OWN_READER_CONFIG` can point elsewhere). For the full list and defaults see [config.example.conf](config.example.conf). Commonly used:

| Variable | Default | Description |
|---|---|---|
| `OWN_READER_CALIBRE` | `~/Calibre Library` | Calibre library directory |
| `OWN_READER_DATA` | `~/Library/Application Support/own-reader` | Data directory |
| `OWN_READER_DEFAULT_BACKEND` | `local` | Backend used for asking about a selection |
| `OWN_READER_FALLBACK_BACKEND` | empty | Backend used when the default backend fails |
| `OWN_READER_LOCAL_LLM_URL` | `http://127.0.0.1:8080/v1` | Local OpenAI-compatible service |
| `OWN_READER_RECS_BACKEND` / `OWN_READER_LIBRARY_BACKEND` | `claude` | Backend for book recommendations / library scoring |
| `OWN_READER_TRANSLATE_CHAIN` | `mt_free,mt_local` | Order of translation backends |
| `OWN_READER_ANTHROPIC_KEY_FILE` | `~/.config/anthropic/api-key.txt` | Claude API key file (`claude_api` backend) |
| `OWN_READER_API_MONTHLY_CAP` | `20` | Claude API spend cap per credit period (US dollars, local estimate) |
| `OWN_READER_CREDIT_DAY` | `1` | Day of month the credit period starts (1–28; 1 = calendar month) |
| `OWN_READER_INK_ENGINE` | `off` | Handwriting recognition: `off` / `openai` / `claude` |
| `OWN_READER_INK_URL` / `OWN_READER_INK_MODEL` | empty | Compatible endpoint and model for the `openai` engine |
| `OWN_READER_INK_API_KEY` | empty | Optional bearer key for a hosted `openai`-compatible endpoint |
| `OWN_READER_INK_CLAUDE_MODEL` | `claude-sonnet-5-5` | Model used by the `claude` engine |
| `OWN_READER_GLOSS_BACKEND` | `claude_api` | Backend for close-reading glosses |
| `OWN_READER_GEO_DIR` | `<data dir>/geo` | Gazetteer directory for geographic annotation |
| `OWN_READER_TAXONOMY` | built-in generic categories | Custom category table; see `taxonomy.example.json` for the format |
| `OWN_READER_WEREAD_KEY_FILE` | `~/.config/weread/api-key.txt` | WeChat Read import (optional) |
| `OWN_READER_HOMECINEMA_DB` / `OWN_READER_NEWS_API` | empty | Auxiliary signals for book recommendations (optional) |

There are no secrets in the repository. Each backend uses the credentials its own CLI is already logged in with, or the key file you specify.

**What leaves your machine**: reading behavior itself (opening, page turns, dwell time, highlights, notes) is written only to local SQLite. But book recommendations, library scoring, and Chinese/English edition matching splice your reading history, highlights, thoughts and questions into prompts and send them to the AI backend you configured (default claude, i.e. a third-party server); translation sends the book's text to the configured translation backend; recommendation results are looked up at the WeChat Read store, Open Library and the National Diet Library (国立国会図書館). The **Claude API** (close-reading glosses, per-chapter guides, whole-book guide, whole-book batch translation) receives the book's text (for the whole-book guide, the entire book) and your reader profile; the whole-book guide also includes the titles of books you have read and taken notes on, and has Anthropic perform web searches on your behalf. **Geographic annotation** sends the selected passage plus its surrounding context to the backend used for questions, sends modern place-name lookups to OpenStreetMap Nominatim, and loads map tiles from OpenTopoMap / Esri. **Handwriting recognition**, when `OWN_READER_INK_ENGINE` is `openai` or `claude`, sends the stroke image and the nearby original text to that endpoint; `off` sends nothing. If you want everything to stay fully local, configure all backends as `local`, and do not use the Claude API features or geographic annotation.

`POLICY_DRAFT` in `server/llm.py` restricts backends by data sensitivity: your notes, questions, reading records and reader profile are by default **not** sent to `free` (the OpenRouter free tier) or `gemini`; the book's text (for translation) may be sent to any backend. Adjust this table to your own trust boundaries.

## Where data is stored

- `<data dir>/reader.sqlite`: the event store (the single source of truth), plus derived tables: translation cache, close-reading gloss cache (`gloss_*`), whole-book guides (`book_dossier`), chapter segmentation (`chapter_seg`), Batch jobs (`batch_job`), the Claude API spend ledger (`api_spend`), library classification and scores, book recommendations, imported external notes, and a model-call audit log (`llm_calls`, which stores only lengths and metadata, not the prompt text).
- `<data dir>/reader_profile.md`: the reader profile used by close-reading glosses and guides.
- `<data dir>/geo/`: raw data for geographic annotation and `geo.sqlite` (downloaded and generated by you; see "Geographic annotation").
- `<data dir>/booktext/`, `bilingual/`, `annotated/`, `import/`: full-text paragraph cache, bilingual EPUBs, annotated EPUBs, raw WeChat Read JSON cache.
- `<data dir>/profile.md`, `needs.md`, `should.md`, `positions.md`, `rec_memo.md`: the "about you" texts read by book recommendations and scoring; templates are generated on first run — edit them freely.
- `<data dir>/fonts/`: ttf/otf files placed here appear in the reading page's font menu (`~/Library/Fonts` is also read).
- The Calibre library is read-only and is never written to.

## Limitations

- This was developed by the author on their own machine for their own use; in the public version, private integrations have been turned into configuration options or removed, and some features need your own configuration to work: AI backends, the WeChat Read key, translation models, auxiliary signals for book recommendations.
- Single user, no login, no access control; only suitable for a network that only you can access.
- WeChat Read import uses its agent gateway API, which may stop working at any time if the API changes.
- The prompts for book recommendations and library scoring are written in Chinese and aimed at Chinese readers.
- The `claude` / `grok` / `kimi` / `gemini` backends use your subscriptions by calling each vendor's official CLI in non-interactive mode; please confirm for yourself whether this complies with the rules of your region and your subscription terms.
- PDF requires you to add pdf.js yourself (see "Dependencies").
- Close-reading glosses, guides and whole-book batch translation support EPUB only and depend on the Claude API (pay-as-you-go); model IDs and the price table are written in `server/llm.py` and must be updated manually when official prices change or new model generations ship. The verbatim-quote threshold for glosses only guarantees that "what is quoted is the original text," not that the explanation is correct.
- Geographic annotation: there is no open gazetteer with coordinates for the pre-Qin period, so most places only reach levels ②③; CHGIS prefecture boundary polygons have sparse coverage; the model's judgment of the category of the same place name may be inconsistent. Place-name data must be downloaded yourself (license restrictions, see above).

## English (short)

own-reader is a single-user, self-hosted reading app: it serves your Calibre library to any browser on your own network, renders EPUB/PDF with foliate-js, and records every reading action (progress, highlights, notes, AI questions) in a local SQLite event log. Select a passage to ask an AI backend of your choice (a local OpenAI-compatible server, OpenRouter, or the official Claude / Grok / Kimi / Gemini CLIs); foreign-language books get on-demand interlinear translation. Optional Claude API features (bring your own key, with a local monthly spend cap; whole-book jobs use Message Batches): close-reading glosses whose quotes must match the source verbatim, per-chapter guides, and a whole-book dossier with web-sourced citations. A geography mode maps place names in a passage via the CHGIS V6 / Pleiades gazetteers; CHGIS data cannot be redistributed, so you download it yourself and run `server/geo_build.py`. It also generates book recommendations and scores your whole library against your own reading history. Configuration is by environment variables or `config.conf` (see `config.example.conf`); no keys are stored in the repo. The UI and prompts are in Chinese. Developed on the author's own Mac; some integrations need your own setup. License: GPL-3.0 (vendored libraries keep their own licenses).

## License

GPL-3.0, see [LICENSE](LICENSE). `vendor/foliate-js` is MIT (see the LICENSE in its directory), `vendor/marked.min.js` is MIT (see the file header), `vendor/leaflet` is BSD-2-Clause (see the LICENSE in its directory). Map tiles come from OpenStreetMap / OpenTopoMap (CC-BY-SA) and Esri, used under their respective terms. CHGIS and Pleiades data are not in this repository; each is subject to its own license.
