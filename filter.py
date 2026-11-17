"""Fetch RSS/Atom feeds, filter their items by regex, write the results to public/.

A feed that fails to fetch or parse falls back to the copy already published at
PAGES_URL, so a broken source never empties a subscription. Any failure makes
the script exit 1 after all feeds are written.
"""

import html
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import lxml.html
import trafilatura
import yaml
from lxml import etree

ROOT = Path(__file__).parent
OUT = ROOT / "public"
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")
USER_AGENT = "Mozilla/5.0 (compatible; feed-filter)"
ATOM = "http://www.w3.org/2005/Atom"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
FIELDS = {"title", "description", "category", "link"}
NAME_RE = re.compile(r"^[a-z0-9-]+$")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse(raw: bytes) -> etree._Element:

    # Recover mode tolerates common feed mistakes, such as whitespace before
    # the XML declaration. Entities and network access stay off.
    parser = etree.XMLParser(
        recover=True, resolve_entities=False, no_network=True, huge_tree=False
    )
    root = etree.fromstring(raw.lstrip(), parser)
    if root is None:
        raise ValueError("not parseable as XML")
    return root


def local(el: etree._Element) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def items(root: etree._Element) -> list[etree._Element]:
    found = [el for el in root.iter() if local(el) in ("item", "entry")]
    if not found and local(root) not in ("rss", "feed", "RDF"):
        raise ValueError(f"unknown feed type <{local(root)}>")
    return found


def children(item: etree._Element, *names: str) -> list[etree._Element]:
    return [el for el in item if local(el) in names]


def text(el: etree._Element) -> str:
    return "".join(el.itertext()).strip()


def field_values(item: etree._Element, field: str) -> list[str]:
    if field == "title":
        return [text(el) for el in children(item, "title")]
    if field == "description":
        return [text(el) for el in children(item, "description", "summary", "content", "encoded")]
    if field == "category":
        return [el.get("term") or text(el) for el in children(item, "category", "subject")]
    if field == "link":
        return [el.get("href") or text(el) for el in children(item, "link")]
    raise ValueError(f"unknown field {field!r}")


def filter_feed(root: etree._Element, spec: dict) -> tuple[list[etree._Element], int]:
    """Remove items that fail the rule, then all but the first `limit` (feed order)."""
    keep = "keep" in spec
    pattern = re.compile(spec["keep"] if keep else spec["drop"], re.IGNORECASE)
    field = spec.get("field", "title")
    limit = spec.get("limit")
    all_items = items(root)
    kept = []
    for item in all_items:
        hit = any(pattern.search(v) for v in field_values(item, field))
        if hit == keep and (limit is None or len(kept) < limit):
            kept.append(item)
        else:
            item.getparent().remove(item)
    return kept, len(all_items)


def item_link(item: etree._Element) -> str:
    links = field_values(item, "link")
    return links[0].strip() if links else ""


def body_element(item: etree._Element) -> etree._Element | None:
    for el in item:
        if local(el) == "encoded" or (local(el) == "content" and etree.QName(el).namespace == ATOM):
            return el
    return None


def published_bodies(name: str) -> dict[str, str]:
    """Full text already in the published feed, keyed by article link."""
    if not PAGES_URL:
        return {}
    try:
        root = parse(fetch(f"{PAGES_URL}/{name}.xml"))
    except Exception:
        return {}
    bodies = {}
    for item in items(root):
        el = body_element(item)
        if el is not None and item_link(item):
            bodies[item_link(item)] = el.text or ""
    return bodies


def extract(url: str) -> str | None:
    page = fetch(url).decode("utf-8", errors="replace")
    body = trafilatura.extract(
        page,
        url=url,
        output_format="html",
        include_images=True,
        include_links=True,
        include_formatting=True,
        favor_recall=True,
    )
    if not body:
        return None
    doc = lxml.html.document_fromstring(body)
    doc.make_links_absolute(url, resolve_base_href=False)

    # The feed item already shows the title.
    for h1 in doc.findall(".//h1")[:1]:
        h1.drop_tree()
    root = doc.find("body") if doc.find("body") is not None else doc
    return (root.text or "") + "".join(
        lxml.html.tostring(child, encoding="unicode") for child in root
    )


def add_fulltext(kept: list[etree._Element], name: str) -> str:
    """Put each article's text into its item. Items that fail keep their summary."""
    cache = published_bodies(name)
    fetched = reused = failed = 0
    for item in kept:
        link = item_link(item)
        if not link or body_element(item) is not None:
            continue
        body = cache.get(link)
        if body:
            reused += 1
        else:
            try:
                body = extract(link)
            except Exception:
                body = None
            time.sleep(1)
            if not body:
                failed += 1
                continue
            fetched += 1
        if local(item) == "entry":
            el = etree.SubElement(item, f"{{{ATOM}}}content", type="html")
            el.text = body
        else:
            el = etree.SubElement(item, f"{{{CONTENT}}}encoded", nsmap={"content": CONTENT})
            el.text = etree.CDATA(body)
    return f"full text: {fetched} fetched, {reused} reused, {failed} failed"


def validate(spec: dict) -> None:
    name = spec.get("name", "")
    if not NAME_RE.match(name):
        raise ValueError(f"bad name {name!r}: use a-z, 0-9 and hyphens")
    if not spec.get("url"):
        raise ValueError(f"{name}: missing url")
    if ("keep" in spec) == ("drop" in spec):
        raise ValueError(f"{name}: set exactly one of keep or drop")
    if spec.get("field", "title") not in FIELDS:
        raise ValueError(f"{name}: field must be one of {sorted(FIELDS)}")
    limit = spec.get("limit")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError(f"{name}: limit must be a positive whole number")
    if type(spec.get("fulltext", False)) is not bool:
        raise ValueError(f"{name}: fulltext must be true or false")


def build(spec: dict) -> str:
    root = parse(fetch(spec["url"]))
    kept, total = filter_feed(root, spec)
    report = f"kept {len(kept)} of {total}"
    if spec.get("fulltext"):
        report += "; " + add_fulltext(kept, spec["name"])
    data = etree.tostring(root, xml_declaration=True, encoding="utf-8")
    (OUT / f"{spec['name']}.xml").write_bytes(data)
    return report


def fallback(spec: dict) -> str:
    if not PAGES_URL:
        return "no PAGES_URL, nothing published"
    try:
        data = fetch(f"{PAGES_URL}/{spec['name']}.xml")
        parse(data)
    except Exception as exc:
        return f"no published copy ({exc})"
    (OUT / f"{spec['name']}.xml").write_bytes(data)
    return "kept last published copy"


def feed_url(name: str) -> str:
    return f"{PAGES_URL}/{name}.xml" if PAGES_URL else f"{name}.xml"


def write_index(specs: list[dict]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows, outlines = [], []
    for s in specs:
        if not (OUT / f"{s['name']}.xml").exists():
            continue
        title = html.escape(s.get("title", s["name"]))
        url = html.escape(feed_url(s["name"]))
        src = html.escape(s["url"])
        rule = html.escape(f"{'keep' if 'keep' in s else 'drop'} {s.get('field', 'title')} ~ {s.get('keep') or s.get('drop')}")
        rows.append(f'<li><a href="{url}">{title}</a><br><code>{rule}</code> · source <a href="{src}">{src}</a></li>')
        outlines.append(f'<outline type="rss" text="{title}" title="{title}" xmlUrl="{url}" htmlUrl="{src}"/>')

    (OUT / "index.html").write_text(
        f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Filtered feeds</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}}li{{margin-bottom:1rem}}code{{font-size:.85em}}</style>
<h1>Filtered feeds</h1>
<ul>
{chr(10).join(rows)}
</ul>
<p><a href="feeds.opml">OPML</a> · built {now}</p>
""",
        encoding="utf-8",
    )
    (OUT / "feeds.opml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<opml version="2.0">
<head><title>Filtered feeds</title></head>
<body>
{chr(10).join(outlines)}
</body>
</opml>
""",
        encoding="utf-8",
    )


def main() -> int:
    specs = yaml.safe_load((ROOT / "feeds.yaml").read_text(encoding="utf-8")) or []
    for spec in specs:
        validate(spec)
    names = [s["name"] for s in specs]
    if len(names) != len(set(names)):
        raise ValueError("feed names must be unique")

    OUT.mkdir(exist_ok=True)
    failed = 0
    for spec in specs:
        try:
            print(f"ok    {spec['name']}: {build(spec)}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {spec['name']}: {exc}; {fallback(spec)}", file=sys.stderr)
    write_index(specs)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
