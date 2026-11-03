#!/usr/bin/env python3
"""Check every internal link in the static site resolves to a file in the repo.

Run from anywhere (no dependencies, standard library only):

    python3 -I scripts/check-links.py            # summary + every broken reference
    python3 -I scripts/check-links.py --summary  # counts only
    python3 -I scripts/check-links.py --json     # machine-readable list

It scans every ``*.html`` file in the repo and checks the ``href``, ``src`` and
``srcset`` attributes of every element. A reference is *internal* when it is

* relative (``../about-cowello/``, ``img.png``) - resolved from the page's own
  directory, exactly as a browser does;
* root-relative (``/pricing/``) - resolved from the repo root;
* absolute to the site itself (``https://cowello.com/...``,
  ``https://www.cowello.com/...``), including ``<meta content="...">`` values
  such as ``og:url`` and every ``<loc>`` in ``sitemap.xml``.

Everything else (other hosts such as ``app.cowello.com``, ``mailto:``,
``tel:``, ``javascript:``, ``data:``, bare ``#fragment``) is ignored. An
``about:blank`` reference is reported as broken, since it never points at a
page, except an ``<iframe src="about:blank" data-lazy-src="...">``: that is
WP Rocket's lazy-load placeholder, replaced by the real URL at runtime.

A target resolves when the file exists, or when it is a directory that holds
an ``index.html`` (GitHub Pages serves ``foo/`` and redirects ``foo`` to
``foo/``). Query strings and fragments are ignored.

Exit status: 0 when nothing is broken, 1 when at least one reference is broken.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_HOSTS = {"cowello.com", "www.cowello.com"}
# A synthetic origin for resolving relative URLs; never fetched.
ORIGIN = "https://cowello.com"
SKIP_DIRS = {".git", "node_modules", "scripts"}
LINK_ATTRS = {"href", "src", "srcset"}
SITE_URL = re.compile(r"https?://(?:www\.)?cowello\.com(?:[/?#]|$)", re.I)


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[int, str, str, str]] = []  # line, tag, attr, value

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0]
        names = {n for n, _ in attrs}
        for name, value in attrs:
            if value is None:
                continue
            if name == "content" and SITE_URL.match(value.strip()):
                # <meta property="og:url" content="https://cowello.com/...">
                self.refs.append((line, tag, name, value.strip()))
                continue
            if name not in LINK_ATTRS:
                continue
            if (name == "src" and value.strip().lower() == "about:blank"
                    and "data-lazy-src" in names):
                # WP Rocket lazy-load placeholder: lazyload.min.js swaps in
                # data-lazy-src (checked below as an external URL), so this is
                # not a link to anything.
                continue
            if name == "srcset":
                for part in value.split(","):
                    url = part.strip().split(" ")[0]
                    if url:
                        self.refs.append((line, tag, name, url))
            else:
                self.refs.append((line, tag, name, value.strip()))

    handle_startendtag = handle_starttag


def html_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f.endswith(".html"):
                yield os.path.join(dirpath, f)


def page_url(root: str, path: str) -> str:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    return f"{ORIGIN}/{rel}"


def resolve(root: str, page: str, ref: str):
    """Return (is_internal, target_path_or_None, resolves: bool)."""
    if not ref or ref.startswith("#"):
        return False, None, True
    low = ref.lower()
    if low.startswith("about:"):
        return True, ref, False
    split = urlsplit(ref)
    if split.scheme and split.scheme not in ("http", "https"):
        return False, None, True  # mailto:, tel:, javascript:, data:, ...
    if split.netloc and split.netloc.lower() not in SITE_HOSTS:
        return False, None, True  # external host, including app.cowello.com
    absolute = urljoin(page_url(root, page), ref)
    path = unquote(urlsplit(absolute).path)
    target = os.path.normpath(os.path.join(root, path.lstrip("/")))
    if not target.startswith(root):
        return True, path, False
    if path.endswith("/") or os.path.isdir(target):
        ok = os.path.isfile(os.path.join(target, "index.html"))
    else:
        ok = os.path.isfile(target)
    return True, path, ok


def check(root: str):
    broken = []
    internal = 0
    pages = 0
    for page in html_files(root):
        pages += 1
        parser = _Collector()
        with open(page, encoding="utf-8", errors="replace") as fh:
            parser.feed(fh.read())
        for line, tag, attr, ref in parser.refs:
            is_internal, target, ok = resolve(root, page, ref)
            if not is_internal:
                continue
            internal += 1
            if not ok:
                broken.append({
                    "page": os.path.relpath(page, root),
                    "line": line,
                    "tag": tag,
                    "attr": attr,
                    "ref": ref,
                    "target": target,
                })
    sitemap = os.path.join(root, "sitemap.xml")
    if os.path.isfile(sitemap):
        with open(sitemap, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", text):
            line = text.count("\n", 0, m.start()) + 1
            is_internal, target, ok = resolve(root, sitemap, m.group(1))
            if not is_internal:
                continue
            internal += 1
            if not ok:
                broken.append({"page": "sitemap.xml", "line": line, "tag": "loc",
                               "attr": "", "ref": m.group(1), "target": target})
    return pages, internal, broken


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=REPO, help="site root (default: repo root)")
    ap.add_argument("--summary", action="store_true", help="print counts only")
    ap.add_argument("--json", action="store_true", help="print broken refs as JSON")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    pages, internal, broken = check(root)

    if args.json:
        json.dump({"pages": pages, "internal_refs": internal, "broken": broken}, sys.stdout, indent=1)
        print()
        return 1 if broken else 0

    by_target = defaultdict(list)
    for b in broken:
        by_target[b["target"]].append(b)
    print(f"Scanned {pages} pages, {internal} internal references.")
    print(f"Broken: {len(broken)} references to {len(by_target)} distinct targets "
          f"on {len({b['page'] for b in broken})} pages.")
    if not args.summary:
        for target, items in sorted(by_target.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            print(f"\n{target}  ({len(items)})")
            for b in items:
                print(f"  {b['page']}:{b['line']}  <{b['tag']} {b['attr']}=\"{b['ref']}\">")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
