#!/usr/bin/env python3
"""
mela2mealie.py — Migrate recipes from Mela to Mealie

Usage:
    1. In Mela (iOS/macOS): long-press "All" category → Export → Save .melarecipes file
    2. Copy config.json.dist to config.json and fill in your Mealie URL and API token
    3. Run:  python3 mela2mealie.py Recipes.melarecipes

    You can also pass --url and --token on the command line to override config.json.

What it does:
    - Extracts .melarecipes (zip of .melarecipe JSON files)
    - Maps Mela fields → Mealie recipe format
    - Creates each recipe via POST /api/recipes → PATCH /api/recipes/{slug}
    - Uploads embedded base64 images
    - Preserves: categories, ingredients, instructions, notes, nutrition,
      prep/cook/total time, yield, source URL
    - Tags all imported recipes with "mela-import" for easy identification

Requirements:
    pip install requests  (that's it)
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests")
    sys.exit(1)


def slugify(text: str) -> str:
    """Convert a string to a URL-friendly slug (matching Mealie's convention)."""
    import unicodedata
    # Normalize unicode (e.g. ü → u)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)   # remove non-word chars
    text = re.sub(r'[\s_]+', '-', text)     # spaces/underscores → hyphens
    text = re.sub(r'-+', '-', text)         # collapse multiple hyphens
    return text.strip('-')


def parse_time_to_iso(time_str: str) -> str | None:
    """Convert Mela time strings to ISO 8601 duration if possible.
    
    Mela stores times as freeform strings like "30 min", "1 hour 15 minutes",
    "45 Minuten", etc. Mealie accepts ISO 8601 or freeform — we try ISO first.
    """
    if not time_str or not time_str.strip():
        return None

    time_str = time_str.strip().lower()

    # Already looks like PT format
    if time_str.startswith("pt"):
        return time_str.upper()

    hours = 0
    minutes = 0

    # Match patterns like "1h 30m", "1 hour 30 minutes", "1 Stunde 30 Minuten"
    h_match = re.search(r'(\d+)\s*(?:h(?:ours?|r)?|stunde[n]?)', time_str)
    m_match = re.search(r'(\d+)\s*(?:m(?:in(?:ute[ns]?)?)?|minute[n]?)', time_str)

    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))

    # If we only found a bare number, assume minutes
    if not h_match and not m_match:
        bare = re.match(r'^(\d+)$', time_str)
        if bare:
            minutes = int(bare.group(1))
        else:
            # Can't parse — return as-is, Mealie handles freeform
            return time_str

    if hours == 0 and minutes == 0:
        return time_str  # fallback: pass through

    parts = "PT"
    if hours:
        parts += f"{hours}H"
    if minutes:
        parts += f"{minutes}M"
    return parts


def mela_instructions_to_steps(instructions_str: str) -> list[dict]:
    """Convert Mela instruction string (newline-separated) to Mealie steps.

    Mela uses:
    - \n to separate steps
    - # for section headers (group titles)
    - ** for bold, * for italic, [text](url) for links

    Mealie requires all fields: id, title, summary, text, ingredientReferences.
    """
    if not instructions_str:
        return []

    steps = []
    for line in instructions_str.split("\n"):
        line = line.strip()
        if not line:
            continue

        step = {
            "id": str(uuid.uuid4()),
            "title": "",
            "summary": "",
            "text": "",
            "ingredientReferences": [],
        }

        # Mela uses # for section headers in instructions
        if line.startswith("#"):
            step["title"] = line.lstrip("#").strip()
        else:
            step["text"] = line

        steps.append(step)

    return steps


def mela_ingredients_to_list(ingredients_str: str) -> list[dict]:
    """Convert Mela ingredients string to Mealie ingredient list.

    Mela uses:
    - \n to separate ingredients
    - # for group/section headers

    In Mealie, section headers are set via the 'title' field on the first
    ingredient of that section (not as a separate empty item).
    """
    if not ingredients_str:
        return []

    ingredients = []
    pending_title = None
    for line in ingredients_str.split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("#"):
            pending_title = line.lstrip("#").strip()
        else:
            ing = {"note": line, "referenceId": str(uuid.uuid4())}
            if pending_title:
                ing["title"] = pending_title
                pending_title = None
            ingredients.append(ing)

    return ingredients


def convert_mela_to_mealie(mela_recipe: dict, category_lookup: dict, tag_lookup: dict) -> dict:
    """Convert a single .melarecipe JSON dict to Mealie PATCH payload.

    category_lookup/tag_lookup map slug → {"id": ..., "name": ..., "slug": ...}
    """

    mealie = {}

    # Direct mappings
    if mela_recipe.get("title"):
        mealie["name"] = mela_recipe["title"]

    if mela_recipe.get("text"):
        mealie["description"] = mela_recipe["text"]

    if mela_recipe.get("yield"):
        mealie["recipeYield"] = mela_recipe["yield"]

    # Time fields
    if mela_recipe.get("prepTime"):
        mealie["prepTime"] = parse_time_to_iso(mela_recipe["prepTime"])
    if mela_recipe.get("cookTime"):
        mealie["performTime"] = parse_time_to_iso(mela_recipe["cookTime"])
    if mela_recipe.get("totalTime"):
        mealie["totalTime"] = parse_time_to_iso(mela_recipe["totalTime"])

    # Source URL / attribution
    if mela_recipe.get("link"):
        mealie["orgURL"] = mela_recipe["link"]

    # Date — Mela stores as NSDate (seconds since Jan 1, 2001)
    if mela_recipe.get("date"):
        nsdate_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
        dt = nsdate_epoch + timedelta(seconds=mela_recipe["date"])
        mealie["dateAdded"] = dt.strftime("%Y-%m-%d")
        mealie["createdAt"] = dt.isoformat()

    # Structured data
    mealie["recipeIngredient"] = mela_ingredients_to_list(
        mela_recipe.get("ingredients", "")
    )
    mealie["recipeInstructions"] = mela_instructions_to_steps(
        mela_recipe.get("instructions", "")
    )

    # Notes — Mealie has a notes field (list of dicts)
    notes_parts = []
    if mela_recipe.get("notes"):
        notes_parts.append({"title": "Notes", "text": mela_recipe["notes"]})
    if mela_recipe.get("nutrition"):
        notes_parts.append({"title": "Nutrition", "text": mela_recipe["nutrition"]})
    if notes_parts:
        mealie["notes"] = notes_parts

    # Categories — reference pre-created categories by id
    if mela_recipe.get("categories"):
        cats = mela_recipe["categories"]
        if isinstance(cats, list) and cats:
            cat_refs = []
            for c in cats:
                if not c:
                    continue
                cat_slug = slugify(c)
                if cat_slug in category_lookup:
                    cat_refs.append(category_lookup[cat_slug])
            if cat_refs:
                mealie["recipeCategory"] = cat_refs

    # Tags — reference pre-created tags by id
    tag_refs = []
    for tag_slug in ["mela-import"]:
        if tag_slug in tag_lookup:
            tag_refs.append(tag_lookup[tag_slug])
    if mela_recipe.get("favorite") and "favorite" in tag_lookup:
        tag_refs.append(tag_lookup["favorite"])
    if mela_recipe.get("wantToCook") and "want-to-cook" in tag_lookup:
        tag_refs.append(tag_lookup["want-to-cook"])
    if tag_refs:
        mealie["tags"] = tag_refs

    return mealie


def upload_image(
    session: requests.Session,
    base_url: str,
    slug: str,
    image_b64: str,
) -> bool:
    """Upload a base64-encoded image to a Mealie recipe."""
    try:
        img_data = base64.b64decode(image_b64)
    except Exception as e:
        print(f"    ⚠ Failed to decode image: {e}")
        return False

    # Detect image type from magic bytes
    ext = "jpg"
    if img_data[:8] == b'\x89PNG\r\n\x1a\n':
        ext = "png"
    elif img_data[:4] == b'RIFF' and img_data[8:12] == b'WEBP':
        ext = "webp"

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(img_data)
        tmp_path = tmp.name

    try:
        url = f"{base_url}/api/recipes/{slug}/image"
        with open(tmp_path, "rb") as f:
            resp = session.put(
                url,
                files={"image": (f"recipe.{ext}", f, f"image/{ext}")},
                data={"extension": ext},
            )
        if resp.status_code == 200:
            return True
        else:
            print(f"    ⚠ Image upload returned {resp.status_code}: {resp.text[:200]}")
            return False
    finally:
        os.unlink(tmp_path)


def migrate(
    export_path: str,
    base_url: str,
    token: str,
    dry_run: bool = False,
    skip_images: bool = False,
):
    """Main migration logic."""
    base_url = base_url.rstrip("/")
    export_path = Path(export_path)

    if not export_path.exists():
        print(f"✗ File not found: {export_path}")
        sys.exit(1)

    # Set up session
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })

    # Verify connection
    if not dry_run:
        try:
            resp = session.get(f"{base_url}/api/app/about")
            if resp.status_code != 200:
                print(f"✗ Cannot reach Mealie at {base_url} (HTTP {resp.status_code})")
                sys.exit(1)
            info = resp.json()
            print(f"✓ Connected to Mealie {info.get('version', '?')} at {base_url}")
        except requests.ConnectionError:
            print(f"✗ Cannot connect to {base_url}")
            sys.exit(1)

    # Extract recipes from export
    recipes = []

    if export_path.suffix == ".melarecipes":
        # It's a zip of .melarecipe files
        with zipfile.ZipFile(export_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".melarecipe"):
                    with zf.open(name) as f:
                        try:
                            recipe = json.loads(f.read())
                            recipes.append(recipe)
                        except json.JSONDecodeError as e:
                            print(f"  ⚠ Skipping {name}: invalid JSON ({e})")
                elif name.endswith(".melarecipes"):
                    # Nested zip — Mela sometimes double-wraps
                    with zf.open(name) as inner_zip_file:
                        inner_data = inner_zip_file.read()
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp.write(inner_data)
                        tmp_path = tmp.name
                    try:
                        with zipfile.ZipFile(tmp_path, "r") as inner_zf:
                            for inner_name in inner_zf.namelist():
                                if inner_name.endswith(".melarecipe"):
                                    with inner_zf.open(inner_name) as f:
                                        try:
                                            recipe = json.loads(f.read())
                                            recipes.append(recipe)
                                        except json.JSONDecodeError as e:
                                            print(f"  ⚠ Skipping {inner_name}: invalid JSON ({e})")
                    finally:
                        os.unlink(tmp_path)

    elif export_path.suffix == ".melarecipe":
        # Single recipe file
        with open(export_path, "r") as f:
            recipes.append(json.load(f))
    else:
        print(f"✗ Unknown file type: {export_path.suffix}")
        print("  Expected .melarecipes (bulk export) or .melarecipe (single recipe)")
        sys.exit(1)

    print(f"✓ Found {len(recipes)} recipe(s) to migrate\n")

    if not recipes:
        return

    # Pre-create categories and tags so we can reference them by id
    category_lookup = {}  # slug → {"id": ..., "name": ..., "slug": ...}
    tag_lookup = {}       # slug → {"id": ..., "name": ..., "slug": ...}

    if not dry_run:
        # Collect all unique category names
        all_cats = set()
        for mela in recipes:
            cats = mela.get("categories", [])
            if isinstance(cats, list):
                for c in cats:
                    if c:
                        all_cats.add(c)

        # Collect all tag names we'll use
        all_tags = {"mela-import"}
        for mela in recipes:
            if mela.get("favorite"):
                all_tags.add("favorite")
            if mela.get("wantToCook"):
                all_tags.add("want-to-cook")

        # Create categories
        if all_cats:
            print(f"Creating {len(all_cats)} categories...")
            for cat_name in sorted(all_cats):
                resp = session.post(f"{base_url}/api/organizers/categories", json={"name": cat_name})
                if resp.status_code in (200, 201):
                    data = resp.json()
                    category_lookup[data["slug"]] = {"id": data["id"], "name": data["name"], "slug": data["slug"]}
                elif resp.status_code == 409:
                    # Already exists — fetch it
                    cat_slug = slugify(cat_name)
                    resp2 = session.get(f"{base_url}/api/organizers/categories/slug/{cat_slug}")
                    if resp2.status_code == 200:
                        data = resp2.json()
                        category_lookup[data["slug"]] = {"id": data["id"], "name": data["name"], "slug": data["slug"]}
                else:
                    print(f"  ⚠ Failed to create category '{cat_name}': {resp.status_code}")

        # Create tags
        if all_tags:
            print(f"Creating {len(all_tags)} tags...")
            for tag_name in sorted(all_tags):
                resp = session.post(f"{base_url}/api/organizers/tags", json={"name": tag_name})
                if resp.status_code in (200, 201):
                    data = resp.json()
                    tag_lookup[data["slug"]] = {"id": data["id"], "name": data["name"], "slug": data["slug"]}
                elif resp.status_code == 409:
                    tag_slug = slugify(tag_name)
                    resp2 = session.get(f"{base_url}/api/organizers/tags/slug/{tag_slug}")
                    if resp2.status_code == 200:
                        data = resp2.json()
                        tag_lookup[data["slug"]] = {"id": data["id"], "name": data["name"], "slug": data["slug"]}
                else:
                    print(f"  ⚠ Failed to create tag '{tag_name}': {resp.status_code}")

        print(f"✓ {len(category_lookup)} categories, {len(tag_lookup)} tags ready\n")

    # Migrate each recipe
    success = 0
    failed = 0
    skipped = 0

    for i, mela in enumerate(recipes, 1):
        title = mela.get("title", "Untitled")
        print(f"[{i}/{len(recipes)}] {title}")

        if dry_run:
            mealie_data = convert_mela_to_mealie(mela, {}, {})
            print(f"  → Would create: {mealie_data.get('name', '?')}")
            cats = [c["name"] for c in mealie_data.get("recipeCategory", [])]
            print(f"    Categories: {', '.join(cats) if cats else '(none)'}")
            ing_count = len(mealie_data.get("recipeIngredient", []))
            step_count = len(mealie_data.get("recipeInstructions", []))
            print(f"    {ing_count} ingredients, {step_count} steps")
            has_images = bool(mela.get("images"))
            print(f"    Images: {'yes' if has_images else 'no'}")
            success += 1
            continue

        # Step 1: Create recipe stub (POST returns slug)
        try:
            resp = session.post(
                f"{base_url}/api/recipes",
                json={"name": title},
            )
        except requests.RequestException as e:
            print(f"  ✗ Connection error: {e}")
            failed += 1
            continue

        if resp.status_code == 201:
            slug = resp.text.strip().strip('"')
        elif resp.status_code == 409:
            # Recipe already exists — generate unique slug
            print(f"  ⚠ Recipe '{title}' already exists, appending timestamp")
            title_unique = f"{title} (mela-{int(time.time())})"
            resp = session.post(
                f"{base_url}/api/recipes",
                json={"name": title_unique},
            )
            if resp.status_code == 201:
                slug = resp.text.strip().strip('"')
            else:
                print(f"  ✗ Create failed even with unique name: {resp.status_code}")
                failed += 1
                continue
        else:
            print(f"  ✗ Create failed: HTTP {resp.status_code} — {resp.text[:200]}")
            failed += 1
            continue

        # Step 2: PATCH with full recipe data
        mealie_data = convert_mela_to_mealie(mela, category_lookup, tag_lookup)
        resp = session.patch(
            f"{base_url}/api/recipes/{slug}",
            json=mealie_data,
        )

        if resp.status_code != 200:
            print(f"  ✗ Update failed: HTTP {resp.status_code} — {resp.text[:200]}")
            failed += 1
            continue

        # Step 3: Upload image (first one only — Mealie has one hero image)
        images = mela.get("images", [])
        if images and not skip_images:
            if upload_image(session, base_url, slug, images[0]):
                print(f"  ✓ Created with image → /recipe/{slug}")
            else:
                print(f"  ✓ Created (image upload failed) → /recipe/{slug}")
        else:
            print(f"  ✓ Created → /recipe/{slug}")

        success += 1

        # Small delay to be polite to the API
        if i < len(recipes):
            time.sleep(0.3)

    # Summary
    print(f"\n{'═' * 50}")
    print(f"Migration complete:")
    print(f"  ✓ {success} succeeded")
    if failed:
        print(f"  ✗ {failed} failed")
    if skipped:
        print(f"  ⊘ {skipped} skipped")
    print(f"\nAll imported recipes are tagged 'mela-import'")
    if not dry_run:
        print(f"View them at: {base_url}/g/home?tag=mela-import")


def load_config(config_path: Path) -> dict:
    """Load configuration from a JSON file."""
    try:
        with open(config_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        print(f"✗ Invalid JSON in {config_path}: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate recipes from Mela to Mealie",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using config.json (copy config.json.dist and fill in your values):
  python3 mela2mealie.py ~/Recipes.melarecipes

  # Dry run — see what would be imported:
  python3 mela2mealie.py ~/Recipes.melarecipes --dry-run

  # Single recipe:
  python3 mela2mealie.py ~/Lasagna.melarecipe

  # Override config with CLI flags:
  python3 mela2mealie.py ~/Recipes.melarecipes --url http://mealie.example.com:9925 --token abc123

  # Skip images (faster):
  python3 mela2mealie.py ~/Recipes.melarecipes --skip-images
""",
    )
    parser.add_argument("export", help="Path to .melarecipes or .melarecipe file")
    parser.add_argument("--url", help="Mealie base URL (overrides config.json)")
    parser.add_argument("--token", help="Mealie API token (overrides config.json)")
    parser.add_argument("--config", help="Path to config file (default: config.json next to script)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without importing")
    parser.add_argument("--skip-images", action="store_true", help="Skip image uploads")

    args = parser.parse_args()

    # Load config file
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"✗ Config file not found: {config_path}")
            sys.exit(1)
        config = load_config(config_path)
    else:
        config_path = Path(__file__).parent / "config.json"
        config = load_config(config_path)

    # CLI args override config file values
    url = args.url or config.get("mealie_url")
    token = args.token or config.get("api_token")

    if not url or not token:
        missing = []
        if not url:
            missing.append("Mealie URL (--url or mealie_url in config)")
        if not token:
            missing.append("API token (--token or api_token in config)")
        print(f"✗ Missing required configuration: {', '.join(missing)}")
        print()
        print("Set up a config file:")
        print(f"  cp config.json.dist config.json")
        print(f"  # Edit config.json with your Mealie URL and API token")
        print()
        print("Or pass them on the command line:")
        print(f"  python3 {sys.argv[0]} {args.export} --url <MEALIE_URL> --token <API_TOKEN>")
        sys.exit(1)

    migrate(args.export, url, token, args.dry_run, args.skip_images)


if __name__ == "__main__":
    main()