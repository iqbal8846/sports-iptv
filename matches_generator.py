#!/usr/bin/env python3
"""
Sports Match Schedule & Smart M3U8 Broadcaster Matcher
======================================================
Generates `output/matches.json` for the SportsWatch Android App (`D:\\sportsapp`).
Uses 100% Free Public Scoreboard APIs (ESPN Soccer + ESPN Cricket) — Zero API keys required!

Features:
  1. Fetches 7-day window (Mon-Sun / -1 to +6 days) of Football & Cricket fixtures + live scores
  2. Converts all dates & kickoff times to Bangladesh Time (BDT / UTC+6) + UTC epoch ms
  3. Matches each fixture/league to verified working `.m3u8` channels from `output/sports_all.m3u`
     using both Official TV Broadcaster tags and Regional Broadcast Rights rules (Global + BDIX)
  4. Outputs `output/matches.json` in the exact schema consumed by Android's `CricfyApiClient.java`
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
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
# 1. VERIFIED M3U CHANNEL LOADER
# =====================================================================

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
        # Clean resolution tags like (1080p) or [Geo-blocked] from button title
        cleaned = re.sub(r"\[[^\]]*\]", "", self.name)
        cleaned = re.sub(r"\(\d+p\)", "", cleaned, flags=re.IGNORECASE).strip()
        if self.is_bdix and "bdix" not in cleaned.lower():
            return f"{cleaned} (BDIX)"
        return cleaned or self.name


def load_verified_streams(m3u_path: str) -> List[VerifiedStream]:
    """Load working streams already verified by `generator.py` from `output/sports_all.m3u`."""
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
            # Skip geo-blocked tagged channels if better ones exist, or keep clean URL
            if "[geo-blocked]" in extinf.lower():
                extinf = None
                continue

            # Extract pipe options if present (e.g. url|user-agent=Mozilla)
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

            # Strip URL fragments (#...) that may confuse HLS parsers
            clean_url = raw_url.split("#", 1)[0].strip()

            attrs = dict(ATTR_REGEX.findall(extinf))
            comma_idx = extinf.rfind(",")
            name = extinf[comma_idx + 1 :].strip() if comma_idx != -1 else "Sports Stream"
            group = attrs.get("group-title", "")
            logo = attrs.get("tvg-logo", "")
            is_bdix = "bdix" in group.lower() or "bdix" in name.lower()

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
# 2. SMART BROADCASTER & LEAGUE RIGHTS MATCHER
# =====================================================================

LEAGUE_PRIORITY_KEYWORDS = [
    # Top International & European Football
    "champions league", "premier league", "laliga", "la liga", "serie a",
    "bundesliga", "ligue 1", "europa league", "conference league",
    "nations league", "world cup", "euro ", "copa america", "afcon", "asian cup",
    "fa cup", "copa del rey", "dfb", "coppa italia", "carabao", "efl",
    "saudi", "mls", "indian super league", "isl", "international friendly",
    # Top Cricket
    "icc", "ipl", "indian premier league", "bpl", "bangladesh", "psl",
    "big bash", "bbl", "ashes", "test", "odi", "twenty20", "t20",
]

FOOTBALL_LEAGUE_CHANNEL_RULES: List[Tuple[List[str], List[str]]] = [
    (
        ["premier league", "epl", "fa cup", "efl", "carabao"],
        ["star sports", "sky sports", "tnt sports", "setanta", "optus", "supersport", "arena sport", "bein"],
    ),
    (
        ["laliga", "la liga", "copa del rey", "supercopa"],
        ["bein", "espn", "setanta", "eleven sports", "arena sport", "t sports", "premier sports"],
    ),
    (
        ["champions league", "europa", "conference league", "nations league", "uefa", "world cup", "fifa", "friendly"],
        ["sony", "tnt sports", "bein", "cbs sports", "setanta", "eurosport", "arena sport", "eleven sports", "fox sports", "t sports"],
    ),
    (
        ["serie a", "coppa italia"],
        ["cbs sports", "bein", "setanta", "arena sport", "eleven sports", "tnt sports"],
    ),
    (
        ["bundesliga", "dfb"],
        ["espn", "sky sports", "setanta", "eleven sports", "bein", "sony"],
    ),
    (
        ["ligue 1"],
        ["bein", "setanta", "arena sport", "eleven sports"],
    ),
    (
        ["mls", "nwsl", "concacaf", "conmebol", "copa", "liga mx", "brazil", "argentina"],
        ["fox sports", "cbs sports", "espn", "tsn", "bein", "sport tv"],
    ),
    (
        ["saudi", "afc", "isl", "indian"],
        ["ssc", "sony", "star sports", "t sports", "alkass", "dubai sports", "abu dhabi sports", "fox sports"],
    ),
]

CLUB_CHANNEL_RULES: List[Tuple[str, str]] = [
    ("real madrid", "real madrid tv"),
    ("barcelona", "barca"),
    ("manchester united", "mutv"),
    ("liverpool", "lfctv"),
    ("celtic", "celtic tv"),
    ("benfica", "benfica tv"),
]


def match_streams_for_fixture(
    sport: str,
    league_name: str,
    team1: str,
    team2: str,
    official_broadcasters: List[str],
    all_streams: List[VerifiedStream],
    max_streams: int = 6,
) -> List[VerifiedStream]:
    """
    Match a specific game to the most accurate working `.m3u8` channels:
      1. Official TV broadcaster names reported by ESPN (e.g., CBS, ESPN, Sky Sports, Willow)
      2. Team-specific TV channels (e.g., Real Madrid TV, Barca TV, MUTV)
      3. Regional Broadcast Rights rules for that League / Country (Global + BDIX)
      4. Category fallback so every match has playable servers
    """
    league_lower = league_name.lower()
    teams_lower = f"{team1} {team2}".lower()
    picked: List[VerifiedStream] = []
    seen_urls: Set[str] = set()
    seen_titles: Set[str] = set()

    def kw_matches(text_lower: str, kw: str) -> bool:
        return bool(re.search(r"\b" + re.escape(kw), text_lower))

    def try_add_matching(
        keywords: List[str],
        allow_categories: Set[str],
        limit_add: int,
        prefer_bdix: Optional[bool] = None,
    ) -> None:
        added = 0
        ordered = sorted(
            all_streams,
            key=lambda s: (
                0 if s.stream_type == "m3u8" else 1,
                (0 if s.is_bdix else 1) if prefer_bdix else (1 if s.is_bdix else 0),
            ),
        )
        for st in ordered:
            if len(picked) >= max_streams or added >= limit_add:
                break
            if st.url in seen_urls:
                continue
            if prefer_bdix is not None and st.is_bdix != prefer_bdix:
                continue

            name_lower = st.name.lower()
            # Skip Thai channel "T Sports 7" when matching South Asian T Sports
            if "t sports 7" in name_lower or "chtsport" in st.url.lower():
                continue

            base_cat = st.group.replace("BDIX ", "").strip()
            if allow_categories and base_cat not in allow_categories:
                continue

            if any(kw_matches(name_lower, kw) for kw in keywords):
                title_key = f"{st.clean_display_name.lower()}_{st.is_bdix}"
                if sum(1 for t in seen_titles if t == title_key) >= 1:
                    continue
                seen_urls.add(st.url)
                seen_titles.add(title_key)
                picked.append(st)
                added += 1

    if sport == "Cricket":
        allowed = {"Cricket", "Multi-Sport"}
        bcast_kws = [b.lower() for b in official_broadcasters if len(b.strip()) >= 3]
        if bcast_kws:
            try_add_matching(bcast_kws, allowed, limit_add=2)

        if "bangladesh" in teams_lower or "bangladesh" in league_lower or "bpl" in league_lower:
            try_add_matching(["t sports", "gazi", "gtv", "willow", "star sports", "ptv sports"], allowed, limit_add=3, prefer_bdix=False)
            try_add_matching(["t sports", "gazi", "gtv", "willow", "star sports", "a sports"], allowed, limit_add=3, prefer_bdix=True)
        elif "pakistan" in teams_lower or "pakistan" in league_lower or "psl" in league_lower:
            try_add_matching(["willow", "ptv sports", "a sports", "star sports"], allowed, limit_add=3, prefer_bdix=False)
            try_add_matching(["ptv sports", "a sports", "ten sports", "willow", "t sports"], allowed, limit_add=3, prefer_bdix=True)
        elif "australia" in teams_lower or "sheffield" in league_lower or "big bash" in league_lower or "marsh" in league_lower:
            try_add_matching(["cricket gold", "willow", "fox sports", "dd sports"], allowed, limit_add=3, prefer_bdix=False)
            try_add_matching(["cricket gold", "willow", "star sports", "fox sports"], allowed, limit_add=3, prefer_bdix=True)
        elif "india" in teams_lower or "ipl" in league_lower or "ranji" in league_lower:
            try_add_matching(["willow", "dd sports", "star sports", "cricket gold"], allowed, limit_add=3, prefer_bdix=False)
            try_add_matching(["star sports", "dd sports", "willow", "t sports"], allowed, limit_add=3, prefer_bdix=True)
        else:
            try_add_matching(["willow", "cricket gold", "dd sports", "star sports"], allowed, limit_add=3, prefer_bdix=False)
            try_add_matching(["t sports", "ptv sports", "a sports", "willow", "gazi", "star sports"], allowed, limit_add=3, prefer_bdix=True)

        # Fill remaining slots if under max_streams
        try_add_matching(["willow", "t sports", "ptv sports", "a sports", "star sports", "gazi", "cricket"], allowed, limit_add=max_streams)

    else:
        # Football / Soccer
        allowed = {"Football", "Multi-Sport", "Live Sports"}
        # 1. Club channel bonus if club is playing
        for club_kw, ch_kw in CLUB_CHANNEL_RULES:
            if club_kw in teams_lower:
                try_add_matching([ch_kw], allowed, limit_add=1)

        # 2. Official broadcast tags from ESPN (e.g., CBS, ESPN, FOX, Sky, TNT, beIN)
        bcast_kws = []
        for b in official_broadcasters:
            bl = b.lower().strip()
            if "cbs" in bl or "paramount" in bl:
                bcast_kws.append("cbs sports")
            elif "espn" in bl:
                bcast_kws.append("espn")
            elif "fox" in bl or "fs1" in bl or "fs2" in bl:
                bcast_kws.append("fox sports")
            elif "sky" in bl:
                bcast_kws.append("sky sports")
            elif "tnt" in bl or "bt sport" in bl:
                bcast_kws.append("tnt sports")
            elif "bein" in bl:
                bcast_kws.append("bein")
            elif "dazn" in bl:
                bcast_kws.append("dazn")
            elif "eurosport" in bl:
                bcast_kws.append("eurosport")
        if bcast_kws:
            try_add_matching(bcast_kws, allowed, limit_add=2)

        # 3. League broadcast rights rules
        matched_league = False
        for lg_kws, ch_kws in FOOTBALL_LEAGUE_CHANNEL_RULES:
            if any(k in league_lower for k in lg_kws):
                matched_league = True
                try_add_matching(ch_kws, allowed, limit_add=3, prefer_bdix=False)
                try_add_matching(ch_kws + ["t sports", "star sports", "bein"], allowed, limit_add=3, prefer_bdix=True)
                break

        # Only fill extra servers if this match is genuinely televised (official TV tag, club TV, or top league)
        if picked or matched_league or bcast_kws:
            try_add_matching(
                ["bein", "setanta", "eurosport", "t sports", "star sports", "espn", "cbs sports", "fox sports"],
                allowed,
                limit_add=max_streams,
            )

    return picked[:max_streams]


# =====================================================================
# 3. ESPN PUBLIC SCOREBOARD FETCHER (7-DAY WINDOW)
# =====================================================================

def fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        print(f"  [WARN] Failed to fetch {url}: {exc}")
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


def compute_league_rank(sport: str, league_name: str, is_international: bool = False) -> int:
    ll = league_name.lower()
    if sport == "Cricket":
        if is_international or any(k in ll for k in ("icc", "world cup", "bangladesh", "india", "pakistan", "australia", "england", "ipl", "bpl", "psl", "big bash", "ashes")):
            return 0
        return 1

    for idx, kw in enumerate(LEAGUE_PRIORITY_KEYWORDS):
        if kw in ll:
            return idx
    return 50


def extract_broadcasters(ev: dict) -> List[str]:
    names: List[str] = []
    bcast_str = ev.get("broadcast")
    if isinstance(bcast_str, str) and bcast_str.strip():
        names.extend([p.strip() for p in bcast_str.split("/") if p.strip()])

    for b in ev.get("broadcasts", []) or []:
        if isinstance(b, dict):
            names.extend(b.get("names", []) or [])
            media = b.get("media", {})
            if isinstance(media, dict) and media.get("shortName"):
                names.append(media["shortName"])

    for gb in ev.get("geoBroadcasts", []) or []:
        if isinstance(gb, dict):
            media = gb.get("media", {})
            if isinstance(media, dict) and media.get("shortName"):
                names.append(media["shortName"])

    return names


def fetch_week_fixtures(all_streams: List[VerifiedStream], max_soccer_per_day: int = 35) -> List[dict]:
    """
    Fetch full 7-day current week (Mon..Sun) plus tomorrow/next days so Android's
    Mon-Sun date bar has real Football & Cricket matches on every single day.
    """
    now_bd = datetime.datetime.now(BD_TZ)
    today_midnight_bd = now_bd.replace(hour=0, minute=0, second=0, microsecond=0)

    # Query from yesterday (for late-night LIVE games) through +7 days ahead; skip old finished days
    query_dates = [today_midnight_bd + datetime.timedelta(days=i) for i in range(-1, 7)]
    seen_match_day_keys: Set[str] = set()
    raw_matches: List[Tuple[str, int, int, dict]] = []  # (date_bdt, status_rank, league_rank, match_json)

    print(f"[Step 1] Fetching 7-day Football & Cricket schedules ({query_dates[0].strftime('%Y-%m-%d')} to {query_dates[-1].strftime('%Y-%m-%d')})...")

    tasks = []
    for day_dt in query_dates:
        yyyymmdd = day_dt.strftime("%Y%m%d")
        for sport_param, sport_label in (("cricket", "Cricket"), ("soccer", "Football")):
            api_url = f"https://site.web.api.espn.com/apis/v2/scoreboard/header?sport={sport_param}&dates={yyyymmdd}"
            tasks.append((day_dt, sport_label, api_url))

    fetched_payloads: List[Tuple[datetime.datetime, str, Optional[dict]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futs = {executor.submit(fetch_json, u, 15): (d, sl) for d, sl, u in tasks}
        for fut in concurrent.futures.as_completed(futs):
            d, sl = futs[fut]
            fetched_payloads.append((d, sl, fut.result()))

    fetched_payloads.sort(key=lambda x: (x[0], x[1]))

    for day_dt, sport_label, payload in fetched_payloads:
        queried_iso = day_dt.strftime("%Y-%m-%d")
        if True:
            if not payload:
                continue

            sports_arr = payload.get("sports") or []
            if not sports_arr:
                continue

            leagues_arr = sports_arr[0].get("leagues") or []
            day_sport_items: List[Tuple[int, int, dict]] = []

            for lg in leagues_arr:
                league_name = (lg.get("name") or lg.get("shortName") or sport_label).strip()
                events = lg.get("events") or []

                for ev in events:
                    ev_id = str(ev.get("id") or "")
                    if not ev_id:
                        continue

                    competitors = ev.get("competitors") or []
                    if len(competitors) < 2:
                        continue

                    # Order home first, away second when marked
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

                    dt_utc = parse_iso_utc(ev.get("date", ""))
                    end_utc = parse_iso_utc(ev.get("endDate", ""))
                    if dt_utc:
                        dt_bd = dt_utc.astimezone(BD_TZ)
                        match_date_bdt = dt_bd.strftime("%Y-%m-%d")
                        match_time_bdt = dt_bd.strftime("%I:%M %p")
                        start_ts_ms = int(dt_utc.timestamp() * 1000)
                    else:
                        match_date_bdt = queried_iso
                        match_time_bdt = "TBD"
                        start_ts_ms = 0

                    # Multi-day Test/First-Class Cricket: if match spans multiple days, attach to queried day
                    event_type = (ev.get("eventType") or "").lower()
                    if sport_label == "Cricket" and event_type in ("test", "first-class", "multi-day") and end_utc and dt_utc:
                        if dt_utc.date() <= day_dt.date() <= end_utc.date():
                            match_date_bdt = queried_iso
                            # Adjust start_ts_ms to today's play session time so Android app knows today's session
                            session_dt_bd = datetime.datetime.combine(
                                day_dt.date(),
                                dt_utc.astimezone(BD_TZ).time(),
                                tzinfo=BD_TZ,
                            )
                            start_ts_ms = int(session_dt_bd.timestamp() * 1000)

                    dedup_key = f"{sport_label}_{ev_id}_{match_date_bdt}"
                    if dedup_key in seen_match_day_keys:
                        continue
                    seen_match_day_keys.add(dedup_key)

                    # Status & Score formatting for Android `CricfyApiClient.java`
                    full_status = ev.get("fullStatus") or {}
                    st_type = full_status.get("type") or {}
                    state = (st_type.get("state") or ev.get("status") or "pre").lower()
                    short_detail = (st_type.get("shortDetail") or st_type.get("detail") or "").strip()
                    summary_txt = (full_status.get("summary") or ev.get("summary") or "").strip()

                    if state == "in":
                        meta_keywords = "LIVE"
                        status_rank = 0
                        if sport_label == "Football":
                            s1 = score1 if score1 != "" else "0"
                            s2 = score2 if score2 != "" else "0"
                            clock_lbl = short_detail or ev.get("clock") or "LIVE"
                            description = f"{s1} : {s2} | {clock_lbl}"
                        else:
                            # Cricket live score
                            active_score = score2 if ("(" in score2 or "/" in score2) else (score1 or "LIVE")
                            # Keep center text compact for Android card
                            short_score = active_score.split("(")[0].strip()
                            if len(short_score) > 14:
                                short_score = short_score[:14].strip()
                            sub_info = summary_txt or short_detail or "Live"
                            description = f"{short_score or 'LIVE'} | {sub_info}"
                    elif state == "post":
                        # Skip finished matches that no longer have an active live stream
                        continue
                    else:
                        meta_keywords = "UPCOMING"
                        status_rank = 1
                        venue = (ev.get("location") or "").strip()
                        description = venue if venue else f"{league_name}"

                    broadcasters = extract_broadcasters(ev)
                    matched_streams = match_streams_for_fixture(
                        sport=sport_label,
                        league_name=league_name,
                        team1=team1,
                        team2=team2,
                        official_broadcasters=broadcasters,
                        all_streams=all_streams,
                        max_streams=6,
                    )

                    # Strictly exclude matches that have 0 available streaming links
                    if not matched_streams:
                        continue

                    stream_ads = []
                    for st in matched_streams:
                        s_entry = {
                            "title": st.clean_display_name,
                            "quality": st.quality_label,
                            "type": st.stream_type,
                            "url": st.url,
                        }
                        if st.user_agent:
                            s_entry["user_agent"] = st.user_agent
                        if st.referer:
                            s_entry["referer"] = st.referer
                        stream_ads.append(s_entry)

                    league_rank = compute_league_rank(sport_label, league_name, is_international=is_intl)

                    match_obj = {
                        "id": f"{sport_label[:fb_len(sport_label)].lower()}_{ev_id}_{match_date_bdt.replace('-', '')}",
                        "category": sport_label,
                        "event_name": league_name,
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
                        "official_tv": broadcasters[:4],
                        "stream_ads": stream_ads,
                    }
                    day_sport_items.append((status_rank, league_rank, match_obj))

            # Sort by LIVE first, then top leagues first, then start time
            day_sport_items.sort(key=lambda x: (x[0], x[1], x[2].get("start_timestamp_ms", 0)))
            if sport_label == "Football":
                day_sport_items = day_sport_items[:max_soccer_per_day]
            else:
                day_sport_items = day_sport_items[:20]

            for st_rank, lg_rank, m_obj in day_sport_items:
                raw_matches.append((m_obj["date"], st_rank, lg_rank, m_obj))

    # Final global sort: LIVE matches first on each day, then top leagues, then kickoff time
    raw_matches.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            item[3].get("start_timestamp_ms", 0),
        )
    )
    return [item[3] for item in raw_matches]


def fb_len(sport_label: str) -> int:
    return 4 if sport_label == "Football" else 5


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate matches.json schedule + matched M3U8 streams for Android app.")
    parser.add_argument("--output-dir", default="output", help="Directory containing sports_all.m3u and output matches.json")
    parser.add_argument("--max-soccer-per-day", type=int, default=35, help="Max football fixtures kept per day")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = args.output_dir if os.path.isabs(args.output_dir) else os.path.join(base_dir, args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    m3u_path = os.path.join(output_dir, "sports_all.m3u")
    verified_streams = load_verified_streams(m3u_path)
    print(f"[Init] Loaded {len(verified_streams)} verified online streams from {m3u_path}")

    matches = fetch_week_fixtures(verified_streams, max_soccer_per_day=args.max_soccer_per_day)

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
    print(" DONE! Android JSON API Generated: output/matches.json")
    print(f"   * Total Matches (7-Day Week) : {len(matches)}")
    print(f"   * Football Matches           : {fb_count}")
    print(f"   * Cricket Matches            : {cr_count}")
    print(f"   * Currently LIVE             : {live_count}")
    print("====================================================")


if __name__ == "__main__":
    main()
