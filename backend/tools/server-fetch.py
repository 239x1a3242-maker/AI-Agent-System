#!/usr/bin/env python3
"""
MCP Fetch Server (Python migration of server-fetch.js).

This file preserves the Node server's tool surface and behavior as closely as
possible while implementing the server with the Python MCP SDK FastMCP API.
"""

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import gzip
import html
import importlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import tempfile
import traceback
import unicodedata
from functools import lru_cache
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP

try:
    DDGS = importlib.import_module("ddgs").DDGS
except Exception:
    try:
        DDGS = importlib.import_module("duckduckgo_search").DDGS
    except Exception:
        DDGS = None

try:
    from readability import Document as ReadabilityDocument
except Exception:
    ReadabilityDocument = None

try:
    from markdownify import markdownify as markdownify_html
except Exception:
    markdownify_html = None


# ---------------------------------------------------------------------------
# Constants and configuration (mirrors src/types.ts defaults)
# ---------------------------------------------------------------------------

PACKAGE_VERSION = "1.1.2"
SERVER_NAME = "zcaceres/fetch"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_LIMIT_ENV = os.environ.get("DEFAULT_LIMIT", "5000")
MAX_RESPONSE_BYTES_ENV = os.environ.get("MAX_RESPONSE_BYTES", "10485760")

try:
    DOWNLOAD_LIMIT = int(DEFAULT_LIMIT_ENV)
except (TypeError, ValueError):
    DOWNLOAD_LIMIT = 5000

try:
    MAX_RESPONSE_BYTES = int(MAX_RESPONSE_BYTES_ENV)
except (TypeError, ValueError):
    MAX_RESPONSE_BYTES = 10 * 1024 * 1024

REQUEST_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 10
DEFAULT_WORKERS = 8
MAX_WORKERS = 8
FETCH_PARSER_MODE = os.environ.get("FETCH_PARSER_MODE", "auto").strip().lower()
if FETCH_PARSER_MODE not in ("auto", "strict", "fallback"):
    FETCH_PARSER_MODE = "auto"


FETCH_HTML_DESC = "Fetch a website and return its unmodified contents as HTML"
FETCH_MARKDOWN_DESC = "Fetch a website and return its contents converted to Markdown"
FETCH_TXT_DESC = "Fetch a website, convert the content to plain text (no HTML)"
FETCH_JSON_DESC = "Fetch a JSON file from a URL"
FETCH_URL_DESC = "Fetch a URL and return fully parsed main-page content with structured metadata"
FETCH_READABLE_DESC = (
    "Fetch a website and return its main content parsed by Mozilla Readability, "
    "converted to Markdown. Strips away navigation, ads, and boilerplate. "
    "Ideal for articles and blog posts."
)
FETCH_YOUTUBE_DESC = "Fetch a YouTube video page and extract its captions/transcript"
WEB_SEARCH_DESC = "Search the web by keyword and return ranked results"


OUTPUT_FORMAT = {
    "content": [{"type": "text", "text": "string"}],
    "isError": "boolean",
}


# ---------------------------------------------------------------------------
# Output format compatibility
# ---------------------------------------------------------------------------


def success_text_result(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def error_text_result(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------------
# Request models / validators (zod-equivalent behavior)
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    """Raised when tool arguments are invalid."""


def _require_url(value: Any, field_name: str = "url") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f'Invalid value for "{field_name}": expected non-empty string URL')

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(f'Invalid value for "{field_name}": expected http/https URL')

    return value


def _validate_headers(value: Any) -> Optional[Dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError('Invalid value for "headers": expected object of string:string')

    normalized: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValidationError('Invalid value for "headers": expected object of string:string')
        normalized[k] = v
    return normalized


def _validate_non_negative_int(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f'Invalid value for "{field_name}": expected integer >= 0')
    return value


def _validate_proxy(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError('Invalid value for "proxy": expected URL string')

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError('Invalid value for "proxy": expected http/https URL')
    return value


def _validate_lang(value: Any) -> str:
    if value is None:
        return "en"
    if not isinstance(value, str) or not value:
        raise ValidationError('Invalid value for "lang": expected string')
    return value


def _validate_query(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError('Invalid value for "query": expected non-empty string')
    return value.strip()


def _validate_positive_int(value: Any, field_name: str, default: int, minimum: int = 1, maximum: Optional[int] = None) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValidationError(f'Invalid value for "{field_name}": expected integer >= {minimum}')
    if maximum is not None and value > maximum:
        return maximum
    return value


def _validate_optional_string(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f'Invalid value for "{field_name}": expected string')
    cleaned = value.strip()
    return cleaned if cleaned else None


def _validate_workers(value: Any) -> int:
    return _validate_positive_int(
        value,
        "workers",
        default=DEFAULT_WORKERS,
        minimum=1,
        maximum=MAX_WORKERS,
    )


def parse_request_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "url": _require_url(args.get("url"), "url"),
        "headers": _validate_headers(args.get("headers")),
        "max_length": _validate_non_negative_int(args.get("max_length"), "max_length", DOWNLOAD_LIMIT),
        "start_index": _validate_non_negative_int(args.get("start_index"), "start_index", 0),
        "proxy": _validate_proxy(args.get("proxy")),
    }


def parse_youtube_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    payload = parse_request_payload(args)
    payload["lang"] = _validate_lang(args.get("lang"))
    return payload


def parse_fetch_url_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    # fetch_url follows the same network args as fetch_* tools.
    payload = parse_request_payload(args)
    payload["workers"] = _validate_workers(args.get("workers"))
    return payload


def parse_web_search_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    safesearch = _validate_optional_string(args.get("safesearch"), "safesearch")
    if safesearch is None:
        safesearch = "moderate"
    if safesearch not in ("off", "moderate", "strict"):
        raise ValidationError('Invalid value for "safesearch": expected one of off|moderate|strict')

    return {
        "query": _validate_query(args.get("query")),
        "max_results": _validate_positive_int(args.get("max_results"), "max_results", 10, minimum=1, maximum=50),
        "region": _validate_optional_string(args.get("region"), "region"),
        "safesearch": safesearch,
        "workers": _validate_workers(args.get("workers")),
    }


# ---------------------------------------------------------------------------
# Network and response processing layer
# ---------------------------------------------------------------------------


@dataclass
class FetchResponse:
    final_url: str
    status_code: int
    headers: Dict[str, str]
    body_bytes: bytes


class LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS
    max_repeats = 4


def apply_length_limits(text: str, max_length: int, start_index: int) -> str:
    if start_index >= len(text):
        return ""
    if max_length > 0:
        end = min(start_index + max_length, len(text))
    else:
        end = len(text)
    return text[start_index:end]


def _is_private_or_local_ip(host_or_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(host_or_ip)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_multicast
            or ip.is_reserved
        )
    except ValueError:
        return False


def _normalize_hostname_for_check(hostname: str) -> str:
    if hostname.startswith("[") and hostname.endswith("]"):
        return hostname[1:-1]
    return hostname


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise Exception(
            f'Fetcher blocked URL with disallowed protocol "{parsed.scheme}:". '
            "Only HTTP and HTTPS are allowed."
        )

    hostname = parsed.hostname or ""
    bare = _normalize_hostname_for_check(hostname)
    if bare == "localhost" or _is_private_or_local_ip(bare):
        raise Exception(
            f'Fetcher blocked request to private address "{bare}". '
            "This prevents SSRF attacks where a local MCP server could "
            "access privileged internal services."
        )


def validate_resolved_ip(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    bare = _normalize_hostname_for_check(hostname)
    if not bare:
        return

    try:
        infos = _resolve_host_addrs(bare)
        for info in infos:
            sockaddr = info[4]
            address = sockaddr[0]
            if _is_private_or_local_ip(address):
                raise Exception(
                    f'Fetcher blocked request: hostname "{bare}" resolved '
                    f'to private IP "{address}". This prevents DNS rebinding SSRF attacks.'
                )
    except Exception as exc:
        # Preserve source behavior: block only on explicit "Fetcher blocked" checks.
        if "Fetcher blocked" in str(exc):
            raise


@lru_cache(maxsize=256)
def _resolve_host_addrs(hostname: str) -> Tuple[Tuple[Any, ...], ...]:
    # DNS lookups are repeated across tool calls; cache to reduce latency.
    return tuple(socket.getaddrinfo(hostname, None))


@lru_cache(maxsize=16)
def _build_opener(proxy: Optional[str]) -> urllib.request.OpenerDirector:
    handlers: List[urllib.request.BaseHandler] = [LimitedRedirectHandler()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _decode_compressed_body(body: bytes, headers: Dict[str, str]) -> bytes:
    encoding = headers.get("content-encoding", "").lower().strip()
    if not encoding:
        return body

    try:
        if "gzip" in encoding:
            return gzip.decompress(body)
        if "deflate" in encoding:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        # If decompression fails, preserve raw bytes.
        return body
    return body


def _read_limited_response_bytes(raw_response: Any) -> bytes:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = raw_response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise Exception(
                f"Response too large: exceeded {MAX_RESPONSE_BYTES} byte limit while reading"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def detect_content_type(headers: Dict[str, str], url: str) -> str:
    content_type = headers.get("content-type", "")
    if content_type:
        return content_type.split(";", 1)[0].strip().lower()

    guessed, _ = mimetypes.guess_type(url)
    return (guessed or "application/octet-stream").lower()


def _extract_charset(headers: Dict[str, str]) -> str:
    content_type = headers.get("content-type", "")
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip("\"'")
    return "utf-8"


def parse_response_payload(response: FetchResponse) -> Dict[str, Any]:
    ctype = detect_content_type(response.headers, response.final_url)
    charset = _extract_charset(response.headers)

    if ctype == "application/json" or ctype.endswith("+json"):
        text = response.body_bytes.decode(charset, errors="replace")
        return {"kind": "json", "content_type": ctype, "value": json.loads(text)}

    if ctype.startswith("text/") or ctype in (
        "application/xml",
        "text/xml",
        "application/xhtml+xml",
        "application/javascript",
    ):
        text = response.body_bytes.decode(charset, errors="replace")
        return {"kind": "text", "content_type": ctype, "value": text}

    return {
        "kind": "binary",
        "content_type": ctype,
        "value": base64.b64encode(response.body_bytes).decode("ascii"),
    }


def fetch_http(
    *,
    url: str,
    headers: Optional[Dict[str, str]],
    proxy: Optional[str],
    method: str = "GET",
    body: Optional[bytes] = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> FetchResponse:
    validate_url(url)
    validate_resolved_ip(url)

    merged_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    if headers:
        merged_headers.update(headers)

    request = urllib.request.Request(url=url, data=body, headers=merged_headers, method=method)
    opener = _build_opener(proxy)

    try:
        with opener.open(request, timeout=timeout) as resp:
            status_code = int(getattr(resp, "status", 200))
            final_url = resp.geturl()
            response_headers = {k.lower(): v for k, v in resp.headers.items()}

            content_length = response_headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        raise Exception(
                            f"Response too large: {content_length} bytes exceeds "
                            f"{MAX_RESPONSE_BYTES} byte limit"
                        )
                except ValueError:
                    pass

            body_bytes = _read_limited_response_bytes(resp)
            body_bytes = _decode_compressed_body(body_bytes, response_headers)

    except urllib.error.HTTPError as exc:
        raise Exception(f"Failed to fetch {url}: HTTP error: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise Exception(f"Failed to fetch {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise Exception(f"Failed to fetch {url}: Request timed out") from exc
    except Exception as exc:
        message = str(exc)
        if message.startswith("Response too large:"):
            raise Exception(message) from exc
        raise Exception(f"Failed to fetch {url}: {message}") from exc

    if final_url and final_url != url:
        validate_url(final_url)
        validate_resolved_ip(final_url)

    if status_code < 200 or status_code >= 300:
        raise Exception(f"Failed to fetch {url}: HTTP error: {status_code}")

    return FetchResponse(
        final_url=final_url,
        status_code=status_code,
        headers=response_headers,
        body_bytes=body_bytes,
    )


def read_response_text(response: FetchResponse) -> str:
    charset = _extract_charset(response.headers)
    return response.body_bytes.decode(charset, errors="replace")


# ---------------------------------------------------------------------------
# HTML / Markdown helpers (stdlib-only implementation)
# ---------------------------------------------------------------------------


TAG_BLOCK_BREAKS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "main",
    "br",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "tr",
}


class PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if self._skip_depth == 0 and tag in TAG_BLOCK_BREAKS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth == 0 and tag in TAG_BLOCK_BREAKS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._parts.append(data)

    def get_text(self) -> str:
        return " ".join("".join(self._parts).split())


class MarkdownExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: List[str] = []
        self._skip_depth = 0
        self._list_stack: List[str] = []
        self._href_stack: List[Optional[str]] = []

    def _append(self, value: str) -> None:
        self._out.append(value)

    def _newline(self) -> None:
        if not self._out or not self._out[-1].endswith("\n"):
            self._append("\n")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        attrs_dict = dict(attrs)

        if tag in ("p", "div", "section", "article", "main"):
            self._newline()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._newline()
            self._append("#" * level + " ")
        elif tag == "br":
            self._append("\n")
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self._newline()
        elif tag == "li":
            self._newline()
            if self._list_stack and self._list_stack[-1] == "ol":
                self._append("1. ")
            else:
                self._append("- ")
        elif tag in ("strong", "b"):
            self._append("**")
        elif tag in ("em", "i"):
            self._append("*")
        elif tag == "code":
            self._append("`")
        elif tag == "a":
            self._append("[")
            self._href_stack.append(attrs_dict.get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return

        if tag in ("p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline()
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._newline()
        elif tag in ("strong", "b"):
            self._append("**")
        elif tag in ("em", "i"):
            self._append("*")
        elif tag == "code":
            self._append("`")
        elif tag == "a":
            href = self._href_stack.pop() if self._href_stack else None
            self._append("]")
            if href:
                self._append(f"({href})")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._append(data)

    def get_markdown(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


def html_to_txt(html_content: str) -> str:
    parser = PlainTextExtractor()
    parser.feed(html_content)
    parser.close()
    return parser.get_text()


def html_to_markdown(html_content: str) -> str:
    parser = MarkdownExtractor()
    parser.feed(html_content)
    parser.close()
    return parser.get_markdown()


def _strict_parser_available() -> bool:
    return ReadabilityDocument is not None and markdownify_html is not None


def _use_strict_parser() -> bool:
    if FETCH_PARSER_MODE == "fallback":
        return False
    if FETCH_PARSER_MODE in ("auto", "strict"):
        return _strict_parser_available()
    return _strict_parser_available()


def html_to_markdown_compat(html_content: str) -> str:
    if _use_strict_parser() and markdownify_html is not None:
        try:
            sanitized = re.sub(
                r"<head\b[^>]*>[\s\S]*?</head>",
                "",
                html_content,
                flags=re.IGNORECASE,
            )
            sanitized = re.sub(
                r"<script\b[^>]*>[\s\S]*?</script>",
                "",
                sanitized,
                flags=re.IGNORECASE,
            )
            sanitized = re.sub(
                r"<style\b[^>]*>[\s\S]*?</style>",
                "",
                sanitized,
                flags=re.IGNORECASE,
            )
            sanitized = re.sub(
                r"<noscript\b[^>]*>[\s\S]*?</noscript>",
                "",
                sanitized,
                flags=re.IGNORECASE,
            )
            return markdownify_html(
                sanitized,
                heading_style="ATX",
                bullets="-",
                strip=["script", "style", "noscript", "head"],
            ).strip()
        except Exception:
            # Preserve existing behavior on parser failures.
            pass
    return html_to_markdown(html_content)


def extract_readable_html(html_content: str) -> str:
    # Prefer <article> if available; otherwise strip common boilerplate containers.
    article_match = re.search(r"<article\b[^>]*>([\s\S]*?)</article>", html_content, flags=re.IGNORECASE)
    if article_match:
        return article_match.group(1)

    body_match = re.search(r"<body\b[^>]*>([\s\S]*?)</body>", html_content, flags=re.IGNORECASE)
    body = body_match.group(1) if body_match else html_content

    body = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<noscript\b[^>]*>[\s\S]*?</noscript>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<nav\b[^>]*>[\s\S]*?</nav>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<header\b[^>]*>[\s\S]*?</header>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<footer\b[^>]*>[\s\S]*?</footer>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<aside\b[^>]*>[\s\S]*?</aside>", "", body, flags=re.IGNORECASE)
    return body


def extract_readable_html_compat(html_content: str) -> str:
    if _use_strict_parser() and ReadabilityDocument is not None:
        try:
            doc = ReadabilityDocument(html_content)
            summary = doc.summary(html_partial=True)
            if summary and summary.strip():
                return summary
        except Exception:
            # Preserve fallback behavior on readability parser failures.
            pass
    return extract_readable_html(html_content)


def advanced_clean_text(text: str, url: Optional[str] = None) -> str:
    """Noise-focused cleaning adapted from main_content_cleaner.py."""
    if not text:
        return ""

    cleaned = html.unescape(text)
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", " ", cleaned)

    lines: List[str] = []
    for line in cleaned.split("\n"):
        line = line.strip()
        if re.match(r"^[=*\-_~]{3,}$", line):
            continue
        if re.match(r"^\s*\d+\s*$|^Page\s+\d+", line):
            continue
        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def extract_content_sections(text: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {"Main Content": []}
    current = "Main Content"
    heading_patterns = [
        r"^#{1,6}\s+(.*)$",
        r"^([A-Z][A-Za-z\s]{3,50}):?$",
        r"^(Introduction|Overview|Background|Methods|Results|Discussion|Conclusion|References):?$",
        r"^[A-Z\s]{5,30}$",
    ]

    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        is_heading = False
        for pattern in heading_patterns:
            match = re.match(pattern, s)
            if match and len(s.split()) < 10:
                title = match.group(1) if match.groups() else s
                title = re.sub(r"[#*_`]", "", title).strip(": ").strip()
                if title and title != current:
                    current = title
                    sections[current] = []
                is_heading = True
                break
        if not is_heading and len(s) > 10:
            sections[current].append(s)

    return {k: v for k, v in sections.items() if v}


def paragraphs(text: str) -> List[str]:
    if not text:
        return []
    raw = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    result: List[str] = []
    for para in raw:
        if len(para) < 40:
            continue
        alpha_ratio = sum(1 for c in para if c.isalpha()) / len(para) if para else 0.0
        if alpha_ratio < 0.4:
            continue
        result.append(para)
    return result


def sentences(text: str) -> List[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if len(p.strip()) > 15]


def top_keywords(text: str, n: int = 15, min_word_length: int = 4) -> List[str]:
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z'][a-zA-Z'-]{%d,}\b" % (min_word_length - 1), text.lower())
    filtered = [w for w in words if len(w) >= min_word_length]
    counts = Counter(filtered)
    return [word for word, _ in counts.most_common(n)]


def calculate_readability_metrics(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    sents = sentences(text)
    words = re.findall(r"\b\w+\b", text)
    if not sents or not words:
        return {}

    syllables = 0
    for word in words:
        lw = word.lower()
        if len(lw) <= 3:
            syllables += 1
        else:
            syllables += max(1, len(re.findall(r"[aeiouy]+", lw)))

    num_words = len(words)
    num_sents = len(sents)
    avg_sentence_length = num_words / num_sents
    avg_word_length = sum(len(w) for w in words) / num_words
    flesch = 206.835 - 1.015 * (num_words / num_sents) - 84.6 * (syllables / num_words)
    return {
        "word_count": num_words,
        "sentence_count": num_sents,
        "avg_sentence_length": round(avg_sentence_length, 1),
        "avg_word_length": round(avg_word_length, 1),
        "flesch_reading_ease": round(max(flesch, 0), 1),
    }


def extract_title_from_html(html_content: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title>", html_content, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html.unescape(re.sub(r"\s+", " ", match.group(1)).strip())


def _normalize_search_result(idx: int, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "position": idx,
        "title": result.get("title", "No title"),
        "url": result.get("href", ""),
        "snippet": result.get("body", ""),
        "source": "DuckDuckGo",
    }


def ddgs_web_search(
    query: str,
    max_results: int,
    region: Optional[str],
    safesearch: str,
    workers: int,
) -> List[Dict[str, Any]]:
    if DDGS is None:
        raise Exception('web_search requires "ddgs" or "duckduckgo_search" package to be installed')

    kwargs: Dict[str, Any] = {"max_results": max_results, "safesearch": safesearch}
    if region:
        kwargs["region"] = region

    raw_results: List[Dict[str, Any]] = []
    with DDGS(timeout=REQUEST_TIMEOUT_SECONDS) as ddgs:
        try:
            raw_results = list(ddgs.text(keywords=query, **kwargs))
        except TypeError:
            try:
                raw_results = list(ddgs.text(query, **kwargs))
            except TypeError:
                raw_results = list(ddgs.text(query, max_results))

    indexed_results = list(enumerate(raw_results, start=1))
    if not indexed_results:
        return []

    normalized: List[Dict[str, Any]] = []
    if workers <= 1 or len(indexed_results) == 1:
        normalized = [_normalize_search_result(idx, result) for idx, result in indexed_results]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(indexed_results))) as executor:
            normalized = list(executor.map(lambda item: _normalize_search_result(item[0], item[1]), indexed_results))
    return normalized


# ---------------------------------------------------------------------------
# YouTube transcript extraction layer
# ---------------------------------------------------------------------------


class YouTubeTranscript:
    @staticmethod
    def extract_player_response(html_page: str) -> Dict[str, Any]:
        match = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\});", html_page, flags=re.DOTALL)
        if not match:
            raise Exception("Could not find ytInitialPlayerResponse in page HTML")
        return json.loads(match.group(1))

    @staticmethod
    def get_caption_tracks(player_response: Dict[str, Any]) -> List[Dict[str, Any]]:
        tracks = (
            player_response.get("captions", {})
            .get("playerCaptionsTracklistRenderer", {})
            .get("captionTracks")
        )
        if not isinstance(tracks, list) or not tracks:
            raise Exception("No caption tracks found for this video")
        return tracks

    @staticmethod
    def decode_html_entities(text: str) -> str:
        return html.unescape(text)

    @staticmethod
    def format_timestamp(total_seconds: float) -> str:
        minutes = int(total_seconds // 60)
        seconds = int(total_seconds % 60)
        return f"{minutes}:{seconds:02d}"

    @staticmethod
    def parse_transcript_xml(xml_text: str) -> List[str]:
        lines: List[str] = []

        text_pattern = re.compile(r'<text\s+start="([^"]+)"[^>]*>([\s\S]*?)</text>')
        p_pattern = re.compile(r'<p\s+t="(\d+)"[^>]*>([\s\S]*?)</p>')

        text_matches = list(text_pattern.finditer(xml_text))
        if text_matches:
            for match in text_matches:
                seconds = float(match.group(1))
                content = re.sub(r"<[^>]+>", "", match.group(2)).strip()
                content = YouTubeTranscript.decode_html_entities(content)
                if content:
                    lines.append(f"[{YouTubeTranscript.format_timestamp(seconds)}] {content}")
            return lines

        for match in p_pattern.finditer(xml_text):
            ms = int(match.group(1))
            seconds = ms / 1000.0
            content = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            content = YouTubeTranscript.decode_html_entities(content)
            if content:
                lines.append(f"[{YouTubeTranscript.format_timestamp(seconds)}] {content}")
        return lines


# ---------------------------------------------------------------------------
# Fetcher class (function parity with Node static methods)
# ---------------------------------------------------------------------------


class Fetcher:
    has_ytdlp: Optional[bool] = None

    @staticmethod
    def apply_length_limits(text: str, max_length: int, start_index: int) -> str:
        return apply_length_limits(text, max_length, start_index)

    @staticmethod
    def validate_url(url: str) -> None:
        validate_url(url)

    @staticmethod
    def validate_resolved_ip(url: str) -> None:
        validate_resolved_ip(url)

    @staticmethod
    def _fetch(request_payload: Dict[str, Any]) -> FetchResponse:
        return fetch_http(
            url=request_payload["url"],
            headers=request_payload.get("headers"),
            proxy=request_payload.get("proxy"),
            method=request_payload.get("method", "GET"),
            body=request_payload.get("body"),
        )

    @staticmethod
    def read_response_text(response: FetchResponse) -> str:
        return read_response_text(response)

    @staticmethod
    def html(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = Fetcher._fetch(request_payload)
            html_content = Fetcher.read_response_text(response)
            html_content = Fetcher.apply_length_limits(
                html_content,
                request_payload.get("max_length", DOWNLOAD_LIMIT),
                request_payload.get("start_index", 0),
            )
            return success_text_result(html_content)
        except Exception as error:
            return error_text_result(str(error))

    @staticmethod
    def json(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = Fetcher._fetch(request_payload)
            text = Fetcher.read_response_text(response)
            parsed_json = json.loads(text)
            json_text = json.dumps(parsed_json, ensure_ascii=False)
            json_text = Fetcher.apply_length_limits(
                json_text,
                request_payload.get("max_length", DOWNLOAD_LIMIT),
                request_payload.get("start_index", 0),
            )
            return success_text_result(json_text)
        except Exception as error:
            return error_text_result(str(error))

    @staticmethod
    def txt(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = Fetcher._fetch(request_payload)
            html_content = Fetcher.read_response_text(response)
            normalized_text = html_to_txt(html_content)
            normalized_text = Fetcher.apply_length_limits(
                normalized_text,
                request_payload.get("max_length", DOWNLOAD_LIMIT),
                request_payload.get("start_index", 0),
            )
            return success_text_result(normalized_text)
        except Exception as error:
            return error_text_result(str(error))

    @staticmethod
    def fetch_transcript_via_ytdlp(video_url: str, lang: str) -> Dict[str, str]:
        if not re.fullmatch(r"[a-zA-Z0-9-]+", lang or ""):
            raise Exception(
                f'Invalid language code: "{lang}". '
                "Only letters, digits, and hyphens are allowed."
            )

        with tempfile.TemporaryDirectory(prefix="mcp-fetch-ytdlp-") as tmp_dir:
            output_template = os.path.join(tmp_dir, "sub")
            cmd = [
                "yt-dlp",
                "--write-sub",
                "--sub-lang",
                lang,
                "--sub-format",
                "srv1",
                "--skip-download",
                "-o",
                output_template,
                video_url,
            ]

            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

            files = [f for f in os.listdir(tmp_dir) if f.endswith(".srv1")]
            if not files:
                raise Exception("yt-dlp did not produce subtitle files")

            filename = files[0]
            with open(os.path.join(tmp_dir, filename), "r", encoding="utf-8") as handle:
                xml = handle.read()

            match = re.search(r"\.([^.]+)\.srv1$", filename)
            matched_lang = match.group(1) if match else lang
            return {"xml": xml, "lang": matched_lang, "langName": matched_lang}

    @staticmethod
    def fetch_transcript_direct(request_payload: Dict[str, Any]) -> Dict[str, str]:
        response = Fetcher._fetch(request_payload)
        html_content = Fetcher.read_response_text(response)

        player_response = YouTubeTranscript.extract_player_response(html_content)
        tracks = YouTubeTranscript.get_caption_tracks(player_response)

        lang = request_payload.get("lang", "en")
        track = None
        for candidate in tracks:
            if candidate.get("languageCode") == lang:
                track = candidate
                break
        if track is None:
            track = tracks[0]

        base_url = track.get("baseUrl", "")
        caption_url = base_url if "fmt=" in base_url else f"{base_url}&fmt=srv1"

        caption_response = Fetcher._fetch(
            {
                "url": caption_url,
                "headers": request_payload.get("headers"),
                "proxy": request_payload.get("proxy"),
                "max_length": request_payload.get("max_length", DOWNLOAD_LIMIT),
                "start_index": request_payload.get("start_index", 0),
            }
        )
        xml = Fetcher.read_response_text(caption_response)

        lang_name = "Unknown"
        name_field = track.get("name")
        if isinstance(name_field, dict):
            lang_name = str(name_field.get("simpleText", "Unknown"))

        return {
            "xml": xml,
            "lang": str(track.get("languageCode", lang)),
            "langName": lang_name,
        }

    @staticmethod
    def check_ytdlp() -> bool:
        if Fetcher.has_ytdlp is not None:
            return Fetcher.has_ytdlp

        try:
            subprocess.run(
                ["yt-dlp", "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            Fetcher.has_ytdlp = True
        except Exception:
            Fetcher.has_ytdlp = False
        return Fetcher.has_ytdlp

    @staticmethod
    def youtube_transcript(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            lang = request_payload.get("lang", "en")
            result: Dict[str, str]

            if Fetcher.check_ytdlp():
                if not re.fullmatch(r"[a-zA-Z0-9-]+", lang or ""):
                    raise Exception(
                        f'Invalid language code: "{lang}". '
                        "Only letters, digits, and hyphens are allowed."
                    )
                try:
                    result = Fetcher.fetch_transcript_via_ytdlp(request_payload["url"], lang)
                except Exception:
                    result = Fetcher.fetch_transcript_direct(request_payload)
            else:
                result = Fetcher.fetch_transcript_direct(request_payload)

            lines = YouTubeTranscript.parse_transcript_xml(result["xml"])
            header = f"[Transcript language: {result['lang']} — {result['langName']}]\n\n"
            transcript = header + "\n".join(lines)
            transcript = Fetcher.apply_length_limits(
                transcript,
                request_payload.get("max_length", DOWNLOAD_LIMIT),
                request_payload.get("start_index", 0),
            )
            return success_text_result(transcript)
        except Exception as error:
            return error_text_result(str(error))

    @staticmethod
    def readable(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = Fetcher._fetch(request_payload)
            html_content = Fetcher.read_response_text(response)
            readable_html = extract_readable_html_compat(html_content)
            markdown = html_to_markdown_compat(readable_html)
            if not markdown.strip():
                raise Exception("Failed to parse readable content from the page")

            markdown = Fetcher.apply_length_limits(
                markdown,
                request_payload.get("max_length", DOWNLOAD_LIMIT),
                request_payload.get("start_index", 0),
            )
            return success_text_result(markdown)
        except Exception as error:
            return error_text_result(str(error))

    @staticmethod
    def markdown(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = Fetcher._fetch(request_payload)
            html_content = Fetcher.read_response_text(response)
            markdown = html_to_markdown_compat(html_content)
            markdown = Fetcher.apply_length_limits(
                markdown,
                request_payload.get("max_length", DOWNLOAD_LIMIT),
                request_payload.get("start_index", 0),
            )
            return success_text_result(markdown)
        except Exception as error:
            return error_text_result(str(error))

    @staticmethod
    def web_search(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            results = ddgs_web_search(
                query=request_payload["query"],
                max_results=request_payload["max_results"],
                region=request_payload.get("region"),
                safesearch=request_payload.get("safesearch", "moderate"),
                workers=request_payload.get("workers", DEFAULT_WORKERS),
            )
            payload = {
                "query": request_payload["query"],
                "max_results": request_payload["max_results"],
                "workers_used": request_payload.get("workers", DEFAULT_WORKERS),
                "count": len(results),
                "results": results,
            }
            return success_text_result(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as error:
            return error_text_result(str(error))

    @staticmethod
    def fetch_url(request_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = Fetcher._fetch(request_payload)
            html_content = Fetcher.read_response_text(response)

            readable_html = extract_readable_html_compat(html_content)
            basis_html = readable_html if readable_html.strip() else html_content

            markdown = html_to_markdown_compat(basis_html)
            plain_text = html_to_txt(basis_html)
            cleaned_content = advanced_clean_text(markdown if markdown.strip() else plain_text, request_payload.get("url"))
            workers = request_payload.get("workers", DEFAULT_WORKERS)

            max_length = request_payload.get("max_length", DOWNLOAD_LIMIT)
            start_index = request_payload.get("start_index", 0)

            limited_cleaned = apply_length_limits(cleaned_content, max_length, start_index)
            limited_markdown = apply_length_limits(markdown, max_length, start_index)
            limited_text = apply_length_limits(plain_text, max_length, start_index)

            if limited_cleaned.strip():
                with ThreadPoolExecutor(max_workers=min(workers, MAX_WORKERS)) as executor:
                    para_future = executor.submit(paragraphs, limited_cleaned)
                    sections_future = executor.submit(extract_content_sections, limited_cleaned)
                    sentences_future = executor.submit(sentences, limited_cleaned)
                    keywords_future = executor.submit(top_keywords, limited_cleaned, 15)
                    readability_future = executor.submit(calculate_readability_metrics, limited_cleaned)

                    para_list = para_future.result()
                    content_sections = sections_future.result()
                    sentence_samples = sentences_future.result()[:5]
                    keywords = keywords_future.result()
                    readability = readability_future.result()
            else:
                para_list = []
                content_sections = {}
                sentence_samples = []
                keywords = []
                readability = {}

            structured = {
                "url": request_payload.get("url"),
                "final_url": response.final_url,
                "status_code": response.status_code,
                "content_type": detect_content_type(response.headers, response.final_url),
                "title": extract_title_from_html(html_content),
                "parser_mode": FETCH_PARSER_MODE,
                "strict_parser_active": _use_strict_parser(),
                "workers_used": workers,
                "content_word_count": len(limited_cleaned.split()) if limited_cleaned else 0,
                "cleaned_content": limited_cleaned,
                "first_paragraph": para_list[0] if para_list else "",
                "paragraphs": para_list[:20],
                "content_sections": content_sections,
                "sample_sentences": sentence_samples,
                "top_keywords": keywords,
                "readability_metrics": readability,
                "markdown": limited_markdown,
                "text": limited_text,
            }
            return success_text_result(json.dumps(structured, ensure_ascii=False, indent=2))
        except Exception as error:
            return error_text_result(str(error))


TOOL_CATALOG: List[Dict[str, Any]] = [
    {
        "name": "fetch_html",
        "description": FETCH_HTML_DESC,
        "input_format": {
            "url": "string (required)",
            "headers": "object<string,string> (optional)",
            "max_length": "number int>=0 (optional)",
            "start_index": "number int>=0 (optional)",
            "proxy": "string URL (optional)",
            "workers": "number int>=1 (optional, default: 8, max: 8)",
        },
        "output_format": OUTPUT_FORMAT,
    },
    {
        "name": "fetch_markdown",
        "description": FETCH_MARKDOWN_DESC,
        "input_format": {
            "url": "string (required)",
            "headers": "object<string,string> (optional)",
            "max_length": "number int>=0 (optional)",
            "start_index": "number int>=0 (optional)",
            "proxy": "string URL (optional)",
        },
        "output_format": OUTPUT_FORMAT,
    },
    {
        "name": "fetch_txt",
        "description": FETCH_TXT_DESC,
        "input_format": {
            "url": "string (required)",
            "headers": "object<string,string> (optional)",
            "max_length": "number int>=0 (optional)",
            "start_index": "number int>=0 (optional)",
            "proxy": "string URL (optional)",
        },
        "output_format": OUTPUT_FORMAT,
    },
    {
        "name": "fetch_json",
        "description": FETCH_JSON_DESC,
        "input_format": {
            "url": "string (required)",
            "headers": "object<string,string> (optional)",
            "max_length": "number int>=0 (optional)",
            "start_index": "number int>=0 (optional)",
            "proxy": "string URL (optional)",
        },
        "output_format": OUTPUT_FORMAT,
    },
    {
        "name": "fetch_url",
        "description": FETCH_URL_DESC,
        "input_format": {
            "url": "string (required)",
            "headers": "object<string,string> (optional)",
            "max_length": "number int>=0 (optional)",
            "start_index": "number int>=0 (optional)",
            "proxy": "string URL (optional)",
        },
        "output_format": OUTPUT_FORMAT,
    },
    {
        "name": "fetch_readable",
        "description": FETCH_READABLE_DESC,
        "input_format": {
            "url": "string (required)",
            "headers": "object<string,string> (optional)",
            "max_length": "number int>=0 (optional)",
            "start_index": "number int>=0 (optional)",
            "proxy": "string URL (optional)",
        },
        "output_format": OUTPUT_FORMAT,
    },
    {
        "name": "web_search",
        "description": WEB_SEARCH_DESC,
        "input_format": {
            "query": "string (required)",
            "max_results": "number int>=1 (optional, default: 10, max: 50)",
            "region": "string (optional)",
            "safesearch": "string off|moderate|strict (optional, default: moderate)",
            "workers": "number int>=1 (optional, default: 8, max: 8)",
        },
        "output_format": OUTPUT_FORMAT,
    },
    {
        "name": "fetch_youtube_transcript",
        "description": FETCH_YOUTUBE_DESC,
        "input_format": {
            "url": "string (required)",
            "headers": "object<string,string> (optional)",
            "max_length": "number int>=0 (optional)",
            "start_index": "number int>=0 (optional)",
            "proxy": "string URL (optional)",
            "lang": "string (optional, default: en)",
        },
        "output_format": OUTPUT_FORMAT,
    },
]


# ---------------------------------------------------------------------------
# MCP tool registry and server wiring
# ---------------------------------------------------------------------------


mcp = FastMCP(SERVER_NAME)


@mcp.resource(
    "fetch://tools",
    name="fetch_tools_inventory",
    description="Returns metadata for all fetch tools exposed by this server.",
    mime_type="application/json",
)
def fetch_tools_inventory() -> str:
    inventory = {
        "tools": TOOL_CATALOG,
        "tool_names": [tool["name"] for tool in TOOL_CATALOG],
        "defaults": {
            "max_length": DOWNLOAD_LIMIT,
            "start_index": 0,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "workers": DEFAULT_WORKERS,
            "max_workers": MAX_WORKERS,
        },
        "parser_mode": FETCH_PARSER_MODE,
        "strict_parser_available": _strict_parser_available(),
        "strict_parser_active": _use_strict_parser(),
    }
    return json.dumps(inventory, ensure_ascii=False)


def _tool_error_boundary(fn_name: str, args: Dict[str, Any], parser_fn: Any, handler_fn: Any) -> Dict[str, Any]:
    try:
        payload = parser_fn(args)
        return handler_fn(payload)
    except ValidationError as exc:
        return error_text_result(str(exc))
    except Exception as exc:
        # Keep tool failure shape stable and include concise trace context.
        err = str(exc)
        if not err:
            err = "Unknown error"
        return error_text_result(err)


@mcp.tool(name="fetch_html", description=FETCH_HTML_DESC)
def fetch_html(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_length: Optional[int] = None,
    start_index: Optional[int] = None,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a website and return its unmodified contents as HTML."""
    args: Dict[str, Any] = {
        "url": url,
        "headers": headers,
        "max_length": max_length,
        "start_index": start_index,
        "proxy": proxy,
    }
    return _tool_error_boundary("fetch_html", args, parse_request_payload, Fetcher.html)


@mcp.tool(name="fetch_markdown", description=FETCH_MARKDOWN_DESC)
def fetch_markdown(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_length: Optional[int] = None,
    start_index: Optional[int] = None,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a website and return its contents converted to Markdown."""
    args: Dict[str, Any] = {
        "url": url,
        "headers": headers,
        "max_length": max_length,
        "start_index": start_index,
        "proxy": proxy,
    }
    return _tool_error_boundary("fetch_markdown", args, parse_request_payload, Fetcher.markdown)


@mcp.tool(name="fetch_txt", description=FETCH_TXT_DESC)
def fetch_txt(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_length: Optional[int] = None,
    start_index: Optional[int] = None,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a website, convert the content to plain text (no HTML)."""
    args: Dict[str, Any] = {
        "url": url,
        "headers": headers,
        "max_length": max_length,
        "start_index": start_index,
        "proxy": proxy,
    }
    return _tool_error_boundary("fetch_txt", args, parse_request_payload, Fetcher.txt)


@mcp.tool(name="fetch_json", description=FETCH_JSON_DESC)
def fetch_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_length: Optional[int] = None,
    start_index: Optional[int] = None,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a JSON file from a URL."""
    args: Dict[str, Any] = {
        "url": url,
        "headers": headers,
        "max_length": max_length,
        "start_index": start_index,
        "proxy": proxy,
    }
    return _tool_error_boundary("fetch_json", args, parse_request_payload, Fetcher.json)


@mcp.tool(name="fetch_url", description=FETCH_URL_DESC)
def fetch_url(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_length: Optional[int] = None,
    start_index: Optional[int] = None,
    proxy: Optional[str] = None,
    workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch a URL and return fully parsed main-page content with structured metadata."""
    args: Dict[str, Any] = {
        "url": url,
        "headers": headers,
        "max_length": max_length,
        "start_index": start_index,
        "proxy": proxy,
        "workers": workers,
    }
    return _tool_error_boundary("fetch_url", args, parse_fetch_url_payload, Fetcher.fetch_url)


@mcp.tool(
    name="fetch_readable",
    description=FETCH_READABLE_DESC,
)
def fetch_readable(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_length: Optional[int] = None,
    start_index: Optional[int] = None,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a website and return its main content parsed by Mozilla Readability, converted to Markdown. Strips away navigation, ads, and boilerplate. Ideal for articles and blog posts."""
    args: Dict[str, Any] = {
        "url": url,
        "headers": headers,
        "max_length": max_length,
        "start_index": start_index,
        "proxy": proxy,
    }
    return _tool_error_boundary("fetch_readable", args, parse_request_payload, Fetcher.readable)


@mcp.tool(name="fetch_youtube_transcript", description=FETCH_YOUTUBE_DESC)
def fetch_youtube_transcript(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    max_length: Optional[int] = None,
    start_index: Optional[int] = None,
    proxy: Optional[str] = None,
    lang: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a YouTube video page and extract its captions/transcript."""
    args: Dict[str, Any] = {
        "url": url,
        "headers": headers,
        "max_length": max_length,
        "start_index": start_index,
        "proxy": proxy,
        "lang": lang,
    }
    return _tool_error_boundary(
        "fetch_youtube_transcript",
        args,
        parse_youtube_payload,
        Fetcher.youtube_transcript,
    )


@mcp.tool(name="web_search", description=WEB_SEARCH_DESC)
def web_search(
    query: str,
    max_results: Optional[int] = None,
    region: Optional[str] = None,
    safesearch: Optional[str] = None,
    workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Search the web by keyword and return ranked results."""
    args: Dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "region": region,
        "safesearch": safesearch,
        "workers": workers,
    }
    return _tool_error_boundary("web_search", args, parse_web_search_payload, Fetcher.web_search)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    transport = os.environ.get("FASTMCP_TRANSPORT", "stdio").strip().lower()
    mount_path = os.environ.get("FASTMCP_MOUNT_PATH")
    if transport not in ("stdio", "sse", "streamable-http"):
        print(
            f'Invalid FASTMCP_TRANSPORT="{transport}"; falling back to "stdio"',
            file=sys.stderr,
        )
        transport = "stdio"

    try:
        if transport == "stdio":
            mcp.run(transport="stdio")
        elif transport == "sse":
            mcp.run(transport="sse", mount_path=mount_path)
        else:
            mcp.run(transport="streamable-http", mount_path=mount_path)
    except Exception:
        print("Fatal error in main():", file=sys.stderr)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
