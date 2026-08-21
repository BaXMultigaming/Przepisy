#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RECIPES = DATA / "recipes"
IMAGES = ROOT / "assets" / "images"

SITEMAP_URL = "https://www.allrecipes.com/sitemap.xml"
KNOWN_PROBE_ID = "89268"
KNOWN_PROBE_URL = "https://www.allrecipes.com/recipe/89268/triple-dipped-fried-chicken/"

URLS_FILE = DATA / "allrecipes_urls.txt"
DISCOVERY_FILE = DATA / "discovery_report.json"
FAILURES_FILE = DATA / "failures.json"
STATE_FILE = DATA / "crawl_state.json"
MANIFEST_FILE = DATA / "recipes_manifest.json"

USER_AGENT = os.getenv(
    "ALLRECIPES_USER_AGENT",
    "AuthorizedRecipeArchive/1.0 (+GitHub Actions; licensed Allrecipes archival use)"
)
WORKERS = max(1, int(os.getenv("WORKERS", "6")))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
REQUEST_DELAY = max(0.0, float(os.getenv("REQUEST_DELAY", "0.20")))
IMAGE_MODE = os.getenv("IMAGE_MODE", "recipe").lower().strip()
# IMAGE_MODE:
#   none   = do not store images locally
#   hero   = main recipe image only
#   recipe = main image + instructional step images
IMAGE_MAX_PX = max(320, int(os.getenv("IMAGE_MAX_PX", "1280")))
IMAGE_QUALITY = min(95, max(50, int(os.getenv("IMAGE_QUALITY", "82"))))

for p in (DATA, RECIPES, IMAGES):
    p.mkdir(parents=True, exist_ok=True)

_thread_local = __import__("threading").local()


def session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is not None:
        return s

    s = requests.Session()
    retries = Retry(
        total=6,
        connect=6,
        read=6,
        status=6,
        backoff_factor=1.0,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20))
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8",
    })
    _thread_local.session = s
    return s


def polite_pause():
    if REQUEST_DELAY:
        time.sleep(REQUEST_DELAY + random.uniform(0, min(0.10, REQUEST_DELAY / 2)))


def fetch(url: str, *, binary: bool = False) -> tuple[bytes, requests.Response]:
    polite_pause()
    r = session().get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.content, r


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    scheme = "https"
    host = p.netloc.lower().replace(":443", "")
    path = re.sub(r"/+", "/", p.path)
    if not path.endswith("/") and "/recipe/" in path:
        path += "/"
    return urlunsplit((scheme, host, path, "", ""))


def is_allrecipes_recipe(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (
            p.netloc.lower().endswith("allrecipes.com")
            and re.search(r"/recipe/\d+(?:/|$)", p.path, flags=re.I) is not None
        )
    except Exception:
        return False


def recipe_id_from_url(url: str) -> str:
    m = re.search(r"/recipe/(\d+)(?:/|$)", urlsplit(url).path, flags=re.I)
    if m:
        return m.group(1)
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def sitemap_urls(url: str, seen: set[str], sitemap_files: list[str], depth: int = 0) -> set[str]:
    if depth > 12:
        raise RuntimeError(f"Sitemap recursion too deep at {url}")

    url = canonical_url(url) if "allrecipes.com" in url else url
    if url in seen:
        return set()
    seen.add(url)
    sitemap_files.append(url)

    raw, response = fetch(url, binary=True)
    ctype = (response.headers.get("content-type") or "").lower()

    if url.lower().endswith(".gz") or "gzip" in ctype:
        raw = gzip.decompress(raw)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid XML sitemap {url}: {exc}") from exc

    kind = localname(root.tag)
    found: set[str] = set()

    if kind == "sitemapindex":
        children = []
        for node in root.iter():
            if localname(node.tag) == "loc" and node.text:
                children.append(node.text.strip())

        for child in children:
            found |= sitemap_urls(urljoin(url, child), seen, sitemap_files, depth + 1)

    elif kind == "urlset":
        for node in root.iter():
            if localname(node.tag) == "loc" and node.text:
                found.add(canonical_url(node.text.strip()))
    else:
        raise RuntimeError(f"Unknown sitemap root {kind!r}: {url}")

    return found


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_text_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def write_json_if_changed(path: Path, data) -> bool:
    return write_text_if_changed(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def discover() -> dict:
    print(f"Discovering Allrecipes URLs from {SITEMAP_URL} ...", flush=True)

    seen_sitemaps: set[str] = set()
    sitemap_files: list[str] = []
    all_urls = sitemap_urls(SITEMAP_URL, seen_sitemaps, sitemap_files)

    current_recipe_urls = {u for u in all_urls if is_allrecipes_recipe(u)}
    previous_urls = set(load_lines(URLS_FILE))

    # Never silently lose recipes that were present in an earlier sitemap snapshot.
    archive_urls = previous_urls | current_recipe_urls

    # Canary/probe: this is the recipe the user explicitly reported missing.
    probe_in_current_sitemap = any(recipe_id_from_url(u) == KNOWN_PROBE_ID for u in current_recipe_urls)
    if not probe_in_current_sitemap:
        # Keep the probe in the archive and report that it was absent from the current sitemap.
        archive_urls.add(KNOWN_PROBE_URL)

    sorted_urls = sorted(archive_urls, key=lambda u: (int(recipe_id_from_url(u)) if recipe_id_from_url(u).isdigit() else 10**20, u))
    write_text_if_changed(URLS_FILE, "\n".join(sorted_urls) + "\n")

    ids = [recipe_id_from_url(u) for u in sorted_urls]
    id_counts = Counter(ids)
    duplicate_ids = sorted(x for x, n in id_counts.items() if n > 1)

    suspicious_drop = False
    if previous_urls and len(current_recipe_urls) < len(previous_urls) * 0.90:
        suspicious_drop = True

    report = {
        "source_sitemap": SITEMAP_URL,
        "sitemap_files_scanned": len(sitemap_files),
        "sitemap_files": sitemap_files,
        "all_urls_in_current_sitemaps": len(all_urls),
        "recipe_urls_in_current_sitemaps": len(current_recipe_urls),
        "previous_archived_recipe_urls": len(previous_urls),
        "archived_recipe_urls_after_union": len(sorted_urls),
        "new_recipe_urls": len(archive_urls - previous_urls),
        "duplicate_recipe_ids": duplicate_ids,
        "probe": {
            "id": KNOWN_PROBE_ID,
            "url": KNOWN_PROBE_URL,
            "present_in_current_sitemap": probe_in_current_sitemap,
            "present_in_archive": any(recipe_id_from_url(u) == KNOWN_PROBE_ID for u in archive_urls),
        },
        "warning_suspicious_sitemap_drop": suspicious_drop,
    }

    write_json_if_changed(DISCOVERY_FILE, report)

    print(
        f"Discovery: {len(current_recipe_urls)} recipe URLs currently in sitemap; "
        f"{len(sorted_urls)} URLs in preserved archive.",
        flush=True,
    )
    if suspicious_drop:
        print("WARNING: current sitemap count dropped by >10%; previous URLs were preserved.", flush=True)
    if not probe_in_current_sitemap:
        print("WARNING: canary recipe 89268 was not in the current sitemap; preserved explicitly.", flush=True)

    return report


def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v)


def types_of(obj: dict) -> set[str]:
    t = obj.get("@type")
    if isinstance(t, str):
        return {t.lower()}
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return set()


def extract_recipe_schema(soup: BeautifulSoup) -> dict | None:
    candidates = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = tag.string or tag.get_text("", strip=False)
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        for obj in walk_json(payload):
            if "recipe" in types_of(obj):
                candidates.append(obj)

    if not candidates:
        return None

    # Prefer the richest Recipe object.
    candidates.sort(
        key=lambda x: (
            bool(x.get("recipeIngredient")),
            bool(x.get("recipeInstructions")),
            len(json.dumps(x, ensure_ascii=False)),
        ),
        reverse=True,
    )
    return candidates[0]


def flatten_text(value) -> list[str]:
    out = []
    if value is None:
        return out
    if isinstance(value, str):
        s = re.sub(r"\s+", " ", BeautifulSoup(value, "html.parser").get_text(" ", strip=True)).strip()
        if s:
            out.append(s)
    elif isinstance(value, dict):
        for key in ("text", "name", "description"):
            if value.get(key):
                out += flatten_text(value[key])
                break
    elif isinstance(value, list):
        for item in value:
            out += flatten_text(item)
    return out


def image_urls(value) -> list[str]:
    urls: list[str] = []

    def add(u):
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.append(u)

    if isinstance(value, str):
        add(value)
    elif isinstance(value, list):
        for x in value:
            urls.extend(image_urls(x))
    elif isinstance(value, dict):
        for key in ("url", "contentUrl", "thumbnailUrl"):
            add(value.get(key))
        for key in ("image", "thumbnail"):
            if key in value:
                urls.extend(image_urls(value[key]))

    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def normalize_instruction_entries(value) -> list[dict]:
    steps: list[dict] = []

    def visit(node, section=None):
        if node is None:
            return
        if isinstance(node, str):
            txt = flatten_text(node)
            if txt:
                steps.append({"text": txt[0], "section": section, "image_sources": []})
            return
        if isinstance(node, list):
            for x in node:
                visit(x, section)
            return
        if not isinstance(node, dict):
            return

        typ = types_of(node)
        if "howtosection" in typ:
            sec = node.get("name") or section
            visit(node.get("itemListElement") or node.get("steps"), sec)
            return

        text = node.get("text") or node.get("description") or node.get("name")
        if text:
            cleaned = flatten_text(text)
            if cleaned:
                steps.append({
                    "text": cleaned[0],
                    "section": section,
                    "name": node.get("name") if node.get("name") != text else None,
                    "url": node.get("url"),
                    "image_sources": image_urls(node.get("image")),
                })

        nested = node.get("itemListElement")
        if nested:
            visit(nested, section)

    visit(value)
    return steps


def dom_fallback(soup: BeautifulSoup) -> tuple[list[str], list[dict]]:
    ingredients = []
    steps = []

    ingredient_selectors = [
        "ul.mm-recipes-structured-ingredients__list li",
        ".mntl-structured-ingredients__list-item",
        "[data-ingredient-name]",
    ]
    for sel in ingredient_selectors:
        nodes = soup.select(sel)
        if nodes:
            for node in nodes:
                txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                if txt:
                    ingredients.append(txt)
            if ingredients:
                break

    direction_selectors = [
        "#mntl-sc-block_2-0 ol li",
        ".comp.mntl-sc-block-group--OL li",
        "ol li",
    ]
    for sel in direction_selectors:
        nodes = soup.select(sel)
        if nodes:
            for node in nodes:
                txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                if len(txt) >= 8:
                    steps.append({"text": txt, "section": None, "image_sources": []})
            if steps:
                break

    return ingredients, steps


def clean_str(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def normalize_list(v) -> list[str]:
    """Normalize Schema.org values to a clean, unique list of strings."""
    out: list[str] = []

    def add(x):
        if x is None:
            return
        if isinstance(x, str):
            # Most Allrecipes category/cuisine values are either a single label
            # or comma-separated labels.
            parts = [x]
            if "," in x:
                parts = x.split(",")
            for part in parts:
                s = re.sub(r"\s+", " ", part).strip()
                if s:
                    out.append(s)
        elif isinstance(x, dict):
            for key in ("name", "text"):
                if x.get(key):
                    add(x[key])
                    return
        elif isinstance(x, list):
            for item in x:
                add(item)
        else:
            add(str(x))

    add(v)

    seen = set()
    result = []
    for x in out:
        key = x.casefold()
        if key not in seen:
            seen.add(key)
            result.append(x)
    return result


def extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    names: list[str] = []

    for tag in soup.find_all("script", attrs={"type": re.compile(r"application/ld\\+json", re.I)}):
        raw = tag.string or tag.get_text("", strip=False)
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        for obj in walk_json(payload):
            if "breadcrumblist" not in types_of(obj):
                continue
            for item in obj.get("itemListElement") or []:
                if isinstance(item, dict):
                    name = item.get("name")
                    if not name and isinstance(item.get("item"), dict):
                        name = item["item"].get("name")
                    if name:
                        names.extend(normalize_list(name))

    seen = set()
    result = []
    for name in names:
        key = name.casefold()
        if key not in seen and key not in {"allrecipes", "recipes"}:
            seen.add(key)
            result.append(name)
    return result


def parse_recipe(url: str, html: bytes, final_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    schema = extract_recipe_schema(soup)

    rid = recipe_id_from_url(final_url or url)
    canonical_tag = soup.find("link", rel=lambda x: x and "canonical" in x)
    canonical = canonical_url(canonical_tag.get("href")) if canonical_tag and canonical_tag.get("href") else canonical_url(final_url or url)

    if schema:
        ingredients = [clean_str(x) for x in (schema.get("recipeIngredient") or [])]
        ingredients = [x for x in ingredients if isinstance(x, str) and x]
        steps = normalize_instruction_entries(schema.get("recipeInstructions"))
    else:
        ingredients, steps = dom_fallback(soup)

    if not ingredients or not steps:
        fb_ing, fb_steps = dom_fallback(soup)
        if not ingredients:
            ingredients = fb_ing
        if not steps:
            steps = fb_steps

    title = None
    if schema:
        title = clean_str(schema.get("name"))
    if not title:
        h1 = soup.find("h1")
        title = clean_str(h1.get_text(" ", strip=True)) if h1 else None

    if not title:
        raise ValueError("missing title")
    if not ingredients:
        raise ValueError("missing ingredients")
    if not steps:
        raise ValueError("missing directions")

    hero_sources = image_urls(schema.get("image")) if schema else []

    author = schema.get("author") if schema else None
    author_names = []
    if isinstance(author, str):
        author_names = [author]
    elif isinstance(author, dict):
        if author.get("name"):
            author_names = [str(author["name"])]
    elif isinstance(author, list):
        for a in author:
            if isinstance(a, str):
                author_names.append(a)
            elif isinstance(a, dict) and a.get("name"):
                author_names.append(str(a["name"]))

    category = schema.get("recipeCategory") if schema else None
    cuisine = schema.get("recipeCuisine") if schema else None
    keywords = schema.get("keywords") if schema else None

    categories = normalize_list(category)
    cuisines = normalize_list(cuisine)
    breadcrumbs = extract_breadcrumbs(soup)

    recipe = {
        "id": rid,
        "source": "Allrecipes",
        "source_url": canonical,
        "requested_url": canonical_url(url),
        "title": title,
        "description": clean_str(schema.get("description")) if schema else None,
        "authors": author_names,
        "date_published": clean_str(schema.get("datePublished")) if schema else None,
        "date_modified": clean_str(schema.get("dateModified")) if schema else None,
        "prep_time": clean_str(schema.get("prepTime")) if schema else None,
        "cook_time": clean_str(schema.get("cookTime")) if schema else None,
        "total_time": clean_str(schema.get("totalTime")) if schema else None,
        "yield": normalize_list(schema.get("recipeYield")) if schema else [],
        "categories": categories,
        "cuisines": cuisines,
        "regions": cuisines.copy(),
        "breadcrumbs": breadcrumbs,
        "keywords": normalize_list(keywords),
        "ingredients": ingredients,
        "steps": steps,
        "nutrition": schema.get("nutrition") if schema else None,
        "aggregate_rating": schema.get("aggregateRating") if schema else None,
        "video": schema.get("video") if schema else None,
        "image_sources": hero_sources,
        "images": {
            "hero": None,
            "steps": [],
        },
    }
    return recipe


def download_as_webp(url: str, target: Path) -> str | None:
    try:
        raw, _ = fetch(url, binary=True)
        with Image.open(io.BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im)
            if getattr(im, "is_animated", False):
                im.seek(0)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            elif im.mode == "RGBA":
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            im.thumbnail((IMAGE_MAX_PX, IMAGE_MAX_PX), Image.Resampling.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp.webp")
            im.save(tmp, "WEBP", quality=IMAGE_QUALITY, method=6)
            tmp.replace(target)
        return target.relative_to(ROOT).as_posix()
    except Exception as exc:
        print(f"WARN image failed {url}: {exc}", file=sys.stderr, flush=True)
        return None


def save_recipe_images(recipe: dict):
    if IMAGE_MODE == "none":
        return

    rid = recipe["id"]
    img_dir = IMAGES / rid

    hero_sources = recipe.get("image_sources") or []
    if hero_sources:
        hero_path = img_dir / "hero.webp"
        if not hero_path.exists():
            local = download_as_webp(hero_sources[0], hero_path)
        else:
            local = hero_path.relative_to(ROOT).as_posix()
        recipe["images"]["hero"] = local

    if IMAGE_MODE != "recipe":
        return

    step_local = []
    for i, step in enumerate(recipe.get("steps") or [], start=1):
        sources = step.get("image_sources") or []
        if not sources:
            step_local.append(None)
            continue
        target = img_dir / f"step_{i:02d}.webp"
        if not target.exists():
            local = download_as_webp(sources[0], target)
        else:
            local = target.relative_to(ROOT).as_posix()
        step["image"] = local
        step_local.append(local)

    recipe["images"]["steps"] = step_local


def scrape_one(url: str) -> tuple[str, dict | None, str | None]:
    rid = recipe_id_from_url(url)
    try:
        raw, response = fetch(url, binary=True)
        ctype = (response.headers.get("content-type") or "").lower()
        if "html" not in ctype and raw[:100].lower().find(b"<html") < 0:
            raise ValueError(f"unexpected content-type {ctype}")

        recipe = parse_recipe(url, raw, response.url)
        if recipe["id"] != rid:
            recipe["canonical_id"] = recipe["id"]
            recipe["id"] = rid

        save_recipe_images(recipe)
        return url, recipe, None
    except Exception as exc:
        return url, None, f"{type(exc).__name__}: {exc}"


def load_failures() -> dict:
    if not FAILURES_FILE.exists():
        return {}
    try:
        obj = json.loads(FAILURES_FILE.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def existing_recipe_ids() -> set[str]:
    return {p.stem for p in RECIPES.glob("*.json") if p.is_file()}


def build_manifest() -> dict:
    items = []
    invalid_files = []
    category_counts = Counter()
    region_counts = Counter()

    for path in RECIPES.glob("*.json"):
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
            title = r.get("title")
            if not title:
                raise ValueError("no title")

            categories = normalize_list(
                r.get("categories")
                or r.get("category")
            )
            regions = normalize_list(
                r.get("regions")
                or r.get("cuisines")
                or r.get("cuisine")
                or r.get("area")
            )

            for value in categories:
                category_counts[value] += 1
            for value in regions:
                region_counts[value] += 1

            items.append({
                "id": str(r.get("id") or path.stem),
                "title": title,
                "source_url": r.get("source_url"),
                "file": path.relative_to(ROOT).as_posix(),
                "image": ((r.get("images") or {}).get("hero")),
                "categories": categories,
                "regions": regions,
                # Keep cuisines for backward compatibility with older index versions.
                "cuisines": regions,
                "ingredients_search": r.get("ingredients") or [],
            })
        except Exception as exc:
            invalid_files.append({"file": path.name, "error": str(exc)})

    items.sort(key=lambda x: x["title"].casefold())

    manifest = {
        "source": "Allrecipes",
        "count": len(items),
        "invalid_files": invalid_files,
        "facets": {
            "categories": dict(sorted(category_counts.items(), key=lambda kv: kv[0].casefold())),
            "regions": dict(sorted(region_counts.items(), key=lambda kv: kv[0].casefold())),
        },
        "recipes": items,
    }
    write_json_if_changed(MANIFEST_FILE, manifest)
    return manifest


def current_status() -> dict:
    urls = load_lines(URLS_FILE)
    ids = {recipe_id_from_url(u) for u in urls}
    done = existing_recipe_ids()
    failures = load_failures()

    pending_ids = ids - done
    # Failures remain pending and are retried on later runs.
    probe_file = RECIPES / f"{KNOWN_PROBE_ID}.json"
    probe_ok = False
    probe_detail = None
    if probe_file.exists():
        try:
            probe = json.loads(probe_file.read_text(encoding="utf-8"))
            probe_ok = (
                bool(probe.get("title"))
                and bool(probe.get("ingredients"))
                and bool(probe.get("steps"))
            )
            probe_detail = {
                "title": probe.get("title"),
                "ingredients": len(probe.get("ingredients") or []),
                "steps": len(probe.get("steps") or []),
            }
        except Exception as exc:
            probe_detail = {"error": str(exc)}

    status = {
        "discovered": len(ids),
        "downloaded": len(ids & done),
        "pending": len(pending_ids),
        "failed_last_attempt": len([k for k in failures if k in pending_ids]),
        "completion_percent": round((len(ids & done) / len(ids) * 100), 5) if ids else 0.0,
        "probe_89268_ok": probe_ok,
        "probe_89268": probe_detail,
        "complete": bool(ids) and not pending_ids and probe_ok,
    }

    write_json_if_changed(STATE_FILE, status)
    return status


def scrape_batch(batch_size: int) -> dict:
    urls = load_lines(URLS_FILE)
    if not urls:
        raise RuntimeError("No discovered URLs. Run --discover first.")

    done = existing_recipe_ids()
    failures = load_failures()

    pending = [u for u in urls if recipe_id_from_url(u) not in done]

    # Put previous failures first so transient problems are retried promptly.
    pending.sort(key=lambda u: (0 if u in failures else 1, int(recipe_id_from_url(u)) if recipe_id_from_url(u).isdigit() else 10**20))
    batch = pending[:batch_size]

    if not batch:
        build_manifest()
        return current_status()

    print(f"Scraping batch of {len(batch)} from {len(pending)} pending URLs with {WORKERS} workers...", flush=True)

    processed = 0
    success = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(scrape_one, u): u for u in batch}
        for future in as_completed(futures):
            url, recipe, error = future.result()
            processed += 1

            if recipe is not None:
                rid = str(recipe["id"])
                write_json_if_changed(RECIPES / f"{rid}.json", recipe)
                failures.pop(url, None)
                success += 1
            else:
                old = failures.get(url, {})
                failures[url] = {
                    "url": url,
                    "id": recipe_id_from_url(url),
                    "error": error,
                    "attempts": int(old.get("attempts", 0)) + 1,
                }
                failed += 1

            if processed % 50 == 0 or processed == len(batch):
                print(f"Progress {processed}/{len(batch)} | ok {success} | failed {failed}", flush=True)

    write_json_if_changed(FAILURES_FILE, failures)
    build_manifest()
    status = current_status()
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
    return status


def verify(strict: bool) -> int:
    status = current_status()
    manifest = build_manifest()

    urls = load_lines(URLS_FILE)
    ids = {recipe_id_from_url(u) for u in urls}
    files = existing_recipe_ids()

    missing = sorted(ids - files, key=lambda x: int(x) if x.isdigit() else 10**20)
    extra = sorted(files - ids)
    manifest_ids = {str(x["id"]) for x in manifest["recipes"]}
    manifest_missing = sorted((ids & files) - manifest_ids)

    checks = {
        "status": status,
        "missing_recipe_files_count": len(missing),
        "missing_recipe_files_sample": missing[:100],
        "extra_recipe_files_count": len(extra),
        "manifest_missing_count": len(manifest_missing),
        "manifest_invalid_files": manifest.get("invalid_files") or [],
        "canary_expected_title_contains": "Triple-Dipped Fried Chicken",
    }

    canary_title_ok = False
    canary_path = RECIPES / f"{KNOWN_PROBE_ID}.json"
    if canary_path.exists():
        try:
            canary = json.loads(canary_path.read_text(encoding="utf-8"))
            canary_title_ok = "triple-dipped fried chicken" in (canary.get("title") or "").lower()
        except Exception:
            pass

    checks["canary_title_ok"] = canary_title_ok
    checks["all_checks_pass"] = (
        status["complete"]
        and not missing
        and not manifest_missing
        and not manifest.get("invalid_files")
        and canary_title_ok
    )

    write_json_if_changed(DATA / "verification_report.json", checks)
    print(json.dumps(checks, ensure_ascii=False, indent=2), flush=True)

    if strict and not checks["all_checks_pass"]:
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--batch", type=int, default=0)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if not any((args.discover, args.batch, args.verify, args.status)):
        ap.error("Choose --discover, --batch N, --verify or --status")

    if args.discover:
        discover()
        build_manifest()
        current_status()

    if args.batch:
        scrape_batch(max(1, args.batch))

    if args.status:
        print(json.dumps(current_status(), ensure_ascii=False, indent=2))

    if args.verify:
        raise SystemExit(verify(args.strict))


if __name__ == "__main__":
    main()
