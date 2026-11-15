#
#            PySceneDetect: Python-Based Video Scene Detector
#   -------------------------------------------------------------------
#     [  Site:    https://scenedetect.com                           ]
#     [  Docs:    https://scenedetect.com/docs/                     ]
#     [  Github:  https://github.com/Breakthrough/PySceneDetect/    ]
#
# Copyright (C) 2026 Brandon Castellano <http://www.bcastell.com>.
# PySceneDetect is licensed under the BSD 3-Clause License; see the
# included LICENSE file, or visit one of the above pages for details.
#
"""Static site builder, link verifier, and preview server for scenedetect.com."""

from __future__ import annotations

import argparse
import html
import html.parser
import http.server
import json
import posixpath
import re
import shutil
import struct
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2
import markdown
import yaml
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import BashLexer, PythonLexer

SITE_URL = "https://www.scenedetect.com"
REPO_URL = "https://github.com/Breakthrough/PySceneDetect"
PYPI_URL = "https://pypi.org/project/scenedetect/"
DEFAULT_VERSION = "v0.7.3"
DEFAULT_RELEASE_DATE = "November 8, 2026"
DEFAULT_DESCRIPTION = (
    "Fast, frame-accurate video scene cut detection and splitting tool in Python "
    "with command-line interface and API."
)

WEBSITE_DIR = Path(__file__).resolve().parent
PAGES_DIR = WEBSITE_DIR / "pages"
IMG_SRC_DIR = PAGES_DIR / "img"
THEME_DIR = WEBSITE_DIR / "theme"
BUILD_DIR = WEBSITE_DIR / "build"

NAV_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "PySceneDetect",
        [
            ("Home", "index.md", ""),
            ("Features", "features.md", "features"),
            ("Download", "download.md", "download"),
            ("Changelog", "changelog.md", "changelog"),
        ],
    ),
    (
        "Reference",
        [
            ("Documentation", "docs.md", "docs"),
            ("Command-Line", "cli.md", "cli"),
            ("Python API", "api.md", "api"),
            ("Benchmarks", "benchmarks.md", "benchmarks"),
        ],
    ),
    (
        "Support",
        [
            ("FAQ", "faq.md", "faq"),
            ("Bugs & Contributing", "contributing.md", "contributing"),
            ("Development & Support", "supporting.md", "supporting"),
        ],
    ),
    (
        "Resources",
        [
            ("Similar Projects", "similar.md", "similar"),
            ("Research & Literature", "literature.md", "literature"),
            ("License & Copyright", "copyright.md", "copyright"),
        ],
    ),
]

TOP_NAV_PRIMARY: list[tuple[str, str]] = [
    ("Overview", ""),
    ("Features", "features"),
    ("Download", "download"),
    ("CLI", "cli"),
    ("Python API", "api"),
    ("Benchmarks", "benchmarks"),
    ("FAQ", "faq"),
]

TOP_NAV_MOBILE_EXTRA: list[tuple[str, str]] = [
    ("Documentation", "docs"),
    ("Changelog", "changelog"),
    ("Contributing", "contributing"),
    ("Support", "supporting"),
    ("Similar Projects", "similar"),
    ("Literature", "literature"),
    ("License", "copyright"),
]

HERO_SNIPPETS: list[tuple[str, str, str, str, str]] = [
    (
        "python",
        "Python API",
        "AdaptiveDetector",
        "python",
        (
            "from scenedetect import (\n"
            "    detect, AdaptiveDetector, split_video_ffmpeg,\n"
            ")\n\n"
            "# Detect shot boundaries and split with ffmpeg\n"
            'scenes = detect("goldeneye.mp4", AdaptiveDetector())\n'
            'split_video_ffmpeg("goldeneye.mp4", scenes)\n'
        ),
    ),
    (
        "cli",
        "Command Line",
        "scenedetect",
        "bash",
        (
            "# Split footage on every detected shot cut\n"
            "scenedetect -i goldeneye.mp4 split-video\n\n"
            "# Export CSV cut list, thumbnails, and HTML sheet\n"
            "scenedetect -i goldeneye.mp4 list-scenes save-images save-html\n"
        ),
    ),
    (
        "docker",
        "Docker",
        "ghcr.io",
        "bash",
        (
            "# Full CLI + ffmpeg + mkvmerge + PyAV in one container\n"
            'docker run --rm -v "$(pwd):/files" \\\n'
            "    ghcr.io/breakthrough/pyscenedetect \\\n"
            "    -i /files/goldeneye.mp4 split-video -o /files\n"
        ),
    ),
]

MONTH_ABBREVS: dict[str, str] = {
    "January": "Jan",
    "February": "Feb",
    "March": "Mar",
    "April": "Apr",
    "May": "May",
    "June": "Jun",
    "July": "Jul",
    "August": "Aug",
    "September": "Sep",
    "October": "Oct",
    "November": "Nov",
    "December": "Dec",
}

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
SPHINX_REL_RE = re.compile(r"^(?:latest|head|\d+\.\d+(?:\.\d+)?)(?:/.*)?$")
SPHINX_ABS_RE = re.compile(r"^/docs/(?:latest|head|\d+\.\d+(?:\.\d+)?)(?:/.*)?$")
IMG_PATH_RE = re.compile(r"^(?:\./|\.\./|/)*img/(.+)$")
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:")

KNOWN_SLUGS: set[str] = {slug for _, items in NAV_GROUPS for _, _, slug in items}


@dataclass(frozen=True)
class PageSpec:
    """Static definition of a single page in the canonical site tree."""

    group: str
    title: str
    src_name: str
    slug: str

    @property
    def is_home(self) -> bool:
        return self.slug == ""

    @property
    def url_path(self) -> str:
        return "/" if self.is_home else f"/{self.slug}/"

    @property
    def root_prefix(self) -> str:
        return "" if self.is_home else "../"

    @property
    def out_rel_path(self) -> Path:
        if self.is_home:
            return Path("index.html")
        return Path(self.slug) / "index.html"


@dataclass
class TocItem:
    """Right-hand 'On this page' anchor entry."""

    id: str
    title: str
    children: list[TocItem] = field(default_factory=list)


def iter_page_specs() -> list[PageSpec]:
    """Flatten NAV_GROUPS into the ordered 14-page sequence."""
    specs: list[PageSpec] = []
    for group_title, entries in NAV_GROUPS:
        for title, src_name, slug in entries:
            specs.append(PageSpec(group=group_title, title=title, src_name=src_name, slug=slug))
    return specs


def page_href(target_slug: str, root_prefix: str) -> str:
    """Compute a clean relative (or root-anchored for 404) href to target_slug."""
    if root_prefix == "/":
        return "/" if target_slug == "" else f"/{target_slug}/"
    if target_slug == "":
        return "./" if root_prefix == "" else root_prefix
    return f"{root_prefix}{target_slug}/"


def parse_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """Extract optional YAML frontmatter block and return (metadata, markdown_body)."""
    match = FRONTMATTER_RE.match(raw_text)
    if not match:
        return {}, raw_text
    parsed = yaml.safe_load(match.group(1))
    meta = parsed if isinstance(parsed, dict) else {}
    return meta, raw_text[match.end() :]


def shorten_date(date_str: str) -> str:
    """Shorten full English month names for compact UI pills."""
    result = date_str
    for full_name, short_name in MONTH_ABBREVS.items():
        if result.startswith(full_name + " "):
            return short_name + result[len(full_name) :]
    return result


def strip_tags(fragment: str) -> str:
    """Remove HTML tags and unescape entities into collapsed plain text."""
    no_tags = re.sub(r"<[^>]+>", " ", fragment)
    unescaped = html.unescape(no_tags).replace("\xa0", " ")
    return re.sub(r"\s+", " ", unescaped).strip()


def slugify_heading(text: str, used_ids: set[str]) -> str:
    """Generate a unique URL-safe heading id."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
    candidate = base
    counter = 2
    while candidate in used_ids:
        candidate = f"{base}-{counter}"
        counter += 1
    used_ids.add(candidate)
    return candidate


def get_image_dimensions(img_path: Path) -> tuple[int, int] | None:
    """Read intrinsic (width, height) in pixels using pure Python stdlib."""
    if not img_path.is_file():
        return None
    data = img_path.read_bytes()
    suffix = img_path.suffix.lower()

    if suffix == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)

    if suffix == ".ico" and len(data) >= 22:
        reserved, ico_type, count = struct.unpack("<HHH", data[:6])
        if reserved == 0 and ico_type == 1 and count >= 1:
            best_w, best_h = 0, 0
            for idx in range(count):
                offset = 6 + idx * 16
                if offset + 2 > len(data):
                    break
                w = data[offset] or 256
                h = data[offset + 1] or 256
                if w * h > best_w * best_h:
                    best_w, best_h = w, h
            if best_w > 0 and best_h > 0:
                return best_w, best_h

    if suffix in {".jpg", ".jpeg"} and len(data) >= 4 and data[:2] == b"\xff\xd8":
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            while marker == 0xFF and idx + 2 < len(data):
                idx += 1
                marker = data[idx + 1]
            if marker in {0xD8, 0xD9}:
                idx += 2
                continue
            if idx + 4 > len(data):
                break
            seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3} and idx + 9 <= len(data):
                height, width = struct.unpack(">HH", data[idx + 5 : idx + 9])
                return int(width), int(height)
            idx += 2 + seg_len

    if suffix == ".svg":
        head_txt = data[:4096].decode("utf-8", errors="ignore")
        svg_match = re.search(r"<svg\b[^>]*>", head_txt, re.IGNORECASE | re.DOTALL)
        if svg_match:
            svg_tag = svg_match.group(0)
            w_px = _parse_svg_length(svg_tag, "width")
            h_px = _parse_svg_length(svg_tag, "height")
            if w_px and h_px:
                return w_px, h_px
            vb_match = re.search(
                r'viewBox\s*=\s*["\']\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)',
                svg_tag,
                re.IGNORECASE,
            )
            if vb_match:
                vw = max(1, round(float(vb_match.group(1))))
                vh = max(1, round(float(vb_match.group(2))))
                return vw, vh

    return None


def _parse_svg_length(svg_tag: str, attr_name: str) -> int | None:
    """Parse an SVG width/height attribute (px, pt, or unitless) to integer pixels."""
    match = re.search(
        rf'\b{attr_name}\s*=\s*["\']\s*([\d.]+)\s*(px|pt|)?\s*["\']',
        svg_tag,
        re.IGNORECASE,
    )
    if not match:
        return None
    val = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit == "pt":
        val *= 96.0 / 72.0
    return max(1, round(val))


def rewrite_internal_href(href: str, current_slug: str, root_prefix: str) -> str:
    """Normalize Markdown and relative internal links to canonical built paths."""
    raw = href.strip()
    if not raw or raw.startswith("#") or raw.startswith(EXTERNAL_SCHEMES):
        return href

    path_part, sep, frag = raw.partition("#")
    suffix = f"#{frag}" if sep else ""
    if not path_part:
        return href

    if path_part.startswith("/docs/"):
        rem = path_part[len("/docs/") :]
        if rem and SPHINX_REL_RE.match(rem):
            return f"/docs/{rem}{suffix}"

    base_dir = "/" if current_slug == "" else f"/{current_slug}/"
    joined = posixpath.normpath(posixpath.join(base_dir, path_part))
    if path_part.endswith("/") and not joined.endswith("/"):
        joined += "/"

    if joined.startswith("/docs/"):
        rem = joined[len("/docs/") :]
        if rem and SPHINX_REL_RE.match(rem):
            return f"/docs/{rem}{suffix}"

    clean_path = path_part.split("?", 1)[0]
    img_match = IMG_PATH_RE.match(clean_path)
    if img_match:
        return f"{root_prefix}img/{img_match.group(1)}{suffix}"

    if clean_path.endswith(".md"):
        stem = posixpath.basename(clean_path)[:-3]
        if stem == "index":
            return f"{page_href('', root_prefix)}{suffix}"
        if stem in KNOWN_SLUGS:
            return f"{page_href(stem, root_prefix)}{suffix}"

    if joined == "/":
        return f"{page_href('', root_prefix)}{suffix}"

    slug_candidate = joined.strip("/")
    if slug_candidate in KNOWN_SLUGS:
        return f"{page_href(slug_candidate, root_prefix)}{suffix}"

    return href


def _parse_attrs(attr_str: str) -> list[tuple[str, str | None]]:
    """Parse HTML tag attributes preserving order."""
    attrs: list[tuple[str, str | None]] = []
    pattern = re.compile(
        r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
    )
    for match in pattern.finditer(attr_str):
        name = match.group(1)
        if match.group(2) is not None:
            val: str | None = match.group(2)
        elif match.group(3) is not None:
            val = match.group(3)
        elif match.group(4) is not None:
            val = match.group(4)
        else:
            val = None
        attrs.append((name, val))
    return attrs


def _format_attrs(attrs: list[tuple[str, str | None]]) -> str:
    """Serialize HTML tag attributes cleanly."""
    parts: list[str] = []
    for name, val in attrs:
        if val is None:
            parts.append(name)
        else:
            escaped = html.escape(html.unescape(val), quote=True)
            parts.append(f'{name}="{escaped}"')
    return (" " + " ".join(parts)) if parts else ""


def postprocess_html(
    rendered_html: str,
    spec: PageSpec,
    img_dims: dict[str, tuple[int, int]],
) -> str:
    """Apply accessibility, CLS, code-block, table, heading, and link transforms."""
    out = rendered_html

    # On the home page, strip any legacy top logo banner if present in index.md so it
    # does not duplicate the Style A hero header.
    if spec.is_home:
        out = re.sub(
            r"^\s*(?:<p>\s*)?<img\b[^>]*pyscenedetect_logo_small\.png[^>]*>\s*(?:</p>)?\s*",
            "",
            out,
            count=1,
            flags=re.IGNORECASE,
        )

    # Protect <pre>...</pre> blocks while transforming prose headings, tables, and links,
    # and normalize <pre tabindex="0"><code>...</code></pre>.
    pre_blocks: list[str] = []

    def _stash_pre(match: re.Match[str]) -> str:
        raw_attrs = match.group(1)
        inner = match.group(2)
        attrs = _parse_attrs(raw_attrs)
        attr_names = {k.lower() for k, _ in attrs}
        if "tabindex" not in attr_names:
            attrs.append(("tabindex", "0"))
        stripped = inner.lstrip()
        if stripped.startswith("<span></span>"):
            inner = inner.replace("<span></span>", "", 1)
            stripped = inner.lstrip()
        if not re.match(r"^<code\b", stripped, re.IGNORECASE):
            inner = f"<code>{inner}</code>"
        normalized_pre = f"<pre{_format_attrs(attrs)}>{inner}</pre>"
        token = f"__PSD_PRE_BLOCK_{len(pre_blocks)}__"
        pre_blocks.append(normalized_pre)
        return token

    out = re.sub(r"<pre\b([^>]*)>(.*?)</pre>", _stash_pre, out, flags=re.DOTALL | re.IGNORECASE)

    # Normalize heading hierarchy (single <h1> per page) and ensure every heading has an id.
    used_ids: set[str] = set()
    for existing_id in re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', out):
        used_ids.add(existing_id)

    seen_h1 = False

    def _normalize_heading(match: re.Match[str]) -> str:
        nonlocal seen_h1
        level = int(match.group(1))
        attrs = _parse_attrs(match.group(2))
        inner = match.group(3)

        if spec.is_home and level == 1:
            level = 2
        elif not spec.is_home and level == 1:
            if seen_h1:
                level = 2
            else:
                seen_h1 = True

        has_id = any(k.lower() == "id" and v for k, v in attrs)
        if not has_id:
            plain = strip_tags(inner)
            new_id = slugify_heading(plain, used_ids)
            attrs.insert(0, ("id", new_id))

        return f"<h{level}{_format_attrs(attrs)}>{inner}</h{level}>"

    out = re.sub(
        r"<h([1-6])\b([^>]*)>(.*?)</h\1>",
        _normalize_heading,
        out,
        flags=re.DOTALL | re.IGNORECASE,
    )

    if not spec.is_home and not seen_h1:
        title_id = slugify_heading(spec.title, used_ids)
        out = f'<h1 id="{title_id}">{html.escape(spec.title)}</h1>\n' + out

    # Upgrade raw <table> elements with <thead>/<th scope="col"> and wrap in .table-wrapper.
    out = _wrap_tables_with_regions(out)

    # Rewrite <a href="..."> internal links.
    def _rewrite_anchor(match: re.Match[str]) -> str:
        attrs = _parse_attrs(match.group(1))
        new_attrs: list[tuple[str, str | None]] = []
        for k, v in attrs:
            if k.lower() == "href" and v is not None:
                v = rewrite_internal_href(v, spec.slug, spec.root_prefix)
            elif k.lower() == "alt":
                # Drop invalid alt attribute on <a> tags if present in legacy markdown.
                continue
            new_attrs.append((k, v))
        return f"<a{_format_attrs(new_attrs)}>"

    out = re.sub(r"<a\b([^>]*)>", _rewrite_anchor, out, flags=re.IGNORECASE)

    # Rewrite <img ...> src, inject width/height from website/pages/img/*, ensure alt & lazy load.
    def _rewrite_img(match: re.Match[str]) -> str:
        attrs = _parse_attrs(match.group(1))
        attr_map = {k.lower(): v for k, v in attrs}
        src = attr_map.get("src") or ""
        local_img_name: str | None = None

        if src and not src.startswith(EXTERNAL_SCHEMES):
            clean_src = src.split("#", 1)[0].split("?", 1)[0]
            img_match = IMG_PATH_RE.match(clean_src)
            if img_match:
                local_img_name = img_match.group(1)
                src = f"{spec.root_prefix}img/{local_img_name}"

        rebuilt: list[tuple[str, str | None]] = []
        has_alt = False
        has_width = False
        has_height = False
        has_loading = False

        for k, v in attrs:
            kl = k.lower()
            if kl == "src":
                rebuilt.append(("src", src))
            elif kl == "alt":
                has_alt = True
                rebuilt.append((k, v if v is not None else ""))
            elif kl == "width" and v:
                has_width = True
                rebuilt.append((k, v))
            elif kl == "height" and v:
                has_height = True
                rebuilt.append((k, v))
            elif kl == "loading" and v:
                has_loading = True
                rebuilt.append((k, v))
            elif kl == "style" and v and v.replace(" ", "").rstrip(";") == "max-width:100%":
                continue
            else:
                rebuilt.append((k, v))

        if not has_alt:
            rebuilt.append(("alt", ""))

        if local_img_name and local_img_name in img_dims:
            w, h = img_dims[local_img_name]
            if not has_width:
                rebuilt.append(("width", str(w)))
            if not has_height:
                rebuilt.append(("height", str(h)))

        if not has_loading and attr_map.get("fetchpriority") != "high":
            rebuilt.append(("loading", "lazy"))

        return f"<img{_format_attrs(rebuilt)}>"

    out = re.sub(r"<img\b([^>]*?)\s*/?>", _rewrite_img, out, flags=re.IGNORECASE)

    # Restore stashed <pre> blocks.
    for idx, pre_html in enumerate(pre_blocks):
        out = out.replace(f"__PSD_PRE_BLOCK_{idx}__", pre_html)

    return out


def _upgrade_table_semantics(table_html: str) -> str:
    """Ensure <thead>/<th scope='col'> semantics on both Markdown and raw HTML tables."""
    inner_upgraded = table_html
    if not re.search(r"<thead\b", inner_upgraded, re.IGNORECASE):
        m_open = re.match(r"(<table\b[^>]*>)(.*)(</table>)\Z", inner_upgraded, re.DOTALL)
        if m_open:
            t_open, t_body, t_close = m_open.group(1), m_open.group(2), m_open.group(3)
            rows = list(re.finditer(r"<tr\b[^>]*>.*?</tr>", t_body, re.DOTALL | re.IGNORECASE))
            if rows:
                first_row = rows[0].group(0)
                has_th = bool(re.search(r"<th\b", first_row, re.IGNORECASE))
                has_td = bool(re.search(r"<td\b", first_row, re.IGNORECASE))
                if has_th and not has_td:
                    rest = t_body[rows[0].end() :].strip()
                    tbody_part = f"\n<tbody>\n{rest}\n</tbody>" if rest else ""
                    inner_upgraded = (
                        f"{t_open}\n<thead>\n{first_row}\n</thead>{tbody_part}\n{t_close}"
                    )

    def _scope_thead(match: re.Match[str]) -> str:
        thead_block = match.group(0)

        def _add_col_scope(th_match: re.Match[str]) -> str:
            attrs = _parse_attrs(th_match.group(1))
            if not any(k.lower() == "scope" for k, _ in attrs):
                attrs.insert(0, ("scope", "col"))
            return f"<th{_format_attrs(attrs)}>"

        return re.sub(r"<th\b([^>]*)>", _add_col_scope, thead_block, flags=re.IGNORECASE)

    return re.sub(
        r"<thead\b[^>]*>.*?</thead>",
        _scope_thead,
        inner_upgraded,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _wrap_tables_with_regions(html_text: str) -> str:
    """Wrap every <table> in an accessible scrollable .table-wrapper named by preceding heading."""
    token_re = re.compile(
        r"(<div\b[^>]*class\s*=\s*[\"'][^\"']*table-wrapper[^\"']*[\"'][^>]*>.*?</div>)"
        r"|(<h[1-6]\b[^>]*>.*?</h[1-6]>)"
        r"|(<table\b[^>]*>.*?</table>)",
        re.DOTALL | re.IGNORECASE,
    )
    last_heading = "Data table"
    pieces: list[str] = []
    pos = 0

    for match in token_re.finditer(html_text):
        pieces.append(html_text[pos : match.start()])
        pos = match.end()
        if match.group(1):
            pieces.append(match.group(1))
        elif match.group(2):
            h_text = strip_tags(match.group(2))
            if h_text:
                last_heading = h_text
            pieces.append(match.group(2))
        elif match.group(3):
            upgraded = _upgrade_table_semantics(match.group(3))
            label = html.escape(last_heading, quote=True)
            pieces.append(
                f'<div tabindex="0" role="region" aria-label="{label}" '
                f'class="table-wrapper">{upgraded}</div>'
            )

    pieces.append(html_text[pos:])
    return "".join(pieces)


def extract_toc(html_text: str) -> list[TocItem]:
    """Extract h2 and nested h3 headings from final article HTML for 'On this page'."""
    items: list[TocItem] = []
    heading_re = re.compile(r"<h([23])\b([^>]*)>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
    for match in heading_re.finditer(html_text):
        level = int(match.group(1))
        attrs = dict(_parse_attrs(match.group(2)))
        hid = attrs.get("id") or ""
        title = strip_tags(match.group(3))
        if not hid or not title:
            continue
        if level == 2:
            items.append(TocItem(id=hid, title=title))
        elif level == 3:
            if items:
                items[-1].children.append(TocItem(id=hid, title=title))
            else:
                items.append(TocItem(id=hid, title=title))
    return items


def extract_search_entries(spec: PageSpec, html_text: str) -> list[dict[str, str]]:
    """Chunk a page's HTML by h1/h2/h3 sections for instant client-side jump search."""
    heading_re = re.compile(r"<h([123])\b([^>]*)>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
    matches = list(heading_re.finditer(html_text))
    entries: list[dict[str, str]] = []

    if not matches:
        body_txt = strip_tags(html_text)
        if body_txt:
            entries.append(
                {
                    "title": spec.title,
                    "section": spec.title,
                    "url": spec.url_path,
                    "text": body_txt[:600],
                }
            )
        return entries

    for idx, match in enumerate(matches):
        level = int(match.group(1))
        attrs = dict(_parse_attrs(match.group(2)))
        hid = attrs.get("id") or ""
        sec_title = strip_tags(match.group(3)) or spec.title
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(html_text)
        chunk_text = strip_tags(html_text[start:end])
        if not chunk_text and level == 1 and idx == 0 and spec.is_home:
            continue
        url = spec.url_path if (level == 1 and idx == 0) or not hid else f"{spec.url_path}#{hid}"
        entries.append(
            {
                "title": spec.title,
                "section": sec_title,
                "url": url,
                "text": chunk_text[:600],
            }
        )

    if spec.is_home:
        entries.insert(
            0,
            {
                "title": "Home",
                "section": "Overview & Quickstart",
                "url": "/",
                "text": (
                    "Fast, frame-accurate shot and scene cut detection for video. "
                    "pip install scenedetect AdaptiveDetector ContentDetector HashDetector "
                    "HistogramDetector ThresholdDetector split-video ffmpeg mkvmerge Docker."
                ),
            },
        )

    return entries


def render_hero_tabs() -> list[dict[str, str]]:
    """Pre-highlight the 3 interactive quickstart code tabs with Pygments."""
    formatter = HtmlFormatter(nowrap=True)
    tabs: list[dict[str, str]] = []
    for tab_id, label, hint, lang, code in HERO_SNIPPETS:
        lexer = PythonLexer() if lang == "python" else BashLexer()
        highlighted = highlight(code, lexer, formatter).rstrip("\n")
        tabs.append(
            {
                "id": tab_id,
                "label": label,
                "hint": hint,
                "html": highlighted,
            }
        )
    return tabs


def build_nav_context(current_slug: str | None, root_prefix: str) -> dict[str, Any]:
    """Build sidebar groups and responsive top-navbar link lists for a given page."""
    groups: list[dict[str, Any]] = []
    for group_title, items in NAV_GROUPS:
        group_items: list[dict[str, Any]] = []
        for item_title, _, slug in items:
            group_items.append(
                {
                    "title": item_title,
                    "slug": slug,
                    "href": page_href(slug, root_prefix),
                    "is_current": current_slug == slug,
                }
            )
        groups.append({"title": group_title, "pages": group_items})

    top_primary = [
        {
            "title": label,
            "slug": slug,
            "href": page_href(slug, root_prefix),
            "is_current": current_slug == slug,
        }
        for label, slug in TOP_NAV_PRIMARY
    ]
    top_mobile_extra = [
        {
            "title": label,
            "slug": slug,
            "href": page_href(slug, root_prefix),
            "is_current": current_slug == slug,
        }
        for label, slug in TOP_NAV_MOBILE_EXTRA
    ]
    return {
        "nav_groups": groups,
        "top_nav_primary": top_primary,
        "top_nav_mobile_extra": top_mobile_extra,
    }


def build_versions_manifest(default_latest: str) -> dict[str, Any]:
    """Build versions.json manifest for dynamic Sphinx version-switcher discovery."""
    latest_file = WEBSITE_DIR.parent / "docs" / "LATEST_VERSION"
    latest_ver = (
        latest_file.read_text(encoding="utf-8").strip().lstrip("v")
        if latest_file.is_file()
        else default_latest.lstrip("v")
    )

    stable_slugs: list[str] = []
    legacy_slugs: list[str] = []
    docs_md = PAGES_DIR / "docs.md"
    if docs_md.is_file():
        section = ""
        for raw_line in docs_md.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                section = line[3:].strip().lower()
                continue
            match = re.match(r"^-\s*\[([^\]]+)\]\(/docs/([^/]+)/\)", line)
            if not match:
                continue
            slug = match.group(2).strip()
            if slug in {"latest", "head"}:
                continue
            if section == "legacy":
                legacy_slugs.append(slug)
            else:
                stable_slugs.append(slug)

    entries: list[dict[str, Any]] = [
        {"slug": "latest", "label": f"v{latest_ver} (latest)", "same_layout": True},
        {"slug": "head", "label": "dev (head)", "same_layout": True},
    ]
    for slug in stable_slugs:
        entries.append(
            {
                "slug": slug,
                "label": f"v{slug}",
                "same_layout": not slug.startswith("0.6"),
            }
        )
    if legacy_slugs:
        entries.append(
            {
                "slug": legacy_slugs[0],
                "label": f"v{legacy_slugs[0]} (legacy)",
                "same_layout": False,
            }
        )
    return {"latest": latest_ver, "versions": entries}


def build_site() -> None:
    """Cleanly generate website/build/ from website/pages/ and website/theme/."""
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # Copy static theme assets and image assets.
    assets_out = BUILD_DIR / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(THEME_DIR / "site.css", assets_out / "site.css")
    shutil.copy2(THEME_DIR / "site.js", assets_out / "site.js")

    img_out = BUILD_DIR / "img"
    shutil.copytree(IMG_SRC_DIR, img_out)
    favicon_src = IMG_SRC_DIR / "favicon.ico"
    if favicon_src.is_file():
        shutil.copy2(favicon_src, BUILD_DIR / "favicon.ico")

    img_dims: dict[str, tuple[int, int]] = {}
    for img_file in IMG_SRC_DIR.iterdir():
        if img_file.is_file():
            dims = get_image_dimensions(img_file)
            if dims is not None:
                img_dims[img_file.name] = dims

    logo_w, logo_h = img_dims.get("pyscenedetect_logo_small.png", (462, 217))

    # Read index.md frontmatter first for release metadata overrides.
    index_raw = (PAGES_DIR / "index.md").read_text(encoding="utf-8")
    index_meta, _ = parse_frontmatter(index_raw)
    raw_ver = str(index_meta.get("version", DEFAULT_VERSION)).strip()
    version_tag = raw_ver if raw_ver.startswith("v") else f"v{raw_ver}"
    release_date = str(index_meta.get("release_date", DEFAULT_RELEASE_DATE)).strip()
    release_date_short = str(
        index_meta.get("release_date_short", shorten_date(release_date))
    ).strip()

    site_ctx = {
        "name": "PySceneDetect",
        "url": SITE_URL,
        "repo_url": REPO_URL,
        "pypi_url": PYPI_URL,
        "version": version_tag,
        "release_date": release_date,
        "release_date_short": release_date_short,
        "logo_width": logo_w,
        "logo_height": logo_h,
    }

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(THEME_DIR)),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    base_tmpl = env.get_template("base.html")
    not_found_tmpl = env.get_template("404.html")

    specs = iter_page_specs()
    hero_tabs = render_hero_tabs()
    search_index: list[dict[str, str]] = []

    for idx, spec in enumerate(specs):
        md_path = PAGES_DIR / spec.src_name
        raw_md = md_path.read_text(encoding="utf-8")
        meta, md_body = parse_frontmatter(raw_md)

        md = markdown.Markdown(
            extensions=[
                "fenced_code",
                "codehilite",
                "tables",
                "toc",
                "attr_list",
                "admonition",
                "sane_lists",
            ],
            extension_configs={
                "codehilite": {"guess_lang": False, "css_class": "codehilite"},
                "toc": {"permalink": False, "toc_depth": "2-3"},
            },
        )
        raw_html = md.convert(md_body)
        processed_html = postprocess_html(raw_html, spec, img_dims)
        toc_items = extract_toc(processed_html)
        search_index.extend(extract_search_entries(spec, processed_html))

        prev_spec = specs[idx - 1] if idx > 0 else None
        next_spec = specs[idx + 1] if idx + 1 < len(specs) else None

        page_title = str(meta.get("title", spec.title)).strip() or spec.title
        html_title = (
            "PySceneDetect - Video Scene Cut Detection & Analysis"
            if spec.is_home
            else f"{page_title} | PySceneDetect"
        )
        description = str(meta.get("description", DEFAULT_DESCRIPTION)).strip()

        page_ctx = {
            "title": page_title,
            "html_title": html_title,
            "description": description,
            "src_name": spec.src_name,
            "slug": spec.slug,
            "url_path": spec.url_path,
            "is_home": spec.is_home,
            "content": Markup(processed_html),
            "toc": toc_items,
            "prev_page": (
                {
                    "title": prev_spec.title,
                    "href": page_href(prev_spec.slug, spec.root_prefix),
                }
                if prev_spec
                else None
            ),
            "next_page": (
                {
                    "title": next_spec.title,
                    "href": page_href(next_spec.slug, spec.root_prefix),
                }
                if next_spec
                else None
            ),
            "edit_url": f"{REPO_URL}/blob/main/website/pages/{spec.src_name}",
            "hero_title": str(
                meta.get(
                    "hero_title",
                    "Fast, frame-accurate shot & scene cut detection for video.",
                )
            ),
            "hero_lead": str(
                meta.get(
                    "hero_lead",
                    "Detect hard cuts, dissolves, and fades from the command line or Python. "
                    "Automatically split footage with ffmpeg or mkvmerge, export EDL/OTIO "
                    "timelines, or inspect frame-by-frame metrics.",
                )
            ),
        }

        nav_ctx = build_nav_context(spec.slug, spec.root_prefix)
        rendered_page = base_tmpl.render(
            site=site_ctx,
            page=page_ctx,
            root_prefix=spec.root_prefix,
            hero_tabs=hero_tabs,
            **nav_ctx,
        )

        out_file = BUILD_DIR / spec.out_rel_path
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(rendered_page, encoding="utf-8")

    # Render 404.html with root_prefix="/" so deep 404 routes (/issues/558) load assets cleanly.
    nav_404_ctx = build_nav_context(None, "/")
    rendered_404 = not_found_tmpl.render(
        site=site_ctx,
        page={
            "title": "Page Not Found",
            "html_title": "Page Not Found | PySceneDetect",
            "description": DEFAULT_DESCRIPTION,
            "url_path": "/404.html",
            "is_home": False,
        },
        root_prefix="/",
        **nav_404_ctx,
    )
    (BUILD_DIR / "404.html").write_text(rendered_404, encoding="utf-8")

    # Write search_index.json, versions.json, and sitemap.xml.
    (BUILD_DIR / "search_index.json").write_text(
        json.dumps(search_index, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    (BUILD_DIR / "versions.json").write_text(
        json.dumps(build_versions_manifest(version_tag), ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )

    sitemap_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for spec in specs:
        sitemap_lines.append(f"  <url><loc>{SITE_URL}{spec.url_path}</loc></url>")
    sitemap_lines.append("</urlset>")
    (BUILD_DIR / "sitemap.xml").write_text("\n".join(sitemap_lines) + "\n", encoding="utf-8")


class _PageInspector(html.parser.HTMLParser):
    """Collect element IDs, headings, links, and asset references from built HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.h1_count = 0
        self.links: list[str] = []
        self.assets: list[tuple[str, str]] = []
        self.imgs_missing_alt: list[str] = []
        self.imgs_missing_dims: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        elem_id = attr_dict.get("id")
        if elem_id:
            self.ids.add(elem_id)
        if tag == "h1":
            self.h1_count += 1
        if tag == "a":
            href = attr_dict.get("href")
            if href is not None:
                self.links.append(href)
        elif tag == "img":
            src = attr_dict.get("src") or ""
            if src:
                self.assets.append(("img", src))
            if "alt" not in attr_dict or attr_dict["alt"] is None:
                self.imgs_missing_alt.append(src or "<img without src>")
            if (
                src
                and not src.startswith(EXTERNAL_SCHEMES)
                and (not attr_dict.get("width") or not attr_dict.get("height"))
            ):
                self.imgs_missing_dims.append(src)
        elif tag == "link":
            rel = (attr_dict.get("rel") or "").lower()
            href = attr_dict.get("href")
            if href and rel != "canonical":
                self.assets.append(("link", href))
        elif tag == "script":
            src = attr_dict.get("src")
            if src:
                self.assets.append(("script", src))


def verify_build() -> list[str]:
    """Strictly validate routes, internal links, #fragment anchors, and asset paths."""
    errors: list[str] = []
    specs = iter_page_specs()

    required_files = [BUILD_DIR / spec.out_rel_path for spec in specs] + [
        BUILD_DIR / "404.html",
        BUILD_DIR / "assets" / "site.css",
        BUILD_DIR / "assets" / "site.js",
        BUILD_DIR / "favicon.ico",
        BUILD_DIR / "search_index.json",
        BUILD_DIR / "versions.json",
        BUILD_DIR / "sitemap.xml",
    ]
    for req in required_files:
        if not req.is_file():
            errors.append(f"Missing expected output file: {req.relative_to(WEBSITE_DIR)}")

    html_files = sorted(BUILD_DIR.rglob("*.html"))
    inspectors: dict[Path, _PageInspector] = {}
    for html_file in html_files:
        parser = _PageInspector()
        parser.feed(html_file.read_text(encoding="utf-8"))
        inspectors[html_file] = parser
        rel_label = str(html_file.relative_to(BUILD_DIR)).replace("\\", "/")
        if parser.h1_count != 1:
            errors.append(f"{rel_label}: expected exactly 1 <h1>, found {parser.h1_count}")
        for bad_img in parser.imgs_missing_alt:
            errors.append(f"{rel_label}: <img> missing alt attribute ({bad_img})")
        for bad_dim in parser.imgs_missing_dims:
            errors.append(f"{rel_label}: local <img> missing width/height ({bad_dim})")

    for html_file, parser in inspectors.items():
        rel_file = html_file.relative_to(BUILD_DIR)
        rel_label = str(rel_file).replace("\\", "/")
        parent_posix = rel_file.parent.as_posix()
        base_url_dir = "/" if parent_posix == "." else f"/{parent_posix}/"

        for href in parser.links:
            if not href or href.startswith(EXTERNAL_SCHEMES):
                continue
            if href.startswith("#"):
                frag = urllib.parse.unquote(href[1:])
                if frag and frag not in parser.ids:
                    errors.append(f"{rel_label}: broken same-page anchor '{href}'")
                continue

            path_part, sep, frag = href.partition("#")
            path_part = path_part.split("?", 1)[0]
            site_path = _resolve_site_path(base_url_dir, path_part)

            if SPHINX_ABS_RE.match(site_path):
                continue

            target_file = _map_site_path_to_file(site_path)
            if target_file is None or not target_file.is_file():
                errors.append(
                    f"{rel_label}: broken internal link '{href}' (resolved to '{site_path}')"
                )
                continue

            if sep and frag and target_file in inspectors:
                decoded_frag = urllib.parse.unquote(frag)
                if decoded_frag not in inspectors[target_file].ids:
                    target_label = str(target_file.relative_to(BUILD_DIR)).replace("\\", "/")
                    errors.append(
                        f"{rel_label}: broken fragment '{href}' "
                        f"(id '{decoded_frag}' not found in {target_label})"
                    )

        for tag_name, src in parser.assets:
            if src.startswith(EXTERNAL_SCHEMES):
                continue
            clean_src = src.split("#", 1)[0].split("?", 1)[0]
            site_path = _resolve_site_path(base_url_dir, clean_src)
            target_file = BUILD_DIR / site_path.lstrip("/")
            if not target_file.is_file():
                errors.append(
                    f"{rel_label}: missing <{tag_name}> asset '{src}' (resolved to '{site_path}')"
                )

    return errors


def _resolve_site_path(base_url_dir: str, path_part: str) -> str:
    """Resolve a root-relative or page-relative URL path into a normalized site path."""
    if path_part.startswith("/"):
        norm = posixpath.normpath(path_part)
        if path_part.endswith("/") and not norm.endswith("/"):
            norm += "/"
        return norm

    joined = posixpath.normpath(posixpath.join(base_url_dir, path_part))
    is_dir_ref = path_part in {".", "./", "..", "../"} or path_part.endswith("/")
    if is_dir_ref and not joined.endswith("/"):
        joined += "/"
    return joined


def _map_site_path_to_file(site_path: str) -> Path | None:
    """Map a normalized site path (e.g. '/cli/') to an existing file path in BUILD_DIR."""
    stripped = site_path.lstrip("/")
    if site_path.endswith("/"):
        return (BUILD_DIR / stripped / "index.html") if stripped else (BUILD_DIR / "index.html")
    candidate = BUILD_DIR / stripped
    if candidate.is_dir():
        return candidate / "index.html"
    return candidate


def serve_site(port: int) -> None:
    """Serve website/build/ locally on 127.0.0.1:<port> with custom 404.html handling."""

    class PreviewHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(BUILD_DIR), **kwargs)

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            not_found_path = BUILD_DIR / "404.html"
            if code == 404 and not_found_path.is_file():
                body = not_found_path.read_bytes()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().send_error(code, message, explain)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler)
    print(f"Serving {BUILD_DIR} at http://127.0.0.1:{port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped preview server.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for building, strictly validating, and serving scenedetect.com."""
    parser = argparse.ArgumentParser(description="Build the PySceneDetect static website.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Verify all expected routes, internal links, #fragment anchors, and assets.",
    )
    parser.add_argument(
        "--serve",
        nargs="?",
        const=8000,
        type=int,
        metavar="PORT",
        help="Build and serve website/build/ locally on 127.0.0.1:PORT (default: 8000).",
    )
    args = parser.parse_args(argv)

    build_site()
    print(f"Built 14 pages + 404 + search index into {BUILD_DIR}")

    if args.strict:
        errors = verify_build()
        if errors:
            for err in errors:
                print(f"[STRICT ERROR] {err}", file=sys.stderr)
            print(f"Strict check failed with {len(errors)} error(s).", file=sys.stderr)
            return 1
        print("Strict verification passed (all routes, anchors, and assets verified).")

    if args.serve is not None:
        serve_site(args.serve)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
