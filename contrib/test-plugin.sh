#!/usr/bin/env bash
# Smoke-test the zotero-mcp translate endpoint against publishers whose
# behaviour under a plain HTTP client is already known (measured 2026-09-03).
#
# The point is NOT "does it 200". It is the open question in the plugin's
# README: does Zotero's own Gecko fetch get past the WAFs that block a plain
# Python client? The BLOCKED rows below are the discriminator -- a plain
# client gets zero citation_* tags from them, Chrome gets a full page.
#
#   usage: contrib/test-plugin.sh
set -u

EP=http://127.0.0.1:23119/zotero-mcp/translate

hit () {
  local label="$1" expectation="$2" url="$3"
  printf '\n=== %-32s (plain HTTP client: %s)\n    %s\n' "$label" "$expectation" "$url"
  local body
  body=$(curl -s -m 90 -X POST "$EP" \
           -H 'Content-Type: application/json' \
           -d "{\"url\":\"$url\"}" 2>&1)
  if [ -z "$body" ]; then
    echo "    NO RESPONSE -- is Zotero running and the plugin installed?"
    return
  fi
  printf %s "$body" | python3 "$(dirname "$0")/_show_translate_result.py"
}

echo "Zotero connector server:"
curl -s -m 5 -o /dev/null -w '    ping HTTP %{http_code}\n' http://127.0.0.1:23119/connector/ping \
  || echo "    unreachable -- Zotero is not running"

# These four serve citation_* tags to a plain HTTP client, so a good result
# here proves the endpoint works but says nothing about the WAF question.
hit "SpringerLink"  "OK"      "https://link.springer.com/article/10.1006/bulm.1999.0141"
hit "PLOS ONE"      "OK"      "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0173664"

# These are the discriminators: a plain client gets nothing from them.
hit "Wiley"         "BLOCKED" "https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1461-0248.2005.00792.x"
hit "IEEE Xplore"   "BLOCKED" "https://ieeexplore.ieee.org/document/726791"
hit "ScienceDirect" "BLOCKED" "https://www.sciencedirect.com/science/article/pii/S0959652620311367"
hit "Oxford (OUP)"  "BLOCKED" "https://academic.oup.com/bioinformatics/article/30/15/2114/2390096"

cat <<'EOF'

------------------------------------------------------------------
How to read this:

  BLOCKED rows come back with a real translator, full author list and a
  PDF attachment
      -> Gecko beats the WAFs. The plugin is worth finishing: it gives
         connector-quality metadata AND the PDF, with no Node and no
         browser.

  BLOCKED rows come back NO_TRANSLATOR, or thin (few creators, no
  abstract, no attachment)
      -> Gecko is challenged too. The plugin only helps on publishers a
         plain HTTP client already handled, and the honest answer for the
         rest is to script a real browser.

Either way, compare the OK rows against what `zotero-cli add doi ...`
produces for the same DOIs -- that is the metadata-quality half of the
question, separate from the WAF half.
EOF
