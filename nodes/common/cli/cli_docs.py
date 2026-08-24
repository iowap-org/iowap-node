"""CLI docs subcommand — read relay documentation (T-117 split).

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time
in the facade.
"""

from __future__ import annotations

import html as html_mod
import json
import re as _re
import sys

import httpx

from nodes.common.relay_client import RelayClient


def _html_to_text(html: str) -> str:
    """Best-effort conversion of an HTML document to terminal-friendly text.

    The docs endpoint serves rendered Markdown as HTML. On a headless node
    we want something readable in the terminal, so we strip tags, expand
    block elements to newlines, decode the common entities, and collapse
    excess blank lines. This is intentionally simple — it does not aim to
    reproduce a full browser.
    """
    # Drop <head>…</head> (style/title) entirely.
    html = _re.sub(r"<head\b.*?</head>", "", html, flags=_re.S | _re.I)
    # Drop <style>…</style> and <script>…</script>.
    html = _re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=_re.S | _re.I)
    # Block-level elements → surrounding newlines.
    block_tags = (
        "p", "br", "div", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "pre", "blockquote", "table", "tr",
    )
    html = _re.sub(
        rf"</?({ '|'.join(block_tags) })\b[^>]*>",
        "\n",
        html,
        flags=_re.I,
    )
    # <td>/<th> → tab separator, <hr> → rule.
    html = _re.sub(r"</?(td|th)\b[^>]*>", "\t", html, flags=_re.I)
    html = _re.sub(r"<hr\b[^>]*/?>", "\n----\n", html, flags=_re.I)
    # Code spans keep their text only.
    html = _re.sub(r"</?code\b[^>]*>", "", html, flags=_re.I)
    # Strip all remaining tags.
    html = _re.sub(r"<[^>]+>", "", html)
    # Decode HTML entities (&amp; &lt; …).
    html = html_mod.unescape(html)
    # Collapse runs of whitespace inside lines, keep newlines.
    html = _re.sub(r"[ \t]+", " ", html)
    html = _re.sub(r" *\n *", "\n", html)
    # Trim leading whitespace per line.
    html = "\n".join(line.rstrip() for line in html.splitlines())
    # Collapse 3+ blank lines to 2.
    html = _re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _cmd_docs(client: RelayClient, args) -> int:
    """node-cli docs [<name>] — read relay documentation from the server.

    Without an argument, lists all public documents (name + URL).
    With a name, fetches the document and prints it as readable text.
    """
    try:
        if args.name:
            resp = client._get_with_retry(f"/relay/v2/docs/{args.name}")
            if resp.status_code == 404:
                print(f"Document '{args.name}' not found.", file=sys.stderr)
                return 1
            resp.raise_for_status()
            body = resp.text
            ctype = resp.headers.get("content-type", "")
            if "html" in ctype.lower() or body.lstrip().lower().startswith("<!doctype"):
                print(_html_to_text(body))
            else:
                # Server returned JSON with content/markdown, or raw text.
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        text = data.get("content") or data.get("markdown")
                        if text:
                            print(text)
                            return 0
                    if args.json:
                        print(json.dumps(data, default=str))
                        return 0
                    print(json.dumps(data, indent=2, default=str))
                except (json.JSONDecodeError, ValueError):
                    print(body)
            return 0

        # List all docs.
        resp = client._get_with_retry("/relay/v2/docs")
        resp.raise_for_status()
        data = resp.json()
        docs = data if isinstance(data, list) else data.get("docs", [])
        if args.json:
            print(json.dumps(docs, default=str))
            return 0
        print(f"Relay documentation ({len(docs)} pages):\n")
        for doc in docs:
            name = doc.get("name") or doc.get("title", "?")
            url = doc.get("url", "")
            available = doc.get("available", True)
            marker = "📄" if available else "🚫"
            print(f"  {marker} {name}")
            if url:
                print(f"     {url}")
            print()
        return 0
    except httpx.HTTPStatusError as exc:
        print(
            f"docs request failed: {exc.response.status_code} {exc.response.text}",
            file=sys.stderr,
        )
        return 1