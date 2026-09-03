# zotero-mcp translate endpoint (prototype)

**Status: unproven prototype. Not wired into `zotero-mcp`, not proposed upstream.**
It exists to answer one question, stated at the bottom of this file.

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

## Installing

Zip the three files (not the directory containing them) into
`zotero-mcp-translate.xpi`, then Zotero → Tools → Add-ons → gear →
*Install Add-on From File*. Or symlink for development: create
`<profile>/extensions/zotero-mcp-translate@danmackinlay.name` containing the
absolute path to this directory.

```bash
cd contrib/zotero-mcp-translate && zip -r ../zotero-mcp-translate.xpi manifest.json bootstrap.js README.md
```

## Security note

The endpoint is not `permitBookmarklet`, deliberately. It fetches an arbitrary
URL through the user's Zotero and in `save` mode writes to their library, so it
is for a local process the user started — not for any web page that can reach
127.0.0.1. Anything listening on loopback is reachable by every page in the
user's browser, so this is the difference between a tool and a vulnerability.

## The open question

**Does Zotero's own fetch get past the publisher WAFs that block plain HTTP
clients?** This has not been tested and is the thing that decides whether the
plugin is worth finishing.

Measured against `link.springer.com` on 2026-09-03, a `Mozilla/`-prefixed
User-Agent got a ~3 KB challenge page while an honest `zotero-mcp/1.0` UA got
the full article. Chrome gets through everywhere. Where a plain Python client
lands is publisher-specific — of twelve publishers tested, four served their
`citation_*` tags to a plain HTTP client and eight did not.

`Zotero.HTTP.processDocuments` uses Gecko's network stack, which is a real
browser's, so it may well do better than `requests`. If it does, this plugin
gives connector-quality metadata *and* the PDF with no Node and no browser. If
it doesn't, it only helps on publishers that were never the problem, and the
honest answer is to script a real browser instead.

To find out, install it and try a blocked publisher:

```bash
curl -s -X POST http://127.0.0.1:23119/zotero-mcp/translate \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1461-0248.2005.00792.x"}'
```

Wiley served 0 citation meta tags to curl and 29 to Chrome, so it is a clean
discriminator. A `NO_TRANSLATOR` response, or a record thinner than the browser
produces, means Gecko was challenged too.
