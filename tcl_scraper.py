import requests
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import traceback

# --- Configuration & Constants ---
COUNTRY_CODE = 'US'
STATE_CODE = 'OH'
DEVICE_ID = '1776786148042-4c4uc'
BASE_URL = "https://gateway-prod.ideonow.com"
IMAGE_BASE = "https://tcl-channel-cdn.ideonow.com"
ORIGIN = "https://tcltv.plus"
EPG_URL = "https://raw.githubusercontent.com/BuddyChewChew/tcl-playlist-generator/refs/heads/main/tcl_epg.xml"

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({
    "Accept": "application/json, text/plain, */*",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
})

# --- Title Parsing ---
_TCL_COLON_RE = re.compile(r'^(.+?)\s+S(\d+):\s+(.+)$', re.IGNORECASE)
_TCL_TRAILING_CODE = re.compile(r'\s+\d+$')
_TCL_DASH_RE = re.compile(r'^(.+?)\s+S(\d+)(?:\s+E(\d+))?(?:\s*[-–]\s*"?(.+?)"?\s*)?$', re.IGNORECASE)
_TCL_PLAIN_DASH_RE = re.compile(r'^(.+?)\s{1,2}-\s+(.+)$')

def parse_tcl_title(raw, api_season, api_episode):
    if not raw:
        return raw, api_season, api_episode, None
    s = raw.strip()
    m = _TCL_COLON_RE.match(s)
    if m:
        return m.group(1).strip(), int(m.group(2)), api_episode, _TCL_TRAILING_CODE.sub('', m.group(3)).strip() or None
    m = _TCL_DASH_RE.match(s)
    if m:
        return (m.group(1).strip(),
                int(m.group(2)) if m.group(2) else api_season,
                int(m.group(3)) if m.group(3) else api_episode,
                m.group(4).strip().strip('"') if m.group(4) else None)
    if api_season is None and api_episode is None:
        m = _TCL_PLAIN_DASH_RE.match(s)
        if m:
            return m.group(1).strip(), None, None, m.group(2).strip() or None
    return s, api_season, api_episode, None

def get_common_params():
    return {
        "userId": DEVICE_ID,
        "device_type": "web",
        "device_model": "web",
        "device_id": DEVICE_ID,
        "app_version": "1.0",
        "country_code": COUNTRY_CODE,
        "state_code": STATE_CODE,
    }

# --- API Methods ---
def resolve_stream(bundle_id, source, media):
    payload = {"type": "channel", "bundle_id": bundle_id, "device_id": DEVICE_ID, "source": source, "stream_url": media}
    params = {"country_code": COUNTRY_CODE, "app_version": "3.2.7"}
    try:
        resp = session.post(f"{BASE_URL}/api/metadata/v1/format-stream-url", params=params, json=payload, timeout=20)
        return resp.json().get("stream_url") or media
    except Exception as e:
        logger.warning(f"Stream resolve failed for {bundle_id}: {e}")
        return media

def fetch_data():
    logger.info("=== Starting TCL API scrape ===")
    livetab = session.get(f"{BASE_URL}/api/metadata/v2/livetab", params=get_common_params()).json()
    channels_map = {}
    stubs = []
    
    now = datetime.now(timezone.utc)
    range_params = {
        "start": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    logger.info(f"Time range: {range_params['start']} → {range_params['end']}")
    logger.info(f"Found {len(livetab.get('lines', []))} categories in livetab")

    for line in livetab.get("lines", []):
        cat_id = line["id"]
        cat_name = line.get("name", "Unknown")
        logger.info(f"Fetching category: {cat_name} (ID: {cat_id})")

        params = get_common_params()
        params.update({"category_id": cat_id, **range_params})
        
        try:
            data = session.get(f"{BASE_URL}/api/metadata/v1/epg/programlist/by/category", 
                             params=params, timeout=30).json()
            
            channels_in_cat = data.get("channels", [])
            logger.info(f"  → Got {len(channels_in_cat)} channels in this category")

            for ch in channels_in_cat:
                bid = str(ch.get("bundle_id") or ch.get("id"))
                channel_desc = ch.get("description", "").strip()
                
                if bid not in channels_map:
                    stream = resolve_stream(bid, ch.get("source"), ch.get("media", ""))
                    channels_map[bid] = {
                        "id": bid,
                        "name": ch.get("name"),
                        "logo": f"{IMAGE_BASE}{ch.get('logo_color')}" if ch.get('logo_color') else "",
                        "stream": stream,
                        "category": cat_name,
                        "description": channel_desc if len(channel_desc) > 30 else ""  # only keep good descriptions
                    }
                    if channel_desc:
                        logger.info(f"    → Channel '{ch.get('name')}' has description ({len(channel_desc)} chars)")

                # Log program structure on first channel of first category
                if len(stubs) < 5 and channels_in_cat.index(ch) == 0:
                    if ch.get("programs"):
                        prog = ch["programs"][0]
                        logger.info(f"    Sample program keys: {list(prog.keys())}")

                for prog in ch.get("programs", []):
                    stubs.append((bid, prog))

        except Exception as e:
            logger.error(f"Failed to fetch category {cat_id} ({cat_name}): {e}")
            logger.debug(traceback.format_exc())

    logger.info(f"=== Scrape complete: {len(channels_map)} unique channels, {len(stubs)} total program entries ===")
    return channels_map, stubs

# --- File Generation ---
def generate_files(channels_map, stubs):
    logger.info("Generating M3U8 and EPG files...")

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
        if ch["logo"]:
            ET.SubElement(channel_el, "icon", src=ch["logo"])

    desc_count = 0
    for bid, p in stubs:
        start_str = p["start"].replace("-", "").replace("T", "").replace(":", "").replace("Z", " +0000")
        stop_str = p["end"].replace("-", "").replace("T", "").replace(":", "").replace("Z", " +0000")
        
        prog_el = ET.SubElement(root, "programme", start=start_str, stop=stop_str, channel=bid)
        
        title = p.get("title", "No Title")
        clean_title, season, episode, subtitle = parse_tcl_title(title, p.get("season"), p.get("episode"))
        
        ET.SubElement(prog_el, "title").text = clean_title
        
        if subtitle or p.get("subtitle"):
            ET.SubElement(prog_el, "sub-title").text = subtitle or p.get("subtitle")
        
        # Channel-level description as fallback
        ch_desc = channels_map.get(bid, {}).get("description", "")
        if ch_desc:
            ET.SubElement(prog_el, "desc").text = ch_desc
            desc_count += 1
        
        # Episode number
        if season is not None or episode is not None:
            ep_num = ET.SubElement(prog_el, "episode-num", system="onscreen")
            ep_num.text = f"S{season or 0:02d}E{episode or 0:02d}"
        
        # Rating
        rating_val = p.get("rating", "TV-NR")
        rating_el = ET.SubElement(prog_el, "rating", system="VCHIP")
        ET.SubElement(rating_el, "value").text = rating_val

    tree = ET.ElementTree(root)
    tree.write("tcl_epg.xml", encoding="utf-8", xml_declaration=True)
    
    logger.info(f"EPG generated with {len(stubs)} programs — {desc_count} have channel descriptions")
    logger.info("Files saved: tcl.m3u8 and tcl_epg.xml")

if __name__ == "__main__":
    try:
        channels, programs = fetch_data()
        generate_files(channels, programs)
        logger.info("=== TCL Scraper finished successfully ===")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        logger.debug(traceback.format_exc())
