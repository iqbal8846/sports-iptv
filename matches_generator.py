#!/usr/bin/env python3
"""
Precision Sports Match Schedule & Exact Broadcaster Matcher
===========================================================
Generates `output/matches.json` for the SportsWatch Android App (`D:\\sportsapp`).

Priority Order (Strictly Curated — Zero Amateur/Non-Televised Matches):
  1. English Football: Premier League (eng.1), EFL Championship (eng.2), FA Cup, Carabao Cup, League One
  2. UEFA Competitions: UEFA Champions League, Europa League, Conference League, Nations League, Euro
  3. American & Top Leagues: American MLS (usa.1), American MLC (Major League Cricket),
     Spanish LaLiga, Italian Serie A, German Bundesliga, French Ligue 1, Saudi Pro League, ISL
  4. Tier-1 Televised Cricket: ICC Events, IPL, MLC, BPL, PSL, BBL, The Hundred, SA20, CPL, T10, NPL,
     and Senior International Tours (Full Member nations — excludes A-teams & non-televised minnows)

Smart Channel Filtering:
  - Blocks replay/news/studio-only channels (Cricket Gold, CBS Sports HQ, Fox Sports News, Club TV, etc.)
  - Blocks wrong-region homonyms (e.g., TNT Sports Argentina/Chile on UK/UEFA games, Thai T Sports 7)
  - Maps exact regional broadcast rights (e.g., EPL -> Star Sports Select 1/2, Sky Sports 1, TNT Sports 1,
    Setanta 1/2, beIN Sports 1/2; UCL -> Sony Ten 1/3/5, TNT Sports 1, beIN Sports 1/2, CBS Golazo)
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import ssl
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple


BD_TZ = datetime.timezone(datetime.timedelta(hours=6))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def create_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


SSL_CTX = create_ssl_context()
ATTR_REGEX = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


# =====================================================================
# 1. VERIFIED M3U CHANNEL LOADER & JUNK/REPLAY CHANNEL BLOCKER
# =====================================================================

# Channels that air 24/7 classic replays, news tickers, studio talk, or non-football/cricket sports
BLOCKED_CHANNEL_SUBSTRINGS = (
    "cricket gold",          # 24/7 classic archive replays, never live matches
    "cbs sports hq",         # 24/7 news ticker
    "fox sports news",       # 24/7 news
    "sky sports news",       # 24/7 news
    "tele sondrio",          # local Italian news
    "the ocho",              # novelty/archive sports
    "trace sport",           # celebrity lifestyle channel
    "real madrid tv",        # studio audio only during live matches
    "top barca",             # club archive/studio
    "barca tv",              # club archive/studio
    "mutv",                  # club studio only during live EPL
    "lfctv",                 # club studio only during live EPL
    "celtic tv",
    "benfica tv",
    "sporting tv",
    "dazn darts",
    "dazn combat",
    "dazn ringside",
    "dazn heldinnen",
    "unbeaten",
    "supertennis",
    "tennis+",
    "nba ",
    "formula ",
    "t sports 7",            # Thai channel (chtsport), not Bangladesh T Sports
    "[not 24/7]",
    "[geo-blocked]",
)


@dataclass
class VerifiedStream:
    name: str
    url: str
    group: str
    logo: str = ""
    user_agent: str = ""
    referer: str = ""
    is_bdix: bool = False

    @property
    def stream_type(self) -> str:
        lower = self.url.lower().split("?")[0]
        if ".mpd" in lower:
            return "mpd"
        if ".m3u8" in lower:
            return "m3u8"
        return "ts"

    @property
    def quality_label(self) -> str:
        upper = self.name.upper()
        prefix = "BDIX · " if self.is_bdix else ""
        if "4K" in upper or "2160P" in upper or "UHD" in upper:
            return f"{prefix}4K"
        if "1080P" in upper or "FHD" in upper:
            return f"{prefix}1080p"
        if "720P" in upper or "HD" in upper:
            return f"{prefix}HD"
        return f"{prefix}Live"

    @property
    def clean_display_name(self) -> str:
        cleaned = re.sub(r"\[[^\]]*\]", "", self.name)
        cleaned = re.sub(r"\(\d+p\)", "", cleaned, flags=re.IGNORECASE)
        # Clean repeated "(3) (3) (3)" artifacts from community BDIX playlists
        cleaned = re.sub(r"(?:\(\d+\)\s*){2,}", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if self.is_bdix and "bdix" not in cleaned.lower():
            return f"{cleaned} (BDIX)"
        return cleaned or self.name


def is_blocked_channel(name: str, url: str) -> bool:
    nl = name.lower()
    ul = url.lower()
    if "chtsport" in ul:
        return True
    if any(b in nl for b in BLOCKED_CHANNEL_SUBSTRINGS):
        return True
    # Skip static match-recording titles like "Liechtenstein vs Azerbaijan - 14 Nov..."
    if " vs " in nl and any(m in nl for m in (" jan ", " feb ", " mar ", " apr ", " may ", " jun ", " jul ", " aug ", " sep ", " oct ", " nov ", " dec ")):
        return True
    return False


def load_verified_streams(m3u_path: str) -> List[VerifiedStream]:
    if not os.path.exists(m3u_path):
        print(f"  [WARN] {m3u_path} not found. Run `python generator.py` first.")
        return []

    streams: List[VerifiedStream] = []
    with open(m3u_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [ln.strip() for ln in f if ln.strip()]

    extinf: Optional[str] = None
    ua = ""
    ref = ""

    for line in lines:
        if line.startswith("#EXTINF:"):
            extinf = line
            ua = ""
            ref = ""
        elif line.startswith("#EXTVLCOPT:"):
            body = line.split(":", 1)[1] if ":" in line else ""
            if body.lower().startswith("http-user-agent="):
                ua = body.split("=", 1)[1].strip()
            elif body.lower().startswith(("http-referrer=", "http-referer=")):
                ref = body.split("=", 1)[1].strip()
        elif not line.startswith("#") and extinf and line.startswith(("http://", "https://")):
            raw_url = line
            if "|" in raw_url:
                base_part, pipe_opts = raw_url.split("|", 1)
                raw_url = base_part.strip()
                for kv in pipe_opts.split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        if k.strip().lower() == "user-agent" and not ua:
                            ua = v.strip()
                        elif k.strip().lower() in ("referer", "referrer") and not ref:
                            ref = v.strip()

            clean_url = raw_url.split("#", 1)[0].strip()
            attrs = dict(ATTR_REGEX.findall(extinf))
            comma_idx = extinf.rfind(",")
            name = extinf[comma_idx + 1 :].strip() if comma_idx != -1 else "Sports Stream"
            group = attrs.get("group-title", "")
            logo = attrs.get("tvg-logo", "")

            if is_blocked_channel(name, clean_url):
                extinf = None
                continue

            is_bdix = "bdix" in group.lower() or "bdix" in name.lower() or "[bd]" in name.lower()
            streams.append(
                VerifiedStream(
                    name=name,
                    url=clean_url,
                    group=group,
                    logo=logo,
                    user_agent=ua,
                    referer=ref,
                    is_bdix=is_bdix,
                )
            )
            extinf = None

    return streams


# =====================================================================
# 2. CURATED TOP-PRIORITY LEAGUES & EXACT BROADCASTER MATRIX
# =====================================================================

# Exact ESPN Soccer League Slugs -> (Priority Rank, Clean Display Name, Ordered Broadcaster Keywords)
# Lower Rank number = Higher priority at the very top of the Android app!
CURATED_SOCCER_LEAGUES: Dict[str, Tuple[int, str, List[str]]] = {
    # --- TIER 1: ENGLISH LEAGUES (User Top Priority #1) ---
    "eng.1": (
        1,
        "English Premier League",
        ["star sports select 1", "star sports select 2", "sky sports 1", "tnt sports 1", "bein sports 1", "setanta sports 1", "star sports 1"],
    ),
    "eng.2": (
        2,
        "EFL Championship",
        ["sky sports 1", "cbs sports golazo", "bein sports 1", "bein sports 2", "setanta sports 1", "premier sports"],
    ),
    "eng.fa": (
        3,
        "English FA Cup",
        ["sony ten sports 1", "sony ten sports 5", "sony sports ten", "tnt sports 1", "bein sports 1", "espn (united states)"],
    ),
    "eng.league_cup": (
        4,
        "English Carabao Cup (EFL Cup)",
        ["sky sports 1", "cbs sports golazo", "bein sports 1", "setanta sports 1"],
    ),
    "eng.3": (
        5,
        "English League One",
        ["sky sports 1", "cbs sports golazo", "bein sports 2"],
    ),
    "sco.1": (
        6,
        "Scottish Premiership",
        ["sky sports 1", "cbs sports golazo", "premier sports"],
    ),

    # --- TIER 2: UEFA & FIFA TOP COMPETITIONS (User Top Priority #2) ---
    "uefa.champions": (
        5,
        "UEFA Champions League",
        ["sony ten sports 1", "sony ten sports 5", "sony sports ten", "tnt sports 1", "bein sports 1", "bein sports 2", "cbs sports golazo", "setanta sports 1"],
    ),
    "uefa.europa": (
        6,
        "UEFA Europa League",
        ["sony ten sports 1", "sony ten sports 5", "tnt sports 1", "bein sports 1", "cbs sports golazo", "setanta sports 1"],
    ),
    "uefa.europa.conf": (
        7,
        "UEFA Conference League",
        ["sony ten sports 1", "tnt sports 1", "bein sports 2", "cbs sports golazo"],
    ),
    "uefa.nations": (
        8,
        "UEFA Nations League",
        ["sony ten sports 1", "sony ten sports 5", "fox sports 1", "fox sports 2", "bein sports 1", "setanta sports 1"],
    ),
    "uefa.euro": (
        8,
        "UEFA EURO",
        ["sony ten sports 1", "fox sports 1", "t sports", "bein sports 1"],
    ),
    "fifa.world": (
        8,
        "FIFA World Cup",
        ["t sports", "gazi tv", "fox sports 1", "bein sports 1", "sony ten sports 1"],
    ),
    "fifa.worldq.uefa": (
        9,
        "FIFA World Cup Qualifiers (UEFA)",
        ["sony ten sports 1", "fox sports 1", "fox sports 2", "bein sports 1", "espn 2 (united states)"],
    ),
    "fifa.worldq.conmebol": (
        9,
        "FIFA World Cup Qualifiers (CONMEBOL)",
        ["bein sports 1", "espn (brazil)", "espn latin", "tsn 1"],
    ),
    "fifa.worldq.afc": (
        10,
        "FIFA World Cup Qualifiers (AFC)",
        ["t sports", "star sports", "bein sports 1", "alkass"],
    ),
    "uefa.wchampions": (
        10,
        "UEFA Women's Champions League",
        ["cbs sports golazo", "tnt sports 1", "espn 2 (united states)", "eurosport 1"],
    ),

    # --- TIER 3: AMERICAN MLS & TOP EUROPEAN/ASIAN LEAGUES (User Top Priority #3) ---
    "usa.1": (
        9,
        "American MLS",
        ["fox sports 1", "fox sports 2", "fox sports", "tsn 1", "tsn 2", "espn (united states)", "bein sports usa"],
    ),
    "usa.nwsl": (
        15,
        "American NWSL",
        ["cbs sports golazo", "espn (united states)", "espn 2 (united states)", "tsn 1"],
    ),
    "esp.1": (
        16,
        "Spanish LaLiga",
        ["espn (united states)", "espn 2 (united states)", "premier sports", "bein sports 1", "setanta sports 1", "t sports", "eleven sports 1"],
    ),
    "esp.copa_del_rey": (
        17,
        "Spanish Copa del Rey",
        ["espn (united states)", "espn 2 (united states)", "premier sports", "bein sports 1"],
    ),
    "ita.1": (
        18,
        "Italian Serie A",
        ["cbs sports golazo", "tnt sports 1", "bein sports 1", "setanta sports 1", "eleven sports 1", "arena sport 1"],
    ),
    "ger.1": (
        19,
        "German Bundesliga",
        ["sony ten sports 1", "sony ten sports 5", "espn (united states)", "sky sports 1", "bein sports 1", "setanta sports 1", "eleven sports 1"],
    ),
    "fra.1": (
        20,
        "French Ligue 1",
        ["bein sports 1", "bein sports 2", "setanta sports 1", "arena sport 1", "eleven sports 1"],
    ),
    "ksa.1": (
        21,
        "Saudi Pro League",
        ["sony ten sports 1", "fox sports 1", "Fox Sports 2", "t sports", "bein sports"],
    ),
    "ind.1": (
        22,
        "Indian Super League (ISL)",
        ["star sports 1", "star sports 2", "dd sports"],
    ),
}

# Explicit high-priority endpoints fetched directly so they NEVER get missed even on busy Saturdays
DIRECT_PRIORITY_SOCCER_SLUGS = [
    "eng.1",
    "eng.2",
    "eng.fa",
    "eng.league_cup",
    "uefa.champions",
    "uefa.europa",
    "usa.1",
    "esp.1",
    "ita.1",
    "ger.1",
    "fra.1",
]

FULL_MEMBER_CRICKET_TEAMS = (
    "england", "australia", "india", "bangladesh", "pakistan",
    "south africa", "new zealand", "west indies", "sri lanka",
    "afghanistan", "ireland", "zimbabwe", "scotland", "netherlands", "united states", "usa",
)

TOP_CRICKET_TOURNAMENTS: List[Tuple[int, List[str], List[str]]] = [
    # (Priority Rank, League/Series Match Keywords, Ordered Broadcaster Keywords)
    (
        9,
        ["major league cricket", "mlc"],
        ["willow sports", "willow tv", "fox cricket", "sony ten cricket", "tnt sports 1", "a sports", "star sports 1"],
    ),
    (
        11,
        ["indian premier league", "ipl", "icc", "world cup", "champions trophy", "asia cup"],
        ["star sports 1", "t sports", "gazi tv", "willow sports", "sky sports 1", "ptv sports", "a sports", "fox cricket"],
    ),
    (
        11,
        ["bangladesh premier league", "bpl"],
        ["t sports", "gazi tv", "willow sports", "sony ten cricket", "a sports"],
    ),
    (
        13,
        ["vitality blast", "the hundred", "county championship"],
        ["sky sports 1", "sony ten cricket", "sony ten sports 1", "willow sports", "fox cricket", "ptv sports"],
    ),
    (
        14,
        ["big bash", "bbl", "wbbl", "sheffield shield", "marsh cup", "ashes"],
        ["fox cricket", "star sports 1", "star sports 2", "willow sports", "sky sports 1", "tnt sports 1"],
    ),
    (
        12,
        ["pakistan super league", "psl"],
        ["a sports", "ptv sports", "willow sports", "sony ten cricket", "t sports"],
    ),
    (
        15,
        ["sa20", "caribbean premier league", "cpl", "ilt20", "international league t20", "lanka premier", "lpl", "abu dhabi t10", "nepal premier"],
        ["star sports 1", "t sports", "willow sports", "a sports", "sony ten cricket", "fox cricket"],
    ),
]


def is_south_american_tnt(stream_name: str) -> bool:
    nl = stream_name.lower()
    return "tnt" in nl and any(r in nl for r in ("argentina", "chile", "brazil", "premium"))


def pick_ranked_streams(
    channel_keywords: List[str],
    all_streams: List[VerifiedStream],
    allow_south_am_tnt: bool = False,
    max_streams: int = 5,
) -> List[VerifiedStream]:
    """
    Match streams strictly in the exact priority order of `channel_keywords`!
    Avoids wrong-region homonyms (e.g., TNT Argentina on English games) and avoids
    attaching unrelated filler channels.
    """
    picked: List[VerifiedStream] = []
    seen_urls: Set[str] = set()
    seen_clean_names: Set[str] = set()

    # Prefer HLS .m3u8 first, and rank main numbered channels ahead of "XTRA" filler channels
    def stream_sort_key(s: VerifiedStream) -> Tuple[int, int, int]:
        nl = s.name.lower()
        is_xtra = 1 if "xtra" in nl or "extra" in nl else 0
        not_hls = 0 if s.stream_type == "m3u8" else 1
        bdix_order = 1 if s.is_bdix else 0
        return (is_xtra, not_hls, bdix_order)

    ordered_pool = sorted(all_streams, key=stream_sort_key)

    # Pass 1: Global/Main channels in strict broadcaster keyword order, then BDIX mirrors
    for kw in channel_keywords:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        pat = re.compile(r"\b" + re.escape(kw_lower), re.IGNORECASE)

        for st in ordered_pool:
            if len(picked) >= max_streams:
                break
            if st.url in seen_urls:
                continue
            if not allow_south_am_tnt and is_south_american_tnt(st.name):
                continue

            if pat.search(st.name):
                dedup_title = f"{st.clean_display_name.lower()}"
                if dedup_title in seen_clean_names:
                    continue
                seen_urls.add(st.url)
                seen_clean_names.add(dedup_title)
                picked.append(st)
                # Take at most 2 variants per broadcaster keyword (e.g., 1 Global + 1 BDIX)
                if sum(1 for p in picked if pat.search(p.name)) >= 2:
                    break

    return picked[:max_streams]


def evaluate_cricket_fixture(
    league_name: str,
    team1: str,
    team2: str,
    is_intl_flag: bool,
    all_streams: List[VerifiedStream],
) -> Optional[Tuple[int, List[VerifiedStream]]]:
    """
    Returns (priority_rank, matched_streams) ONLY if this cricket match is genuinely
    televised (Top Tournament or Full-Member Senior International).
    Returns None for non-televised associate minnows (e.g., Falkland Islands vs Costa Rica) or A-teams!
    """
    ll = league_name.lower()
    t1 = team1.lower()
    t2 = team2.lower()
    both = f"{t1} | {t2}"

    # 1. Exclude unofficial "A" team / U-19 warm-up / club non-televised tours
    if re.search(r"\b(?:a|xi|under-19|u19|emerging|academy)\b", t1) or re.search(
        r"\b(?:a|xi|under-19|u19|emerging|academy)\b", t2
    ):
        if "world cup" not in ll and "asia cup" not in ll:
            return None

    # 2. Check curated televised tournaments first (MLC, IPL, ICC, BPL, Vitality Blast, BBL, PSL, T10, NPL...)
    for rank, tour_kws, bcast_kws in TOP_CRICKET_TOURNAMENTS:
        if any(k in ll for k in tour_kws):
            streams = pick_ranked_streams(bcast_kws, all_streams, max_streams=5)
            return (rank, streams) if streams else None

    # 3. Senior International Bilateral Series — BOTH or HOST must be a televised Full-Member nation!
    t1_major = any(m in t1 for m in FULL_MEMBER_CRICKET_TEAMS)
    t2_major = any(m in t2 for m in FULL_MEMBER_CRICKET_TEAMS)
    if not (t1_major and t2_major):
        # Filters out non-televised associate matches like "Falkland Islands vs Costa Rica"
        return None

    # Determine Host Country from ESPN series name (e.g., "Sri Lanka tour of Pakistan" -> host is Pakistan!)
    host_text = ll.split("tour of", 1)[1] if "tour of" in ll else f"{ll} {t1}"

    if "pakistan" in host_text:
        bcast_kws = ["ptv sports", "a sports", "willow sports", "willow tv", "t sports", "sony ten cricket"]
    elif "bangladesh" in host_text or "bangladesh" in both:
        bcast_kws = ["t sports", "gazi tv", "willow sports", "willow tv", "sony ten cricket"]
    elif "india" in host_text:
        bcast_kws = ["star sports 1", "dd sports", "willow sports", "t sports", "sky sports 1"]
    elif "england" in host_text:
        bcast_kws = ["sky sports 1", "sony ten cricket", "sony ten sports 1", "willow sports", "fox cricket"]
    elif "australia" in host_text:
        bcast_kws = ["fox cricket", "star sports 1", "willow sports", "tnt sports 1", "ptv sports"]
    elif "new zealand" in host_text:
        bcast_kws = ["sony ten cricket", "sony ten sports 1", "tnt sports 1", "willow sports", "star sports 1"]
    elif "south africa" in host_text:
        bcast_kws = ["sky sports 1", "star sports 1", "willow sports", "t sports", "gazi tv", "ptv sports"]
    elif "west indies" in host_text or "sri lanka" in host_text or "zimbabwe" in host_text or "ireland" in host_text:
        bcast_kws = ["sony ten cricket", "tnt sports 1", "willow sports", "t sports", "ptv sports", "a sports"]
    else:
        bcast_kws = ["willow sports", "sky sports 1", "star sports 1", "t sports", "ptv sports"]

    # Add visiting nation's home broadcaster if Bangladesh or Pakistan is playing away!
    if "bangladesh" in both and "t sports" not in bcast_kws:
        bcast_kws = ["t sports", "gazi tv"] + bcast_kws
    if "pakistan" in both and "ptv sports" not in bcast_kws:
        bcast_kws = ["a sports", "ptv sports"] + bcast_kws

    streams = pick_ranked_streams(bcast_kws, all_streams, max_streams=5)
    return (11, streams) if streams else None


# =====================================================================
# 3. ESPN MULTI-ENDPOINT FETCHER (DEDICATED TOP LEAGUES + CRICKET)
# =====================================================================

def fetch_json(url: str, timeout: int = 14) -> Optional[dict]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None


def parse_iso_utc(iso_str: str) -> Optional[datetime.datetime]:
    if not iso_str:
        return None
    cleaned = iso_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def map_espn_tv_to_keywords(official_tv: List[str]) -> List[str]:
    """Convert official ESPN TV tags (e.g. 'ESPN', 'FOX', 'Sky', 'TNT') into exact channel keywords."""
    extra: List[str] = []
    for tv in official_tv:
        tl = tv.lower().strip()
        if "espn" in tl and "+" not in tl:
            extra.append("espn (united states)")
        elif "fox" in tl or "fs1" in tl or "fs2" in tl:
            extra.append("fox sports 1")
        elif "cbs" in tl or "golazo" in tl:
            extra.append("cbs sports golazo")
        elif "sky" in tl:
            extra.append("sky sports 1")
        elif "tnt" in tl:
            extra.append("tnt sports 1")
        elif "bein" in tl:
            extra.append("bein sports 1")
    return extra


def parse_dedicated_soccer_scoreboard(
    payload: dict,
    slug: str,
    now_bd: datetime.datetime,
    all_streams: List[VerifiedStream],
) -> List[Tuple[str, int, int, dict]]:
    """Parse dedicated `site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard` response."""
    if not payload or slug not in CURATED_SOCCER_LEAGUES:
        return []

    league_rank, clean_league_name, base_bcast_kws = CURATED_SOCCER_LEAGUES[slug]
    results: List[Tuple[str, int, int, dict]] = []
    now_epoch_ms = int(now_bd.timestamp() * 1000)

    for ev in payload.get("events") or []:
        ev_id = str(ev.get("id") or "")
        comps = ev.get("competitions") or []
        if not ev_id or not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            continue

        c1, c2 = competitors[0], competitors[1]
        if c2.get("homeAway") == "home" and c1.get("homeAway") == "away":
            c1, c2 = c2, c1

        t1_obj = c1.get("team") or {}
        t2_obj = c2.get("team") or {}
        team1 = (t1_obj.get("displayName") or t1_obj.get("name") or "Home").strip()
        team2 = (t2_obj.get("displayName") or t2_obj.get("name") or "Away").strip()
        if team1.lower() in ("tbd", "tba") and team2.lower() in ("tbd", "tba"):
            continue

        logo1 = (t1_obj.get("logo") or "").strip()
        logo2 = (t2_obj.get("logo") or "").strip()
        score1 = str(c1.get("score") or "").strip()
        score2 = str(c2.get("score") or "").strip()

        dt_utc = parse_iso_utc(comp.get("date") or ev.get("date") or "")
        if not dt_utc:
            continue
        dt_bd = dt_utc.astimezone(BD_TZ)
        match_date_bdt = dt_bd.strftime("%Y-%m-%d")
        match_time_bdt = dt_bd.strftime("%I:%M %p")
        start_ts_ms = int(dt_utc.timestamp() * 1000)

        st_type = (comp.get("status") or {}).get("type") or {}
        state = (st_type.get("state") or "pre").lower()
        short_detail = (st_type.get("shortDetail") or "").strip()

        if state == "in":
            meta_keywords = "LIVE"
            status_rank = 0
            description = f"{score1 or '0'} : {score2 or '0'} | {short_detail or 'LIVE'}"
        elif state == "post":
            continue
        else:
            if (now_epoch_ms - start_ts_ms) > int(3.5 * 3600 * 1000):
                continue
            meta_keywords = "UPCOMING"
            status_rank = 1
            venue_name = ((comp.get("venue") or {}).get("fullName") or "").strip()
            description = venue_name if venue_name else clean_league_name

        official_tv = []
        for gb in comp.get("geoBroadcasts") or []:
            m_name = ((gb.get("media") or {}).get("shortName") or "").strip()
            if m_name and m_name not in official_tv:
                official_tv.append(m_name)

        combined_kws = map_espn_tv_to_keywords(official_tv) + base_bcast_kws
        matched_streams = pick_ranked_streams(combined_kws, all_streams, max_streams=5)
        if not matched_streams:
            continue

        stream_ads = []
        for st in matched_streams:
            entry = {
                "title": st.clean_display_name,
                "quality": st.quality_label,
                "type": st.stream_type,
                "url": st.url,
            }
            if st.user_agent:
                entry["user_agent"] = st.user_agent
            if st.referer:
                entry["referer"] = st.referer
            stream_ads.append(entry)

        match_obj = {
            "id": f"foot_{ev_id}_{match_date_bdt.replace('-', '')}",
            "category": "Football",
            "event_name": clean_league_name,
            "league_priority": league_rank,
            "team1": team1,
            "team2": team2,
            "logo1": logo1,
            "logo2": logo2,
            "date": match_date_bdt,
            "time": match_time_bdt,
            "start_timestamp_ms": start_ts_ms,
            "timezone": "Asia/Dhaka (UTC+6)",
            "description": description,
            "meta_keywords": meta_keywords,
            "official_tv": official_tv[:4],
            "stream_ads": stream_ads,
        }
        results.append((match_date_bdt, status_rank, league_rank, match_obj))

    return results


def parse_header_payload(
    payload: dict,
    sport_label: str,
    day_dt: datetime.datetime,
    now_bd: datetime.datetime,
    all_streams: List[VerifiedStream],
) -> List[Tuple[str, int, int, dict]]:
    """Parse `scoreboard/header` for Cricket and remaining curated Soccer competitions."""
    if not payload:
        return []
    sports_arr = payload.get("sports") or []
    if not sports_arr:
        return []

    results: List[Tuple[str, int, int, dict]] = []
    now_epoch_ms = int(now_bd.timestamp() * 1000)
    queried_iso = day_dt.strftime("%Y-%m-%d")

    for lg in sports_arr[0].get("leagues") or []:
        slug = (lg.get("slug") or "").strip().lower()
        raw_league_name = (lg.get("name") or lg.get("shortName") or sport_label).strip()

        if sport_label == "Football":
            # Strictly allow ONLY curated televised soccer slugs!
            if slug not in CURATED_SOCCER_LEAGUES:
                continue
            league_rank, clean_league_name, soccer_bcast_kws = CURATED_SOCCER_LEAGUES[slug]
        else:
            clean_league_name = raw_league_name
            league_rank = 5
            soccer_bcast_kws = []

        for ev in lg.get("events") or []:
            ev_id = str(ev.get("id") or "")
            competitors = ev.get("competitors") or []
            if not ev_id or len(competitors) < 2:
                continue

            c1, c2 = competitors[0], competitors[1]
            if c2.get("homeAway") == "home" and c1.get("homeAway") == "away":
                c1, c2 = c2, c1

            team1 = (c1.get("displayName") or c1.get("name") or "Team 1").strip()
            team2 = (c2.get("displayName") or c2.get("name") or "Team 2").strip()
            if team1.lower() in ("tbd", "tba") and team2.lower() in ("tbd", "tba"):
                continue

            logo1 = (c1.get("logo") or "").strip()
            logo2 = (c2.get("logo") or "").strip()
            score1 = str(c1.get("score") or "").strip()
            score2 = str(c2.get("score") or "").strip()
            is_intl = bool(c1.get("isNational") or c2.get("isNational"))

            dt_utc = parse_iso_utc(ev.get("date") or "")
            end_utc = parse_iso_utc(ev.get("endDate") or "")
            if dt_utc:
                dt_bd = dt_utc.astimezone(BD_TZ)
                match_date_bdt = dt_bd.strftime("%Y-%m-%d")
                match_time_bdt = dt_bd.strftime("%I:%M %p")
                start_ts_ms = int(dt_utc.timestamp() * 1000)
            else:
                match_date_bdt = queried_iso
                match_time_bdt = "TBD"
                start_ts_ms = 0

            event_type = (ev.get("eventType") or "").lower()
            if sport_label == "Cricket" and event_type in ("test", "first-class", "multi-day") and end_utc and dt_utc:
                if dt_utc.date() <= day_dt.date() <= end_utc.date():
                    match_date_bdt = queried_iso
                    session_dt_bd = datetime.datetime.combine(
                        day_dt.date(),
                        dt_utc.astimezone(BD_TZ).time(),
                        tzinfo=BD_TZ,
                    )
                    start_ts_ms = int(session_dt_bd.timestamp() * 1000)

            full_status = ev.get("fullStatus") or {}
            st_type = full_status.get("type") or {}
            state = (st_type.get("state") or ev.get("status") or "pre").lower()
            short_detail = (st_type.get("shortDetail") or st_type.get("detail") or "").strip()
            summary_txt = (full_status.get("summary") or ev.get("summary") or "").strip()

            if state == "in":
                meta_keywords = "LIVE"
                status_rank = 0
                if sport_label == "Football":
                    description = f"{score1 or '0'} : {score2 or '0'} | {short_detail or 'LIVE'}"
                else:
                    active_score = score2 if ("(" in score2 or "/" in score2) else (score1 or "LIVE")
                    short_score = active_score.split("(")[0].strip()[:14].strip()
                    description = f"{short_score or 'LIVE'} | {summary_txt or short_detail or 'Live'}"
            elif state == "post":
                continue
            else:
                if start_ts_ms > 0 and (now_epoch_ms - start_ts_ms) > int(3.5 * 3600 * 1000):
                    continue
                meta_keywords = "UPCOMING"
                status_rank = 1
                venue = (ev.get("location") or "").strip()
                description = venue if venue else clean_league_name

            official_tv: List[str] = []
            bcast_str = ev.get("broadcast")
            if isinstance(bcast_str, str) and bcast_str.strip():
                official_tv.extend([p.strip() for p in bcast_str.split("/") if p.strip()])

            if sport_label == "Cricket":
                eval_res = evaluate_cricket_fixture(
                    league_name=clean_league_name,
                    team1=team1,
                    team2=team2,
                    is_intl_flag=is_intl,
                    all_streams=all_streams,
                )
                if not eval_res:
                    continue
                league_rank, matched_streams = eval_res
            else:
                combined_kws = map_espn_tv_to_keywords(official_tv) + soccer_bcast_kws
                matched_streams = pick_ranked_streams(combined_kws, all_streams, max_streams=5)

            if not matched_streams:
                continue

            stream_ads = []
            for st in matched_streams:
                entry = {
                    "title": st.clean_display_name,
                    "quality": st.quality_label,
                    "type": st.stream_type,
                    "url": st.url,
                }
                if st.user_agent:
                    entry["user_agent"] = st.user_agent
                if st.referer:
                    entry["referer"] = st.referer
                stream_ads.append(entry)

            prefix = "foot" if sport_label == "Football" else "crick"
            match_obj = {
                "id": f"{prefix}_{ev_id}_{match_date_bdt.replace('-', '')}",
                "category": sport_label,
                "event_name": clean_league_name,
                "league_priority": league_rank,
                "team1": team1,
                "team2": team2,
                "logo1": logo1,
                "logo2": logo2,
                "date": match_date_bdt,
                "time": match_time_bdt,
                "start_timestamp_ms": start_ts_ms,
                "timezone": "Asia/Dhaka (UTC+6)",
                "description": description,
                "meta_keywords": meta_keywords,
                "official_tv": official_tv[:4],
                "stream_ads": stream_ads,
            }
            results.append((match_date_bdt, status_rank, league_rank, match_obj))

    return results


def fetch_curated_week_fixtures(all_streams: List[VerifiedStream]) -> List[dict]:
    now_bd = datetime.datetime.now(BD_TZ)
    today_midnight_bd = now_bd.replace(hour=0, minute=0, second=0, microsecond=0)
    query_dates = [today_midnight_bd + datetime.timedelta(days=i) for i in range(-1, 8)]

    print(
        f"[Step 1] Querying Top English, UEFA, MLS/MLC & International schedules "
        f"({query_dates[0].strftime('%Y-%m-%d')} to {query_dates[-1].strftime('%Y-%m-%d')})..."
    )

    # Build parallel task queue: Dedicated Priority Soccer Leagues + Header Soccer/Cricket
    tasks: List[Tuple[str, str, datetime.datetime, str]] = []
    for day_dt in query_dates:
        yyyymmdd = day_dt.strftime("%Y%m%d")
        for slug in DIRECT_PRIORITY_SOCCER_SLUGS:
            u = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={yyyymmdd}"
            tasks.append(("dedicated_soccer", slug, day_dt, u))
        tasks.append((
            "header",
            "Cricket",
            day_dt,
            f"https://site.web.api.espn.com/apis/v2/scoreboard/header?sport=cricket&dates={yyyymmdd}",
        ))
        tasks.append((
            "header",
            "Football",
            day_dt,
            f"https://site.web.api.espn.com/apis/v2/scoreboard/header?sport=soccer&dates={yyyymmdd}",
        ))

    collected: List[Tuple[str, int, int, dict]] = []
    seen_ids: Set[str] = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as executor:
        fut_map = {
            executor.submit(fetch_json, url, 14): (kind, tag, day_dt)
            for kind, tag, day_dt, url in tasks
        }
        raw_responses = []
        for fut in concurrent.futures.as_completed(fut_map):
            kind, tag, day_dt = fut_map[fut]
            raw_responses.append((0 if kind == "dedicated_soccer" else 1, kind, tag, day_dt, fut.result()))

    # Process dedicated league scoreboards FIRST so their richer geoBroadcast metadata wins deduplication
    raw_responses.sort(key=lambda x: (x[0], x[3]))
    for _, kind, tag, day_dt, payload in raw_responses:
        if not payload:
            continue
        if kind == "dedicated_soccer":
            items = parse_dedicated_soccer_scoreboard(payload, tag, now_bd, all_streams)
        else:
            items = parse_header_payload(payload, tag, day_dt, now_bd, all_streams)

        for entry in items:
            m_id = entry[3]["id"]
            if m_id not in seen_ids:
                seen_ids.add(m_id)
                collected.append(entry)

    # Sort globally: Within each date -> LIVE first -> Top Priority Leagues first (EPL #1, Championship #2, UCL #7, MLS/MLC...) -> Kickoff Time!
    collected.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            item[3].get("start_timestamp_ms", 0),
        )
    )
    return [item[3] for item in collected]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate curated priority matches.json API.")
    parser.add_argument("--output-dir", default="output", help="Directory containing sports_all.m3u")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = args.output_dir if os.path.isabs(args.output_dir) else os.path.join(base_dir, args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    m3u_path = os.path.join(output_dir, "sports_all.m3u")
    verified_streams = load_verified_streams(m3u_path)
    print(f"[Init] Loaded {len(verified_streams)} clean live broadcast channels (blocked replay/news/studio channels)")

    matches = fetch_curated_week_fixtures(verified_streams)

    matches_json_path = os.path.join(output_dir, "matches.json")
    if not matches and os.path.exists(matches_json_path):
        print("[WARN] No matches fetched (offline/API error). Preserving existing output/matches.json.")
        return

    with open(matches_json_path, "w", encoding="utf-8") as f:
        json.dump(matches, f, indent=2, ensure_ascii=False)

    fb_count = sum(1 for m in matches if m["category"] == "Football")
    cr_count = sum(1 for m in matches if m["category"] == "Cricket")
    live_count = sum(1 for m in matches if m["meta_keywords"] == "LIVE")

    print("\n====================================================")
    print(" DONE! Curated Priority Android API: output/matches.json")
    print(f"   * Total Televised Matches : {len(matches)}")
    print(f"   * Football (EPL/EFL/UCL+) : {fb_count}")
    print(f"   * Cricket (Intl/MLC/IPL+) : {cr_count}")
    print(f"   * Currently LIVE          : {live_count}")
    print("====================================================")


if __name__ == "__main__":
    main()
