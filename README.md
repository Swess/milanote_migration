# Milanote Migration

Export your Milanote boards and convert them into an Obsidian-compatible vault. The pipeline has two steps:

1. **`milanote_export.py`** -- Browser automation that logs into Milanote and recursively exports every board as Markdown, PNG, or both.
2. **`milanote_to_obsidian.py`** -- Converts the exported Markdown files into Obsidian-friendly structure with local assets, wiki-links, and optional frontmatter.

## Requirements

Python 3.10+ and [Playwright](https://playwright.dev/python/):

```bash
pip install playwright
python -m playwright install chromium
```

The conversion script optionally uses `requests` for authenticated downloads. If not installed, it falls back to `urllib`.

## Step 1: Export from Milanote

```bash
python milanote_export.py \
  --email you@example.com \
  --password YOUR_PASSWORD \
  --root-url https://app.milanote.com/BOARD_ID
```

This logs in, navigates to the root board, exports it, then recursively does the same for every child board. The board hierarchy is preserved as nested folders on disk.

### Options

| Flag | Default | Description |
|---|---|---|
| `--email` | *(required)* | Milanote login email |
| `--password` | *(required)* | Milanote login password |
| `--root-url` | *(required)* | URL of the root board to export |
| `--output` | `milanote_export` | Output directory |
| `--mode` | `png` | `markdown`, `png`, or `both` |
| `--headless` / `--no-headless` | `--headless` | Show the browser window with `--no-headless` for debugging |

### Export modes

- **`markdown`** -- Downloads each board as a `.md` file.
- **`png`** -- Downloads each board as a `.png` screenshot.
- **`both`** -- Downloads both formats for every board.

### Interactive controls

While running, press **P + Enter** to pause/resume the export.

### Resumability

Already-exported boards are detected by filename and skipped. In `both` mode, a board is only skipped if *both* the `.md` and `.png` files exist. You can safely re-run the script to pick up where you left off.

## Step 2: Convert to Obsidian

```bash
python milanote_to_obsidian.py \
  --input ./milanote_export \
  --output ~/Obsidian/MyVault
```

This reads the exported Markdown files and produces a vault-ready folder structure:

- One folder per board, nested to match the original hierarchy
- Remote images downloaded into per-page `assets/` folders
- Markdown image/link references rewritten to local relative paths
- Wiki-links (`[[Child/Note|Title]]`) generated for child boards
- Fenced code blocks left untouched

### Options

| Flag | Default | Description |
|---|---|---|
| `--input` | *(required)* | Path to a `.md` file or folder of exported `.md` files |
| `--output` | *(required)* | Output root folder (your Obsidian vault) |
| `--subfolder` | `Milanote` | Subfolder under output for converted pages |
| `--assets-dirname` | `assets` | Name of the per-page assets folder |
| `--note-filename` | *(auto)* | Override the output note filename |
| `--use-h1-title` | off | Name page folders from the first `# ` heading |
| `--download-linked-files` | off | Download remote file links, not just images |
| `--keep-remote-on-failure` | off | Keep original URL if a download fails |
| `--overwrite` | off | Re-download assets that already exist |
| `--max-workers` | `6` | Number of parallel download threads |
| `--cookies` | none | Path to `cookies.txt` (Netscape/Mozilla format) |
| `--cookie-header` | none | Raw `Cookie` header value |
| `--headers` | none | Path to `headers.txt` with custom HTTP headers |
| `--add-frontmatter` | off | Inject YAML frontmatter if missing |
| `--unzip-sibling-zip` | off | Unzip any sibling `.zip` into the assets folder |

## Full pipeline example

```bash
# Export everything as both Markdown and PNG
python milanote_export.py \
  --email you@example.com \
  --password YOUR_PASSWORD \
  --root-url https://app.milanote.com/BOARD_ID \
  --mode both

# Convert the Markdown export into an Obsidian vault
python milanote_to_obsidian.py \
  --input ./milanote_export \
  --output ~/Obsidian/MyVault \
  --subfolder "Milanote" \
  --use-h1-title \
  --add-frontmatter \
  --unzip-sibling-zip
```

After conversion, open the output folder in Obsidian as a vault. Board screenshots (PNG) are automatically embedded if they exist alongside the Markdown files.

## Output structure

```
milanote_export/                  # Raw export (step 1)
  Root_Board/
    Root_Board.md
    Root_Board.png
    Child_Board_A/
      Child_Board_A.md
      Child_Board_A.png

~/Obsidian/MyVault/Milanote/      # Obsidian vault (step 2)
  Root Board/
    Root Board.md
    assets/
      image1.png
      screenshot.png
    Child Board A/
      Child Board A.md
      assets/
        diagram.png
```
