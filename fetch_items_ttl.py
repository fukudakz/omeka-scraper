#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Omeka S の items API（JSON-LD）を読み、Turtle を書き出す。

対象:
    https://www.nihon-animation-eigashi.com/work-db/api/items

このエンドポイントはページ分割されている。rel="next" をたどって全件を取得する。
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

import certifi

API_URL = "https://www.nihon-animation-eigashi.com/work-db/api/items"
CONTEXT_URL = "https://www.nihon-animation-eigashi.com/work-db/api-context"
PER_PAGE = 100
OUT_PATH = Path(__file__).resolve().parent / "output" / "items.ttl"

USER_AGENT = "omeka-scraper/1.0 (+https://www.nihon-animation-eigashi.com/work-db/api/items)"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD = "http://www.w3.org/2001/XMLSchema#"

# API の管理用フィールド。RDF の述語にはしない。
SKIP_KEYS = {
    "@context",
    "@id",
    "@reverse",
    "thumbnail_display_urls",
    "o:id",
    "o:is_public",
}

LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


def fetch_json(url: str) -> tuple[object, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/ld+json, application/json",
            "User-Agent": USER_AGENT,
        },
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        body = response.read().decode("utf-8")
    return json.loads(body), headers


def link_by_rel(headers: dict[str, str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for match in LINK_RE.finditer(headers.get("link", "")):
        links[match.group(2)] = match.group(1)
    return links


def fetch_all_items() -> list[dict]:
    url = f"{API_URL}?per_page={PER_PAGE}&sort_by=id&sort_order=asc"
    items: list[dict] = []
    while url:
        payload, headers = fetch_json(url)
        if not isinstance(payload, list):
            raise SystemExit(f"items が配列ではありません: {url}")
        items.extend(payload)
        total = headers.get("omeka-s-total-results", "?")
        print(f"  {len(items)} / {total}  {url}", file=sys.stderr)
        url = link_by_rel(headers).get("next", "")
    return items


def load_prefixes() -> dict[str, str]:
    payload, _headers = fetch_json(CONTEXT_URL)
    context = payload.get("@context", {})
    prefixes = {
        prefix: iri
        for prefix, iri in context.items()
        if isinstance(iri, str)
    }
    prefixes.setdefault("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#")
    prefixes.setdefault("xsd", XSD)
    return prefixes


def expand(term: str, prefixes: dict[str, str]) -> str:
    if term.startswith("http://") or term.startswith("https://"):
        return term
    prefix, sep, local = term.partition(":")
    if sep and prefix in prefixes:
        return prefixes[prefix] + local
    raise KeyError(term)


def qname(iri: str, prefixes: dict[str, str]) -> str:
    for prefix, base in sorted(prefixes.items(), key=lambda item: -len(item[1])):
        if iri.startswith(base) and iri != base:
            local = iri[len(base) :]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", local):
                return f"{prefix}:{local}"
    return f"<{iri}>"


def turtle_literal(value: object, datatype: str | None, prefixes: dict[str, str]) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    literal = f'"{text}"'
    if datatype:
        literal += "^^" + qname(datatype, prefixes)
    return literal


def add_object(triples: set, subject: str, predicate: str, value) -> None:
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            add_object(triples, subject, predicate, item)
        return
    if isinstance(value, dict):
        value_type = str(value.get("type") or "")
        if "@value" in value and not value_type.startswith(("uri", "resource")):
            triples.add((subject, predicate, ("lit", str(value["@value"]), value.get("@type"))))
            return
        if "@id" in value:
            triples.add((subject, predicate, ("iri", value["@id"])))
        return
    if isinstance(value, bool):
        lexical = "true" if value else "false"
        triples.add((subject, predicate, ("lit", lexical, XSD + "boolean")))
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        datatype = XSD + ("integer" if isinstance(value, int) else "decimal")
        triples.add((subject, predicate, ("lit", str(value), datatype)))
        return
    if isinstance(value, str):
        triples.add((subject, predicate, ("lit", value, None)))


def item_triples(item: dict, prefixes: dict[str, str]) -> set[tuple]:
    subject = item.get("@id")
    if not isinstance(subject, str):
        return set()
    triples: set[tuple] = set()
    for rdf_type in item.get("@type") or []:
        if isinstance(rdf_type, str):
            triples.add((subject, RDF_TYPE, ("iri", expand(rdf_type, prefixes))))
    for key, value in item.items():
        if key in SKIP_KEYS or key.startswith("@"):
            continue
        if ":" not in key:
            continue
        add_object(triples, subject, expand(key, prefixes), value)
    reverse = item.get("@reverse") or {}
    if isinstance(reverse, dict):
        for key, value in reverse.items():
            if not isinstance(value, list):
                continue
            predicate = expand(key, prefixes)
            for node in value:
                if isinstance(node, dict) and isinstance(node.get("@id"), str):
                    triples.add((node["@id"], predicate, ("iri", subject)))
    return triples


def format_object(obj: tuple, prefixes: dict[str, str]) -> str:
    if obj[0] == "iri":
        return qname(obj[1], prefixes)
    return turtle_literal(obj[1], obj[2], prefixes)


def to_turtle(triples: set[tuple], prefixes: dict[str, str], item_count: int) -> str:
    by_subject: dict[str, dict[str, list]] = {}
    for subject, predicate, obj in triples:
        by_subject.setdefault(subject, {}).setdefault(predicate, []).append(obj)

    lines = [
        f"# Source: {API_URL}",
        f"# Items: {item_count}",
        f"# Triples: {len(triples)}",
        "",
    ]
    for prefix in sorted(prefixes, key=lambda name: (name != "rdf", name)):
        lines.append(f"@prefix {prefix}: <{prefixes[prefix]}> .")
    lines.append("")

    for subject in sorted(by_subject):
        lines.append(f"<{subject}>")
        predicates = by_subject[subject]
        ordered = sorted(predicates, key=lambda iri: (iri != RDF_TYPE, iri))
        for index, predicate in enumerate(ordered):
            objects = sorted(predicates[predicate], key=lambda obj: (obj[0], str(obj[1])))
            rendered = ", ".join(format_object(obj, prefixes) for obj in objects)
            punct = " ." if index == len(ordered) - 1 else " ;"
            lines.append(f"    {qname(predicate, prefixes)} {rendered}{punct}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    print("コンテキストを取得中...", file=sys.stderr)
    prefixes = load_prefixes()
    print("アイテムを取得中...", file=sys.stderr)
    items = fetch_all_items()
    triples: set[tuple] = set()
    for item in items:
        triples.update(item_triples(item, prefixes))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(to_turtle(triples, prefixes, len(items)), encoding="utf-8")
    print(f"完了: {len(items)} 件、{len(triples)} トリプル -> {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
