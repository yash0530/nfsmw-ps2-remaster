#!/usr/bin/env python3
"""NFSMW: Rockport 2026 — CC0 1.0 Asset Discovery & Fetcher CLI.

Pure Python 3 standard-library client for querying and downloading CC0 1.0
public-domain textures and HDRIs from:
  - Poly Haven REST API (https://api.polyhaven.com/)
  - ambientCG v3 API (https://ambientcg.com/api/v3/)

Zero third-party dependencies required. Designed for clean-room asset curation
with SHA-256 verification, polite rate-limiting, atomic downloads, and
reproducible attribution/license lockfile generation.

Examples:
    python3 tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10
    python3 tools/fetch_cc0_assets.py search --source ambientcg --query asphalt --limit 10
    python3 tools/fetch_cc0_assets.py fetch --manifest assets_cc0/manifest.example.json --out build/cc0_raw --dry-run
    python3 tools/fetch_cc0_assets.py fetch --manifest assets_cc0/manifest.example.json --out build/cc0_raw --slot road_asphalt_dry
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

USER_AGENT = "nfsmw-ps2-remaster/0.1"
POLYHAVEN_API_BASE = "https://api.polyhaven.com"
AMBIENTCG_API_BASE = "https://ambientcg.com/api/v3/assets"
CC0_LICENSE_ID = "CC0-1.0"
CC0_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"

DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_REQUEST_DELAY_SEC = 0.35
DEFAULT_MAX_RETRIES = 3
STREAM_CHUNK_SIZE = 64 * 1024  # 64 KiB

POLYHAVEN_TYPE_MAP: Dict[int, str] = {
    0: "hdri",
    1: "texture",
    2: "model",
}

POLYHAVEN_STANDARD_TIERS: Tuple[Tuple[int, str], ...] = (
    (1024, "1k"),
    (2048, "2k"),
    (4096, "4k"),
    (8192, "8k"),
    (16384, "16k"),
    (24576, "24k"),
)


class CC0FetchError(RuntimeError):
    """Raised when an API call, manifest entry, or integrity verification fails."""


class RateLimiter:
    """Polite client-side rate limiter to avoid hammering public CC0 APIs."""

    def __init__(self, min_interval_sec: float = DEFAULT_REQUEST_DELAY_SEC) -> None:
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self._last_request_monotonic: float = 0.0

    def wait(self) -> None:
        if self.min_interval_sec <= 0.0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_monotonic
        if self._last_request_monotonic > 0.0 and elapsed < self.min_interval_sec:
            time.sleep(self.min_interval_sec - elapsed)
        self._last_request_monotonic = time.monotonic()


def format_bytes(num_bytes: Optional[int]) -> str:
    """Format a byte count into a concise human-readable string."""
    if num_bytes is None or num_bytes < 0:
        return "unknown"
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} GiB"


def _parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    if not header_val:
        return None
    header_val = header_val.strip()
    try:
        return max(0.0, float(header_val))
    except ValueError:
        return None


def http_get_json(
    url: str,
    limiter: RateLimiter,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Any:
    """Fetch and parse JSON from a URL with custom User-Agent and 429/5xx backoff."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        limiter.wait()
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CC0FetchError(f"Invalid JSON returned from {url}: {exc}") from exc
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 or 500 <= exc.code < 600:
                retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                backoff = retry_after if retry_after is not None else min(15.0, 1.5 * (2 ** (attempt - 1)))
                if attempt < max_retries:
                    print(
                        f"[warn] HTTP {exc.code} from {url}; retrying in {backoff:.1f}s "
                        f"(attempt {attempt}/{max_retries})...",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    continue
            raise CC0FetchError(f"HTTP {exc.code} fetching {url}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < max_retries:
                backoff = min(10.0, 1.0 * (2 ** (attempt - 1)))
                print(
                    f"[warn] Network error fetching {url}: {exc}; retrying in {backoff:.1f}s "
                    f"(attempt {attempt}/{max_retries})...",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                continue
            raise CC0FetchError(f"Network error fetching {url}: {exc}") from exc

    raise CC0FetchError(f"Failed to fetch {url} after {max_retries} attempts: {last_error}")


def hash_local_file(path: Path) -> Tuple[str, str, int]:
    """Compute (sha256_hex, md5_hex, size_bytes) for an existing file."""
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            sha256.update(chunk)
            md5.update(chunk)
            total += len(chunk)
    return sha256.hexdigest(), md5.hexdigest(), total


def download_and_verify(
    url: str,
    dest_path: Path,
    limiter: RateLimiter,
    expected_sha256: Optional[str] = None,
    expected_md5: Optional[str] = None,
    expected_size: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Tuple[str, str, int]:
    """Stream-download a URL to dest_path atomically while verifying SHA-256 / MD5 / size."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_name(dest_path.name + ".part")
    headers = {"User-Agent": USER_AGENT}
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        limiter.wait()
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sha256 = hashlib.sha256()
                md5 = hashlib.md5()
                written = 0
                with part_path.open("wb") as out_fh:
                    while True:
                        chunk = resp.read(STREAM_CHUNK_SIZE)
                        if not chunk:
                            break
                        out_fh.write(chunk)
                        sha256.update(chunk)
                        md5.update(chunk)
                        written += len(chunk)

            sha256_hex = sha256.hexdigest()
            md5_hex = md5.hexdigest()

            if expected_size is not None and written != expected_size:
                part_path.unlink(missing_ok=True)
                raise CC0FetchError(
                    f"Size mismatch for {dest_path.name}: expected {expected_size} bytes, got {written}"
                )
            if expected_md5 and md5_hex.lower() != expected_md5.lower():
                part_path.unlink(missing_ok=True)
                raise CC0FetchError(
                    f"MD5 mismatch for {dest_path.name}: expected {expected_md5}, got {md5_hex}"
                )
            if expected_sha256 and sha256_hex.lower() != expected_sha256.lower():
                part_path.unlink(missing_ok=True)
                raise CC0FetchError(
                    f"SHA-256 mismatch for {dest_path.name}: expected {expected_sha256}, got {sha256_hex}"
                )

            os.replace(part_path, dest_path)
            return sha256_hex, md5_hex, written

        except urllib.error.HTTPError as exc:
            part_path.unlink(missing_ok=True)
            last_error = exc
            if exc.code == 429 or 500 <= exc.code < 600:
                retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                backoff = retry_after if retry_after is not None else min(15.0, 1.5 * (2 ** (attempt - 1)))
                if attempt < max_retries:
                    print(
                        f"[warn] HTTP {exc.code} downloading {url}; retrying in {backoff:.1f}s...",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    continue
            raise CC0FetchError(f"HTTP {exc.code} downloading {url}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            part_path.unlink(missing_ok=True)
            last_error = exc
            if attempt < max_retries:
                backoff = min(10.0, 1.0 * (2 ** (attempt - 1)))
                print(
                    f"[warn] Download error for {url}: {exc}; retrying in {backoff:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                continue
            raise CC0FetchError(f"Download error for {url}: {exc}") from exc

    raise CC0FetchError(f"Failed to download {url}: {last_error}")


# ---------------------------------------------------------------------------
# Poly Haven API helpers
# ---------------------------------------------------------------------------


def _polyhaven_resolutions_from_max(max_res: Any) -> List[str]:
    """Derive standard Poly Haven resolution tiers from metadata max_resolution=[w, h]."""
    if not isinstance(max_res, list) or not max_res:
        return []
    try:
        max_dim = max(int(x) for x in max_res)
    except (TypeError, ValueError):
        return []
    tiers = [label for dim, label in POLYHAVEN_STANDARD_TIERS if dim <= max_dim]
    return tiers or [f"{max_dim}px"]


def _resolution_sort_key(res_str: str) -> Tuple[int, str]:
    match = re.search(r"(\d+)\s*[kK]", res_str)
    if match:
        return (int(match.group(1)), res_str.lower())
    return (999999, res_str.lower())


def search_polyhaven(
    query: str,
    limit: int,
    asset_type: str,
    limiter: RateLimiter,
    timeout: float,
    exact_files: bool = False,
) -> List[Dict[str, Any]]:
    """Search Poly Haven CC0 textures and HDRIs via /assets (and optionally /files/{id})."""
    params: Dict[str, str] = {}
    if asset_type in ("textures", "hdris", "models"):
        params["t"] = asset_type
    url = f"{POLYHAVEN_API_BASE}/assets"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    catalog = http_get_json(url, limiter=limiter, timeout=timeout)
    if not isinstance(catalog, dict):
        raise CC0FetchError("Unexpected response format from Poly Haven /assets endpoint.")

    tokens = [tok for tok in re.split(r"[\s,_-]+", query.strip().lower()) if tok]
    scored: List[Tuple[int, int, str, Dict[str, Any]]] = []

    for asset_id, meta in catalog.items():
        if not isinstance(meta, dict):
            continue
        raw_type = meta.get("type")
        if asset_type == "all" and raw_type not in (0, 1):
            # Default "all" for this texture pipeline prioritizes textures & HDRIs over 3D models.
            continue

        name = str(meta.get("name") or asset_id)
        tags = [str(t) for t in meta.get("tags", []) if isinstance(t, (str, int, float))]
        categories = [str(c) for c in meta.get("categories", []) if isinstance(c, (str, int, float))]
        searchable = " ".join([asset_id, name, *tags, *categories]).lower()

        if tokens:
            if not all(tok in searchable for tok in tokens):
                continue
            score = 0
            for tok in tokens:
                if tok in asset_id.lower():
                    score += 6
                if tok in name.lower():
                    score += 4
                if any(tok == c.lower() for c in categories):
                    score += 3
                if any(tok == t.lower() for t in tags):
                    score += 2
        else:
            score = 1

        dl_count = int(meta.get("download_count") or 0)
        scored.append((score, dl_count, asset_id, meta))

    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    results: List[Dict[str, Any]] = []

    for _score, _dl_count, asset_id, meta in scored[: max(0, limit)]:
        kind = POLYHAVEN_TYPE_MAP.get(meta.get("type"), "unknown")
        resolutions = _polyhaven_resolutions_from_max(meta.get("max_resolution"))
        available_maps: List[str] = []

        if exact_files:
            files_data = http_get_json(f"{POLYHAVEN_API_BASE}/files/{asset_id}", limiter=limiter, timeout=timeout)
            if isinstance(files_data, dict):
                available_maps = [
                    k for k, v in files_data.items() if isinstance(v, dict) and k not in ("blend", "gltf", "mtlx")
                ]
                primary_key = "hdri" if "hdri" in files_data else ("Diffuse" if "Diffuse" in files_data else None)
                if primary_key and isinstance(files_data.get(primary_key), dict):
                    resolutions = sorted(files_data[primary_key].keys(), key=_resolution_sort_key)

        results.append(
            {
                "source": "polyhaven",
                "id": asset_id,
                "name": meta.get("name", asset_id),
                "type": kind,
                "categories": meta.get("categories", []),
                "tags": meta.get("tags", []),
                "resolutions": resolutions,
                "maps": available_maps,
                "authors": list(meta.get("authors", {}).keys()) if isinstance(meta.get("authors"), dict) else [],
                "license": CC0_LICENSE_ID,
                "page_url": f"https://polyhaven.com/a/{asset_id}",
            }
        )

    return results


def resolve_polyhaven_files(
    asset_id: str,
    asset_type: str,
    resolution: str,
    preferred_format: str,
    requested_maps: Sequence[str],
    limiter: RateLimiter,
    timeout: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Resolve direct Poly Haven download descriptors from /files/{id} and /info/{id}."""
    files_url = f"{POLYHAVEN_API_BASE}/files/{urllib.parse.quote(asset_id)}"
    files_data = http_get_json(files_url, limiter=limiter, timeout=timeout)
    if not isinstance(files_data, dict):
        raise CC0FetchError(f"Poly Haven /files/{asset_id} returned unexpected payload.")

    # Fetch lightweight info metadata for attribution authors/name.
    info_meta: Dict[str, Any] = {}
    try:
        info_url = f"{POLYHAVEN_API_BASE}/info/{urllib.parse.quote(asset_id)}"
        raw_info = http_get_json(info_url, limiter=limiter, timeout=timeout)
        if isinstance(raw_info, dict):
            info_meta = raw_info
    except CC0FetchError:
        info_meta = {}

    if requested_maps:
        maps_to_fetch = list(requested_maps)
    elif asset_type == "hdri" or "hdri" in files_data:
        maps_to_fetch = ["hdri"]
    else:
        maps_to_fetch = ["Diffuse"]

    res_norm = resolution.strip().lower()
    fmt_norm = preferred_format.strip().lower()
    resolved: List[Dict[str, Any]] = []

    # Case-insensitive map lookup table
    key_lookup = {k.lower(): k for k in files_data.keys()}

    for req_map in maps_to_fetch:
        actual_map_key = key_lookup.get(req_map.lower())
        if actual_map_key is None and req_map.lower() in ("albedo", "color", "basecolor"):
            actual_map_key = key_lookup.get("diffuse")
        if actual_map_key is None:
            available = [k for k in files_data.keys() if k not in ("blend", "gltf", "mtlx")]
            raise CC0FetchError(
                f"Poly Haven asset '{asset_id}' has no map '{req_map}'. Available: {', '.join(available)}"
            )

        map_node = files_data[actual_map_key]
        if not isinstance(map_node, dict):
            raise CC0FetchError(f"Unexpected map structure for {asset_id}.{actual_map_key}")

        # Special case: Poly Haven 'tonemapped' is a direct leaf {url, size, md5}
        if "url" in map_node:
            resolved.append(
                {
                    "map": actual_map_key,
                    "resolution": "native",
                    "format": Path(urllib.parse.urlparse(map_node["url"]).path).suffix.lstrip(".") or "jpg",
                    "url": map_node["url"],
                    "size": map_node.get("size"),
                    "md5": map_node.get("md5"),
                }
            )
            continue

        res_lookup = {k.lower(): k for k in map_node.keys()}
        chosen_res = res_lookup.get(res_norm)
        if chosen_res is None:
            avail_res = sorted(map_node.keys(), key=_resolution_sort_key)
            raise CC0FetchError(
                f"Poly Haven asset '{asset_id}' map '{actual_map_key}' has no resolution '{resolution}'. "
                f"Available: {', '.join(avail_res)}"
            )

        fmt_node = map_node[chosen_res]
        if not isinstance(fmt_node, dict):
            raise CC0FetchError(f"Unexpected resolution structure for {asset_id}.{actual_map_key}.{chosen_res}")

        fmt_lookup = {k.lower(): k for k in fmt_node.keys()}
        chosen_fmt = fmt_lookup.get(fmt_norm)
        if chosen_fmt is None:
            # Sensible fallback order if requested format isn't published for this map
            for fallback in ("png", "jpg", "hdr", "exr"):
                if fallback in fmt_lookup:
                    chosen_fmt = fmt_lookup[fallback]
                    break
        if chosen_fmt is None:
            raise CC0FetchError(
                f"Poly Haven asset '{asset_id}' map '{actual_map_key}' ({chosen_res}) has no format '{preferred_format}'. "
                f"Available: {', '.join(fmt_node.keys())}"
            )

        leaf = fmt_node[chosen_fmt]
        if not isinstance(leaf, dict) or "url" not in leaf:
            raise CC0FetchError(f"Missing download URL for {asset_id}.{actual_map_key}.{chosen_res}.{chosen_fmt}")

        resolved.append(
            {
                "map": actual_map_key,
                "resolution": chosen_res,
                "format": chosen_fmt,
                "url": leaf["url"],
                "size": leaf.get("size"),
                "md5": leaf.get("md5"),
            }
        )

    return resolved, info_meta


# ---------------------------------------------------------------------------
# ambientCG v3 API helpers
# ---------------------------------------------------------------------------


def _extract_ambientcg_downloads(asset_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract normalized download entries from ambientCG v3 (or fallback v2) payloads."""
    entries: List[Dict[str, Any]] = []

    # Primary ambientCG v3 schema: asset["downloads"] -> list[{attributes, extension, url, size}]
    raw_downloads = asset_obj.get("downloads")
    if isinstance(raw_downloads, list):
        for item in raw_downloads:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("downloadLink") or item.get("rawLink")
            if not url:
                continue
            entries.append(
                {
                    "attributes": str(item.get("attributes") or item.get("attribute") or ""),
                    "extension": str(item.get("extension") or Path(urllib.parse.urlparse(url).path).suffix.lstrip(".")),
                    "url": str(url),
                    "size": int(item["size"]) if isinstance(item.get("size"), int) else None,
                }
            )
        return entries

    # Defensive fallback for nested downloadFolders schema
    download_folders = asset_obj.get("downloadFolders")
    if isinstance(download_folders, dict):
        for folder in download_folders.values():
            if not isinstance(folder, dict):
                continue
            cats = folder.get("downloadFiletypeCategories")
            if not isinstance(cats, dict):
                continue
            for ext, cat_obj in cats.items():
                if not isinstance(cat_obj, dict):
                    continue
                for item in cat_obj.get("downloads", []):
                    if not isinstance(item, dict):
                        continue
                    url = item.get("downloadLink") or item.get("rawLink") or item.get("url")
                    if not url:
                        continue
                    entries.append(
                        {
                            "attributes": str(item.get("attribute") or item.get("attributes") or ""),
                            "extension": str(ext),
                            "url": str(url),
                            "size": int(item["size"]) if isinstance(item.get("size"), int) else None,
                        }
                    )

    return entries


def search_ambientcg(
    query: str,
    limit: int,
    asset_type: str,
    limiter: RateLimiter,
    timeout: float,
) -> List[Dict[str, Any]]:
    """Search ambientCG v3 CC0 assets via https://ambientcg.com/api/v3/assets."""
    params: Dict[str, str] = {
        "include": "downloads,tags",
        "limit": str(max(1, min(limit, 100))),
    }
    if asset_type and asset_type != "all":
        params["type"] = "material" if asset_type in ("textures", "texture", "material") else asset_type
    else:
        params["type"] = "material"

    if query.strip():
        params["q"] = query.strip()

    url = f"{AMBIENTCG_API_BASE}?{urllib.parse.urlencode(params)}"
    payload = http_get_json(url, limiter=limiter, timeout=timeout)
    if not isinstance(payload, dict):
        raise CC0FetchError("Unexpected response format from ambientCG v3 API.")

    raw_assets = payload.get("assets")
    if raw_assets is None:
        raw_assets = payload.get("foundAssets", [])
    if not isinstance(raw_assets, list):
        raise CC0FetchError("ambientCG API response missing 'assets' list.")

    results: List[Dict[str, Any]] = []
    for asset in raw_assets[: max(0, limit)]:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("id") or asset.get("assetId") or "unknown")
        downloads = _extract_ambientcg_downloads(asset)
        resolutions = [d["attributes"] for d in downloads if d.get("attributes")]
        tags = [str(t) for t in asset.get("tags", []) if isinstance(t, (str, int, float))]

        results.append(
            {
                "source": "ambientcg",
                "id": asset_id,
                "name": asset.get("displayName") or asset_id,
                "type": params["type"],
                "categories": [asset.get("category")] if asset.get("category") else [],
                "tags": tags,
                "resolutions": resolutions,
                "maps": ["zip_pack"],
                "authors": ["ambientCG (Lennart Demes)"],
                "license": CC0_LICENSE_ID,
                "page_url": f"https://ambientcg.com/view?id={asset_id}",
            }
        )

    return results


def resolve_ambientcg_files(
    asset_id: str,
    resolution: str,
    preferred_format: str,
    limiter: RateLimiter,
    timeout: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Resolve an ambientCG v3 download bundle matching resolution and format (e.g. 2K-PNG)."""
    params = {
        "id": asset_id,
        "include": "downloads,tags",
    }
    url = f"{AMBIENTCG_API_BASE}?{urllib.parse.urlencode(params)}"
    payload = http_get_json(url, limiter=limiter, timeout=timeout)
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if assets is None and isinstance(payload, dict):
        assets = payload.get("foundAssets")
    if not isinstance(assets, list) or not assets:
        raise CC0FetchError(f"ambientCG asset '{asset_id}' not found via v3 API.")

    asset = assets[0]
    downloads = _extract_ambientcg_downloads(asset)
    if not downloads:
        raise CC0FetchError(f"ambientCG asset '{asset_id}' has no available downloads.")

    res_up = resolution.strip().upper()
    fmt_up = preferred_format.strip().upper()
    target_attr = f"{res_up}-{fmt_up}" if fmt_up else res_up

    # Priority matching: exact "2K-PNG" -> prefix "2K-" with PNG/JPG -> any containing res_up
    chosen: Optional[Dict[str, Any]] = None
    for dl in downloads:
        if dl["attributes"].upper() == target_attr:
            chosen = dl
            break
    if chosen is None:
        for dl in downloads:
            if dl["attributes"].upper().startswith(f"{res_up}-"):
                chosen = dl
                break
    if chosen is None:
        for dl in downloads:
            if res_up in dl["attributes"].upper():
                chosen = dl
                break

    if chosen is None:
        avail = [d["attributes"] for d in downloads]
        raise CC0FetchError(
            f"ambientCG asset '{asset_id}' has no download matching '{target_attr}'. "
            f"Available: {', '.join(avail)}"
        )

    resolved = [
        {
            "map": "material_pack",
            "resolution": chosen["attributes"],
            "format": chosen["extension"] or "zip",
            "url": chosen["url"],
            "size": chosen["size"],
            "md5": None,
        }
    ]
    return resolved, asset


# ---------------------------------------------------------------------------
# Manifest loading, fetching, and lockfile writing
# ---------------------------------------------------------------------------


@dataclass
class PlannedDownload:
    slot: str
    description: str
    source: str
    asset_id: str
    asset_type: str
    map_name: str
    resolution: str
    file_format: str
    url: str
    dest_path: Path
    expected_size: Optional[int]
    expected_md5: Optional[str]
    expected_sha256: Optional[str]
    license_id: str
    license_url: str
    page_url: str
    authors: List[str] = field(default_factory=list)


def _load_manifest(manifest_path: Path) -> Dict[str, Any]:
    if not manifest_path.is_file():
        raise CC0FetchError(f"Manifest file not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CC0FetchError(f"Invalid JSON in manifest {manifest_path}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        raise CC0FetchError(f"Manifest {manifest_path} must be a JSON object with an 'assets' array.")
    return data


def _load_existing_lockfile(lockfile_path: Path) -> Dict[str, Dict[str, Any]]:
    """Index existing lockfile entries by relative destination path for SHA-256 verification."""
    if not lockfile_path.is_file():
        return {}
    try:
        data = json.loads(lockfile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("files", [])
    indexed: Dict[str, Dict[str, Any]] = {}
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict) and item.get("relative_path"):
                indexed[str(item["relative_path"])] = item
    return indexed


def _build_dest_filename(asset_id: str, map_name: str, resolution: str, file_format: str, url: str) -> str:
    parsed_name = Path(urllib.parse.urlparse(url).path).name
    query_params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "file" in query_params and query_params["file"]:
        candidate = Path(query_params["file"][0]).name
        if candidate:
            return candidate
    if parsed_name and "." in parsed_name and not parsed_name.endswith("."):
        return parsed_name
    safe_res = re.sub(r"[^A-Za-z0-9._-]+", "_", resolution)
    safe_map = re.sub(r"[^A-Za-z0-9._-]+", "_", map_name)
    return f"{asset_id}_{safe_map}_{safe_res}.{file_format}"


def _write_attribution_markdown(out_dir: Path, lock_records: Sequence[Dict[str, Any]]) -> Path:
    md_path = out_dir / "ATTRIBUTION_CC0.md"
    lines = [
        "# CC0 1.0 Asset Attribution & Verification Ledger",
        "",
        f"Generated: `{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}`  ",
        "All assets listed below are verified public-domain **CC0 1.0 Universal** downloads.",
        "",
        "| Slot | Source | Asset ID | Resolution | File | Size | SHA-256 (prefix) | License |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for rec in lock_records:
        sha_short = str(rec.get("sha256", ""))[:16]
        size_str = format_bytes(rec.get("size_bytes"))
        page_url = rec.get("page_url", "")
        asset_cell = f"[{rec['asset_id']}]({page_url})" if page_url else rec["asset_id"]
        lines.append(
            f"| `{rec['slot']}` | {rec['source']} | {asset_cell} | `{rec['resolution']}` "
            f"| `{rec['relative_path']}` | {size_str} | `{sha_short}...` | `{rec['license']}` |"
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    limiter = RateLimiter(min_interval_sec=args.delay)
    sources = ["polyhaven", "ambientcg"] if args.source == "all" else [args.source]
    all_results: List[Dict[str, Any]] = []

    for src in sources:
        if src == "polyhaven":
            hits = search_polyhaven(
                query=args.query,
                limit=args.limit,
                asset_type=args.asset_type,
                limiter=limiter,
                timeout=args.timeout,
                exact_files=args.exact_files,
            )
            all_results.extend(hits)
        elif src == "ambientcg":
            hits = search_ambientcg(
                query=args.query,
                limit=args.limit,
                asset_type=args.asset_type,
                limiter=limiter,
                timeout=args.timeout,
            )
            all_results.extend(hits)

    if args.json:
        print(json.dumps({"query": args.query, "source": args.source, "results": all_results}, indent=2))
        return 0

    if not all_results:
        print(f"No CC0 assets found for source='{args.source}' query='{args.query}'.")
        return 0

    print(
        f"Found {len(all_results)} CC0-1.0 asset(s) "
        f"[source={args.source}, query='{args.query or '*'}']:\n"
    )
    for idx, item in enumerate(all_results, start=1):
        res_preview = ", ".join(item["resolutions"][:8]) if item["resolutions"] else "unknown"
        if len(item["resolutions"]) > 8:
            res_preview += f", ... (+{len(item['resolutions']) - 8} more)"
        tags_preview = ", ".join(item["tags"][:8]) if item["tags"] else "-"
        print(f"{idx:2d}. [{item['source']}] {item['id']} ({item['type']}, {item['license']})")
        print(f"    Name        : {item['name']}")
        print(f"    Resolutions : {res_preview}")
        print(f"    Tags        : {tags_preview}")
        if item.get("maps"):
            print(f"    Maps        : {', '.join(item['maps'][:8])}")
        print(f"    URL         : {item['page_url']}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    lockfile_path = Path(args.lockfile).expanduser().resolve() if args.lockfile else out_dir / "cc0_lockfile.json"

    manifest = _load_manifest(manifest_path)
    entries = manifest["assets"]

    slot_filter: Optional[set] = None
    if args.slot:
        slot_filter = set()
        for raw_slot in args.slot:
            for s in raw_slot.split(","):
                s = s.strip()
                if s:
                    slot_filter.add(s)

    selected_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and (slot_filter is None or entry.get("slot") in slot_filter)
    ]

    if not selected_entries:
        raise CC0FetchError(
            f"No manifest entries matched slot filter: {sorted(slot_filter) if slot_filter else 'all'}"
        )

    # Validate license policy up front before touching the network.
    for entry in selected_entries:
        lic = str(entry.get("license") or "").strip()
        if lic != CC0_LICENSE_ID:
            raise CC0FetchError(
                f"Manifest slot '{entry.get('slot')}' has license '{lic}', expected '{CC0_LICENSE_ID}'. "
                "Refusing non-CC0 asset per ASSETS_LICENSE.md policy."
            )

    if args.dry_run and args.offline:
        print(f"[dry-run/offline] Validated {len(selected_entries)} CC0-1.0 slot(s) in {manifest_path}:")
        for entry in selected_entries:
            res = args.resolution or entry.get("resolution", "2k")
            print(
                f"  - {entry['slot']:22s} | {entry['source']:9s} | {entry['asset_id']:28s} "
                f"| res={res} | fmt={entry.get('format', 'png')} | {entry['license']}"
            )
        print(f"\nTarget output directory (not created in dry-run): {out_dir}")
        return 0

    limiter = RateLimiter(min_interval_sec=args.delay)
    existing_lock = _load_existing_lockfile(lockfile_path)
    planned: List[PlannedDownload] = []

    print(f"Resolving {len(selected_entries)} CC0-1.0 asset slot(s) from {manifest_path.name}...")
    for entry in selected_entries:
        slot = str(entry["slot"])
        source = str(entry["source"]).strip().lower()
        asset_id = str(entry["asset_id"]).strip()
        asset_type = str(entry.get("asset_type") or ("material" if source == "ambientcg" else "texture"))
        resolution = str(args.resolution or entry.get("resolution") or "2k")
        preferred_format = str(entry.get("format") or ("hdr" if asset_type == "hdri" else "png"))
        requested_maps = entry.get("maps") if isinstance(entry.get("maps"), list) else []
        pinned_sha256 = entry.get("sha256")

        if source == "polyhaven":
            resolved_files, meta = resolve_polyhaven_files(
                asset_id=asset_id,
                asset_type=asset_type,
                resolution=resolution,
                preferred_format=preferred_format,
                requested_maps=requested_maps,
                limiter=limiter,
                timeout=args.timeout,
            )
            authors = list(meta.get("authors", {}).keys()) if isinstance(meta.get("authors"), dict) else []
            page_url = f"https://polyhaven.com/a/{asset_id}"
        elif source == "ambientcg":
            resolved_files, meta = resolve_ambientcg_files(
                asset_id=asset_id,
                resolution=resolution,
                preferred_format=preferred_format,
                limiter=limiter,
                timeout=args.timeout,
            )
            authors = ["ambientCG (Lennart Demes)"]
            page_url = f"https://ambientcg.com/view?id={asset_id}"
        else:
            raise CC0FetchError(f"Unsupported source '{source}' in slot '{slot}'. Use 'polyhaven' or 'ambientcg'.")

        for rf in resolved_files:
            filename = _build_dest_filename(
                asset_id=asset_id,
                map_name=rf["map"],
                resolution=rf["resolution"],
                file_format=rf["format"],
                url=rf["url"],
            )
            dest_path = out_dir / slot / filename
            rel_key = f"{slot}/{filename}"
            expected_sha = pinned_sha256
            if not expected_sha and rel_key in existing_lock:
                expected_sha = existing_lock[rel_key].get("sha256")

            planned.append(
                PlannedDownload(
                    slot=slot,
                    description=str(entry.get("description") or ""),
                    source=source,
                    asset_id=asset_id,
                    asset_type=asset_type,
                    map_name=rf["map"],
                    resolution=rf["resolution"],
                    file_format=rf["format"],
                    url=rf["url"],
                    dest_path=dest_path,
                    expected_size=rf.get("size"),
                    expected_md5=rf.get("md5"),
                    expected_sha256=expected_sha,
                    license_id=CC0_LICENSE_ID,
                    license_url=str(entry.get("license_url") or CC0_LICENSE_URL),
                    page_url=page_url,
                    authors=authors,
                )
            )

    total_bytes = sum(p.expected_size for p in planned if isinstance(p.expected_size, int))
    print(f"\nResolved {len(planned)} file(s) (estimated total: {format_bytes(total_bytes)}):\n")

    for idx, item in enumerate(planned, start=1):
        rel = item.dest_path.relative_to(out_dir)
        md5_info = f" md5={item.expected_md5[:10]}..." if item.expected_md5 else ""
        print(
            f"  [{idx:2d}/{len(planned):2d}] {item.slot} ({item.source}:{item.asset_id}) "
            f"-> {rel} [{item.resolution}, {format_bytes(item.expected_size)}{md5_info}]"
        )
        print(f"          URL: {item.url}")

    if args.dry_run:
        print("\n[dry-run] No files downloaded and no lockfile written.")
        print(f"[dry-run] Re-run without --dry-run to fetch into: {out_dir}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    lock_records: List[Dict[str, Any]] = []
    downloaded_count = 0
    cached_count = 0

    print(f"\nDownloading into {out_dir} ...")
    for idx, item in enumerate(planned, start=1):
        rel_path = str(item.dest_path.relative_to(out_dir))
        if item.dest_path.is_file() and not args.force:
            sha256_hex, md5_hex, size_bytes = hash_local_file(item.dest_path)
            size_ok = item.expected_size is None or size_bytes == item.expected_size
            md5_ok = not item.expected_md5 or md5_hex.lower() == item.expected_md5.lower()
            sha_ok = not item.expected_sha256 or sha256_hex.lower() == item.expected_sha256.lower()
            if size_ok and md5_ok and sha_ok:
                print(
                    f"  [{idx:2d}/{len(planned):2d}] [cached/verified] {rel_path} "
                    f"(sha256={sha256_hex[:16]}...)"
                )
                cached_count += 1
            else:
                print(
                    f"  [{idx:2d}/{len(planned):2d}] [re-downloading] {rel_path} "
                    f"(existing file failed checksum/size check)"
                )
                sha256_hex, md5_hex, size_bytes = download_and_verify(
                    url=item.url,
                    dest_path=item.dest_path,
                    limiter=limiter,
                    expected_sha256=item.expected_sha256,
                    expected_md5=item.expected_md5,
                    expected_size=item.expected_size,
                    timeout=args.timeout,
                )
                downloaded_count += 1
        else:
            print(f"  [{idx:2d}/{len(planned):2d}] [fetching] {rel_path} ({format_bytes(item.expected_size)})...")
            sha256_hex, md5_hex, size_bytes = download_and_verify(
                url=item.url,
                dest_path=item.dest_path,
                limiter=limiter,
                expected_sha256=item.expected_sha256,
                expected_md5=item.expected_md5,
                expected_size=item.expected_size,
                timeout=args.timeout,
            )
            print(f"          [verified] sha256={sha256_hex[:16]}... ({format_bytes(size_bytes)})")
            downloaded_count += 1

        lock_records.append(
            {
                "slot": item.slot,
                "description": item.description,
                "source": item.source,
                "asset_id": item.asset_id,
                "asset_type": item.asset_type,
                "map": item.map_name,
                "resolution": item.resolution,
                "format": item.file_format,
                "relative_path": rel_path,
                "url": item.url,
                "page_url": item.page_url,
                "authors": item.authors,
                "license": item.license_id,
                "license_url": item.license_url,
                "size_bytes": size_bytes,
                "sha256": sha256_hex,
                "md5": md5_hex,
                "fetched_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    lockfile_payload = {
        "schema_version": "1.0",
        "project": "NFSMW: Rockport 2026",
        "manifest_source": str(manifest_path),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "license_policy": CC0_LICENSE_ID,
        "files": lock_records,
    }
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    lockfile_path.write_text(json.dumps(lockfile_payload, indent=2) + "\n", encoding="utf-8")
    md_path = _write_attribution_markdown(out_dir, lock_records)

    print(
        f"\nDone: {downloaded_count} downloaded, {cached_count} verified from cache.\n"
        f"Lockfile    : {lockfile_path}\n"
        f"Attribution : {md_path}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_cc0_assets.py",
        description=(
            "Search and fetch CC0 1.0 textures and HDRIs from Poly Haven and ambientCG "
            "for NFSMW: Rockport 2026 (pure Python stdlib, zero external dependencies)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10\n"
            "  python3 tools/fetch_cc0_assets.py search --source ambientcg --query asphalt --limit 10\n"
            "  python3 tools/fetch_cc0_assets.py fetch --manifest assets_cc0/manifest.example.json "
            "--out build/cc0_raw --dry-run\n"
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SEC,
        help=f"Polite delay in seconds between API requests (default: {DEFAULT_REQUEST_DELAY_SEC}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"HTTP request timeout in seconds (default: {DEFAULT_TIMEOUT_SEC}).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # search subcommand
    p_search = subparsers.add_parser(
        "search",
        help="Search Poly Haven or ambientCG CC0 1.0 textures and HDRIs.",
        description="Search public CC0 1.0 asset catalogs and display asset IDs, tags, and available resolutions.",
    )
    p_search.add_argument(
        "--source",
        required=True,
        choices=["polyhaven", "ambientcg", "all"],
        help="CC0 asset catalog to query.",
    )
    p_search.add_argument(
        "--query",
        default="",
        help="Keyword query (e.g. asphalt, concrete, brick, sunset, carbon).",
    )
    p_search.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of results per source (default: 10).",
    )
    p_search.add_argument(
        "--asset-type",
        default="all",
        choices=["all", "textures", "hdris", "material"],
        help="Filter by asset type (default: all).",
    )
    p_search.add_argument(
        "--exact-files",
        action="store_true",
        help="For Poly Haven, query /files/{id} per match to display exact map names and resolutions.",
    )
    p_search.add_argument(
        "--json",
        action="store_true",
        help="Emit search results as formatted JSON.",
    )
    p_search.set_defaults(func=cmd_search)

    # fetch subcommand
    p_fetch = subparsers.add_parser(
        "fetch",
        help="Download CC0 1.0 assets from a manifest with SHA-256 verification and lockfile output.",
        description=(
            "Resolve and download CC0 1.0 assets specified in a JSON manifest, verifying SHA-256/MD5 "
            "integrity and writing a machine-readable license & attribution lockfile."
        ),
    )
    p_fetch.add_argument(
        "--manifest",
        required=True,
        help="Path to the CC0 asset manifest JSON (e.g. assets_cc0/manifest.example.json).",
    )
    p_fetch.add_argument(
        "--out",
        required=True,
        help="Output directory for downloaded CC0 assets and attribution lockfile (e.g. build/cc0_raw).",
    )
    p_fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve live API URLs and sizes without downloading files or writing to disk.",
    )
    p_fetch.add_argument(
        "--offline",
        action="store_true",
        help="With --dry-run, validate the manifest locally without making network requests.",
    )
    p_fetch.add_argument(
        "--slot",
        action="append",
        help="Fetch only specific slot(s) from the manifest (repeatable or comma-separated).",
    )
    p_fetch.add_argument(
        "--resolution",
        help="Override manifest resolution for all selected slots (e.g. 1k, 2k, 4k).",
    )
    p_fetch.add_argument(
        "--lockfile",
        help="Custom path for JSON lockfile (default: <out>/cc0_lockfile.json).",
    )
    p_fetch.add_argument(
        "--force",
        action="store_true",
        help="Re-download files even if they already exist locally and pass verification.",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CC0FetchError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[interrupted] Aborted by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
