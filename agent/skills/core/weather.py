import requests

NAME = "weather"
SHORT_DOC = "Real-time weather, forecast (up to 16 days), and air quality data — no API key required."
DOC = (
    "Fetches real weather data (no API key required). "
    "Functions: get_current_weather(city), get_weather(city), get_forecast(city, days=3, up to 16 days), get_air_quality(city)."
)

_BASE = "https://wttr.in"
_HEADERS = {"User-Agent": "TrinityClaw/1.0"}

# WMO Weather interpretation codes -> human-readable descriptions
_WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + slight hail", 99: "Thunderstorm + heavy hail",
}


def _fetch_wttr(city: str) -> dict:
    url = f"{_BASE}/{requests.utils.quote(city)}?format=j1"
    resp = requests.get(url, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get_coords(city: str):
    """Returns (lat, lon, area_name, country, wttr_data) from wttr.in."""
    data = _fetch_wttr(city)
    area = data.get("nearest_area", [{}])[0]
    lat = area.get("latitude", "")
    lon = area.get("longitude", "")
    area_name = area.get("areaName", [{}])[0].get("value", city)
    country = area.get("country", [{}])[0].get("value", "")
    return lat, lon, area_name, country, data


def get_weather(city: str) -> str:
    """Get current weather conditions for a city."""
    try:
        lat, lon, area_name, country, data = _get_coords(city)
        cur = data["current_condition"][0]

        temp_c = cur["temp_C"]
        temp_f = cur["temp_F"]
        feels_c = cur["FeelsLikeC"]
        desc = cur["weatherDesc"][0]["value"]
        humidity = cur["humidity"]
        wind_kmph = cur["windspeedKmph"]
        wind_dir = cur["winddir16Point"]
        visibility = cur["visibility"]
        uv = cur.get("uvIndex", "N/A")
        pressure = cur.get("pressure", "N/A")

        return (
            f"Weather in {area_name}, {country}\n"
            f"Condition: {desc}\n"
            f"Temperature: {temp_c}°C / {temp_f}°F (feels like {feels_c}°C)\n"
            f"Humidity: {humidity}% | UV Index: {uv} | Pressure: {pressure} hPa\n"
            f"Wind: {wind_kmph} km/h {wind_dir}\n"
            f"Visibility: {visibility} km"
        )
    except Exception as e:
        return f"Error fetching weather for '{city}': {e}"


def get_forecast(city: str, days: int = 3) -> str:
    """Get multi-day weather forecast for a city (1-16 days). Uses wttr.in for <=3 days, Open-Meteo for longer."""
    try:
        days = max(1, min(int(days), 16))
        lat, lon, area_name, country, wttr_data = _get_coords(city)

        if not lat or not lon:
            return f"Could not resolve coordinates for '{city}'."

        if days <= 3:
            weather_days = wttr_data.get("weather", [])[:days]
            lines = [f"Forecast for {area_name}, {country} — next {len(weather_days)} day(s):\n"]
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

        # 4-16 days: use Open-Meteo (free, no key, up to 16 days)
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
            f"precipitation_sum,weathercode,windspeed_10m_max,sunrise,sunset"
            f"&timezone=auto&forecast_days={days}"
        )
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        daily = resp.json().get("daily", {})

        dates = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        mean_temps = daily.get("temperature_2m_mean", [])
        precip = daily.get("precipitation_sum", [])
        codes = daily.get("weathercode", [])
        wind_max = daily.get("windspeed_10m_max", [])
        sunrises = daily.get("sunrise", [])
        sunsets = daily.get("sunset", [])

        lines = [f"Forecast for {area_name}, {country} — next {len(dates)} day(s):\n"]
        for i, date in enumerate(dates):
            code = int(codes[i]) if i < len(codes) and codes[i] is not None else 0
            desc = _WMO_CODES.get(code, f"Code {code}")
            hi = f"{max_temps[i]}°C" if i < len(max_temps) and max_temps[i] is not None else "N/A"
            lo = f"{min_temps[i]}°C" if i < len(min_temps) and min_temps[i] is not None else "N/A"
            avg = f"{mean_temps[i]}°C" if i < len(mean_temps) and mean_temps[i] is not None else "N/A"
            rain = f"{precip[i]}mm" if i < len(precip) and precip[i] is not None else "N/A"
            wind = f"{wind_max[i]} km/h" if i < len(wind_max) and wind_max[i] is not None else "N/A"
            rise = sunrises[i].split("T")[1][:5] if i < len(sunrises) and sunrises[i] else "N/A"
            sset = sunsets[i].split("T")[1][:5] if i < len(sunsets) and sunsets[i] else "N/A"
            lines.append(
                f"{date}: {desc} | High {hi} / Low {lo} / Avg {avg}"
                f" | Rain {rain} | Wind {wind} | Sunrise {rise} Sunset {sset}"
            )
        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching forecast for '{city}': {e}"


def get_air_quality(city: str) -> str:
    """Get air quality index for a city using Open-Meteo (free, no key)."""
    try:
        lat, lon, area_name, country, _ = _get_coords(city)

        if not lat or not lon:
            return f"Could not resolve coordinates for '{city}'."

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
            f"Air Quality in {area_name}, {country}\n"
            f"EU AQI: {eu_aqi} | US AQI: {us_aqi}\n"
            f"PM2.5: {pm25} µg/m³ | PM10: {pm10} µg/m³\n"
            f"NO2: {no2} µg/m³ | Ozone: {ozone} µg/m³"
        )
    except Exception as e:
        return f"Error fetching air quality for '{city}': {e}"


# Alias so the LLM can call either name
get_current_weather = get_weather

__all__ = ["NAME", "DOC", "get_weather", "get_current_weather", "get_forecast", "get_air_quality"]
