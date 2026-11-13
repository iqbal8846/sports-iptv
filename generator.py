#!/usr/bin/env python3
"""
Sports M3U Playlist Generator (Football & Cricket)
==================================================
Designed to be clean and easy to read (Java / Dart / PHP friendly style).
Uses ONLY Python Standard Library -> Zero `pip install` needed!

Pipeline:
  1. Load `config.json`
  2. Resolve configured M3U URLs + GitHub Repo links + Auto-Discover updated GitHub repos
  3. Parse `#EXTINF` channels & filter out DRM-locked or VOD (.mp4/.mkv) links
  4. Categorize into Cricket, Football, Multi-Sport, and General Live Sports
  5. Multi-threaded stream health & speed check (with smart BDIX cloud protection)
  6. Generate `sports_all.m3u`, `football.m3u`, `cricket.m3u`, `status.json`, and `index.html`
"""

import argparse
import concurrent.futures
import datetime
import ipaddress
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# =====================================================================
# 1. DATA MODELS (Similar to Dart / Java Model Classes)
# =====================================================================

@dataclass
class SourcePlaylist:
    name: str
    url: str
    is_bdix: bool = False
    default_sports_group: bool = False


@dataclass
class Channel:
    name: str
    stream_url: str
    logo: str = ""
    tvg_id: str = ""
    original_group: str = ""
    category: str = ""           # "Cricket", "Football", "Multi-Sport", "Live Sports"
    source_name: str = ""
    is_bdix: bool = False
    vlc_opts: List[str] = field(default_factory=list)
    custom_user_agent: str = ""
    custom_referrer: str = ""
    is_alive: bool = False
    latency_ms: int = 9999


# =====================================================================
# 2. HTTP & GITHUB API HELPERS
# =====================================================================

def create_ssl_context() -> ssl.SSLContext:
    """Allow standard HTTPS connections even when IPTV servers have self-signed certs."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


SSL_CTX = create_ssl_context()


def build_github_headers(user_agent: str) -> Dict[str, str]:
    """Build headers for GitHub API. Automatically uses GITHUB_TOKEN in GitHub Actions."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_text(url: str, user_agent: str, timeout: int = 15, extra_headers: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Download text/JSON from a URL safely."""
    headers = {"User-Agent": user_agent}
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            raw_bytes = resp.read(10 * 1024 * 1024)  # Cap at 10 MB safety limit
            return raw_bytes.decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"  [WARN] Could not fetch {url}: {exc}")
        return None


def convert_github_blob_to_raw(url: str) -> str:
    """
    If user pastes a browser link like:
      https://github.com/user/repo/blob/main/playlist.m3u
    Automatically convert it to:
      https://raw.githubusercontent.com/user/repo/main/playlist.m3u
    """
    match = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url.strip())
    if match:
        owner, repo, rest = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return url.strip()


def extract_github_repo_slug(url: str) -> Optional[Tuple[str, str]]:
    """
    Detect if a URL points to a GitHub repository root instead of a specific .m3u file.
    Example: https://github.com/abusaeeidx/Mrgify-BDIX-IPTV -> ('abusaeeidx', 'Mrgify-BDIX-IPTV')
    """
    cleaned = url.strip().rstrip("/")
    if cleaned.lower().endswith((".m3u", ".m3u8", ".txt")):
        return None
    if "raw.githubusercontent.com" in cleaned or "/raw/" in cleaned or "/blob/" in cleaned:
        return None

    match = re.match(r"^https?://github\.com/([^/]+)/([^/#?]+)(?:/tree/[^/]+)?$", cleaned)
    if match:
        owner, repo = match.group(1), match.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo
    return None


def scan_github_repo_for_m3u(
    owner: str,
    repo: str,
    user_agent: str,
    max_files: int = 5,
    max_size_kb: int = 2500,
) -> List[str]:
    """Use GitHub public API to find .m3u / .m3u8 files inside any GitHub repository."""
    gh_headers = build_github_headers(user_agent)
    repo_api = f"https://api.github.com/repos/{owner}/{repo}"
    repo_json_text = fetch_text(repo_api, user_agent, timeout=12, extra_headers=gh_headers)
    if not repo_json_text:
        return []

    try:
        repo_info = json.loads(repo_json_text)
        default_branch = repo_info.get("default_branch", "main")
    except json.JSONDecodeError:
        return []

    tree_api = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1"
    tree_json_text = fetch_text(tree_api, user_agent, timeout=12, extra_headers=gh_headers)
    if not tree_json_text:
        return []

    try:
        tree_data = json.loads(tree_json_text)
    except json.JSONDecodeError:
        return []

    candidates: List[Tuple[int, str]] = []
    for item in tree_data.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        size_bytes = item.get("size", 0)
        lower_path = path.lower()

        if not lower_path.endswith((".m3u", ".m3u8")):
            continue
        if size_bytes <= 20 or size_bytes > (max_size_kb * 1024):
            continue

        # Prioritize sports / main playlists over obscure sub-files
        priority = 1
        if any(k in lower_path for k in ("sport", "cricket", "football", "soccer")):
            priority = 0
        elif any(k in lower_path for k in ("playlist", "index", "main", "all", "bdix", "live")):
            priority = 1
        else:
            priority = 2

        encoded_path = "/".join(urllib.parse.quote(part) for part in path.split("/"))
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{encoded_path}"
        candidates.append((priority, raw_url))

    candidates.sort(key=lambda x: x[0])
    return [url for _, url in candidates[:max_files]]


def discover_github_playlists(config: dict, existing_urls: Set[str]) -> List[SourcePlaylist]:
    """Automatically search GitHub for recently updated public M3U repositories."""
    disc_cfg = config.get("github_discovery", {})
    if not disc_cfg.get("enabled", False):
        return []

    user_agent = config["settings"]["user_agent"]
    gh_headers = build_github_headers(user_agent)
    queries = disc_cfg.get("search_queries", [])
    max_repos = disc_cfg.get("max_repos_per_query", 3)
    max_files = disc_cfg.get("max_files_per_repo", 2)
    max_size_kb = disc_cfg.get("max_file_size_kb", 2500)

    discovered: List[SourcePlaylist] = []
    seen_repos: Set[str] = set()

    print("\n[Step 1B] Running GitHub Auto-Discovery for fresh playlists...")
    for query in queries:
        encoded_q = urllib.parse.quote(query)
        search_url = f"https://api.github.com/search/repositories?q={encoded_q}&sort=updated&order=desc&per_page={max_repos}"
        resp_text = fetch_text(search_url, user_agent, timeout=12, extra_headers=gh_headers)
        if not resp_text:
            continue

        try:
            data = json.loads(resp_text)
        except json.JSONDecodeError:
            continue

        for repo_item in data.get("items", [])[:max_repos]:
            full_name = repo_item.get("full_name", "")
            if not full_name or full_name.lower() in seen_repos:
                continue
            seen_repos.add(full_name.lower())

            owner, repo = full_name.split("/", 1)
            description = (repo_item.get("description") or "").lower()
            is_bdix = "bdix" in full_name.lower() or "bdix" in description

            raw_urls = scan_github_repo_for_m3u(
                owner, repo, user_agent, max_files=max_files, max_size_kb=max_size_kb
            )
            for raw_url in raw_urls:
                if raw_url not in existing_urls:
                    existing_urls.add(raw_url)
                    discovered.append(
                        SourcePlaylist(
                            name=f"Auto:{full_name}",
                            url=raw_url,
                            is_bdix=is_bdix,
                            default_sports_group=False,
                        )
                    )
                    print(f"  [+] Discovered: {full_name} -> {raw_url.split('/')[-1]} (BDIX={is_bdix})")

    return discovered


# =====================================================================
# 3. M3U PARSER & KEYWORD CLASSIFIER
# =====================================================================

ATTR_REGEX = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


def compile_keyword_patterns(keywords: List[str]) -> List[re.Pattern]:
    """
    Smart keyword matcher for IPTV channel names:
      - Short acronyms (<= 3 chars like 'ipl', 'ski', 'gtv', 'f1'): strict word boundary \b...\b
        so 'ipl' never matches 'Triple' and 'ski' never matches 'Polski'.
      - 4-char keywords (like 'golf', 'espn', 'fifa', 'dazn'): prefix boundary \b...
        so 'GolfTV', 'ESPN2', 'DAZN1', 'FIFA+' match cleanly.
      - Longer keywords (>= 5 chars like 'star sports', 'willow', 'poker'): flexible spaces/hyphens
        so 'StarSports1', 'Star-Sports', 'WillowHD', and 'PokerGO' all match.
    """
    patterns = []
    for kw in keywords:
        kw_clean = kw.strip().lower()
        if not kw_clean:
            continue
        if len(kw_clean) <= 3:
            pat_str = r"\b" + re.escape(kw_clean) + r"\b"
        elif len(kw_clean) == 4:
            pat_str = r"\b" + re.escape(kw_clean)
        else:
            parts = [re.escape(p) for p in re.split(r"[\s\-_]+", kw_clean) if p]
            pat_str = r"\b" + r"[\s\-_]*".join(parts)
        patterns.append(re.compile(pat_str, re.IGNORECASE))
    return patterns


def matches_any(text: str, patterns: List[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def is_private_or_bdix_url(stream_url: str, source_is_bdix: bool) -> bool:
    """Check if stream uses a local/private IP or BDIX hostname."""
    if source_is_bdix:
        return True
    try:
        parsed = urllib.parse.urlparse(stream_url)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        if "bdix" in host or host.endswith(".local"):
            return True
        ip_obj = ipaddress.ip_address(host)
        return ip_obj.is_private or ip_obj.is_loopback
    except ValueError:
        # Hostname is a regular domain name, not an IP literal
        return False


def parse_m3u_content(content: str, source: SourcePlaylist) -> List[Channel]:
    """Parse raw M3U text into Channel objects while skipping DRM-locked or VOD files."""
    channels: List[Channel] = []
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    current_extinf: Optional[str] = None
    current_vlc_opts: List[str] = []
    current_group_override: str = ""
    has_drm: bool = False

    for line in lines:
        if line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF:"):
            current_extinf = line
            current_vlc_opts = []
            current_group_override = ""
            has_drm = False
            continue

        if line.startswith("#KODIPROP:"):
            lower_prop = line.lower()
            if "license_key" in lower_prop or "license_type" in lower_prop or "widevine" in lower_prop or "clearkey" in lower_prop:
                has_drm = True
            continue

        if line.startswith("#EXTVLCOPT:"):
            current_vlc_opts.append(line)
            continue

        if line.startswith("#EXTGRP:"):
            current_group_override = line.split(":", 1)[1].strip()
            continue

        if line.startswith("#"):
            continue

        # Stream URL line
        if current_extinf and (line.startswith("http://") or line.startswith("https://")):
            stream_url = line
            lower_url = stream_url.lower().split("?")[0]

            # Skip static movie/video files (we want live TV streams) and DRM streams
            if has_drm or lower_url.endswith((".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv")):
                current_extinf = None
                continue

            attrs = dict(ATTR_REGEX.findall(current_extinf))
            # Channel display name is after the last comma in #EXTINF line
            comma_idx = current_extinf.rfind(",")
            ch_name = current_extinf[comma_idx + 1 :].strip() if comma_idx != -1 else attrs.get("tvg-name", "Unknown Channel")

            group_title = attrs.get("group-title", "") or current_group_override
            logo = attrs.get("tvg-logo", "")
            tvg_id = attrs.get("tvg-id", "")

            custom_ua = ""
            custom_ref = ""
            for opt in current_vlc_opts:
                opt_body = opt.split(":", 1)[1] if ":" in opt else ""
                if opt_body.lower().startswith("http-user-agent="):
                    custom_ua = opt_body.split("=", 1)[1].strip()
                elif opt_body.lower().startswith("http-referrer=") or opt_body.lower().startswith("http-referer="):
                    custom_ref = opt_body.split("=", 1)[1].strip()

            bdix_flag = (
                is_private_or_bdix_url(stream_url, source.is_bdix)
                or "bdix" in group_title.lower()
                or "bdix" in ch_name.lower()
            )

            channels.append(
                Channel(
                    name=ch_name,
                    stream_url=stream_url,
                    logo=logo,
                    tvg_id=tvg_id,
                    original_group=group_title,
                    source_name=source.name,
                    is_bdix=bdix_flag,
                    vlc_opts=list(current_vlc_opts),
                    custom_user_agent=custom_ua,
                    custom_referrer=custom_ref,
                )
            )
            current_extinf = None

    return channels


def classify_and_deduplicate(
    raw_channels: List[Channel],
    config: dict,
    source_sports_map: Dict[str, bool],
) -> List[Channel]:
    """Filter for Cricket, Football, Multi-Sport, and General Sports; remove duplicates."""
    filters = config["filters"]
    cricket_pats = compile_keyword_patterns(filters.get("cricket_keywords", []))
    football_pats = compile_keyword_patterns(filters.get("football_keywords", []))
    multi_pats = compile_keyword_patterns(filters.get("multi_sport_keywords", []))
    exclude_pats = compile_keyword_patterns(filters.get("exclude_keywords", []))
    general_sport_pat = re.compile(r"\bsports?\b", re.IGNORECASE)

    filtered: List[Channel] = []
    seen_urls: Set[str] = set()

    for ch in raw_channels:
        norm_url = ch.stream_url.strip()
        if norm_url in seen_urls:
            continue

        search_blob = f"{ch.name} | {ch.original_group}"

        # 1. Exclude unwanted sports/niches (e.g., Poker, Golf, WWE, Horse Racing)
        if matches_any(search_blob, exclude_pats):
            continue

        # 2. Match specific sports categories
        is_cricket = matches_any(ch.name, cricket_pats)
        is_football = matches_any(ch.name, football_pats)
        is_multi = matches_any(ch.name, multi_pats)

        if is_cricket and is_football:
            ch.category = "Multi-Sport"
        elif is_cricket:
            ch.category = "Cricket"
        elif is_football:
            ch.category = "Football"
        elif is_multi:
            ch.category = "Multi-Sport"
        elif general_sport_pat.search(search_blob) or source_sports_map.get(ch.source_name, False):
            ch.category = "Live Sports"
        else:
            # Not a sports channel -> skip
            continue

        seen_urls.add(norm_url)
        filtered.append(ch)

    return filtered


# =====================================================================
# 4. MULTI-THREADED STREAM HEALTH & SPEED CHECKER
# =====================================================================

def check_single_stream(
    ch: Channel,
    default_ua: str,
    timeout: int,
    running_in_cloud: bool,
    test_bdix_local: bool,
) -> Channel:
    """
    Verify if a stream URL is actually online and measure its response latency (ms).
    Smart BDIX Handling:
      - On GitHub Actions cloud servers (US/EU), BDIX local ISP links cannot be reached,
        so we preserve them without false deletion.
    """
    if ch.is_bdix:
        if running_in_cloud or not test_bdix_local:
            ch.is_alive = True
            ch.latency_ms = 5000  # Place after speed-verified global streams
            return ch

    # Strip Kodi/TiviMate pipe options (e.g., url.m3u8|User-Agent=Mozilla) before HTTP probe
    probe_url = ch.stream_url.split("|", 1)[0].strip()

    headers = {
        "User-Agent": ch.custom_user_agent or default_ua,
        "Accept": "*/*",
        "Connection": "close",
    }
    if ch.custom_referrer:
        headers["Referer"] = ch.custom_referrer

    req = urllib.request.Request(probe_url, headers=headers)
    start_time = time.perf_counter()

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            status = resp.status
            if status not in (200, 206):
                ch.is_alive = False
                return ch

            content_type = (resp.headers.get("Content-Type") or "").lower()
            # Read only first 2 KB so live MPEG-TS streams never hang downloading forever
            sample_bytes = resp.read(2048)
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)

            if not sample_bytes:
                ch.is_alive = False
                return ch

            sample_text = sample_bytes.decode("utf-8", errors="ignore").lstrip()
            lower_sample = sample_text.lower()

            # Reject fake HTTP 200 HTML error / domain parking pages
            if lower_sample.startswith("<!doctype html") or lower_sample.startswith("<html"):
                ch.is_alive = False
                return ch

            # Reject encrypted HLS streams requiring DRM keys
            if " SAMPLE-AES" in sample_text or "com.widevine" in lower_sample:
                ch.is_alive = False
                return ch

            is_hls = sample_text.startswith("#EXTM3U")
            is_mpeg_ts = sample_bytes[0] == 0x47  # 0x47 is MPEG-TS sync byte
            is_media_ct = any(
                t in content_type
                for t in ("mpegurl", "video/", "audio/", "octet-stream", "mp2t")
            )

            if is_hls or is_mpeg_ts or is_media_ct:
                ch.is_alive = True
                ch.latency_ms = elapsed_ms
            else:
                ch.is_alive = False
    except Exception:
        ch.is_alive = False

    return ch


def sort_channels(channels: List[Channel]) -> List[Channel]:
    """Sort by Global first (then BDIX), Category Order, Fastest Latency (ms), then Name."""
    category_rank = {"Cricket": 0, "Football": 1, "Multi-Sport": 2, "Live Sports": 3}
    channels.sort(
        key=lambda c: (
            1 if c.is_bdix else 0,
            category_rank.get(c.category, 9),
            c.latency_ms,
            c.name.lower(),
        )
    )
    return channels


def check_all_streams(channels: List[Channel], config: dict, skip_check: bool = False) -> List[Channel]:
    """Run parallel health checks using ThreadPoolExecutor (like Java ExecutorService)."""
    if skip_check:
        for ch in channels:
            ch.is_alive = True
            ch.latency_ms = 100
        return sort_channels(channels)

    settings = config["settings"]
    timeout = settings.get("timeout_seconds", 6)
    max_workers = settings.get("max_workers", 30)
    default_ua = settings.get("user_agent", "Mozilla/5.0")
    test_bdix_local = settings.get("test_bdix_when_local", True)
    running_in_cloud = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

    total = len(channels)
    if total == 0:
        return []

    env_label = "GitHub Cloud (BDIX Protected)" if running_in_cloud else "Local Machine"
    print(f"\n[Step 4] Checking {total} sports streams using {max_workers} parallel threads ({env_label})...")

    alive_channels: List[Channel] = []
    completed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                check_single_stream,
                ch,
                default_ua,
                timeout,
                running_in_cloud,
                test_bdix_local,
            ): ch
            for ch in channels
        }

        for future in concurrent.futures.as_completed(future_map):
            completed += 1
            result_ch = future.result()
            if result_ch.is_alive:
                alive_channels.append(result_ch)

            if completed % 50 == 0 or completed == total:
                print(f"  Progress: {completed}/{total} checked | Working so far: {len(alive_channels)}")

    return sort_channels(alive_channels)


# =====================================================================
# 5. M3U & DASHBOARD OUTPUT WRITER
# =====================================================================

def format_channel_m3u(ch: Channel) -> str:
    """Format a single Channel back into standard #EXTINF M3U syntax."""
    group_label = f"BDIX {ch.category}" if ch.is_bdix else ch.category
    attrs = []
    if ch.tvg_id:
        attrs.append(f'tvg-id="{ch.tvg_id}"')
    if ch.logo:
        attrs.append(f'tvg-logo="{ch.logo}"')
    attrs.append(f'group-title="{group_label}"')

    attr_str = " ".join(attrs)
    lines = [f"#EXTINF:-1 {attr_str},{ch.name}"]
    for opt in ch.vlc_opts:
        lines.append(opt)
    lines.append(ch.stream_url)
    return "\n".join(lines)


def write_m3u_file(filepath: str, channels: List[Channel], playlist_title: str) -> None:
    """Write a complete .m3u playlist file with UTF-8 encoding."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    now_bd = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=6))).strftime("%Y-%m-%d %I:%M %p BDT")

    header = [
        "#EXTM3U",
        f"#PLAYLIST:{playlist_title}",
        f"# Generated: {now_bd} | Working Channels: {len(channels)}",
        "",
    ]
    body = [format_channel_m3u(ch) for ch in channels]
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(header + body) + "\n")


def write_status_and_dashboard(
    output_dir: str,
    all_channels: List[Channel],
    football_channels: List[Channel],
    cricket_channels: List[Channel],
    total_candidates: int,
    sources_count: int,
) -> None:
    """Write status.json (for apps/APIs) and index.html (for browser / GitHub Pages)."""
    bd_tz = datetime.timezone(datetime.timedelta(hours=6))
    now_bd = datetime.datetime.now(bd_tz)
    timestamp_str = now_bd.strftime("%Y-%m-%d %I:%M:%S %p (BDT / UTC+6)")

    status_data = {
        "updated_at_bdt": timestamp_str,
        "updated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources_scanned": sources_count,
        "total_sports_candidates": total_candidates,
        "working_counts": {
            "sports_all": len(all_channels),
            "football": len(football_channels),
            "cricket": len(cricket_channels),
            "bdix": sum(1 for c in all_channels if c.is_bdix),
        },
        "files": {
            "sports_all": "sports_all.m3u",
            "football": "football.m3u",
            "cricket": "cricket.m3u",
        },
        "top_channels_preview": [
            {
                "name": c.name,
                "category": f"BDIX {c.category}" if c.is_bdix else c.category,
                "latency_ms": c.latency_ms,
                "source": c.source_name,
            }
            for c in all_channels[:40]
        ],
    }

    status_path = os.path.join(output_dir, "status.json")
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=2, ensure_ascii=False)

    # Lightweight HTML status page for GitHub Pages
    rows_html = ""
    for c in all_channels[:100]:
        badge = f"BDIX {c.category}" if c.is_bdix else c.category
        speed = "BDIX Local" if c.is_bdix and c.latency_ms >= 5000 else f"{c.latency_ms} ms"
        safe_name = (
            c.name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        rows_html += f"<tr><td>{safe_name}</td><td>{badge}</td><td>{speed}</td><td>{c.source_name}</td></tr>\n"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Live Sports M3U Generator (Football &amp; Cricket)</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
    .container {{ max-width: 960px; margin: 0 auto; }}
    h1 {{ margin-bottom: 4px; color: #38bdf8; }}
    .meta {{ color: #94a3b8; margin-bottom: 24px; font-size: 14px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 28px; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 18px; }}
    .card h3 {{ margin: 0 0 8px 0; color: #e2e8f0; }}
    .count {{ font-size: 28px; font-weight: 700; color: #4ade80; margin-bottom: 12px; }}
    .btn {{ display: inline-block; background: #0284c7; color: white; text-decoration: none; padding: 8px 14px; border-radius: 6px; font-size: 14px; font-weight: 600; }}
    .btn:hover {{ background: #0369a1; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; font-size: 14px; }}
    th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #334155; }}
    th {{ background: #0f172a; color: #94a3b8; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Football &amp; Cricket Auto-Updating M3U</h1>
    <div class="meta">Last Updated: <strong>{timestamp_str}</strong> | Auto-refreshes every 6 hours</div>
    <div class="cards">
      <div class="card">
        <h3>All Sports (Combined)</h3>
        <div class="count">{len(all_channels)} Channels</div>
        <a class="btn" href="sports_all.m3u">Download / Copy Link (sports_all.m3u)</a>
      </div>
      <div class="card">
        <h3>Football / Soccer</h3>
        <div class="count">{len(football_channels)} Channels</div>
        <a class="btn" href="football.m3u">Download / Copy Link (football.m3u)</a>
      </div>
      <div class="card">
        <h3>Cricket Live</h3>
        <div class="count">{len(cricket_channels)} Channels</div>
        <a class="btn" href="cricket.m3u">Download / Copy Link (cricket.m3u)</a>
      </div>
    </div>
    <h3>Top Working Streams Preview (Fastest First)</h3>
    <table>
      <thead><tr><th>Channel</th><th>Category</th><th>Response</th><th>Source</th></tr></thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>
</body>
</html>
"""
    with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


# =====================================================================
# 6. MAIN EXECUTION PIPELINE
# =====================================================================

def resolve_configured_sources(config: dict) -> List[SourcePlaylist]:
    """Expand config.json sources (supports both direct .m3u links and GitHub repo links)."""
    user_agent = config["settings"]["user_agent"]
    max_size_kb = config.get("github_discovery", {}).get("max_file_size_kb", 2500)
    resolved: List[SourcePlaylist] = []

    print("[Step 1A] Resolving configured sources from config.json...")
    for item in config.get("sources", []):
        if not item.get("enabled", True):
            continue

        name = item.get("name", "Custom Source")
        raw_input_url = item.get("url", "").strip()
        if not raw_input_url:
            continue

        is_bdix = item.get("is_bdix", False)
        default_sports = item.get("default_sports_group", False)

        # Check if user pasted a GitHub repo root link (e.g., https://github.com/user/repo)
        repo_slug = extract_github_repo_slug(raw_input_url)
        if repo_slug:
            owner, repo = repo_slug
            print(f"  [*] Scanning GitHub repo {owner}/{repo} for M3U files...")
            m3u_urls = scan_github_repo_for_m3u(owner, repo, user_agent, max_files=5, max_size_kb=max_size_kb)
            for idx, m3u_url in enumerate(m3u_urls, start=1):
                suffix = f" #{idx}" if len(m3u_urls) > 1 else ""
                resolved.append(
                    SourcePlaylist(
                        name=f"{name}{suffix}",
                        url=m3u_url,
                        is_bdix=is_bdix,
                        default_sports_group=default_sports,
                    )
                )
        else:
            direct_url = convert_github_blob_to_raw(raw_input_url)
            resolved.append(
                SourcePlaylist(
                    name=name,
                    url=direct_url,
                    is_bdix=is_bdix,
                    default_sports_group=default_sports,
                )
            )

    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate auto-updated Football & Cricket M3U playlists.")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--output-dir", default="output", help="Output folder for generated .m3u files")
    parser.add_argument("--skip-check", action="store_true", help="Skip live HTTP health check (fast test mode)")
    parser.add_argument("--no-discovery", action="store_true", help="Skip GitHub search auto-discovery")
    parser.add_argument("--local-file", help="Optional local .m3u file path for offline testing")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config if os.path.isabs(args.config) else os.path.join(base_dir, args.config)
    output_dir = args.output_dir if os.path.isabs(args.output_dir) else os.path.join(base_dir, args.output_dir)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    user_agent = config["settings"]["user_agent"]
    all_raw_channels: List[Channel] = []
    source_sports_map: Dict[str, bool] = {}
    sources: List[SourcePlaylist] = []

    if args.local_file:
        print(f"[Offline Mode] Reading local file: {args.local_file}")
        src = SourcePlaylist(name="Local Test", url=args.local_file, is_bdix=False, default_sports_group=False)
        sources = [src]
        source_sports_map[src.name] = False
        with open(args.local_file, "r", encoding="utf-8") as lf:
            all_raw_channels.extend(parse_m3u_content(lf.read(), src))
    else:
        sources = resolve_configured_sources(config)
        existing_urls = {s.url for s in sources}

        if not args.no_discovery:
            discovered = discover_github_playlists(config, existing_urls)
            sources.extend(discovered)

        print(f"\n[Step 2] Downloading and parsing {len(sources)} playlists...")
        for src in sources:
            source_sports_map[src.name] = src.default_sports_group
            text = fetch_text(src.url, user_agent=user_agent, timeout=20)
            if not text:
                continue
            parsed = parse_m3u_content(text, src)
            print(f"  -> {src.name}: parsed {len(parsed)} raw stream entries")
            all_raw_channels.extend(parsed)

    print(f"\n[Step 3] Filtering {len(all_raw_channels)} total entries for Football & Cricket...")
    candidates = classify_and_deduplicate(all_raw_channels, config, source_sports_map)
    print(f"  -> Matched {len(candidates)} unique Football / Cricket / Sports candidates")

    working_channels = check_all_streams(candidates, config, skip_check=args.skip_check)

    include_multi = config["settings"].get("include_multi_sport_in_both", True)
    football_cats = {"Football", "Multi-Sport"} if include_multi else {"Football"}
    cricket_cats = {"Cricket", "Multi-Sport"} if include_multi else {"Cricket"}

    football_list = [c for c in working_channels if c.category in football_cats]
    cricket_list = [c for c in working_channels if c.category in cricket_cats]

    print(f"\n[Step 5] Writing output files to: {output_dir}")
    write_m3u_file(os.path.join(output_dir, "sports_all.m3u"), working_channels, "Live Sports (Football & Cricket)")
    write_m3u_file(os.path.join(output_dir, "football.m3u"), football_list, "Live Football & Multi-Sport")
    write_m3u_file(os.path.join(output_dir, "cricket.m3u"), cricket_list, "Live Cricket & Multi-Sport")

    write_status_and_dashboard(
        output_dir=output_dir,
        all_channels=working_channels,
        football_channels=football_list,
        cricket_channels=cricket_list,
        total_candidates=len(candidates),
        sources_count=len(sources),
    )

    print("\n====================================================")
    print(" DONE! Playlist Generation Summary:")
    print(f"   * sports_all.m3u : {len(working_channels)} channels")
    print(f"   * football.m3u   : {len(football_list)} channels")
    print(f"   * cricket.m3u    : {len(cricket_list)} channels")
    print("====================================================")


if __name__ == "__main__":
    main()
