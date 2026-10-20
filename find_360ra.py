#!/usr/bin/env python3
"""
find_360ra.py — Find all 360 Reality Audio tracks on Amazon Music
by piggybacking on OrpheusDL's existing authenticated session.

Run from your OrpheusDL directory:
    python3 find_360ra.py

Output: 360ra_tracks.json and 360ra_tracks.csv in the same folder.
"""

import sys
import os
import json
import csv
import time
import logging
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config — tweak these if you want
# ---------------------------------------------------------------------------

# Search terms to sweep across. Add more if you want broader coverage.
SEARCH_QUERIES = [
    "360 Reality Audio",
    "spatial audio",
    "immersive audio",
    # Genre sweeps — these catch 360RA albums not tagged with the format name
    "pop 2024",
    "pop 2023",
    "pop 2022",
    "hip hop 2024",
    "hip hop 2023",
    "r&b 2024",
    "jazz 2024",
    "classical 2024",
    "rock 2024",
    "electronic 2024",
    "country 2024",
    "latin 2024",
    "k-pop 2024",
    "j-pop 2024",
]

# How many search results to pull per query (max Amazon returns per page is 30)
RESULTS_PER_QUERY = 30

# Delay between API calls in seconds — be polite to the API
REQUEST_DELAY = 0.5

# Output files
OUTPUT_JSON = "360ra_tracks.json"
OUTPUT_CSV  = "360ra_tracks.csv"

# If True, also resolves full track listings for each discovered 360RA album
# (slower but finds individual track ASINs, not just album ASINs)
RESOLVE_TRACK_LISTS = True

# ---------------------------------------------------------------------------
# Setup logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("find_360ra")

# ---------------------------------------------------------------------------
# Import OrpheusDL's Amazon Music module
# ---------------------------------------------------------------------------
def load_orpheus_amazon():
    """Import the amazonmusic interface from OrpheusDL."""
    # Make sure we can find the orpheus modules
    orpheus_root = Path(__file__).parent
    if str(orpheus_root) not in sys.path:
        sys.path.insert(0, str(orpheus_root))

    try:
        from orpheus.core import Orpheus
        from modules.amazonmusic.interface import ModuleInterface as AmazonMusicInterface
        return Orpheus, AmazonMusicInterface
    except ImportError as e:
        log.error(f"Could not import OrpheusDL modules: {e}")
        log.error("Make sure you're running this from your OrpheusDL directory.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 360RA detection logic
# ---------------------------------------------------------------------------
def is_360ra_quality_name(name: str) -> bool:
    """Check if an official_quality_name or quality string indicates 360RA."""
    name_lower = name.lower()
    return any(x in name_lower for x in [
        "spatial_ra360",
        "ra360",
        "360ra",
        "mha1",
        "mpeg-h",
        "3d",          # Amazon uses "3D" as the official_quality_name for 360RA
    ])


def check_track_is_360ra(module, asin: str) -> Optional[dict]:
    """
    Ask OrpheusDL to resolve quality tiers for a track ASIN.
    Returns track info dict if it has 360RA, else None.
    """
    try:
        track_info = module.get_track_info(asin)
        if track_info is None:
            return None

        # OrpheusDL returns a TrackInfo with a .codec or similar field.
        # We check by looking at the codec enum value (MHA1 = 512) or quality name.
        codec_val = getattr(track_info, 'codec', None)
        quality_name = getattr(track_info, 'quality_name', '') or ''

        # MHA1 codec enum value in OrpheusDL's CodecEnum
        MHA1_VALUE = 512

        is_360 = False
        if codec_val is not None:
            codec_int = codec_val.value if hasattr(codec_val, 'value') else codec_val
            if codec_int == MHA1_VALUE:
                is_360 = True
        if is_360ra_quality_name(quality_name):
            is_360 = True

        if is_360:
            return {
                'asin': asin,
                'title': getattr(track_info, 'name', ''),
                'artist': getattr(track_info, 'artist', '') or '',
                'album': getattr(track_info, 'album', '') or '',
                'album_asin': getattr(track_info, 'album_id', '') or '',
                'duration_sec': getattr(track_info, 'duration', 0),
                'url': f"https://music.amazon.com/tracks/{asin}",
                'quality': quality_name,
                'source': 'track_resolve',
            }
    except Exception as e:
        log.debug(f"  Could not resolve track {asin}: {e}")
    return None


# ---------------------------------------------------------------------------
# Direct API search via OrpheusDL's azapi layer
# ---------------------------------------------------------------------------
def search_and_filter(api, query: str, seen_album_asins: set, seen_track_asins: set) -> list:
    """
    Hit Amazon's internal textsearch endpoint via OrpheusDL's API wrapper,
    then filter results for 360RA content.
    Returns list of result dicts.
    """
    results = []
    try:
        # OrpheusDL's Amazon module stores the api client as module.api or module.session
        # The internal search endpoint is /NA/api/textsearch/search/v1_1/
        params = {
            "keyword": query,
            "store": "music",
            "deviceFamily": "WebPlayer",
            "features": "hd,uhd,3d",   # request 3D quality tier info
            "pageSize": RESULTS_PER_QUERY,
            "resultStartIndex": 0,
        }

        # Use the azapi layer directly — same one Orpheus uses for downloads
        resp = api.post(
            "https://music.amazon.com/NA/api/textsearch/search/v1_1/",
            json=params,
        )
        resp.raise_for_status()
        data = resp.json()

        # Walk the response: albums come under data.albumList, tracks under data.trackList
        album_list  = data.get("albumList", [])
        track_list  = data.get("trackList", [])

        for album in album_list:
            a_asin = album.get("asin", "")
            if not a_asin or a_asin in seen_album_asins:
                continue

            # The isSonicRush flag strongly correlates with 360RA / spatial content.
            # Also check contentEncoding for ra360Available.
            encoding = album.get("contentEncoding", [])
            is_sonic_rush = album.get("isSonicRush", False)

            is_360 = is_sonic_rush or "ra360Available" in encoding

            if is_360:
                seen_album_asins.add(a_asin)
                log.info(f"  ✓ ALBUM  {album.get('title','?')!r:50s}  {a_asin}")
                results.append({
                    'type': 'album',
                    'asin': a_asin,
                    'title': album.get("title", ""),
                    'artist': (album.get("artist") or {}).get("name", ""),
                    'track_count': album.get("trackCount", 0),
                    'url': f"https://music.amazon.com/albums/{a_asin}",
                    'is_sonic_rush': is_sonic_rush,
                    'content_encoding': encoding,
                    'image': album.get("image", ""),
                    'source': query,
                })

                # Optionally expand to individual tracks
                if RESOLVE_TRACK_LISTS:
                    for track in album.get("tracks", []):
                        t_asin = track.get("asin", "")
                        if t_asin and t_asin not in seen_track_asins:
                            seen_track_asins.add(t_asin)
                            results.append({
                                'type': 'track',
                                'asin': t_asin,
                                'title': track.get("title", ""),
                                'artist': (track.get("artist") or {}).get("name", ""),
                                'album': album.get("title", ""),
                                'album_asin': a_asin,
                                'track_num': track.get("trackNum", ""),
                                'duration_sec': track.get("duration", 0),
                                'isrc': track.get("isrc", ""),
                                'url': f"https://music.amazon.com/tracks/{t_asin}",
                                'source': query,
                            })
                            log.info(f"      → track  {track.get('title','?')!r:46s}  {t_asin}")

        for track in track_list:
            t_asin = track.get("asin", "")
            if not t_asin or t_asin in seen_track_asins:
                continue
            is_sonic_rush = track.get("isSonicRush", False)
            encoding = track.get("contentEncoding", [])
            is_360 = is_sonic_rush or "ra360Available" in encoding
            if is_360:
                seen_track_asins.add(t_asin)
                log.info(f"  ✓ TRACK  {track.get('title','?')!r:50s}  {t_asin}")
                results.append({
                    'type': 'track',
                    'asin': t_asin,
                    'title': track.get("title", ""),
                    'artist': (track.get("artist") or {}).get("name", ""),
                    'album': (track.get("album") or {}).get("title", ""),
                    'album_asin': (track.get("album") or {}).get("asin", ""),
                    'track_num': track.get("trackNum", ""),
                    'duration_sec': track.get("duration", 0),
                    'isrc': track.get("isrc", ""),
                    'url': f"https://music.amazon.com/tracks/{t_asin}",
                    'source': query,
                })

    except Exception as e:
        log.warning(f"  Search failed for '{query}': {e}")

    return results


# ---------------------------------------------------------------------------
# Fallback: use OrpheusDL's higher-level search interface
# ---------------------------------------------------------------------------
def orpheus_search_fallback(module, query: str, seen_album_asins: set, seen_track_asins: set) -> list:
    """
    Use OrpheusDL's own search() method if the direct API call isn't available.
    This hits the same endpoint but through the module's abstraction layer.
    """
    results = []
    try:
        search_results = module.search(query, ["albums", "tracks"], 30)
        for item in search_results:
            item_id = getattr(item, 'result_id', None) or getattr(item, 'id', None)
            item_type = getattr(item, 'type', 'unknown')
            item_name = getattr(item, 'name', '') or getattr(item, 'title', '')
            item_artist = getattr(item, 'artists', [''])[0] if hasattr(item, 'artists') else ''
            additional = getattr(item, 'additional', []) or []

            # Check if any additional info fields indicate spatial/360RA
            is_360 = any(
                is_360ra_quality_name(str(x)) or '3d' in str(x).lower() or 'mha1' in str(x).lower()
                for x in additional
            )

            # Also flag anything marked as ATMOS quality tier in orpheus
            quality = getattr(item, 'quality', '') or ''
            if is_360ra_quality_name(quality):
                is_360 = True

            if is_360 and item_id:
                if item_type == 'album' and item_id not in seen_album_asins:
                    seen_album_asins.add(item_id)
                    log.info(f"  ✓ ALBUM  {item_name!r:50s}  {item_id}")
                    results.append({
                        'type': 'album',
                        'asin': item_id,
                        'title': item_name,
                        'artist': item_artist,
                        'url': f"https://music.amazon.com/albums/{item_id}",
                        'source': f"fallback:{query}",
                    })
                elif item_type == 'track' and item_id not in seen_track_asins:
                    seen_track_asins.add(item_id)
                    log.info(f"  ✓ TRACK  {item_name!r:50s}  {item_id}")
                    results.append({
                        'type': 'track',
                        'asin': item_id,
                        'title': item_name,
                        'artist': item_artist,
                        'url': f"https://music.amazon.com/tracks/{item_id}",
                        'source': f"fallback:{query}",
                    })
    except Exception as e:
        log.warning(f"  Fallback search also failed for '{query}': {e}")
    return results


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------
def save_results(all_results: list):
    # JSON — full detail
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    log.info(f"Saved {len(all_results)} entries to {OUTPUT_JSON}")

    # CSV — flat, good for spreadsheets / Finder quick look
    csv_fields = ['type', 'asin', 'title', 'artist', 'album', 'album_asin',
                  'track_num', 'duration_sec', 'isrc', 'url', 'source']
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_results)
    log.info(f"Saved {len(all_results)} entries to {OUTPUT_CSV}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("  360RA Finder — powered by OrpheusDL")
    log.info("=" * 60)

    # Load Orpheus
    Orpheus, AmazonMusicInterface = load_orpheus_amazon()

    # Boot Orpheus (reads ~/.config/orpheusdl/ credentials automatically)
    log.info("Initialising OrpheusDL and Amazon Music module...")
    try:
        orpheus = Orpheus()
        # Instantiate just the Amazon module — we don't need the full download pipeline
        module = orpheus.load_module("amazonmusic")
    except Exception as e:
        log.error(f"Failed to load OrpheusDL: {e}")
        log.error("Check that your OrpheusDL credentials are set up in settings/amazonmusic.json")
        sys.exit(1)

    log.info("Amazon Music module ready. Starting search sweep...\n")

    all_results    = []
    seen_album_asins = set()
    seen_track_asins = set()

    # Try to get the raw API client (azapi) for direct endpoint access
    api = getattr(module, 'api', None) or getattr(module, 'session', None)
    use_direct_api = api is not None and hasattr(api, 'post')

    if use_direct_api:
        log.info("Using direct azapi layer (same endpoint OrpheusDL uses internally)")
    else:
        log.info("Direct API not accessible — using OrpheusDL search() fallback")

    for i, query in enumerate(SEARCH_QUERIES, 1):
        log.info(f"[{i:2d}/{len(SEARCH_QUERIES)}] Searching: {query!r}")

        if use_direct_api:
            results = search_and_filter(api, query, seen_album_asins, seen_track_asins)
        else:
            results = orpheus_search_fallback(module, query, seen_album_asins, seen_track_asins)

        all_results.extend(results)

        if results:
            log.info(f"         Found {len(results)} new 360RA items this query")
        else:
            log.info(f"         No new 360RA items")

        time.sleep(REQUEST_DELAY)

    log.info("")
    log.info("=" * 60)
    albums = [r for r in all_results if r.get('type') == 'album']
    tracks = [r for r in all_results if r.get('type') == 'track']
    log.info(f"  Done! Found {len(albums)} 360RA albums, {len(tracks)} tracks")
    log.info("=" * 60)

    if not all_results:
        log.warning("No results found. Possible causes:")
        log.warning("  1. The isSonicRush / contentEncoding fields aren't returned by search")
        log.warning("     — try running orpheus with --debug on a known 360RA album and")
        log.warning("       check the search response to see what field flags 360RA.")
        log.warning("  2. Auth issue — make sure orpheus works for a normal download first.")
        return

    save_results(all_results)

    # Print a quick summary table
    print("\n" + "─" * 80)
    print(f"{'TYPE':<7}  {'ARTIST':<30}  {'TITLE':<35}  ASIN")
    print("─" * 80)
    for r in sorted(all_results, key=lambda x: (x.get('artist',''), x.get('title',''))):
        rtype  = r.get('type','?')[:6]
        artist = (r.get('artist','') or '')[:30]
        title  = (r.get('title','') or '')[:35]
        asin   = r.get('asin','')
        print(f"{rtype:<7}  {artist:<30}  {title:<35}  {asin}")
    print("─" * 80)
    print(f"\nFull results saved to: {OUTPUT_JSON}  and  {OUTPUT_CSV}")
    print("You can drop any ASIN straight into orpheus:")
    print("  orpheus 'https://music.amazon.com/albums/B0...'")


if __name__ == "__main__":
    main()
