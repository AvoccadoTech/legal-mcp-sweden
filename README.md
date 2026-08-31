# legal-mcp-sweden

[MCP](https://modelcontextprotocol.io) servers over Swedish public legal data.

One server so far: **Lifos**, a currency watcher over Migrationsverket's *rättsliga ställningstaganden*. It answers one question — **has a legal position our knowledge base depends on been superseded?**

It runs on your machine, reads a public RSS feed, and keeps its notes in a JSON file you own. There is no account, no API key, and no service in the middle.

That is deliberate. Which legal positions a firm watches — and which of its own files depend on them — is a map of the firm's matters, and it is not ours to hold.

---

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

## Install

```bash
git clone https://github.com/AvoccadoTech/legal-mcp-sweden.git
cd legal-mcp-sweden
pip install -e .
```

Requires Python 3.11 or newer.

## Run

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

## Tools

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

## Not a substitute for a lawyer

This is a change sensor over a public feed. It does not read the new version, does not say what changed, and does not know whether any of it matters to a matter. A qualified lawyer decides that.

A higher version number means the document was republished. That is all it means.

---

## Roadmap

**Domstolsverket rättspraxis** — `rattspraxis.etjanst.domstol.se/api/v1/publiceringar`. Open, keyless, 17,329 records. Two things to know before starting, both measured on 2026-08-30:

- **`page` is the only parameter.** No search, no date filter, no court filter; `POST /publiceringar/sok` returns 405. A connector has to mirror the corpus and index locally — a sync job, not a proxy.
- **The live flow is narrow.** Of the newest 600 records (Nov 2025 – Aug 2026): Mark- och miljööverdomstolen 55.5%, Högsta domstolen 20.7%, Högsta förvaltningsdomstolen 17%. By subject, miljömål 27%, plan- och bygglagen 19%, fastighetsmål 9% — and brottmål 0.2%. Sampled across the whole corpus back to 1981 it broadens considerably (HD 29.5%, Regeringsrätten 11.3%, Arbetsdomstolen 10.7%), but that archive is the same referat material the commercial databases already sell. The monitorable part is environment, planning and property.

**Riksdagen** — `data.riksdagen.se`, JSON, no key. SFS and förarbeten. Straightforward.

**Bolagsverket** — the free bulk files at `vardefulla-datamangder.bolagsverket.se` carry grunddata including deregistration reason. The full company API, with styrelse and verklig huvudman, is 5,000 SEK to connect plus a monthly fee and a written agreement.

**Lantmäteriet NGP** — digital detaljplaner via API, CC BY 4.0, free. Coverage moved quickly during 2026, so check it yourself before planning around it: a Länsstyrelsen briefing dated 2026-04-16 reports 252 municipalities connected with 16,069 plans, while Lantmäteriet's own pages state no figure at all. One gap is structural rather than a matter of coverage — plans begun before 2022-01-01 need not be published digitally, so the historical layer stays permanently incomplete.

Known dead ends, so nobody spends a day rediscovering them:

- **Post- och Inrikes Tidningar** — konkurs, kallelse på okända borgenärer, skuldsanering. Sweden's official legal-notice organ, behind bot protection, no API and no bulk file. Only commercial scrapers.
- **Kronofogden** — no public API.
- **Jordbruksverket's centrala hästdatabas** — search interface only.

Sister servers follow the same shape as [legal-mcp-croatia](https://github.com/AvoccadoTech/legal-mcp-croatia): one repository per jurisdiction, tools named for the register they query.

## Licence

[Apache-2.0](LICENSE). Built by [Avoccado Tech](https://avoccado.io).

The code is free and always will be. If you would rather someone else kept it working when a government moves an endpoint — as the portlet URL above will eventually be moved — that is what we do for a living.
