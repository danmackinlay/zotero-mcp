/* eslint-env browser */
/* global Zotero, ChromeUtils */

/**
 * zotero-mcp translate endpoint.
 *
 * Zotero runs web translation client-side on purpose: the connector server
 * hands out translator *source* (/connector/detect) for the browser to
 * execute, and /connector/saveItems saves with ATTACHMENT_MODE_IGNORE, so
 * the client never downloads an attachment URL you give it. There is no
 * request an external process can make that gets Zotero to translate a URL.
 *
 * Everything needed is nevertheless already in the running client: the
 * bundled translators, the translate framework, Zotero.HTTP.processDocuments
 * for fetching, and Zotero.Translate.ItemSaver's ATTACHMENT_MODE_DOWNLOAD
 * with its own open-access PDF lookup. This plugin exposes them on the
 * connector server, which Better BibTeX already demonstrates plugins may do.
 *
 * Endpoint: POST http://127.0.0.1:23119/zotero-mcp/translate
 *
 *   {"url": "https://...",            // required
 *    "save": false,                   // default false: translate only
 *    "collection": "C12",             // optional treeViewID, save mode only
 *    "fetch": "browser",              // "browser" (default) or "xhr"
 *    "settleMs": 90000,               // browser only: how long to wait for
 *                                     // a challenge page to clear (max 180000)
 *    "browserOptions": {}}            // browser only: {"useHiddenFrame": true}
 *                                     // for Zotero's windowless frame, with
 *                                     // optional "allowImages": true
 *
 * Translate mode returns the translator's own item objects, INCLUDING their
 * `attachments` array. That is the one thing zotero/translation-server
 * cannot do: its /web pipes items through Zotero.Utilities.Item.itemToAPIJSON,
 * which explicitly skips the attachments field, so the PDF half of a
 * connector save is unreachable that way.
 *
 * Save mode hands them to ItemSaver in ATTACHMENT_MODE_DOWNLOAD, which is
 * what actually downloads the PDF -- the browser's job in a normal
 * connector save.
 */

const ENDPOINT = "/zotero-mcp/translate";

// Zotero 7 runs bootstrap in a sandbox without console; log through Zotero.
function log(msg) {
  Zotero.debug("[zotero-mcp-translate] " + msg);
}

/**
 * The library and collection a save goes to: the named treeViewID, else
 * whatever is selected in the Zotero UI, as a connector save would use.
 */
function saveTarget(collection) {
  const { library, collection: coll } = collection
    ? Zotero.Server.Connector.resolveTarget(collection)
    : Zotero.Server.Connector.getSaveTarget();
  if (!library || !library.editable) {
    throw new Error("save target is not an editable library");
  }
  return { libraryID: library.libraryID, collections: coll ? [coll.id] : undefined };
}

/**
 * fetch: "xhr". Fetch and parse one document, then run the best-matching web
 * translator.
 *
 * Uses Zotero.HTTP.processDocuments rather than a raw fetch so the request
 * goes through the client's own network stack, with its cookie jar and its
 * TLS fingerprint. That is a plain XHR, though, with no JavaScript run on the
 * page: measured 2026-10-03 and again 2026-10-24, Wiley, ScienceDirect and
 * OUP answer it 403 and IEEE returns a null document. See README.md.
 */
async function translateWithXhr(url, { save, collection }) {
  const results = await Zotero.HTTP.processDocuments([url], async (doc) => {
    const translate = new Zotero.Translate.Web();
    translate.setDocument(doc);

    const translators = await translate.getTranslators();
    if (!translators.length) {
      return { translator: null, items: [] };
    }

    // getTranslators returns them in priority order; the connector picks
    // the head of that list too. A lower-priority match is usually the
    // "Embedded Metadata" fallback, which this package can already do
    // for itself without a plugin.
    const chosen = translators[0];
    translate.setTranslator(chosen);

    if (!save) {
      // libraryID false makes the framework hand back the raw translator
      // items instead of saving them -- and it returns before ItemSaver
      // runs, which is why `attachments` survives.
      const items = await translate.translate({ libraryID: false });
      return { translator: chosen.label, items };
    }

    const saved = await translate.translate({
      ...saveTarget(collection),
      // The point of saving through the client rather than over the web
      // API: this is the mode that downloads the PDF, and falls back to
      // Zotero's own open-access resolvers when the translator's URL fails.
      saveAttachments: true,
    });

    return { translator: chosen.label, items: saved.map(savedSummary) };
  });

  return results[0];
}

function savedSummary(item) {
  return {
    key: item.key,
    itemType: Zotero.ItemTypes.getName(item.itemTypeID),
    title: item.getDisplayTitle(),
    attachments: item.getAttachments().length,
  };
}

// How long the browser path waits for a page to settle, by default and at
// most, and how often it reloads a page that is still blocked. Measured
// 2026-10-24: Wiley's Cloudflare check clears after about 60 s of
// challenge, but the challenge page itself never moves on to the article in
// a hidden browser; only the next load gets through. So a blocked page is
// reloaded every RELOAD_MS, and the default wait covers that minute.
// ScienceDirect and OUP never cleared, at 145 s of reloads.
const SETTLE_MS = 90000;
const SETTLE_MAX_MS = 180000;
const SETTLE_POLL_MS = 2000;
const RELOAD_MS = 15000;

// An error status, or a Cloudflare interstitial (which Wiley, ScienceDirect
// and OUP all serve with 403 and this title). Translators match on the URL
// alone, so detect() succeeds on a challenge page and translate() then turns
// the challenge itself into an item titled "Just a moment...". Such a page
// is never translated.
function isBlocked(page) {
  return (page.status !== null && page.status >= 400)
    || /^just a moment/i.test(page.title || "");
}

/**
 * fetch: "browser". Load the page in a HiddenBrowser, which renders it with
 * JavaScript, and run the translator inside it through RemoteTranslate.
 *
 * This is how Zotero itself translates a feed item from its web page
 * (Zotero.FeedItem.prototype.translate), so the translator sees the
 * rendered page, as it would in the connector, and not a parsed XHR body.
 * Save mode goes through RemoteTranslate's own ItemSaver in
 * ATTACHMENT_MODE_DOWNLOAD.
 *
 * Returns the HTTP status and page title of the document it ended on, so a
 * challenge page is reported as one rather than as a bare NO_TRANSLATOR.
 */
async function translateWithBrowser(url, { save, collection, settleMs, browserOptions }) {
  const { HiddenBrowser } = ChromeUtils.importESModule(
    "chrome://zotero/content/HiddenBrowser.mjs");
  const { RemoteTranslate } = ChromeUtils.importESModule(
    "chrome://zotero/content/RemoteTranslate.mjs");

  // A browser attached to the main window, not Zotero's default windowless
  // frame. Measured 2026-10-24: Wiley's Cloudflare check passed it on three
  // articles the plugin had never loaded and blocked the windowless frame on
  // the same articles; images on in the frame made no difference. Without a
  // main window (Zotero on macOS can run with it closed) HiddenBrowser
  // refuses main-window mode, so fall back to the frame and say so.
  const mainWindow = browserOptions.useHiddenFrame !== true && !!Zotero.getMainWindow();
  const browser = new HiddenBrowser(mainWindow
    ? { useHiddenFrame: false }
    : browserOptions.allowImages ? { docShell: { allowImages: true } } : {});
  const started = Date.now();
  const deadline = started + settleMs;
  let translate = null;
  let page = {};
  try {
    if (!(await browser.load(url))) {
      throw new Error("hidden browser failed to load " + url);
    }
    let loadedAt = Date.now();
    let loads = 1;

    for (;;) {
      await browser.waitForDocument({ allowInteractiveAfter: 5000 });
      const data = await browser.getPageData(["title", "channelInfo"]);
      page = {
        finalUrl: browser.currentURI.spec,
        status: data.channelInfo ? data.channelInfo.responseStatus : null,
        title: data.title,
        elapsedMs: Date.now() - started,
        browser: mainWindow ? "main-window" : "hidden-frame",
        loads,
      };

      if (!isBlocked(page)) {
        // A navigation replaces the window global the Translation actor
        // lives on, so each attempt needs a RemoteTranslate bound to it.
        if (translate) translate.dispose();
        translate = new RemoteTranslate({ disableErrorReporting: true });
        await translate.setBrowser(browser);
        const translators = await translate.detect();
        if (translators && translators.length) {
          try {
            // detect() leaves the whole priority-ordered list selected,
            // and translate() runs its head, as the connector does.
            const label = translators[0].label;
            if (!save) {
              const items = await translate.translate({ libraryID: false });
              return { translator: label, items: items || [], page };
            }
            const saved = await translate.translate(saveTarget(collection));
            return { translator: label, items: (saved || []).map(savedSummary), page };
          }
          catch (e) {
            // The page navigated under the translator (IEEE does this), so
            // its actor went with it: look again at wherever it went.
            if (!/destroyed before query/.test(String(e && e.message)) || Date.now() > deadline) {
              throw e;
            }
            log(`page navigated during translation of ${url}; retrying`);
          }
        }
      }

      if (Date.now() > deadline) {
        page.elapsedMs = Date.now() - started;
        return { translator: null, items: [], page, blocked: isBlocked(page) };
      }
      if (isBlocked(page) && Date.now() - loadedAt > RELOAD_MS) {
        await browser.load(url);
        loadedAt = Date.now();
        loads += 1;
        continue;
      }
      await Zotero.Promise.delay(SETTLE_POLL_MS);
    }
  }
  finally {
    if (translate) translate.dispose();
    browser.destroy();
  }
}

async function translateUrl(url, options) {
  return options.fetch === "browser"
    ? translateWithBrowser(url, options)
    : translateWithXhr(url, options);
}

const TranslateEndpoint = function () {};

TranslateEndpoint.prototype = {
  supportedMethods: ["POST"],
  supportedDataTypes: ["application/json"],
  // Deliberately NOT permitBookmarklet: this endpoint fetches an arbitrary
  // URL through the user's Zotero and can write to their library. It is for
  // a local process the user started, not for any page that can reach
  // 127.0.0.1.
  permitBookmarklet: false,

  init: async function (requestData) {
    const data = requestData.data || {};
    const url = data.url;

    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
      return [400, "application/json", JSON.stringify({
        error: "BAD_URL",
        message: "url must be an absolute http(s) URL",
      })];
    }

    const fetch = data.fetch === undefined ? "browser" : data.fetch;
    if (fetch !== "xhr" && fetch !== "browser") {
      return [400, "application/json", JSON.stringify({
        error: "BAD_FETCH",
        message: 'fetch must be "xhr" or "browser"',
      })];
    }

    const settleMs = Number.isFinite(data.settleMs)
      ? Math.min(Math.max(data.settleMs, 0), SETTLE_MAX_MS)
      : SETTLE_MS;

    try {
      const browserOptions = data.browserOptions && typeof data.browserOptions === "object"
        ? data.browserOptions : {};
      const result = await translateUrl(url, {
        fetch,
        settleMs,
        browserOptions,
        save: data.save === true,
        collection: typeof data.collection === "string" ? data.collection : null,
      });

      if (result && result.blocked) {
        return [502, "application/json", JSON.stringify({
          error: "BLOCKED",
          message: `the publisher served an error or challenge page for ${url}`,
          fetch,
          page: result.page,
        })];
      }

      if (!result || !result.translator) {
        return [404, "application/json", JSON.stringify({
          error: "NO_TRANSLATOR",
          message: `no web translator matched ${url}`,
          fetch,
          page: result && result.page,
        })];
      }

      return [200, "application/json", JSON.stringify({
        url,
        fetch,
        translator: result.translator,
        saved: data.save === true,
        page: result.page,
        items: result.items,
      })];
    } catch (e) {
      Zotero.logError(e);
      return [500, "application/json", JSON.stringify({
        error: "TRANSLATION_FAILED",
        message: String(e && e.message ? e.message : e),
      })];
    }
  },
};

function install() {}
function uninstall() {}

async function startup() {
  await Zotero.initializationPromise;
  Zotero.Server.Endpoints[ENDPOINT] = TranslateEndpoint;
  log("registered " + ENDPOINT);
}

function shutdown() {
  delete Zotero.Server.Endpoints[ENDPOINT];
  log("unregistered " + ENDPOINT);
}
