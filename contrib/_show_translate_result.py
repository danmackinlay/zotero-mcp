"""Render one /zotero-mcp/translate response for test-plugin.sh.

Kept out of the shell script because the interesting fields -- creator count
and whether an attachment came back -- are exactly what a grep would get
wrong, and inline python inside a shell heredoc is where quoting goes to die.
"""

import json
import sys


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"    NOT JSON: {raw[:200]}")
        return

    if "error" in data:
        print(f"    {data['error']}: {str(data.get('message', ''))[:120]}")
        return

    print(f"    translator : {data.get('translator')}")
    items = data.get("items") or []
    if not items:
        print("    NO ITEMS")
        return

    item = items[0]
    creators = item.get("creators") or []
    names = [c.get("lastName") or c.get("name") for c in creators[:4]]
    attachments = item.get("attachments") or []
    pdfs = [
        a for a in attachments
        if "pdf" in str(a.get("mimeType", "")).lower()
        or str(a.get("title", "")).lower().endswith("pdf")
    ]

    print(f"    title      : {str(item.get('title'))[:70]}")
    print(f"    creators   : {len(creators)}  {names}")
    print(f"    abstract   : {bool(item.get('abstractNote'))}")
    print(f"    attachments: {len(attachments)} ({len(pdfs)} pdf)")
    for a in attachments[:3]:
        title = str(a.get("title"))[:40]
        print(f"        - {title!r} {a.get('mimeType')} {str(a.get('url') or '')[:60]}")


if __name__ == "__main__":
    main()
