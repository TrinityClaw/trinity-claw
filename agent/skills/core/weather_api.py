import requests

NAME = "weather_api"
DOC = (
    "Fetches real weather data using wttr.in (no API key required). "
    "Functions: get_weather(city), get_forecast(city, days=3), get_air_quality(city)."
)

_BASE = "https://wttr.in"
_HEADERS = {"User-Agent": "TrinityClaw/1.0"}


def _fetch(city: str) -> dict:
    url = f"{_BASE}/{requests.utils.quote(city)}?format=j1"
    resp = requests.get(url, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_weather(city: str) -> str:
    """Get current weather conditions for a city."""
    try:
        data = _fetch(city)
        cur = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        area_name = area.get("areaName", [{}])[0].get("value", city)
        country = area.get("country", [{}])[0].get("value", "")

        temp_c = cur["temp_C"]
        temp_f = cur["temp_F"]
        feels_c = cur["FeelsLikeC"]
        desc = cur["weatherDesc"][0]["value"]
        humidity = cur["humidity"]
        wind_kmph = cur["windspeedKmph"]
        wind_dir = cur["winddir16Point"]
        visibility = cur["visibility"]

        return (
            f"Weather in {area_name}, {country}\n"
            f"Condition: {desc}\n"
            f"Temperature: {temp_c}°C / {temp_f}°F (feels like {feels_c}°C)\n"
            f"Humidity: {humidity}%\n"
            f"Wind: {wind_kmph} km/h {wind_dir}\n"
            f"Visibility: {visibility} km"
        )
    except Exception as e:
        return f"Error fetching weather for '{city}': {e}"


def get_forecast(city: str, days: int = 3) -> str:
    """Get multi-day weather forecast for a city (max 3 days from wttr.in)."""
    try:
        data = _fetch(city)
        weather_days = data.get("weather", [])[:min(days, 3)]

        lines = [f"Forecast for {city} — next {len(weather_days)} day(s):\n"]
        for day in weather_days:
            date = day["date"]
            max_c = day["maxtempC"]
            min_c = day["mintempC"]
            avg_c = day["avgtempC"]
            desc = day["hourly"][4]["weatherDesc"][0]["value"] if day.get("hourly") else "N/A"
            sunrise = day.get("astronomy", [{}])[0].get("sunrise", "N/A")
            sunset = day.get("astronomy", [{}])[0].get("sunset", "N/A")
            lines.append(
                f"{date}: {desc} | High {max_c}°C / Low {min_c}°C / Avg {avg_c}°C"
                f" | Sunrise {sunrise} Sunset {sunset}"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching forecast for '{city}': {e}"


def get_air_quality(city: str) -> str:
    """
    Get air quality index for a city using Open-Meteo (free, no key).
    First resolves city coordinates via wttr.in, then queries Open-Meteo AQ API.
    """
    try:
        # Step 1: get lat/lon from wttr.in
        data = _fetch(city)
        area = data.get("nearest_area", [{}])[0]
        lat = area.get("latitude", "")
        lon = area.get("longitude", "")
        area_name = area.get("areaName", [{}])[0].get("value", city)

        if not lat or not lon:
            return f"Could not resolve coordinates for '{city}'."

        # Step 2: query Open-Meteo air quality
        aq_url = (
            f"https://air-quality-api.open-meteo.com/v1/air-quality"
            f"?latitude={lat}&longitude={lon}"
            f"&current=european_aqi,us_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,ozone"
        )
        resp = requests.get(aq_url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        aq = resp.json().get("current", {})

        eu_aqi = aq.get("european_aqi", "N/A")
        us_aqi = aq.get("us_aqi", "N/A")
        pm25 = aq.get("pm2_5", "N/A")
        pm10 = aq.get("pm10", "N/A")
        no2 = aq.get("nitrogen_dioxide", "N/A")
        ozone = aq.get("ozone", "N/A")

        return (
            f"Air Quality in {area_name}\n"
            f"EU AQI: {eu_aqi} | US AQI: {us_aqi}\n"
            f"PM2.5: {pm25} µg/m³ | PM10: {pm10} µg/m³\n"
            f"NO₂: {no2} µg/m³ | Ozone: {ozone} µg/m³"
        )
    except Exception as e:
        return f"Error fetching air quality for '{city}': {e}"


__all__ = ["NAME", "DOC", "get_weather", "get_forecast", "get_air_quality"]
