# zotero-mcp translate endpoint (prototype)

**Status: prototype. Not wired into `zotero-mcp`, not proposed upstream.**
It existed to answer one question, whether Zotero's own fetch gets past the
publisher firewalls. The answer, measured, is at the bottom of this file: it
does for four of the six publishers tried, and two still block it.

## Why

Adding a paper by DOI goes through CrossRef, and the result is routinely worse
than the same paper saved from the browser: shouted names, missing authors, no
abstract, no PDF. The reason is that the browser runs a *site-specific
translator* — one of ~750 shipped with Zotero — against the publisher's page,
while a DOI lookup gets whatever the publisher deposited years ago.

That path cannot be invoked from outside Zotero. From the shipped client
(9.0.6, `xpcom/server/server_connector.js`):

- 19 `/connector/*` endpoints, none of which translates a page for you.
  `/connector/detect` returns translator **source code** for the caller to run.
- `/connector/saveItems` uses `ATTACHMENT_MODE_IGNORE` (`saveSession.js`), so
  the client will not download attachment URLs you hand it. In a real connector
  save the *browser* fetches the PDF and POSTs the bytes back.

So translation needs a JavaScript runtime. The usual answer is
[`zotero/translation-server`](https://github.com/zotero/translation-server),
which has two problems here: it is a separate Node service to install and keep
alive, and its `/web` output goes through `Zotero.Utilities.Item.itemToAPIJSON`,
which **explicitly skips the `attachments` field** — so it can never tell you
where the PDF is, whatever else it gets right.

Meanwhile the running Zotero already has every piece: the bundled translators,
the translate framework, `Zotero.HTTP.processDocuments` for fetching, and
`ATTACHMENT_MODE_DOWNLOAD` with its own open-access PDF cascade. They just
aren't reachable over HTTP. Better BibTeX shows a plugin may serve on 23119.

## What it does

`POST http://127.0.0.1:23119/zotero-mcp/translate`

```json
{"url": "https://link.springer.com/article/10.1006/bulm.1999.0141"}
```

Translate mode (the default) returns the translator's own item objects,
including their `attachments` array — the thing translation-server drops:

```json
{
  "url": "...",
  "translator": "Springer Link",
  "saved": false,
  "items": [{"itemType": "journalArticle", "title": "...",
             "creators": [...], "attachments": [{"url": "...", "mimeType": "application/pdf"}]}]
}
```

`{"save": true}` instead saves through the client with
`saveAttachments: true`, which is the mode that actually downloads the PDF.
Optional `"collection": "C12"` (a `treeViewID`) overrides the collection
selected in the Zotero UI, which is otherwise where a connector save lands.

### How the page is fetched

`"fetch"` picks how the page is fetched before the translator runs on it:

- `"browser"` (the default) loads it in Zotero's `HiddenBrowser`, which
  renders it with JavaScript, and runs the translator inside the rendered
  page through `RemoteTranslate`. This is how Zotero translates a feed item
  from its web page. The browser is attached to Zotero's main window, which
  is what got past Wiley's check (see below). If the main window is closed
  it falls back to Zotero's windowless frame, and `page.browser` in the
  response says which was used. `"browserOptions": {"useHiddenFrame": true}`
  forces the frame.
- `"xhr"` uses `Zotero.HTTP.processDocuments`, a plain request with no
  JavaScript, which is what the first version did.

A browser fetch keeps looking for up to `"settleMs"` (default 90000, at
most 180000) while the page is an HTTP error or a Cloudflare "Just a
moment..." page, reloading it every 15 s, since a challenge can clear in
the background without the page itself moving on. It never translates one: translators match on the URL alone, so
they would otherwise turn the challenge page into an item titled "Just a
moment...". A page still blocked at the deadline comes back as
`502 {"error": "BLOCKED", "page": {...}}`, with the HTTP status and title
of where it ended up.

## Installing

Zip the three files (not the directory containing them) into
`zotero-mcp-translate.xpi`, then Zotero → Tools → Add-ons → gear →
*Install Add-on From File*. Or symlink for development: create
`<profile>/extensions/zotero-mcp-translate@danmackinlay.name` containing the
absolute path to this directory.

```bash
cd contrib/zotero-mcp-translate && zip -r ../zotero-mcp-translate.xpi manifest.json bootstrap.js README.md
```

Zotero refuses a plugin whose manifest has no `applications.zotero.update_url`
("update_url not provided" in the Browser Console). It points at
`updates.json` here, which lists no updates, so update checks find nothing
rather than a 404.

## Security note

The endpoint is not `permitBookmarklet`, deliberately. It fetches an arbitrary
URL through the user's Zotero and in `save` mode writes to their library, so it
is for a local process the user started — not for any web page that can reach
127.0.0.1. Anything listening on loopback is reachable by every page in the
user's browser, so this is the difference between a tool and a vulnerability.

## The open question, answered

**Does Zotero's own fetch get past the publisher firewalls that block plain
HTTP clients?** For four of the six publishers tried, yes. Measured on
Zotero 10.0.6 with `contrib/test-plugin.sh` and new articles from each
publisher, 2026-10-24:

| Publisher | Plain HTTP client | `"xhr"` | `"browser"` (main window) |
|---|---|---|---|
| SpringerLink | OK | full record + PDF | full record + PDF, ~2 s |
| PLOS ONE | OK | full record + PDF | full record + PDF, ~1 s |
| IEEE Xplore | blocked | null document, worked 1 run in 3 | full record + PDF, 2–8 s, every run |
| Wiley | blocked | 403 | full record + PDF once a challenge clears (see below), then ~2 s |
| ScienceDirect | blocked | 403 | Cloudflare challenge, never cleared |
| Oxford (OUP) | blocked | 403 | Cloudflare challenge, never cleared |
| Cambridge Core (book) | | book record with ISBN and publisher | the same, ~33 s |
| UNC Press (book) | blocked | Embedded Metadata: a `blogPost`, no authors | the same |
| Oxford Academic (book) | | 403 | Cloudflare challenge, never cleared |

"Full record" means every author, with accents intact, plus the abstract.
The three book sites are the ones in this issue's original report. Zotero
has no UNC Press translator, so the connector would fall back to Embedded
Metadata there too: that record is the page's own metadata.

Wiley sits behind a Cloudflare challenge that clears after some time on
it: about a minute in one run, more than 90 s (the default `settleMs`) in
another, after which the next try passed in 18 s. The challenge page never
moves on to the article by itself in a hidden browser; only a later load
gets through, which is why a blocked page is reloaded every 15 s. Once cleared, later loads of other Wiley articles
pass in about 2 s for a while. Even the `"xhr"` fetch then gets the article
page, though Wiley still refuses its follow-up citation request. The
clearance did not survive a Zotero restart. Zotero's windowless frame
(`"useHiddenFrame": true`) never cleared it: two new articles stayed
blocked through 150 s and 10 reloads each, which is why the main window is
the default.

ScienceDirect and OUP stayed on the challenge through 145 s of reloads in
the main window. They need a real browser.

So the plugin gets connector-quality metadata and a PDF link from four of
the six, with no Node and no visible browser, two more than a DOI lookup or
a plain HTTP client. The cost is latency: a request to a publisher that
stays blocked takes the full `settleMs` (90 s by default) to fail.

The `"xhr"` column first measured the same way on 2026-10-03 (Zotero
10.0.5), apart from the IEEE run that worked.
