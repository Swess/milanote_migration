#!/usr/bin/env python3
"""
Milanote Markdown -> Obsidian-friendly Markdown

Features:
- One folder per page/board
- Downloads remote images (e.g., Milanote CDN URLs) into per-page assets folder
- Copies local referenced images into per-page assets folder
- Rewrites Markdown image links and HTML <img src="..."> to local relative paths
- Keeps fenced code blocks untouched
- Optional cookies.txt (Netscape format) + optional headers for authenticated downloads

Tested logic targets common Markdown/image patterns; no Milanote API needed.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import dataclasses
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Prefer requests if available; fallback to urllib
try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

try:
    import http.cookiejar as cookiejar
except Exception:
    cookiejar = None


IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".avif"
}


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def sanitize_fs_name(name: str, max_len: int = 120) -> str:
    """
    Sanitize a string to be safe as a folder/file name across OSes.
    """
    name = unicodedata.normalize("NFKC", name).strip()
    name = re.sub(r"\s+", " ", name)

    # Windows-forbidden chars: <>:"/\|?* plus control chars
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()

    # Avoid trailing dots/spaces (problematic on Windows)
    name = name.rstrip(" .")

    if not name:
        name = "Untitled"

    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")

    return name


def detect_title_from_markdown(md: str, fallback: str) -> str:
    """
    Best-effort: use the first H1 as folder/title, else fallback to filename stem.
    """
    # Skip frontmatter
    text = md
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            t = line[2:].strip()
            if t:
                return t
    return fallback


def iter_markdown_files(input_path: Path) -> List[Path]:
    if input_path.is_file() and input_path.suffix.lower() == ".md":
        return [input_path]
    if input_path.is_dir():
        return sorted([p for p in input_path.rglob("*.md") if p.is_file()])
    raise FileNotFoundError(f"Input path not found: {input_path}")


def is_remote_url(s: str) -> bool:
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


def strip_wrapping_angle_brackets(url: str) -> str:
    u = url.strip()
    if u.startswith("<") and u.endswith(">"):
        return u[1:-1].strip()
    return u


def url_basename(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        base = Path(urllib.parse.unquote(parsed.path)).name
        return base or "asset"
    except Exception:
        return "asset"


def split_fenced_code_blocks(md: str) -> List[Tuple[bool, str]]:
    """
    Splits Markdown into segments: (is_code_block, text).
    We treat fenced blocks ``` or ~~~ as code and do not rewrite inside them.
    """
    segments: List[Tuple[bool, str]] = []
    buf: List[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    fence_open_re = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)\n?$")

    lines = md.splitlines(keepends=True)
    for line in lines:
        if not in_fence:
            m = fence_open_re.match(line)
            if m:
                # flush non-code
                if buf:
                    segments.append((False, "".join(buf)))
                    buf = []
                in_fence = True
                fence = m.group(1)
                fence_char = fence[0]
                fence_len = len(fence)
                buf.append(line)
            else:
                buf.append(line)
        else:
            buf.append(line)
            # closing fence: same char repeated >= fence_len, with optional leading whitespace
            close_re = re.compile(r"^[ \t]*" + re.escape(fence_char) + r"{" + str(fence_len) + r",}[ \t]*\n?$")
            if close_re.match(line):
                segments.append((True, "".join(buf)))
                buf = []
                in_fence = False
                fence_char = ""
                fence_len = 0

    if buf:
        segments.append((in_fence, "".join(buf)))
    return segments


@dataclasses.dataclass
class AssetRecord:
    token: str
    kind: str  # "remote" | "local"
    original: str  # URL or local path as found in md
    resolved: str  # resolved absolute source path for local, or URL for remote
    dest_rel: str  # e.g., "assets/foo.png"
    dest_abs: str  # absolute on disk
    status: str = "pending"  # pending|downloaded|copied|failed|skipped
    error: Optional[str] = None


class Downloader:
    def __init__(
        self,
        cookies_path: Optional[Path] = None,
        cookie_header: Optional[str] = None,
        headers_path: Optional[Path] = None,
        user_agent: str = "milanote-md-to-obsidian/1.0",
        timeout_connect: int = 15,
        timeout_read: int = 120,
        retries: int = 3,
        retry_backoff: float = 1.6,
    ) -> None:
        self.timeout = (timeout_connect, timeout_read)
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.user_agent = user_agent

        self.headers: Dict[str, str] = {"User-Agent": user_agent}

        if headers_path:
            self.headers.update(self._load_headers_file(headers_path))

        if cookie_header:
            # Simple Cookie: header
            self.headers["Cookie"] = cookie_header.strip()

        self.session = None
        if requests is not None:
            self.session = requests.Session()
            self.session.headers.update(self.headers)
            if cookies_path:
                self._load_mozilla_cookies_into_requests(cookies_path)

    def _load_headers_file(self, p: Path) -> Dict[str, str]:
        out: Dict[str, str] = {}
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in raw:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
        return out

    def _load_mozilla_cookies_into_requests(self, cookies_path: Path) -> None:
        if cookiejar is None or requests is None or self.session is None:
            return
        cj = cookiejar.MozillaCookieJar(str(cookies_path))
        try:
            cj.load(ignore_discard=True, ignore_expires=True)
        except Exception as ex:
            eprint(f"[WARN] Failed to load cookies from {cookies_path}: {ex}")
            return

        # Transfer cookies into requests' jar
        for c in cj:
            try:
                self.session.cookies.set(
                    name=c.name,
                    value=c.value,
                    domain=c.domain,
                    path=c.path,
                )
            except Exception:
                continue

    def fetch_to_file(self, url: str, dest: Path, overwrite: bool = False) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Download URL to dest. Returns (success, content_type, error).
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not overwrite:
            return True, None, None

        # If requests is missing, fallback to urllib
        if requests is None or self.session is None:
            import urllib.request

            attempt = 0
            last_err = None
            while attempt < self.retries:
                attempt += 1
                try:
                    req = urllib.request.Request(url, headers=self.headers)
                    with urllib.request.urlopen(req, timeout=self.timeout[0]) as resp:
                        ct = resp.headers.get("Content-Type")
                        tmp = dest.with_suffix(dest.suffix + ".part")
                        with open(tmp, "wb") as f:
                            shutil.copyfileobj(resp, f)
                        tmp.replace(dest)
                    return True, ct, None
                except Exception as ex:
                    last_err = str(ex)
                    time.sleep(self.retry_backoff ** attempt)
            return False, None, last_err

        # requests path
        attempt = 0
        last_err = None
        while attempt < self.retries:
            attempt += 1
            try:
                r = self.session.get(url, stream=True, timeout=self.timeout, allow_redirects=True)
                if r.status_code >= 400:
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(self.retry_backoff ** attempt)
                    continue

                ct = r.headers.get("Content-Type")

                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                tmp.replace(dest)
                return True, ct, None
            except Exception as ex:
                last_err = str(ex)
                time.sleep(self.retry_backoff ** attempt)

        return False, None, last_err


class MilanoteToObsidianConverter:
    TOKEN_PREFIX = "@@MILANOTE_ASSET_"

    def __init__(
        self,
        source_md_path: Path,
        page_dir: Path,
        assets_dirname: str = "assets",
        downloader: Optional[Downloader] = None,
        download_linked_files: bool = False,
        keep_remote_on_failure: bool = True,
        overwrite: bool = False,
        max_workers: int = 6,
    ) -> None:
        self.source_md_path = source_md_path
        self.source_base_dir = source_md_path.parent
        self.page_dir = page_dir
        self.assets_dirname = assets_dirname
        self.assets_dir = page_dir / assets_dirname
        self.assets_dir.mkdir(parents=True, exist_ok=True)

        self.downloader = downloader or Downloader()
        self.download_linked_files = download_linked_files
        self.keep_remote_on_failure = keep_remote_on_failure
        self.overwrite = overwrite
        self.max_workers = max_workers

        self._token_counter = 0
        self.records: Dict[str, AssetRecord] = {}  # token -> record

    def _next_token(self) -> str:
        self._token_counter += 1
        return f"{self.TOKEN_PREFIX}{self._token_counter}@@"

    def _guess_extension(self, url: str, content_type: Optional[str]) -> str:
        # Try URL suffix first
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
        suf = Path(path).suffix.lower()
        if suf in IMAGE_EXTS:
            return suf

        # Try content type
        if content_type:
            ct = content_type.split(";")[0].strip().lower()
            ext = mimetypes.guess_extension(ct) or ""
            if ext == ".jpe":
                ext = ".jpg"
            if ext and ext.startswith("."):
                return ext

        # Fallback: if it looks like an image endpoint with no extension, pick .jpg as a conservative default
        return ".bin"

    def _make_dest_filename(self, url_or_path: str, ext_hint: Optional[str] = None) -> str:
        base = url_basename(url_or_path)
        base = sanitize_fs_name(base, max_len=80)

        stem = Path(base).stem
        stem = sanitize_fs_name(stem, max_len=70).replace(" ", "_")

        h = hashlib.sha256(url_or_path.encode("utf-8", errors="ignore")).hexdigest()[:10]

        ext = ext_hint or Path(base).suffix.lower()
        if ext and not ext.startswith("."):
            ext = "." + ext
        if not ext:
            ext = ".bin"

        return f"{stem}-{h}{ext}"

    def _should_treat_as_image_ref(self, url: str) -> bool:
        u = strip_wrapping_angle_brackets(url)
        if u.startswith("data:image/"):
            return False  # we won't download data URLs
        if not is_remote_url(u):
            # local paths may still be images; we decide based on suffix
            return Path(u).suffix.lower() in IMAGE_EXTS
        # remote: suffix check; if none, still treat as image if it's in an image tag
        suf = Path(urllib.parse.urlparse(u).path).suffix.lower()
        return (suf in IMAGE_EXTS) or (suf == "")

    def _looks_like_downloadable_file(self, url: str) -> bool:
        # Extend as needed; default focuses on images unless download_linked_files is enabled
        u = strip_wrapping_angle_brackets(url)
        if not is_remote_url(u):
            return False
        suf = Path(urllib.parse.urlparse(u).path).suffix.lower()
        if suf in IMAGE_EXTS:
            return True
        if self.download_linked_files:
            return True
        return False

    def _register_remote(self, url: str, kind: str = "remote") -> str:
        token = self._next_token()
        clean_url = strip_wrapping_angle_brackets(url)

        # Precompute a filename; final extension might be corrected after download by content-type,
        # but we keep stable naming. If extension is empty, use .bin and still save.
        dest_name = self._make_dest_filename(clean_url)
        dest_abs = str(self.assets_dir / dest_name)
        dest_rel = str(Path(self.assets_dirname) / dest_name)

        self.records[token] = AssetRecord(
            token=token,
            kind=kind,
            original=url,
            resolved=clean_url,
            dest_rel=dest_rel.replace("\\", "/"),
            dest_abs=dest_abs,
        )
        return token

    def _register_local_copy(self, ref: str) -> Optional[str]:
        """
        If ref is a local file path that exists relative to source md, register it for copying.
        Returns token or None.
        """
        ref_clean = strip_wrapping_angle_brackets(ref)
        # ignore anchors and mailto etc
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref_clean):
            return None

        # Normalize and resolve against md's folder
        candidate = (self.source_base_dir / ref_clean).resolve()
        try:
            # ensure candidate is actually under source_base_dir to avoid traversal
            _ = candidate.relative_to(self.source_base_dir.resolve())
        except Exception:
            return None

        if not candidate.exists() or not candidate.is_file():
            return None

        ext = candidate.suffix.lower() or ".bin"
        dest_name = self._make_dest_filename(str(candidate), ext_hint=ext)
        token = self._next_token()
        dest_abs = self.assets_dir / dest_name
        dest_rel = (Path(self.assets_dirname) / dest_name).as_posix()

        self.records[token] = AssetRecord(
            token=token,
            kind="local",
            original=ref,
            resolved=str(candidate),
            dest_rel=dest_rel,
            dest_abs=str(dest_abs),
        )
        return token

    def _rewrite_html_img_src(self, text: str) -> str:
        """
        Rewrites <img ... src="..."> occurrences to tokens.
        """
        img_re = re.compile(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'][^>]*>)', flags=re.IGNORECASE)

        def repl(m: re.Match) -> str:
            prefix, src, suffix = m.group(1), m.group(2), m.group(3)
            # Always treat as image reference
            if is_remote_url(src):
                token = self._register_remote(src, kind="remote")
                return f"{prefix}{token}{suffix}"

            local_token = self._register_local_copy(src)
            if local_token:
                return f"{prefix}{local_token}{suffix}"

            return m.group(0)

        return img_re.sub(repl, text)

    def _find_inline_link(self, s: str, start: int) -> Optional[Tuple[int, int, int, int]]:
        """
        Parses an inline Markdown link/image starting at s[start] which should be '[' or '!' before '['.

        Returns tuple:
          (link_start, link_end_exclusive, dest_span_start, dest_span_end)
        where dest_span includes optional <...> around the URL.

        If not a valid inline link, return None.
        """
        link_start = start
        i = start

        is_image = False
        if s[i] == "!":
            is_image = True
            i += 1
            if i >= len(s) or s[i] != "[":
                return None
        if s[i] != "[":
            return None

        # find closing bracket for [alt]
        j = i + 1
        while j < len(s):
            if s[j] == "]" and (j == 0 or s[j - 1] != "\\"):
                break
            j += 1
        if j >= len(s) or s[j] != "]":
            return None

        # must have '(' right after
        if j + 1 >= len(s) or s[j + 1] != "(":
            return None
        open_paren = j + 1

        # parse destination
        k = open_paren + 1
        while k < len(s) and s[k].isspace():
            k += 1
        if k >= len(s):
            return None

        dest_span_start = k
        dest_span_end = k

        if s[k] == "<":
            # <...> form
            end = s.find(">", k + 1)
            if end == -1:
                return None
            dest_span_end = end + 1
            after_dest = dest_span_end
        else:
            # unwrapped URL; allow nested parentheses within URL
            depth = 0
            end = k
            while end < len(s):
                c = s[end]
                if c == "\\" and end + 1 < len(s):
                    end += 2
                    continue
                if c == "(":
                    depth += 1
                    end += 1
                    continue
                if c == ")":
                    if depth == 0:
                        break
                    depth -= 1
                    end += 1
                    continue
                if c.isspace() and depth == 0:
                    break
                end += 1

            dest_span_end = end
            after_dest = end

        # Now parse optional title and find closing ')'
        t = after_dest
        while t < len(s) and s[t].isspace():
            t += 1

        def parse_quoted_title(pos: int, quote: str) -> int:
            pos += 1
            while pos < len(s):
                if s[pos] == "\\" and pos + 1 < len(s):
                    pos += 2
                    continue
                if s[pos] == quote:
                    return pos + 1
                pos += 1
            return pos

        def parse_paren_title(pos: int) -> int:
            # title enclosed in parentheses
            depth = 1
            pos += 1
            while pos < len(s) and depth > 0:
                if s[pos] == "\\" and pos + 1 < len(s):
                    pos += 2
                    continue
                if s[pos] == "(":
                    depth += 1
                elif s[pos] == ")":
                    depth -= 1
                pos += 1
            return pos

        if t < len(s) and s[t] in ("'", '"'):
            t = parse_quoted_title(t, s[t])
            while t < len(s) and s[t].isspace():
                t += 1
        elif t < len(s) and s[t] == "(":
            t = parse_paren_title(t)
            while t < len(s) and s[t].isspace():
                t += 1

        if t >= len(s) or s[t] != ")":
            # fallback: find the next ')'
            close = s.find(")", after_dest)
            if close == -1:
                return None
            link_end_excl = close + 1
        else:
            link_end_excl = t + 1

        return (link_start, link_end_excl, dest_span_start, dest_span_end)

    def _rewrite_markdown_inline_links(self, text: str) -> str:
        """
        Rewrites Markdown inline images and (optionally) links to tokens.
        Keeps everything else identical.
        """
        out: List[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "!" and i + 1 < len(text) and text[i + 1] == "[":
                parsed = self._find_inline_link(text, i)
                if not parsed:
                    out.append(ch)
                    i += 1
                    continue
                link_start, link_end_excl, dest_start, dest_end = parsed
                full = text[link_start:link_end_excl]
                dest_raw = text[dest_start:dest_end]
                dest_clean = strip_wrapping_angle_brackets(dest_raw)

                # This is an image by syntax
                if is_remote_url(dest_clean):
                    token = self._register_remote(dest_clean, kind="remote")
                    rewritten = full[: dest_start - link_start] + token + full[dest_end - link_start :]
                    out.append(rewritten)
                    i = link_end_excl
                    continue

                local_token = self._register_local_copy(dest_clean)
                if local_token:
                    rewritten = full[: dest_start - link_start] + local_token + full[dest_end - link_start :]
                    out.append(rewritten)
                    i = link_end_excl
                    continue

                out.append(full)
                i = link_end_excl
                continue

            if ch == "[":
                parsed = self._find_inline_link(text, i)
                if not parsed:
                    out.append(ch)
                    i += 1
                    continue
                link_start, link_end_excl, dest_start, dest_end = parsed
                full = text[link_start:link_end_excl]
                dest_raw = text[dest_start:dest_end]
                dest_clean = strip_wrapping_angle_brackets(dest_raw)

                # Regular link: only download if enabled (and it looks like a file worth downloading)
                if self._looks_like_downloadable_file(dest_clean):
                    token = self._register_remote(dest_clean, kind="remote")
                    rewritten = full[: dest_start - link_start] + token + full[dest_end - link_start :]
                    out.append(rewritten)
                    i = link_end_excl
                    continue

                # If it's a local file and download_linked_files enabled, we can copy it too
                if self.download_linked_files:
                    local_token = self._register_local_copy(dest_clean)
                    if local_token:
                        rewritten = full[: dest_start - link_start] + local_token + full[dest_end - link_start :]
                        out.append(rewritten)
                        i = link_end_excl
                        continue

                out.append(full)
                i = link_end_excl
                continue

            out.append(ch)
            i += 1

        return "".join(out)

    def rewrite(self, md_text: str) -> str:
        """
        Stage 1: replace asset refs with tokens while collecting a manifest.
        """
        segments = split_fenced_code_blocks(md_text)
        rewritten_segments: List[str] = []
        for is_code, seg in segments:
            if is_code:
                rewritten_segments.append(seg)
                continue
            seg2 = self._rewrite_html_img_src(seg)
            seg3 = self._rewrite_markdown_inline_links(seg2)
            rewritten_segments.append(seg3)
        return "".join(rewritten_segments)

    def _copy_local_asset(self, rec: AssetRecord) -> AssetRecord:
        try:
            src = Path(rec.resolved)
            dst = Path(rec.dest_abs)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and not self.overwrite:
                rec.status = "copied"
                return rec
            shutil.copy2(src, dst)
            rec.status = "copied"
            return rec
        except Exception as ex:
            rec.status = "failed"
            rec.error = str(ex)
            return rec

    def _download_remote_asset(self, rec: AssetRecord) -> AssetRecord:
        url = rec.resolved
        dest = Path(rec.dest_abs)

        ok, content_type, err = self.downloader.fetch_to_file(url, dest, overwrite=self.overwrite)
        if not ok:
            rec.status = "failed"
            rec.error = err
            return rec

        # If we saved as .bin but content-type suggests an image, rename to a better extension
        try:
            current_ext = dest.suffix.lower()
            if current_ext == ".bin" and content_type:
                better_ext = self._guess_extension(url, content_type)
                if better_ext != ".bin":
                    new_name = dest.with_suffix(better_ext)
                    # avoid collisions
                    if not new_name.exists() or self.overwrite:
                        dest.replace(new_name)
                        rec.dest_abs = str(new_name)
                        rec.dest_rel = (Path(self.assets_dirname) / new_name.name).as_posix()
        except Exception:
            pass

        rec.status = "downloaded"
        return rec

    def materialize_assets(self) -> None:
        """
        Stage 2: download/copy all registered assets.
        """
        jobs: List[AssetRecord] = list(self.records.values())
        if not jobs:
            return

        def run_one(r: AssetRecord) -> AssetRecord:
            if r.kind == "local":
                return self._copy_local_asset(r)
            return self._download_remote_asset(r)

        # Use threads: IO-bound
        max_workers = max(1, int(self.max_workers))
        with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for updated in ex.map(run_one, jobs):
                self.records[updated.token] = updated

    def finalize(self, rewritten_with_tokens: str) -> str:
        """
        Stage 3: replace tokens with local relative paths when available,
        else keep remote URLs (or keep token path) depending on keep_remote_on_failure.
        """
        out = rewritten_with_tokens

        # Replace longer tokens first (not strictly necessary here)
        for token, rec in sorted(self.records.items(), key=lambda kv: len(kv[0]), reverse=True):
            if rec.status in ("downloaded", "copied") and Path(rec.dest_abs).exists():
                replacement = rec.dest_rel
            else:
                if self.keep_remote_on_failure:
                    replacement = rec.original
                else:
                    replacement = rec.dest_rel
            out = out.replace(token, replacement)
        return out

def safe_unzip(zip_path: Path, dest_dir: Path) -> None:
    """
    Unzip safely (avoid Zip Slip path traversal).
    """
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            member_path = Path(member.filename)
            # skip directories
            if member.is_dir():
                continue
            # prevent absolute/parent paths
            if member_path.is_absolute() or ".." in member_path.parts:
                continue
            out_path = dest_dir / member_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member, "r") as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def ensure_frontmatter(md: str, source_name: str, original_path: str) -> str:
    if md.lstrip().startswith("---"):
        return md
    fm = (
        "---\n"
        f"source: {source_name}\n"
        f"original: {original_path}\n"
        "---\n\n"
    )
    return fm + md


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert Milanote Markdown exports into Obsidian-friendly folders with local assets.")
    ap.add_argument("--input", required=True, help="Path to a Milanote Markdown export file or a folder containing .md files.")
    ap.add_argument("--output", required=True, help="Output root folder (your Obsidian vault or a subfolder in it).")
    ap.add_argument("--subfolder", default="Milanote", help="Subfolder under output to place converted pages.")
    ap.add_argument("--assets-dirname", default="assets", help="Assets folder name inside each page folder.")
    ap.add_argument("--note-filename", default=None, help="Override note filename (default: derived from board title).")
    ap.add_argument("--use-h1-title", action="store_true", help="Name page folders using the first '# ' heading (H1) if present.")
    ap.add_argument("--download-linked-files", action="store_true", help="Also download remote file links (not just images).")
    ap.add_argument("--keep-remote-on-failure", action="store_true", help="If a download/copy fails, keep original URL in Markdown.")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite already-downloaded assets.")
    ap.add_argument("--max-workers", type=int, default=6, help="Parallel download workers.")
    ap.add_argument("--cookies", default=None, help="Path to cookies.txt in Netscape/MozillaCookieJar format (optional).")
    ap.add_argument("--cookie-header", default=None, help='Raw Cookie header value, e.g. "a=b; c=d" (optional).')
    ap.add_argument("--headers", default=None, help="Path to headers.txt file with lines like 'Authorization: Bearer ...' (optional).")
    ap.add_argument("--add-frontmatter", action="store_true", help="Add YAML frontmatter if missing.")
    ap.add_argument("--unzip-sibling-zip", action="store_true", help="If a sibling .zip exists with same base name, unzip into assets folder.")

    args = ap.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()
    out_base = output_root / sanitize_fs_name(args.subfolder)

    input_root = input_path if input_path.is_dir() else input_path.parent

    md_files = iter_markdown_files(input_path)
    if not md_files:
        eprint("[ERROR] No Markdown files found.")
        return 2

    cookies_path = Path(args.cookies).expanduser().resolve() if args.cookies else None
    headers_path = Path(args.headers).expanduser().resolve() if args.headers else None

    downloader = Downloader(
        cookies_path=cookies_path,
        cookie_header=args.cookie_header,
        headers_path=headers_path,
    )

    # ── Pass 1: collect all (page_dir, note_filename, title) so that parents
    #    can link to their children before writing any files.
    # ─────────────────────────────────────────────────────────────────────────
    @dataclasses.dataclass
    class PageMeta:
        md_path: Path
        page_dir: Path
        note_filename: str   # e.g. "Design.md"
        title: str

    pages: List[PageMeta] = []

    eprint(f"[INFO] Found {len(md_files)} Markdown file(s).")
    for md_path in md_files:
        raw = md_path.read_text(encoding="utf-8", errors="replace")

        title = detect_title_from_markdown(raw, md_path.stem)

        rel_parent = md_path.parent.relative_to(input_root)
        page_dir = out_base / rel_parent

        if args.use_h1_title:
            sanitized_title = sanitize_fs_name(title)
            if sanitized_title != page_dir.name:
                page_dir = page_dir.parent / sanitized_title

        # Note filename: explicit override > title-derived > stem fallback
        if args.note_filename:
            note_filename = sanitize_fs_name(args.note_filename, max_len=80)
        else:
            note_filename = sanitize_fs_name(title, max_len=80) + ".md"

        pages.append(PageMeta(
            md_path=md_path,
            page_dir=page_dir,
            note_filename=note_filename,
            title=title,
        ))

    # ── Pass 2: build a map of page_dir -> [child PageMeta, ...]
    # A child is any page whose page_dir.parent == this page's page_dir.
    # ─────────────────────────────────────────────────────────────────────────
    dir_to_meta: Dict[Path, PageMeta] = {p.page_dir: p for p in pages}

    children_map: Dict[Path, List[PageMeta]] = {p.page_dir: [] for p in pages}
    for p in pages:
        parent_dir = p.page_dir.parent
        if parent_dir in children_map:
            children_map[parent_dir].append(p)

    # ── Pass 3: convert and write each page
    # ─────────────────────────────────────────────────────────────────────────
    for meta in pages:
        raw = meta.md_path.read_text(encoding="utf-8", errors="replace")
        meta.page_dir.mkdir(parents=True, exist_ok=True)

        converter = MilanoteToObsidianConverter(
            source_md_path=meta.md_path,
            page_dir=meta.page_dir,
            assets_dirname=args.assets_dirname,
            downloader=downloader,
            download_linked_files=args.download_linked_files,
            keep_remote_on_failure=args.keep_remote_on_failure,
            overwrite=args.overwrite,
            max_workers=args.max_workers,
        )

        if args.unzip_sibling_zip:
            sibling_zip = meta.md_path.with_suffix(".zip")
            if sibling_zip.exists():
                eprint(f"[INFO] Unzipping sibling ZIP: {sibling_zip.name} -> {converter.assets_dir}")
                safe_unzip(sibling_zip, converter.assets_dir)

        rewritten = converter.rewrite(raw)
        converter.materialize_assets()
        final_md = converter.finalize(rewritten)

        # Prepend board screenshot PNG if it exists alongside the .md file
        board_png = meta.md_path.with_suffix(".png")
        if board_png.exists():
            dest_png = converter.assets_dir / board_png.name
            if not dest_png.exists() or args.overwrite:
                shutil.copy2(board_png, dest_png)
            png_rel = f"{args.assets_dirname}/{board_png.name}"
            final_md = f"![Board Screenshot]({png_rel})\n\n" + final_md

        # Append child board links if this page has children
        child_pages = children_map.get(meta.page_dir, [])
        if child_pages:
            child_links = "\n\n---\n\n## Child Boards\n\n"
            for child in sorted(child_pages, key=lambda c: c.title.lower()):
                # Obsidian wiki-link: relative path from this note's folder
                # Since children live one level deeper, the link is just:
                #   [[ChildFolder/ChildNote|Child Title]]
                rel_note = child.page_dir.name + "/" + child.note_filename[:-3]  # strip .md for wiki-link
                child_links += f"- [[{rel_note}|{child.title}]]\n"
            final_md = final_md.rstrip() + child_links

        if args.add_frontmatter:
            final_md = ensure_frontmatter(final_md, "milanote", str(meta.md_path))

        note_path = meta.page_dir / meta.note_filename
        note_path.write_text(final_md, encoding="utf-8")

        total = len(converter.records)
        ok = sum(1 for r in converter.records.values() if r.status in ("downloaded", "copied"))
        failed = sum(1 for r in converter.records.values() if r.status == "failed")
        eprint(f"[DONE] {meta.md_path.name} -> {note_path} | assets: {ok}/{total} ok, {failed} failed")

    eprint(f"[INFO] Output written to: {out_base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
