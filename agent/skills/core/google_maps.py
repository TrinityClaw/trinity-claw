import time
import logging
import requests

logger = logging.getLogger(__name__)

NAME = "google_maps"
SHORT_DOC = "Geocoding, place search, directions, and nearby search via OpenStreetMap — no API key needed."
DOC = (
    "Maps & location — geocoding, place search, directions, nearby search. "
    "100% free, no API key needed (uses OpenStreetMap/Nominatim + OSRM). "
    "Functions: map_url(address) — returns clickable OpenStreetMap link for any address; "
    "geocode(address), reverse_geocode(lat, lng), "
    "search_places(query, location='', radius='5000'), "
    "nearby_search(location, radius='1000', place_type='restaurant'), "
    "get_directions(origin, destination, mode='driving'), "
    "distance_matrix(origins, destinations, mode='driving'), "
    "status(). "

    "=== FUNCTION SELECTION GUIDE — use the right one: === "
    "User says 'how do I get to X' / 'put do X' / 'directions to X' → get_directions(origin, destination). "
    "  If origin is unknown, ask ONE question: 'Where are you starting from?' then call get_directions. "
    "User says 'where is X' / 'show me X on a map' / 'find X' → map_url(X). "
    "User says 'what is near me' / 'find a restaurant/cafe/etc nearby' → nearby_search(location, place_type=...). "
    "nearby_search is ONLY for finding a CATEGORY of place (restaurant, cafe, etc.) near a spot — "
    "NEVER use it to find a specific named destination or get route directions. "
    "After get_directions, always also call map_url(destination) to give a clickable map link."
)

_NOMINATIM = "https://nominatim.openstreetmap.org"
_OSRM      = "https://router.project-osrm.org"
_OVERPASS  = "https://overpass-api.de/api/interpreter"
_HEADERS   = {"User-Agent": "TrinityClawAgent/1.0 (personal assistant)"}

# OSM amenity/tag mapping for nearby_search — stored as (key, value) tuples
_OSM_TAGS = {
    "restaurant":  ("amenity", "restaurant"),
    "cafe":        ("amenity", "cafe"),
    "bar":         ("amenity", "bar"),
    "hospital":    ("amenity", "hospital"),
    "pharmacy":    ("amenity", "pharmacy"),
    "school":      ("amenity", "school"),
    "university":  ("amenity", "university"),
    "gas_station": ("amenity", "fuel"),
    "fuel":        ("amenity", "fuel"),
    "hotel":       ("tourism", "hotel"),
    "park":        ("leisure", "park"),
    "supermarket": ("shop",    "supermarket"),
    "bank":        ("amenity", "bank"),
    "atm":         ("amenity", "atm"),
    "gym":         ("leisure", "fitness_centre"),
    "parking":     ("amenity", "parking"),
    "bus_stop":    ("highway", "bus_stop"),
    "library":     ("amenity", "library"),
    "museum":      ("tourism", "museum"),
}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get(url: str, params: dict) -> dict:
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _safe_int(value: str, default: int) -> int:
    """Parse int from string, return default on failure."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _geocode_latlng(location: str) -> tuple[float, float]:
    """Resolve 'lat,lng' string or address to (lat, lng) floats."""
    parts = location.split(",")
    if len(parts) == 2:
        try:
            return float(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            pass
    # It's an address — geocode it
    results = _get(f"{_NOMINATIM}/search", {
        "q": location, "format": "json", "limit": 1
    })
    time.sleep(1)  # Nominatim rate limit: 1 req/sec
    if not results:
        raise ValueError(f"Could not geocode location: {location}")
    return float(results[0]["lat"]), float(results[0]["lon"])


# ── Public skill functions ─────────────────────────────────────────────────────

def status() -> str:
    """Check connectivity to Nominatim and OSRM."""
    try:
        r = requests.get(f"{_NOMINATIM}/status", params={"format": "json"},
                         headers=_HEADERS, timeout=5)
        r.raise_for_status()
        return "✅ google_maps skill ready (Nominatim + OSRM — no API key needed)"
    except Exception as e:
        return f"❌ Nominatim unreachable: {e}"


def map_url(address: str) -> str:
    """Return an OpenStreetMap link for any address or place name.
    Tries to geocode first for a precise pin; falls back to OSM search URL if not found.
    Use this whenever the user asks to 'find a map', 'show on map', or 'where is X'."""
    try:
        from urllib.parse import quote
        results = _get(f"{_NOMINATIM}/search", {
            "q": address, "format": "json", "limit": 1
        })
        if results:
            lat = results[0]["lat"]
            lng = results[0]["lon"]
            display = results[0]["display_name"]
            osm_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}&zoom=17"
            gmaps_url = f"https://www.google.com/maps/search/?api=1&query={quote(address)}"
            return (
                f"✅ Map for: {display}\n"
                f"   OpenStreetMap: {osm_url}\n"
                f"   Google Maps:   {gmaps_url}\n"
                f"   Coordinates:   {lat}, {lng}"
            )
        else:
            osm_url = f"https://www.openstreetmap.org/search?query={quote(address)}"
            gmaps_url = f"https://www.google.com/maps/search/?api=1&query={quote(address)}"
            return (
                f"⚠️ Exact address not found in OpenStreetMap — search links:\n"
                f"   OpenStreetMap: {osm_url}\n"
                f"   Google Maps:   {gmaps_url}"
            )
    except Exception as e:
        return f"❌ map_url error: {e}"


def geocode(address: str) -> str:
    """Convert a human-readable address to latitude/longitude coordinates."""
    try:
        results = _get(f"{_NOMINATIM}/search", {
            "q": address, "format": "json", "addressdetails": 1, "limit": 3
        })
        if not results:
            return f"❌ No results found for: {address}"
        lines = [f"✅ Geocode results for '{address}':"]
        for r in results:
            lines.append(
                f"  • {r['display_name']}\n"
                f"    Lat: {r['lat']}, Lng: {r['lon']}  (OSM id: {r['osm_id']})"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Geocode error: {e}"


def reverse_geocode(lat: str, lng: str) -> str:
    """Convert latitude/longitude to a human-readable address."""
    try:
        r = _get(f"{_NOMINATIM}/reverse", {
            "lat": lat, "lon": lng, "format": "json", "addressdetails": 1
        })
        if "error" in r:
            return f"❌ {r['error']}"
        addr = r.get("display_name", "Unknown")
        details = r.get("address", {})
        lines = [
            f"✅ Address for ({lat}, {lng}):",
            f"   {addr}",
        ]
        if city := details.get("city") or details.get("town") or details.get("village"):
            lines.append(f"   City: {city}")
        if country := details.get("country"):
            lines.append(f"   Country: {country}")
        if postcode := details.get("postcode"):
            lines.append(f"   Postcode: {postcode}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Reverse geocode error: {e}"


def search_places(query: str, location: str = "", radius: str = "5000") -> str:
    """Search for places by name or type. Optionally bias results near a location (address or 'lat,lng').
    Examples: search_places('pizza'), search_places('coffee shop', 'London'), search_places('museum', '51.5,-0.1', '2000')"""
    try:
        params = {
            "q": query,
            "format": "json",
            "addressdetails": 1,
            "limit": 10,
        }
        if location:
            lat, lng = _geocode_latlng(location)
            r = _safe_int(radius, 5000) / 111_000  # metres → degrees (rough)
            params["viewbox"] = f"{lng-r},{lat+r},{lng+r},{lat-r}"
            params["bounded"] = 1
        results = _get(f"{_NOMINATIM}/search", params)
        if not results:
            return f"❌ No places found for: {query}"
        lines = [f"✅ Found {len(results)} result(s) for '{query}':"]
        for p in results:
            lines.append(f"  • {p['display_name']}")
            lines.append(f"    Lat: {p['lat']}, Lng: {p['lon']}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Search places error: {e}"


def nearby_search(location: str, radius: str = "1000", place_type: str = "restaurant") -> str:
    """Find nearby places of a given type around a location.
    location: address or 'lat,lng'. radius: metres (default 1000).
    place_type options: restaurant, cafe, bar, hospital, pharmacy, school, hotel, park,
    supermarket, bank, atm, gym, parking, bus_stop, library, museum, gas_station."""
    try:
        lat, lng = _geocode_latlng(location)
        tag_key, tag_val = _OSM_TAGS.get(place_type.lower(), ("amenity", place_type))
        radius_m = _safe_int(radius, 1000)
        query = f"""
[out:json][timeout:15];
(
  node["{tag_key}"="{tag_val}"](around:{radius_m},{lat},{lng});
  way["{tag_key}"="{tag_val}"](around:{radius_m},{lat},{lng});
);
out center 10;
"""
        resp = requests.post(_OVERPASS, data={"data": query},
                             headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
        if not elements:
            return f"❌ No {place_type}s found near '{location}' within {radius_m}m."
        lines = [f"✅ {len(elements)} {place_type}(s) near '{location}' (radius {radius_m}m):"]
        for el in elements[:10]:
            tags = el.get("tags", {})
            name = tags.get("name", "Unnamed")
            addr = ", ".join(filter(None, [
                tags.get("addr:housenumber", ""),
                tags.get("addr:street", ""),
                tags.get("addr:city", ""),
            ])) or "No address"
            phone = tags.get("phone") or tags.get("contact:phone", "")
            website = tags.get("website") or tags.get("contact:website", "")
            center = el.get("center") or {"lat": el.get("lat"), "lon": el.get("lon")}
            line = f"  • {name} — {addr}"
            if phone:
                line += f" | ☎ {phone}"
            if website:
                line += f"\n    🌐 {website}"
            if center.get("lat"):
                line += f"\n    📍 {center['lat']}, {center['lon']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Nearby search error: {e}"


def get_directions(origin: str, destination: str, mode: str = "driving") -> str:
    """Get turn-by-turn directions between two locations.
    mode options: driving, walking, cycling."""
    try:
        mode_map = {"bicycling": "cycling", "bike": "cycling", "foot": "walking"}
        osrm_mode = mode_map.get(mode.lower(), mode.lower())
        if osrm_mode not in ("driving", "walking", "cycling"):
            osrm_mode = "driving"

        olat, olng = _geocode_latlng(origin)
        time.sleep(1)
        dlat, dlng = _geocode_latlng(destination)

        url = f"{_OSRM}/route/v1/{osrm_mode}/{olng},{olat};{dlng},{dlat}"
        resp = requests.get(url, params={"steps": "true", "overview": "false"},
                            headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            return f"❌ No route found from '{origin}' to '{destination}'."

        route = data["routes"][0]
        dist_km = round(route["distance"] / 1000, 1)
        dur_min = round(route["duration"] / 60)
        hours, mins = divmod(dur_min, 60)
        dur_str = f"{hours}h {mins}m" if hours else f"{mins}m"

        lines = [
            f"✅ {osrm_mode.title()} directions: {origin} → {destination}",
            f"   Distance: {dist_km} km | Duration: {dur_str}",
            "   Steps:",
        ]
        steps = route["legs"][0].get("steps", [])
        for i, step in enumerate(steps[:12], 1):
            maneuver = step.get("maneuver", {})
            instruction = maneuver.get("type", "").replace("-", " ").title()
            modifier = maneuver.get("modifier", "")
            if modifier:
                instruction += f" {modifier}"
            name = step.get("name", "")
            d = round(step.get("distance", 0))
            lines.append(f"   {i}. {instruction}" + (f" onto {name}" if name else "") + f" ({d}m)")
        if len(steps) > 12:
            lines.append(f"   ... and {len(steps) - 12} more steps.")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Directions error: {e}"


def distance_matrix(origins: str, destinations: str, mode: str = "driving") -> str:
    """Get travel distance and duration between locations.
    Separate multiple origins/destinations with | (pipe).
    mode: driving, walking, cycling.
    Example: distance_matrix('New York', 'Boston|Philadelphia')"""
    try:
        mode_map = {"bicycling": "cycling", "bike": "cycling"}
        osrm_mode = mode_map.get(mode.lower(), mode.lower())
        if osrm_mode not in ("driving", "walking", "cycling"):
            osrm_mode = "driving"

        origin_list = [o.strip() for o in origins.split("|")]
        dest_list = [d.strip() for d in destinations.split("|")]
        all_locations = origin_list + dest_list

        coords = []
        for loc in all_locations:
            lat, lng = _geocode_latlng(loc)
            coords.append((lat, lng))
            time.sleep(1)

        coord_str = ";".join(f"{lng},{lat}" for lat, lng in coords)
        sources = ";".join(str(i) for i in range(len(origin_list)))
        dests   = ";".join(str(i) for i in range(len(origin_list), len(all_locations)))

        url = f"{_OSRM}/table/v1/{osrm_mode}/{coord_str}"
        resp = requests.get(url, params={"sources": sources, "destinations": dests},
                            headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != "Ok":
            return f"❌ Distance matrix failed: {data.get('message', 'unknown error')}"

        durations = data.get("durations", [])
        distances = data.get("distances", [])

        lines = [f"✅ Distance matrix ({osrm_mode}):"]
        for i, origin_label in enumerate(origin_list):
            for j, dest_label in enumerate(dest_list):
                dur_sec = durations[i][j] if durations else None
                dist_m  = distances[i][j] if distances else None
                if dur_sec is not None:
                    dur_min = round(dur_sec / 60)
                    hours, mins = divmod(dur_min, 60)
                    dur_str = f"{hours}h {mins}m" if hours else f"{mins}m"
                    dist_km = f"{round(dist_m / 1000, 1)} km" if dist_m is not None else "N/A"
                    lines.append(f"  {origin_label} → {dest_label}: {dist_km} / {dur_str}")
                else:
                    lines.append(f"  {origin_label} → {dest_label}: no route found")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Distance matrix error: {e}"
