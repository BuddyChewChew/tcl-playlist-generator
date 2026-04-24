import requests
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# --- Configuration & Constants ---
COUNTRY_CODE = 'US'
STATE_CODE = 'OH'
DEVICE_ID = '1776786148042-4c4uc'
BASE_URL = "https://gateway-prod.ideonow.com"
IMAGE_BASE = "https://tcl-channel-cdn.ideonow.com"
ORIGIN = "https://tcltv.plus"
EPG_URL = "https://raw.githubusercontent.com/BuddyChewChew/tcl-playlist-generator/refs/heads/main/tcl_epg.xml"

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({
    "Accept": "application/json, text/plain, */*",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
})

# --- Regex & Normalization Logic ---
_TCL_COLON_RE = re.compile(r'^(.+?)\s+S(\d+):\s+(.+)$', re.IGNORECASE)
_TCL_TRAILING_CODE = re.compile(r'\s+\d+$')
_TCL_DASH_RE = re.compile(r'^(.+?)\s+S(\d+)(?:\s+E(\d+))?(?:\s*[-–]\s*"?(.+?)"?\s*)?$', re.IGNORECASE)
_TCL_PLAIN_DASH_RE = re.compile(r'^(.+?)\s{1,2}-\s+(.+)$')

_RATING_NORM = {
    'TVY': 'TV-Y', 'TV Y': 'TV-Y', 'TVY7': 'TV-Y7', 'TV Y7': 'TV-Y7',
    'TVG': 'TV-G', 'TV G': 'TV-G', 'TVPG': 'TV-PG', 'TV PG': 'TV-PG',
    'TV14': 'TV-14', 'TV 14': 'TV-14', 'TVMA': 'TV-MA', 'TV MA': 'TV-MA',
    'TVNR': 'TV-NR', 'TV NR': 'TV-NR', 'NR': 'TV-NR', 'NA': 'TV-NR', 'UNRATED': 'TV-NR',
}

def parse_tcl_title(raw, api_season, api_episode):
    if not raw: return raw, api_season, api_episode, None
    s = raw.strip()
    m = _TCL_COLON_RE.match(s)
    if m:
        return m.group(1).strip(), int(m.group(2)), api_episode, _TCL_TRAILING_CODE.sub('', m.group(3)).strip() or None
    m = _TCL_DASH_RE.match(s)
    if m:
        return m.group(1).strip(), int(m.group(2)) if m.group(2) else api_season, int(m.group(3)) if m.group(3) else api_episode, m.group(4).strip().strip('"') if m.group(4) else None
    if api_season is None and api_episode is None:
        m = _TCL_PLAIN_DASH_RE.match(s)
        if m: return m.group(1).strip(), None, None, m.group(2).strip() or None
    return s, api_season, api_episode, None

def normalize_rating(raw):
    if not raw: return "TV-NR"
    base = raw.strip().split()[0].upper()
    return _RATING_NORM.get(base, "TV-NR")

def get_common_params():
    return {
        "userId": DEVICE_ID, "device_type": "web", "device_model": "web",
        "device_id": DEVICE_ID, "app_version": "1.0",
        "country_code": COUNTRY_CODE, "state_code": STATE_CODE,
    }

# --- API Methods ---
def resolve_stream(bundle_id, source, media):
    payload = {"type": "channel", "bundle_id": bundle_id, "device_id": DEVICE_ID, "source": source, "stream_url": media}
    params = {"country_code": COUNTRY_CODE, "app_version": "3.2.7"}
    try:
        resp = session.post(f"{BASE_URL}/api/metadata/v1/format-stream-url", params=params, json=payload, timeout=20)
        return resp.json().get("stream_url") or media
    except:
        return media

def fetch_data():
    logger.info("Fetching channel list...")
    livetab = session.get(f"{BASE_URL}/api/metadata/v2/livetab", params=get_common_params()).json()
    channels_map = {}
    stubs = []
    
    now = datetime.now(timezone.utc)
    range_params = {
        "start": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    for line in livetab.get("lines", []):
        cat_id, cat_name = line["id"], line.get("name", "General")
        params = get_common_params()
        params.update({"category_id": cat_id, **range_params})
        
        try:
            data = session.get(f"{BASE_URL}/api/metadata/v1/epg/programlist/by/category", params=params).json()
        except: continue

        for ch in data.get("channels", []):
            bid = str(ch.get("bundle_id") or ch.get("id"))
            if bid not in channels_map:
                stream = resolve_stream(bid, ch.get("source"), ch.get("media", ""))
                channels_map[bid] = {
                    "id": bid, "name": ch.get("name"), "logo": f"{IMAGE_BASE}{ch.get('logo_color')}" if ch.get('logo_color') else "",
                    "stream": stream, "category": cat_name, "programs": []
                }
            
            for prog in ch.get("programs", []):
                stubs.append((bid, prog))

    return channels_map, stubs

# --- File Generation ---
def generate_files(channels_map, stubs):
    # Build M3U8
    with open("tcl.m3u8", "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
        for ch in channels_map.values():
            f.write(f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-logo="{ch["logo"]}" group-title="{ch["category"]}",{ch["name"]}\n')
            f.write(f'{ch["stream"]}\n')
    
    # Build XML EPG
    root = ET.Element("tv")
    for ch in channels_map.values():
        channel_el = ET.SubElement(root, "channel", id=ch["id"])
        ET.SubElement(channel_el, "display-name").text = ch["name"]
        if ch["logo"]: ET.SubElement(channel_el, "icon", src=ch["logo"])

    for bid, p in stubs:
        start = p["start"].replace("-", "").replace(":", "").replace("Z", " +0000")
        stop = p["end"].replace("-", "").replace(":", "").replace("Z", " +0000")
        
        # Parse titles for Season/Episode/Sub-title data
        title, season, episode, sub_title = parse_tcl_title(
            p.get("title"), 
            p.get("season"), 
            p.get("episode")
        )
        
        prog_el = ET.SubElement(root, "programme", start=start, stop=stop, channel=bid)
        ET.SubElement(prog_el, "title").text = title or "No Title"
        
        if sub_title:
            ET.SubElement(prog_el, "sub-title").text = sub_title
            
        if p.get("desc"):
            ET.SubElement(prog_el, "desc").text = p["desc"]
        
        # Add Season/Episode info if available
        if season is not None and episode is not None:
            # XMLTV format uses 0-based numbering for the attribute: .S.E.
            # But text format S01 E01 is often better for general players
            ep_num = ET.SubElement(prog_el, "episode-num", system="common")
            ep_num.text = f"S{season} E{episode}"
            
        # Add Rating
        rating = normalize_rating(p.get("rating"))
        rating_el = ET.SubElement(prog_el, "rating", system="VCHIP")
        ET.SubElement(rating_el, "value").text = rating
        
    tree = ET.ElementTree(root)
    tree.write("tcl_epg.xml", encoding="utf-8", xml_declaration=True)

if __name__ == "__main__":
    channels, programs = fetch_data()
    generate_files(channels, programs)
    logger.info("Done! Generated tcl.m3u8 and tcl_epg.xml")
