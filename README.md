# mela2mealie

Migrate your recipes from [Mela](https://mela.recipes/) to [Mealie](https://mealie.io/) with full fidelity.

## Features

- Extracts `.melarecipes` bulk exports (or single `.melarecipe` files)
- Preserves categories, ingredients (with section headers), and step-by-step instructions
- Uploads embedded images
- Converts prep/cook/total time to ISO 8601 durations
- Preserves notes, nutrition info, source URLs, and original dates
- Tags all imported recipes with `mela-import` for easy filtering
- Marks Mela favorites and "want to cook" recipes with dedicated tags
- Handles duplicate recipe names gracefully
- Dry-run mode to preview before committing

## Requirements

- Python 3.10+
- `requests` library
- A running Mealie instance (tested with v3.10.2)

## Quick Start

1. Clone the repo:
   ```
   git clone https://github.com/abartok/mela2mealie.git
   cd mela2mealie
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up your config:
   ```
   cp config.json.dist config.json
   # Edit config.json with your Mealie URL and API token
   ```

4. Export from Mela: in Mela (iOS/macOS), long-press the "All" category, tap Export, and save the `.melarecipes` file.

5. Run the migration:
   ```
   python3 mela2mealie.py Recipes.melarecipes
   ```

## Configuration

### Config file (`config.json`)

Copy `config.json.dist` to `config.json` and fill in your values:

```json
{
  "mealie_url": "http://mealie.example.com:9925",
  "api_token": "your-mealie-api-token-here"
}
```

| Field | Description |
|-------|-------------|
| `mealie_url` | Base URL of your Mealie instance (no trailing slash) |
| `api_token` | API token from Mealie (Settings > API Tokens) |

### CLI options

| Flag | Description |
|------|-------------|
| `--url URL` | Override the Mealie URL from config |
| `--token TOKEN` | Override the API token from config |
| `--config PATH` | Use a different config file |
| `--dry-run` | Preview what would be imported without making changes |
| `--skip-images` | Skip image uploads (faster migration) |

CLI flags take precedence over `config.json` values.

## How It Works

1. **Extract** — Unpacks the `.melarecipes` zip archive (handles nested zips too)
2. **Pre-create organizers** — Creates all categories and tags in Mealie so they can be referenced by ID
3. **Create recipe stubs** — POSTs each recipe name to get a slug
4. **Patch full data** — PATCHes each recipe with ingredients, instructions, times, categories, tags, notes, and metadata
5. **Upload images** — Detects image format (JPEG/PNG/WebP) and uploads via the Mealie image API

## Mealie API Notes

A few quirks discovered while building this (Mealie v3.10.2):

- Categories and tags must be pre-created via their respective organizer endpoints before referencing them in a recipe PATCH. Each reference must include `id`, `name`, and `slug`.
- `recipeInstructions` entries require all fields (`id`, `title`, `summary`, `text`, `ingredientReferences`) — omitting any causes a server error.
- `recipeIngredient` entries need a `referenceId` (UUID).
- Image upload requires `extension` as a separate form field alongside the file upload.

## License

[MIT](LICENSE)
