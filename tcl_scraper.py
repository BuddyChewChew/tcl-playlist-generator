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

# --- Title Parsing (unchanged) ---
_TCL_COLON_RE = re.compile(r'^(.+?)\s+S(\d+):\s+(.+)$', re.IGNORECASE)
_TCL_TRAILING_CODE = re.compile(r'\s+\d+$')
_TCL_DASH_RE = re.compile(r'^(.+?)\s+S(\d+)(?:\s+E(\d+))?(?:\s*[-–]\s*"?(.+?)"?\s*)?$', re.IGNORECASE)
_TCL_PLAIN_DASH_RE = re.compile(r'^(.+?)\s{1,2}-\s+(.+)$')

def parse_tcl_title(raw, api_season, api_episode):
    if not raw: return raw, api_season, api_episode, None
    s = raw.strip()
    m = _TCL_COLON_RE.match(s)
    if m:
        return m.group(1).strip(), int(m.group(2)), api_episode, _TCL_TRAILING_CODE.sub('', m.group(3)).strip() or None
    m = _TCL_DASH_RE.match(s)
    if m:
        return (m.group(1).strip(), int(m.group(2)) if m.group(2) else api_season,
                int(m.group(3)) if m.group(3) else api_episode,
                m.group(4).strip().strip('"') if m.group(4) else None)
    if api_season is None and api_episode is None:
        m = _TCL_PLAIN_DASH_RE.match(s)
        if m: return m.group(1).strip(), None, None, m.group(2).strip() or None
    return s, api_season, api_episode, None

def get_common_params():
    return {
        "userId": DEVICE_ID, "device_type": "web", "device_model": "web",
        "device_id": DEVICE_ID, "app_version": "1.0",
        "country_code": COUNTRY_CODE, "state_code": STATE_CODE,
    }

def resolve_stream(bundle_id, source, media):
    payload = {"type": "channel", "bundle_id": bundle_id, "device_id": DEVICE_ID, "source": source, "stream_url": media}
    params = {"country_code": COUNTRY_CODE, "app_version": "3.2.7"}
    try:
        resp = session.post(f"{BASE_URL}/api/metadata/v1/format-stream-url", params=params, json=payload, timeout=20)
        return resp.json().get("stream_url") or media
    except:
        return media

def fetch_data():
    logger.info("=== Starting TCL API scrape ===")
    livetab = session.get(f"{BASE_URL}/api/metadata/v2/livetab", params=get_common_params()).json()
    
    channels_map = {}
    program_map = {}   # program_id variants -> full details
    stubs = []         # (bid, basic_prog)
    
    now = datetime.now(timezone.utc)
    range_params = {
        "start": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Step 1: Fetch all categories and basic programs
    for line in livetab.get("lines", []):
        cat_id, cat_name = line["id"], line.get("name", "General")
        logger.info(f"Fetching category: {cat_name}")
        
        params = get_common_params()
        params.update({"category_id": cat_id, **range_params})
        
        try:
            data = session.get(f"{BASE_URL}/api/metadata/v1/epg/programlist/by/category", params=params, timeout=30).json()
            
            for ch in data.get("channels", []):
                bid = str(ch.get("bundle_id") or ch.get("id"))
                if bid not in channels_map:
                    stream = resolve_stream(bid, ch.get("source"), ch.get("media", ""))
                    channels_map[bid] = {
                        "id": bid,
                        "name": ch.get("name"),
                        "logo": f"{IMAGE_BASE}{ch.get('logo_color')}" if ch.get('logo_color') else "",
                        "stream": stream,
                        "category": cat_name,
                        "description": ch.get("description", "").strip()
                    }
                
                for prog in ch.get("programs", []):
                    if prog.get("id"):
                        stubs.append((bid, prog))
        except Exception as e:
            logger.warning(f"Category {cat_id} failed: {e}")

    # Step 2: Batch fetch detailed program info with improved ID matching
    if stubs:
        unique_ids = set()
        for _, p in stubs:
            pid = p.get("id")
            if pid:
                pid_str = str(pid)
                unique_ids.add(pid_str)
                # Add composite variants for better matching
                if ':' in pid_str:
                    parts = pid_str.split(':')
                    for length in range(1, len(parts) + 1):
                        unique_ids.add(':'.join(parts[:length]))

        unique_ids = list(unique_ids)
        logger.info(f"Fetching details for {len(unique_ids)} unique program ID variants (from {len(stubs)} stubs)...")

        batch_size = 40
        for i in range(0, len(unique_ids), batch_size):
            batch = unique_ids[i:i + batch_size]
            ids_param = ",".join(batch)

            params = get_common_params()
            params["ids"] = ids_param

            try:
                detail_resp = session.get(f"{BASE_URL}/api/metadata/v1/epg/program/detail", params=params, timeout=30).json()

                count_added = 0
                if isinstance(detail_resp, list):
                    for det in detail_resp:
                        if isinstance(det, dict) and "id" in det:
                            det_id = str(det["id"])
                            program_map[det_id] = det
                            count_added += 1
                            # Store under all useful variants
                            if ':' in det_id:
                                parts = det_id.split(':')
                                for length in range(1, len(parts) + 1):
                                    variant = ':'.join(parts[:length])
                                    program_map[variant] = det
                elif isinstance(detail_resp, dict) and "id" in detail_resp:
                    det_id = str(detail_resp["id"])
                    program_map[det_id] = detail_resp
                    count_added = 1
                    if ':' in det_id:
                        parts = det_id.split(':')
                        for length in range(1, len(parts) + 1):
                            variant = ':'.join(parts[:length])
                            program_map[variant] = detail_resp

                logger.info(f"  → Batch {i//batch_size + 1}: added {count_added} details")

                if i == 0 and count_added > 0:
                    sample = detail_resp[0] if isinstance(detail_resp, list) else detail_resp
                    logger.info(f"  Sample keys: {sorted(sample.keys())}")
                    if sample.get("desc"):
                        preview = sample["desc"][:200] + "..." if len(sample["desc"]) > 200 else sample["desc"]
                        logger.info(f"  Sample rich desc: {preview}")

            except Exception as e:
                logger.warning(f"Detail batch failed: {e}")

    logger.info(f"Total: {len(channels_map)} channels, {len(stubs)} programs, {len(program_map)} with details")
    return channels_map, stubs, program_map

# --- File Generation ---
def generate_files(channels_map, stubs, program_map):
    logger.info("Generating M3U8 and EPG...")

    with open("tcl.m3u8", "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
        for ch in channels_map.values():
            f.write(f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-logo="{ch["logo"]}" group-title="{ch["category"]}",{ch["name"]}\n')
            f.write(f'{ch["stream"]}\n')

    root = ET.Element("tv")
    for ch in channels_map.values():
        channel_el = ET.SubElement(root, "channel", id=ch["id"])
        ET.SubElement(channel_el, "display-name").text = ch["name"]
        if ch["logo"]:
            ET.SubElement(channel_el, "icon", src=ch["logo"])

    desc_count = 0
    rich_desc_count = 0
    for bid, p in stubs:
        prog_id = str(p.get("id")) if p.get("id") else None
        detail = None
        if prog_id:
            # Try exact match first, then variant fallbacks
            detail = program_map.get(prog_id)
            if not detail and ':' in prog_id:
                parts = prog_id.split(':')
                for length in range(1, len(parts) + 1):
                    variant = ':'.join(parts[:length])
                    if variant in program_map:
                        detail = program_map[variant]
                        break

        start_str = p["start"].replace("-", "").replace("T", "").replace(":", "").replace("Z", " +0000")
        stop_str = p["end"].replace("-", "").replace("T", "").replace(":", "").replace("Z", " +0000")
        
        prog_el = ET.SubElement(root, "programme", start=start_str, stop=stop_str, channel=bid)
        
        title = p.get("title", "No Title")
        clean_title, season, episode, subtitle = parse_tcl_title(title, p.get("season"), p.get("episode"))
        
        ET.SubElement(prog_el, "title").text = clean_title
        if subtitle or p.get("subtitle"):
            ET.SubElement(prog_el, "sub-title").text = subtitle or p.get("subtitle")
        
        # Priority: rich detail desc > basic prog desc > channel desc
        desc = ""
        if detail and isinstance(detail.get("desc"), str) and detail["desc"].strip():
            desc = detail["desc"].strip()
            rich_desc_count += 1
        elif isinstance(p.get("desc"), str) and p["desc"].strip():
            desc = p["desc"].strip()
        elif channels_map.get(bid, {}).get("description"):
            desc = channels_map[bid]["description"].strip()

        if desc:
            try:
                ET.SubElement(prog_el, "desc").text = desc
                desc_count += 1
            except Exception as e:
                logger.warning(f"Failed to write desc for {clean_title}: {e}")
        # else: no description available

        # Episode numbering
        if season or episode:
            ep_num = ET.SubElement(prog_el, "episode-num", system="onscreen")
            ep_num.text = f"S{season or 0:02d}E{episode or 0:02d}"
        
        # Rating
        rating = detail.get("rating") if detail else p.get("rating", "TV-NR")
        rating_el = ET.SubElement(prog_el, "rating", system="VCHIP")
        ET.SubElement(rating_el, "value").text = rating

    tree = ET.ElementTree(root)
    tree.write("tcl_epg.xml", encoding="utf-8", xml_declaration=True)
    logger.info(f"EPG generated — {desc_count} programs with descriptions ({rich_desc_count} rich from detail API)")

if __name__ == "__main__":
    try:
        channels, programs, details = fetch_data()
        generate_files(channels, programs, details)
        logger.info("=== TCL Scraper completed successfully ===")
    except Exception as e:
        logger.error(f"Critical error: {e}")
        logger.debug(traceback.format_exc())
