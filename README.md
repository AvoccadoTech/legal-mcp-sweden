# legal-mcp-sweden

[MCP](https://modelcontextprotocol.io) servers over Swedish public legal data.

Two servers, each answering one question rather than being a database:

| Server | Question | Source |
|---|---|---|
| **`lifos`** | Has a legal position our knowledge base depends on been superseded? | Migrationsverket's rättsliga ställningstaganden |
| **`rattspraxis`** | Has anything been decided that touches what we are already working on? | Domstolsverket's published case law |

Both run on your machine and keep their state in files you own — no account, no API key, no service in the middle.

That is deliberate. Which legal positions a firm watches, and which decisions it has asked to be told about, is a map of the firm's matters. It is not ours to hold.

**Neither replaces a legal database.** JUNO holds over two million judgments plus the commentaries, and already offers saved-search and paragraph-level monitoring across all of it. The case-law mirror here holds around seventeen thousand records and no commentary.

What these do that a subscription does not is narrower: a watch carries your own matter reference, the result is tool output an agent can act on rather than an email to a person, and the data sits where your documents sit — so a decision can be joined to the position your firm has already taken. Keep the subscription.

---
# Install

```bash
git clone https://github.com/AvoccadoTech/legal-mcp-sweden.git
cd legal-mcp-sweden
pip install -e .
```

Requires Python 3.11 or newer.

```bash
pip install -e ".[dev]"
python -m pytest tests -q -m "not live"   # offline
python -m pytest tests -q                 # includes live calls to both APIs
```

The live tests need no credentials — both sources are open — so they run by default and are marked rather than skipped. Two of them are early warning rather than verification: `test_live_legal_feed_carries_versioned_identifiers` fails when Lifos moves its portlet URL, and `test_live_ordering_is_newest_first` fails if Domstolsverket ever stops serving newest-first, which is the assumption the daily delta sync rests on.

---

# Lifos — legal-position currency
## The idea

Lifos item titles carry the document's identifier **and its version number**:

```
Rättsligt ställningstagande. Uppehållstillstånd för besök - RS/001/2024 (version 3.0)
```

That was published on 18 August 2026. If a firm's knowledge base says *"grundas på RS/001/2024 (version 2.0)"*, that entry is now out of date — and the firm has no way of knowing unless somebody happened to be watching the feed that week.

So the server scans the knowledge base for `RS/…` and `RK/…` references, notes the version each one pins, polls the feed, and names the files that have fallen behind.

```
lifos_scan_kb()            → registers RS/001/2024, knowledge base cites v2.0
lifos_check_updates()      → RS/001/2024 republished at v3.0 on 2026-08-18
lifos_stale_positions()    → mig_uppehallstillstand-besok.md cites 2.0, current is 3.0
lifos_get_document(url)    → read what actually changed
```

A lawyer decides whether the change matters. The tool never does — it reports that the question has been reopened, not what the answer is.

---
## What Lifos is

Migrationsverket's database of country-of-origin information and legal guidance. Two things are published there that matter to a firm doing migration work:

- **Rättsliga ställningstaganden** (`RS/…`) — the agency's stated legal position on a question, revised by version.
- **Rättsliga kommentarer** (`RK/…`) — commentary on a judgment, typically from Migrationsöverdomstolen or the CJEU.

Neither is law. Both are what the decision-maker on the other side of the case is working from, which is why a firm's own positions tend to cite them.

**There is no JSON API.** There is an RSS feed, and for a currency check it is better than an API would have been: the version number is the entire signal, and it arrives without having to diff any text.

---
## Before you build your own: the window

**Lifos serves a five-item rolling window. There is no archive endpoint and no backfill.**

Publication runs at roughly one to two items a month, so five items is usually a comfortable buffer — but it is a buffer, not a record. Poll daily. A weekly cron will *look* like it is working and will silently lose anything that scrolled off, with no way to notice afterwards and nothing to recover it from.

Two consequences are built into this server rather than left to the operator:

- It reports a document as superseded only once it has **observed the newer version itself**. A fresh ledger has no baseline, and the output says so instead of reporting "nothing has changed."
- A knowledge-base citation that names no version is recorded as **unpinned** and is never flagged. Treating it as "probably the current one" would make every future check quietly meaningless.

An unparseable version never reads as newer, either. A false *your knowledge base is stale* costs a lawyer more than a missed one does: the missed one surfaces at the next review, the false one teaches the firm to ignore every alert after it. There is a parametrised regression test in `tests/test_lifos.py` guarding that.

### The fragile part

Lifos runs on Sitevision. The landinformation feed has a clean alias at `/rss`; the rättsavdelningen feed — the one that matters — is reachable only through its portlet URL. If it starts returning 404, re-read the feed links on <https://lifos.migrationsverket.se/> and update `FEED_URLS` in `client.py`. The live test `test_live_legal_feed_carries_versioned_identifiers` fails loudly when this happens.

---
## Run Lifos

The server speaks MCP over stdio. Register it with any MCP-capable client:

```json
{
  "mcpServers": {
    "sweden-lifos": {
      "command": "python",
      "args": ["-m", "sweden_legal_mcp.lifos"],
      "env": {
        "LIFOS_STATE_DIR": "/path/to/knowledge-base/.lifos",
        "LIFOS_KB_PATH": "/path/to/knowledge-base"
      }
    }
  }
}
```

| Variable | Does |
|---|---|
| `LIFOS_STATE_DIR` | Where the ledger is written. Defaults to `./.lifos`. |
| `LIFOS_KB_PATH` | Default knowledge-base root for `lifos_scan_kb`. Optional. |

**Point both at the firm's own storage.** Beside the knowledge base is the natural place, so the two travel together and neither depends on us.

---
## Lifos tools

| Tool | What it does |
|---|---|
| `lifos_check_updates` | Poll and report new documents and new versions. The daily job. |
| `lifos_recent` | Read the feed without touching the ledger |
| `lifos_scan_kb` | Find every `RS/…` / `RK/…` reference in the knowledge base and register it, with the version each citation pins |
| `lifos_stale_positions` | The finding: entries citing a superseded version |
| `lifos_track` / `lifos_untrack` | Watch a document by hand, for entries that name it in prose |
| `lifos_list_tracked` | What is watched, and what each entry cites |
| `lifos_get_document` | Fetch a Lifos page as text, so the change can be read |
| `lifos_list_feeds` | The two feeds and what each carries |

---
## The ledger is a client list

`lifos-state.json` records which ställningstaganden the firm watches and which of its files depend on them. Read the wrong way, that is a list of what the firm is working on.

This is why the server is stdio and not hosted, why `.gitignore` excludes the state file, and why `httpx`'s request logging is turned down — those log lines carry which position was being read. Nothing in this package sends the ledger anywhere.

The file is plain, indented JSON with Swedish characters left readable, because the people it describes should be able to open it.

---
# Rättspraxis — case-law monitor
## Why it mirrors instead of querying

The API is open and deliberately plain:

```
GET /api/v1/publiceringar?page=N     10 records, newest first
GET /api/v1/domstolar                the court list
```

**`page` is the only parameter it honours.** Court codes, date ranges, subject filters and a JSON body on `/publiceringar/sok` are ignored or answered with 405 — verified 2026-08-30 by sending each one and comparing the result against an unfiltered call. So there is nothing to proxy: any question requires the whole corpus, which is why the server mirrors ~17,300 records into local SQLite and answers from there.

The constraint turns out to be the feature. The corpus ends up on the firm's own disk, and so does the watchlist.
## What the corpus actually contains

Two datasets share one endpoint, and they are shaped oppositely. Measured 2026-08-30 over 600 recent records and 970 sampled across the whole corpus:

| Field | Recent flow | Archive |
|---|---|---|
| `rattsomradeLista` | **78%** | 7% |
| `lagrumLista` | 31% | **87%** |
| `nyckelordLista` | 26% | **88%** |
| PDF attached | **68%** | 4% |
| `sammanfattning` | **100%** | **100%** |

**Do not key a watch on statute.** Only ~47% of recent decisions carry a statutory reference anywhere, counting both the structured field and references recovered from the summary text. The misses are not random: courts reason topically, and decisions about buller, riktvärden, täktverksamhet or strandskydd routinely name no section at all. Free text over the summary is the only key that covers everything.

By subject the live flow is narrow and worth knowing before pitching this anywhere: miljömål 27%, plan- och bygglagen 19%, fastighetsmål 9% — and brottmål 0.2%. It is a strong fit for environment, planning and property practices, and close to useless for criminal or family work.
## Run the monitor

```json
{
  "mcpServers": {
    "sweden-rattspraxis": {
      "command": "python",
      "args": ["-m", "sweden_legal_mcp.rattspraxis"],
      "env": { "RATTSPRAXIS_DB": "/path/to/firm/storage/.rattspraxis" }
    }
  }
}
```

Then sync once — about 1,700 requests, a few minutes — and delta daily:

```
rattspraxis_sync(mode="full")     once
rattspraxis_sync()                daily; stops at the first page holding nothing new
```
## Rättspraxis tools

| Tool | What it does |
|---|---|
| `rattspraxis_sync` | Build or refresh the mirror |
| `rattspraxis_add_watch` | Watch a query on behalf of a matter — the point of the server |
| `rattspraxis_check_watchlist` | The daily alert; reports each decision once per watch |
| `rattspraxis_list_watches` / `rattspraxis_remove_watch` | Manage watches |
| `rattspraxis_search` | Search the mirror |
| `rattspraxis_get` | One decision in full |
| `rattspraxis_coverage` | What the mirror holds, and how complete it is |
| `rattspraxis_courts` | Court codes for narrowing |
## Two honesty rules in the code

**Ranking never hides.** Hits are ordered so the most significant surfaces first, but relevance to the watch beats significance — a routine decision on the exact point a matter turns on outranks a `PREJUDIKAT` about something else. `PROVNINGSTILLSTAND` is called out rather than buried: leave to appeal means the question is heading upward, which for anyone with a project exposed to it is the most actionable thing in the feed.

**An empty result says what was not searched.** A partial mirror cannot tell *nothing matched* from *not fetched yet*, so every empty answer points at `rattspraxis_coverage`, and coverage states the record count against the total Domstolsverket reports. Absence here is never absence in Swedish law.

---
# Not a substitute for a lawyer

Both servers are change sensors. Neither reads the new material, says what changed, or knows whether any of it matters to a matter. A qualified lawyer decides that.

A higher version number means a document was republished. A watch hit means some words lined up. That is all either one means.

---
# Roadmap

Built already: `lifos` and `rattspraxis` above.

**Riksdagen** — `data.riksdagen.se`, JSON, no key. SFS and förarbeten. Straightforward, and the natural third: it is the layer both existing servers point at without being able to read.

**Bolagsverket** — the free bulk files at `vardefulla-datamangder.bolagsverket.se` carry grunddata including deregistration reason. The full company API, with styrelse and verklig huvudman, is 5,000 SEK to connect plus a monthly fee and a written agreement.

**Lantmäteriet NGP** — digital detaljplaner via API, CC BY 4.0, free. Coverage moved quickly during 2026, so check it yourself before planning around it: a Länsstyrelsen briefing dated 2026-04-16 reports 252 municipalities connected with 16,069 plans, while Lantmäteriet's own pages state no figure at all. One gap is structural rather than a matter of coverage — plans begun before 2022-01-01 need not be published digitally, so the historical layer stays permanently incomplete.

Known dead ends, so nobody spends a day rediscovering them:

- **Post- och Inrikes Tidningar** — konkurs, kallelse på okända borgenärer, skuldsanering. Sweden's official legal-notice organ, behind bot protection, no API and no bulk file. Only commercial scrapers.
- **Kronofogden** — no public API.
- **Jordbruksverket's centrala hästdatabas** — search interface only.

Sister servers follow the same shape as [legal-mcp-croatia](https://github.com/AvoccadoTech/legal-mcp-croatia): one repository per jurisdiction, tools named for the register they query.
# Licence

[Apache-2.0](LICENSE). Built by [Avoccado Tech](https://avoccado.io).

The code is free and always will be. If you would rather someone else kept it working when a government moves an endpoint — as the portlet URL above will eventually be moved — that is what we do for a living.
