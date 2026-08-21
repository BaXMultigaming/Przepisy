#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = ROOT / "data" / "recipes"
IMAGES_DIR = ROOT / "assets" / "images"
MANIFEST_PATH = ROOT / "data" / "recipes_manifest.json"

API_KEY = os.getenv("THEMEALDB_API_KEY", "").strip() or "1"
API_BASE = f"https://www.themealdb.com/api/json/v1/{API_KEY}"
USER_AGENT = "RecipeRepoSync/2.0 (GitHub Actions; TheMealDB API)"

RECIPES_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def http_get(url: str, *, binary: bool = False, retries: int = 5):
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*" if binary else "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                if binary:
                    return payload, resp.headers.get_content_type()
                return json.loads(payload.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 16))
    raise RuntimeError(f"Request failed: {url}: {last_exc}")


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def extract_ingredients(meal: dict) -> list[dict]:
    result = []
    for i in range(1, 21):
        ingredient = clean(meal.get(f"strIngredient{i}"))
        measure = clean(meal.get(f"strMeasure{i}"))
        if ingredient:
            result.append({
                "ingredient": ingredient,
                "measure": measure or ""
            })
    return result


def split_steps(raw: str | None) -> list[str]:
    if not raw:
        return []

    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Prefer source line breaks.
    chunks = [
        re.sub(r"\s+", " ", x).strip(" -•\t")
        for x in raw.split("\n")
    ]
    chunks = [x for x in chunks if x]

    # Some recipes are returned as one large paragraph.
    if len(chunks) <= 1:
        chunks = [
            x.strip()
            for x in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", raw)
            if x.strip()
        ]

    return chunks


def choose_image_extension(content_type: str, url: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if content_type in mapping:
        return mapping[content_type]

    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def download_image(meal_id: str, image_url: str | None) -> str | None:
    if not image_url:
        return None

    # Medium image keeps the repository much smaller while remaining
    # perfectly suitable for the recipe page.
    download_url = image_url.rstrip("/") + "/medium"

    try:
        data, content_type = http_get(download_url, binary=True)
    except Exception:
        # Fallback to original artwork.
        try:
            data, content_type = http_get(image_url, binary=True)
        except Exception as exc:
            print(f"WARN image {meal_id}: {exc}")
            return None

    ext = choose_image_extension(content_type, image_url)
    rel = Path("assets") / "images" / f"{meal_id}{ext}"
    target = ROOT / rel

    if not target.exists() or target.read_bytes() != data:
        target.write_bytes(data)

    return rel.as_posix()


def normalize_recipe(meal: dict) -> dict:
    meal_id = str(meal.get("idMeal") or "").strip()
    if not meal_id:
        raise ValueError("Recipe without idMeal")

    title = clean(meal.get("strMeal")) or f"Recipe {meal_id}"
    instructions = clean(meal.get("strInstructions")) or ""
    image_source = clean(meal.get("strMealThumb"))
    local_image = download_image(meal_id, image_source)

    tags = []
    raw_tags = clean(meal.get("strTags"))
    if raw_tags:
        tags = [x.strip() for x in raw_tags.split(",") if x.strip()]

    return {
        "id": meal_id,
        "title": title,
        "category": clean(meal.get("strCategory")),
        "area": clean(meal.get("strArea")),
        "tags": tags,
        "ingredients": extract_ingredients(meal),
        "instructions_raw": instructions,
        "steps": split_steps(instructions),
        "image": local_image,
        "image_source": image_source,
        "source_url": clean(meal.get("strSource"))
            or f"https://www.themealdb.com/meal/{meal_id}",
        "youtube": clean(meal.get("strYoutube")),
        "attribution": "Recipe data and artwork via TheMealDB API"
    }


def fetch_all_meals() -> dict[str, dict]:
    meals_by_id: dict[str, dict] = {}

    for letter in "abcdefghijklmnopqrstuvwxyz":
        print(f"Fetching recipes beginning with {letter.upper()}...", flush=True)
        payload = http_get(f"{API_BASE}/search.php?f={letter}")

        for meal in (payload or {}).get("meals") or []:
            meal_id = str(meal.get("idMeal") or "").strip()
            if meal_id:
                meals_by_id[meal_id] = meal

        time.sleep(0.12)

    return meals_by_id


def write_json_if_changed(path: Path, obj) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def remove_stale_files(valid_ids: set[str]) -> None:
    for path in RECIPES_DIR.glob("*.json"):
        if path.stem not in valid_ids:
            path.unlink()

    for path in IMAGES_DIR.iterdir():
        if path.is_file() and path.stem not in valid_ids:
            path.unlink()


def main():
    meals = fetch_all_meals()
    print(f"Found {len(meals)} unique recipes.")

    if not meals:
        raise RuntimeError("API returned no recipes; refusing to erase existing database.")

    manifest = []
    valid_ids = set(meals.keys())

    def sort_id(x):
        return (0, int(x)) if x.isdigit() else (1, x)

    for index, meal_id in enumerate(sorted(meals, key=sort_id), 1):
        recipe = normalize_recipe(meals[meal_id])

        write_json_if_changed(
            RECIPES_DIR / f"{meal_id}.json",
            recipe
        )

        manifest.append({
            "id": recipe["id"],
            "title": recipe["title"],
            "category": recipe["category"],
            "area": recipe["area"],
            "tags": recipe["tags"],
            "ingredients_search": [
                x["ingredient"] for x in recipe["ingredients"]
            ],
            "image": recipe["image"],
            "file": f"data/recipes/{meal_id}.json",
        })

        print(f"[{index}/{len(meals)}] {recipe['title']}", flush=True)

    remove_stale_files(valid_ids)

    manifest.sort(key=lambda x: x["title"].casefold())

    write_json_if_changed(
        MANIFEST_PATH,
        {
            "source": "TheMealDB official API",
                      "count": len(manifest),
            "recipes": manifest,
        }
    )

    print(f"Done. Database contains {len(manifest)} recipes.")


if __name__ == "__main__":
    main()
