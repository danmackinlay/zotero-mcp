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
 *    "collection": "C12"}             // optional treeViewID, save mode only
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
 * Fetch and parse one document, then run the best-matching web translator.
 *
 * Uses Zotero.HTTP.processDocuments rather than a raw fetch so the request
 * goes through the client's own network stack, with its cookie jar and its
 * TLS fingerprint. Whether that is enough to get past the publisher WAFs
 * that challenge plain HTTP clients is the open question this plugin exists
 * to answer; see README.md.
 */
async function translateUrl(url, { save, collection }) {
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

    const { library, collection: coll } = collection
      ? Zotero.Server.Connector.resolveTarget(collection)
      : Zotero.Server.Connector.getSaveTarget();

    if (!library || !library.editable) {
      throw new Error("save target is not an editable library");
    }

    const saved = await translate.translate({
      libraryID: library.libraryID,
      collections: coll ? [coll.id] : undefined,
      // The point of saving through the client rather than over the web
      // API: this is the mode that downloads the PDF, and falls back to
      // Zotero's own open-access resolvers when the translator's URL fails.
      saveAttachments: true,
    });

    return {
      translator: chosen.label,
      items: saved.map((item) => ({
        key: item.key,
        itemType: Zotero.ItemTypes.getName(item.itemTypeID),
        title: item.getDisplayTitle(),
        attachments: item.getAttachments().length,
      })),
    };
  });

  return results[0];
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

    try {
      const result = await translateUrl(url, {
        save: data.save === true,
        collection: typeof data.collection === "string" ? data.collection : null,
      });

      if (!result || !result.translator) {
        return [404, "application/json", JSON.stringify({
          error: "NO_TRANSLATOR",
          message: `no web translator matched ${url}`,
        })];
      }

      return [200, "application/json", JSON.stringify({
        url,
        translator: result.translator,
        saved: data.save === true,
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
