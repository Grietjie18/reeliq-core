import os, uuid, json
import numpy as np
from scipy.interpolate import RBFInterpolator
from shapely.geometry import Point, Polygon
from datetime import datetime, timedelta, timezone, date
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- DATABASE CONFIG ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
database = Database(DATABASE_URL)
metadata = MetaData()

observations_table = Table(
    "observations", metadata,
    Column("observation_id", String, primary_key=True),
    Column("vessel_id", String, index=True),
    Column("timestamp", DateTime),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("sea_surface_temperature", Float),
    Column("speed_over_ground", Float),
    Column("speed_through_water", Float)
)

# --- AUTH ---
VESSEL_CREDENTIALS = {
    "RV_Algoa_0":  ("skipper01", "reef2026"),
    "RV_Algoa_1":  ("skipper02", "reef2026"),
    "RV_Algoa_2":  ("skipper03", "reef2026"),
    "demo":        ("demo",      "demo"),
}
VALID_API_KEYS = {"2026_Reeliq_dev18"}

# --- ALGOA BAY OCEAN POLYGON ---
ALGOA_BAY_POLYGON = Polygon([
    (24.869, -34.300),
    (24.869, -34.195),
    (24.841, -34.145),
    (24.912, -34.085),
    (24.921, -34.079),
    (24.925, -34.052),
    (24.933, -34.032),
    (24.931, -34.011),
    (24.937, -34.005),
    (25.034, -33.970),
    (25.213, -33.969),
    (25.402, -34.034),
    (25.584, -34.048),
    (25.700, -34.029),
    (25.644, -33.955),
    (25.632, -33.865),
    (25.694, -33.815),
    (25.830, -33.727),
    (26.080, -33.707),
    (26.298, -33.763),
    (26.352, -33.760),
    (26.352, -34.300),
    (24.869, -34.300),
])

def is_ocean(lon, lat):
    return ALGOA_BAY_POLYGON.contains(Point(lon, lat))

# --- PYDANTIC MODEL ---
class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latitude: float
    longitude: float
    sea_surface_temperature: float
    speed_over_ground: float
    speed_through_water: float

# --- SATELLITE SST CACHE ---
_copernicus_cache = {"date": None, "points": []}

async def get_satellite_sst():
    today = date.today().isoformat()
    if _copernicus_cache["date"] == today and _copernicus_cache["points"]:
        return _copernicus_cache["points"]
    try:
        with open("satellite_sst.json", "r") as f:
            data = json.load(f)
        points = [(p[0], p[1], p[2]) for p in data["points"]]
        _copernicus_cache["date"] = today
        _copernicus_cache["points"] = points
        print(f"✅ Satellite SST loaded: {len(points)} points from {data['date']}")
        return points
    except Exception as e:
        print(f"⚠️ Could not load satellite_sst.json: {e}")
        return []

@app.on_event("startup")
async def startup():
    await database.connect()
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, data: Observation, api_key: str = Query(...)):
    if api_key not in VALID_API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    query = observations_table.insert().values(
        observation_id=str(data.observation_id),
        vessel_id=vessel_id,
        timestamp=data.timestamp.replace(tzinfo=None),
        latitude=data.latitude,
        longitude=data.longitude,
        sea_surface_temperature=data.sea_surface_temperature,
        speed_over_ground=data.speed_over_ground,
        speed_through_water=data.speed_through_water
    )
    await database.execute(query)
    return {"status": "success"}

@app.post("/api/login")
async def login(username: str = Query(...), password: str = Query(...)):
    for vessel_id, (u, p) in VESSEL_CREDENTIALS.items():
        if u == username and p == password:
            return {"status": "ok", "vessel_id": vessel_id}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/api/interpolated")
async def get_interpolated():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=15)
    query = observations_table.select().where(
        observations_table.c.timestamp >= cutoff
    )
    rows = await database.fetch_all(query)

    satellite_points = await get_satellite_sst()

    all_lats = []
    all_lons = []
    all_temps = []

    for lat, lon, temp in satellite_points:
        all_lats.append(lat)
        all_lons.append(lon)
        all_temps.append(temp)

    for r in rows:
        for _ in range(3):
            all_lats.append(r['latitude'])
            all_lons.append(r['longitude'])
            all_temps.append(r['sea_surface_temperature'])

    if len(all_lats) < 3:
        return JSONResponse([])

    obs_coords = np.column_stack([all_lats, all_lons])
    obs_temps = np.array(all_temps)

    grid_lats = np.linspace(-34.300, -33.700, 60)
    grid_lons = np.linspace(24.840, 26.360, 60)

    MIN_TEMP, MAX_TEMP = 16.0, 24.0

    interpolator = RBFInterpolator(
    obs_coords, obs_temps,
    kernel='thin_plate_spline',
    smoothing=1.5
)

    result = []
    for lat in grid_lats:
        for lon in grid_lons:
            if not is_ocean(lon, lat):
                continue
            temp = float(interpolator([[lat, lon]])[0])
            intensity = max(0.0, min(1.0, (temp - MIN_TEMP) / (MAX_TEMP - MIN_TEMP)))
            result.append([round(lat, 4), round(lon, 4), round(intensity, 3)])

    return JSONResponse(result)

@app.get("/api/vessel/{vessel_id}")
async def get_vessel(vessel_id: str):
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=5)
    query = observations_table.select().where(
        observations_table.c.vessel_id == vessel_id,
        observations_table.c.timestamp >= cutoff
    ).order_by(observations_table.c.timestamp.desc()).limit(1)
    row = await database.fetch_one(query)
    if not row:
        raise HTTPException(status_code=404, detail="No recent data for vessel")

    sog = row['speed_over_ground']
    stw = row['speed_through_water']
    diff = stw - sog

    if diff > 0.5:
        efficiency = "poor"
        efficiency_label = "Fighting Current 🔴"
    elif diff < -0.3:
        efficiency = "good"
        efficiency_label = "Riding Current 🟢"
    else:
        efficiency = "neutral"
        efficiency_label = "Neutral Conditions 🟡"

    return {
        "vessel_id": vessel_id,
        "latitude": row['latitude'],
        "longitude": row['longitude'],
        "sea_surface_temperature": row['sea_surface_temperature'],
        "speed_over_ground": sog,
        "speed_through_water": stw,
        "efficiency": efficiency,
        "efficiency_label": efficiency_label,
        "timestamp": row['timestamp'].isoformat()
    }
    # --- FUEL PRICE CACHE ---
_fuel_cache = {"date": None, "petrol": None, "diesel": None}

async def get_fuel_prices():
    today = date.today().isoformat()
    if _fuel_cache["date"] == today and _fuel_cache["petrol"]:
        return _fuel_cache["petrol"], _fuel_cache["diesel"]
    
    # Fallback prices (updated manually as needed)
    fallback_petrol = 21.70
    fallback_diesel = 19.90

    try:
        import httpx
        # Try DMRE — scrape their published table
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://www.energy.gov.za/files/esources/petroleum/petroleum_prices.html"
            )
        text = r.text
        import re
        # Pull 95 ULP and 500ppm diesel figures
        petrol_match = re.search(r'95\s+ULP[^\d]+([\d]+\.[\d]+)', text)
        diesel_match = re.search(r'500\s*ppm[^\d]+([\d]+\.[\d]+)', text)
        petrol = float(petrol_match.group(1)) if petrol_match else None
        diesel = float(diesel_match.group(1)) if diesel_match else None

        if not petrol or not diesel:
            raise ValueError("DMRE parse failed")

        _fuel_cache.update({"date": today, "petrol": petrol, "diesel": diesel})
        return petrol, diesel

    except Exception as e:
        print(f"⚠️ DMRE fuel fetch failed: {e}, trying AA fallback")
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://www.aa.co.za/fuel-price-history/"
                )
            text = r.text
            petrol_match = re.search(r'95\s*ULP[^\d]+([\d]+\.[\d]+)', text)
            diesel_match = re.search(r'50\s*ppm[^\d]+([\d]+\.[\d]+)', text)
            petrol = float(petrol_match.group(1)) if petrol_match else fallback_petrol
            diesel = float(diesel_match.group(1)) if diesel_match else fallback_diesel
            _fuel_cache.update({"date": today, "petrol": petrol, "diesel": diesel})
            return petrol, diesel
        except Exception as e2:
            print(f"⚠️ AA fallback also failed: {e2}, using hardcoded defaults")
            return fallback_petrol, fallback_diesel

@app.get("/api/fuel-prices")
async def fuel_prices():
    petrol, diesel = await get_fuel_prices()
    return {"petrol": petrol, "diesel": diesel}

# --- ROUTE PLANNING ---
@app.get("/api/route")
async def plan_route(
    start_lat: float = Query(...),
    start_lon: float = Query(...),
    end_lat: float = Query(...),
    end_lon: float = Query(...),
    consumption_lhr: float = Query(...),
    fuel_type: str = Query(...)
):
    # Get current heatmap grid as current-condition proxy
    # We reuse the interpolation logic inline
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=15)
    query = observations_table.select().where(
        observations_table.c.timestamp >= cutoff
    )
    rows = await database.fetch_all(query)
    satellite_points = await get_satellite_sst()

    all_lats, all_lons, all_temps = [], [], []
    for lat, lon, temp in satellite_points:
        all_lats.append(lat); all_lons.append(lon); all_temps.append(temp)
    for r in rows:
        for _ in range(3):
            all_lats.append(r['latitude'])
            all_lons.append(r['longitude'])
            all_temps.append(r['sea_surface_temperature'])

    has_interp = len(all_lats) >= 3

    if has_interp:
        obs_coords = np.column_stack([all_lats, all_lons])
        obs_temps = np.array(all_temps)
        interpolator = RBFInterpolator(
            obs_coords, obs_temps,
            kernel='thin_plate_spline', smoothing=1.5
        )

    # Build a coarse graph of ocean waypoints between start and end
    # Sample points along a grid between the two coords
    n_steps = 8  # waypoints to consider
    lats = np.linspace(start_lat, end_lat, n_steps)
    lons = np.linspace(start_lon, end_lon, n_steps)

    # For each step, try shifting laterally to find most favourable current
    LATERAL_OFFSETS = [-0.04, -0.02, 0.0, 0.02, 0.04]  # degrees
    route = [[start_lat, start_lon]]
    
    for i in range(1, n_steps - 1):
        best_lat, best_lon = lats[i], lons[i]
        best_score = float('inf')

        # Perpendicular direction (rough lateral offset)
        dlat = end_lat - start_lat
        dlon = end_lon - start_lon
        length = max((dlat**2 + dlon**2)**0.5, 0.001)
        perp_lat = -dlon / length
        perp_lon = dlat / length

        for offset in LATERAL_OFFSETS:
            cand_lat = lats[i] + perp_lat * offset
            cand_lon = lons[i] + perp_lon * offset
            if not is_ocean(cand_lon, cand_lat):
                continue
            # Score: lower SST temp = upwelling = stronger currents = more resistance
            # Higher temp = warmer surface water = calmer inshore = less resistance
            # We want to favour zones with moderate-high temp (less upwelling stress)
            if has_interp:
                temp = float(interpolator([[cand_lat, cand_lon]])[0])
                # Penalise cold upwelling zones, reward moderate warm zones
                score = abs(temp - 20.5)  # 20.5°C as ideal
            else:
                score = 0.0
            if score < best_score:
                best_score = score
                best_lat, best_lon = cand_lat, cand_lon

        route.append([best_lat, best_lon])

    route.append([end_lat, end_lon])

    # Calculate distances
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        a = np.sin(dlat/2)**2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon/2)**2
        return R * 2 * np.arcsin(np.sqrt(a))

    route_dist_km = sum(
        haversine(route[i][0], route[i][1], route[i+1][0], route[i+1][1])
        for i in range(len(route)-1)
    )
    direct_dist_km = haversine(start_lat, start_lon, end_lat, end_lon)

    # Assume average speed 20 knots = 37 km/h
    avg_speed_kmh = 37.0
    route_hrs = route_dist_km / avg_speed_kmh
    direct_hrs = direct_dist_km / avg_speed_kmh

    # Current efficiency factor from live data
    # Average SOG/STW ratio across recent observations as proxy
    efficiency_factor = 1.0
    if rows:
        sog_vals = [r['speed_over_ground'] for r in rows if r['speed_over_ground'] > 0]
        stw_vals = [r['speed_through_water'] for r in rows if r['speed_through_water'] > 0]
        if sog_vals and stw_vals:
            avg_sog = np.mean(sog_vals)
            avg_stw = np.mean(stw_vals)
            # If STW > SOG on direct route, current fights you — optimised route avoids this
            efficiency_factor = min(1.3, max(0.85, avg_stw / avg_sog))

    # Fuel calculations
    petrol_price, diesel_price = await get_fuel_prices()
    price_per_litre = diesel_price if fuel_type.lower() == "diesel" else petrol_price

    # Direct route fuel (fighting current)
    direct_fuel_l = consumption_lhr * direct_hrs * efficiency_factor
    # Optimised route fuel (slightly longer but better current)
    optimised_fuel_l = consumption_lhr * route_hrs * (1.0 / efficiency_factor if efficiency_factor > 1 else efficiency_factor)
    optimised_fuel_l = min(optimised_fuel_l, direct_fuel_l)  # route should never cost more

    direct_cost = round(direct_fuel_l * price_per_litre, 2)
    optimised_cost = round(optimised_fuel_l * price_per_litre, 2)
    saving = round(direct_cost - optimised_cost, 2)
    saving_pct = round((saving / direct_cost * 100) if direct_cost > 0 else 0, 1)

    # Suggested departure: next 12 hrs, pick window where current is most favourable
    # We use a simple heuristic: early morning (05:00-07:00) typically has calmer 
    # inshore conditions in Algoa Bay (less sea breeze, less chop)
    now_local = datetime.now()
    suggestions = []
    for h in range(13):
        candidate = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=h)
        hour = candidate.hour
        # Score: 05-08 best (pre-seabreeze), 09-11 good, 12-16 worst (seabreeze), 17-19 ok
        if 5 <= hour <= 8:
            score = 0
        elif 17 <= hour <= 19:
            score = 1
        elif 9 <= hour <= 11:
            score = 2
        elif hour < 5 or hour > 19:
            score = 3
        else:
            score = 4
        suggestions.append((score, candidate.strftime("%H:%M"), candidate.strftime("%a %H:%M")))

    suggestions.sort(key=lambda x: x[0])
    best_departure = suggestions[0][2]
    best_departure_reason = (
        "Early morning conditions typically offer calmer seas and lighter wind in Algoa Bay."
        if suggestions[0][0] == 0
        else "Afternoon sea breeze eases after 17:00 — conditions improve for departure."
    )

    return {
        "route": route,
        "direct_dist_km": round(direct_dist_km, 2),
        "route_dist_km": round(route_dist_km, 2),
        "route_time_hrs": round(route_hrs, 2),
        "direct_fuel_l": round(direct_fuel_l, 1),
        "optimised_fuel_l": round(optimised_fuel_l, 1),
        "direct_cost_zar": direct_cost,
        "optimised_cost_zar": optimised_cost,
        "saving_zar": saving,
        "saving_pct": saving_pct,
        "fuel_price_used": price_per_litre,
        "fuel_type": fuel_type,
        "best_departure": best_departure,
        "best_departure_reason": best_departure_reason,
        "disclaimer": "Route suggestions are based on oceanographic conditions only. Always consult nautical charts, respect MPAs, and navigate responsibly. Reel IQ is not a navigation tool."
    }
@app.get("/", response_class=HTMLResponse)
async def get_landing():
    with open("landing.html", "r") as f:
        return HTMLResponse(content=f.read())
@app.get("/app", response_class=HTMLResponse)
async def get_map():
    html_content = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>REEL IQ</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        :root {
            --cyan: #00f2ff;
            --cyan-dim: rgba(0,242,255,0.15);
            --cyan-border: rgba(0,242,255,0.4);
            --bg: #04080f;
            --panel: rgba(4,12,22,0.92);
            --text: #cce8ff;
            --warn: #ffb800;
            --good: #00ff88;
            --bad: #ff3b3b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg);
            font-family: 'Share Tech Mono', monospace;
            color: var(--text);
            overflow: hidden;
            height: 100vh;
        }
        #login-screen {
            position: fixed; inset: 0; z-index: 9999;
            background: var(--bg);
            display: flex; align-items: center; justify-content: center;
            flex-direction: column;
        }
        .login-bg {
            position: absolute; inset: 0;
            background: radial-gradient(ellipse at 50% 60%, rgba(0,242,255,0.04) 0%, transparent 70%);
        }
        .login-box {
            position: relative; z-index: 2;
            width: min(420px, 92vw);
            padding: 48px 40px;
            border: 1px solid var(--cyan-border);
            border-radius: 4px;
            background: var(--panel);
            backdrop-filter: blur(20px);
        }
        .login-logo {
            font-family: 'Orbitron', sans-serif;
            font-size: 2rem;
            font-weight: 900;
            color: var(--cyan);
            letter-spacing: 0.15em;
            margin-bottom: 4px;
            text-shadow: 0 0 30px rgba(0,242,255,0.5);
        }
        .login-sub {
            font-size: 0.7rem;
            color: rgba(0,242,255,0.5);
            letter-spacing: 0.2em;
            margin-bottom: 40px;
            text-transform: uppercase;
        }
        .login-field { margin-bottom: 16px; }
        .login-field label {
            display: block;
            font-size: 0.65rem;
            letter-spacing: 0.2em;
            color: rgba(0,242,255,0.6);
            margin-bottom: 8px;
            text-transform: uppercase;
        }
        .login-field input {
            width: 100%;
            background: rgba(0,242,255,0.04);
            border: 1px solid var(--cyan-border);
            border-radius: 2px;
            padding: 12px 16px;
            color: var(--cyan);
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.95rem;
            outline: none;
            transition: border-color 0.2s, background 0.2s;
        }
        .login-field input:focus {
            border-color: var(--cyan);
            background: rgba(0,242,255,0.08);
        }
        .login-btn {
            width: 100%;
            margin-top: 24px;
            padding: 14px;
            background: transparent;
            border: 1px solid var(--cyan);
            border-radius: 2px;
            color: var(--cyan);
            font-family: 'Orbitron', sans-serif;
            font-size: 0.8rem;
            font-weight: 700;
            letter-spacing: 0.2em;
            cursor: pointer;
            text-transform: uppercase;
            transition: background 0.2s, box-shadow 0.2s;
        }
        .login-btn:hover {
            background: var(--cyan-dim);
            box-shadow: 0 0 20px rgba(0,242,255,0.2);
        }
        .login-error {
            margin-top: 12px;
            font-size: 0.7rem;
            color: var(--bad);
            text-align: center;
            letter-spacing: 0.1em;
            min-height: 16px;
        }
        .login-demo {
            margin-top: 20px;
            font-size: 0.65rem;
            color: rgba(255,255,255,0.2);
            text-align: center;
            letter-spacing: 0.1em;
        }
        #app-screen { display: none; height: 100vh; flex-direction: column; }
        #map { flex: 1; background: var(--bg); }
        #topbar {
            position: absolute; top: 0; left: 0; right: 0;
            z-index: 1000;
            display: flex; align-items: center; justify-content: space-between;
            padding: 12px 16px;
            background: linear-gradient(to bottom, rgba(4,8,15,0.95), transparent);
            pointer-events: none;
        }
        .topbar-logo {
            font-family: 'Orbitron', sans-serif;
            font-size: 1rem;
            font-weight: 900;
            color: var(--cyan);
            letter-spacing: 0.15em;
            text-shadow: 0 0 15px rgba(0,242,255,0.4);
        }
        .topbar-status {
            font-size: 0.65rem;
            color: rgba(0,242,255,0.5);
            letter-spacing: 0.12em;
            text-align: right;
        }
        #live-dot {
            display: inline-block;
            width: 6px; height: 6px;
            border-radius: 50%;
            background: var(--good);
            margin-right: 6px;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        #legend {
            position: absolute; top: 60px; right: 16px; z-index: 1000;
            background: var(--panel);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 4px;
            padding: 12px 10px;
        }
        .legend-bar {
            width: 12px; height: 160px;
            background: linear-gradient(to top,
                #0000ff 0%, #00ffff 25%, #00ff88 45%,
                #ffff00 65%, #ff8800 82%, #ff0000 100%);
            border-radius: 2px;
        }
        .legend-labels {
            display: flex; flex-direction: column;
            justify-content: space-between;
            height: 160px;
            font-size: 0.6rem;
            color: rgba(255,255,255,0.5);
            margin-left: 6px;
        }
        .legend-row { display: flex; align-items: flex-start; gap: 6px; }
        .legend-title {
            font-size: 0.55rem;
            color: rgba(255,255,255,0.3);
            letter-spacing: 0.1em;
            text-align: center;
            margin-bottom: 6px;
        }
        #sst-panel {
            position: absolute; top: 60px; left: 16px; z-index: 1000;
            background: var(--panel);
            border: 1px solid var(--cyan-border);
            border-radius: 4px;
            padding: 12px 16px;
            min-width: 180px;
        }
        .panel-label {
            font-size: 0.6rem;
            color: rgba(0,242,255,0.5);
            letter-spacing: 0.15em;
            text-transform: uppercase;
            margin-bottom: 4px;
        }
        .panel-value {
            font-size: 1.1rem;
            color: var(--cyan);
            font-weight: bold;
        }
        .panel-sub {
            font-size: 0.6rem;
            color: rgba(255,255,255,0.3);
            margin-top: 2px;
        }
        #efficiency-bar {
            position: absolute; bottom: 0; left: 0; right: 0; z-index: 1000;
            background: var(--panel);
            border-top: 1px solid rgba(255,255,255,0.08);
            padding: 12px 20px;
            display: flex; align-items: center; justify-content: space-between;
            gap: 16px;
        }
        .eff-block {
            display: flex; flex-direction: column; align-items: center; flex: 1;
        }
        .eff-label {
            font-size: 0.55rem;
            color: rgba(255,255,255,0.3);
            letter-spacing: 0.15em;
            text-transform: uppercase;
            margin-bottom: 2px;
        }
        .eff-value { font-size: 0.85rem; font-weight: bold; color: white; }
        .eff-divider { width: 1px; height: 32px; background: rgba(255,255,255,0.1); }
        #eff-status {
            font-size: 0.75rem;
            font-weight: bold;
            text-align: center;
            flex: 2;
            letter-spacing: 0.05em;
        }
        #eff-status.good { color: var(--good); }
        #eff-status.poor { color: var(--bad); }
        #eff-status.neutral { color: var(--warn); }
        #logout-btn {
            position: absolute; bottom: 68px; right: 16px; z-index: 1001;
            background: transparent;
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 2px;
            color: rgba(255,255,255,0.3);
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.6rem;
            letter-spacing: 0.1em;
            padding: 6px 10px;
            cursor: pointer;
        }
        #logout-btn:hover { color: var(--bad); border-color: var(--bad); }
        #route-btn {
    position: absolute; bottom: 68px; left: 16px; z-index: 1001;
    background: rgba(0,242,255,0.08);
    border: 1px solid var(--cyan-border);
    border-radius: 2px;
    color: var(--cyan);
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.65rem;
    letter-spacing: 0.1em;
    padding: 8px 14px;
    cursor: pointer;
    transition: background 0.2s;
}
#route-btn:hover { background: var(--cyan-dim); }
#route-btn.active { background: var(--cyan); color: var(--bg); }

#route-panel {
    display: none;
    position: absolute; bottom: 110px; left: 16px; z-index: 1002;
    background: var(--panel);
    border: 1px solid var(--cyan-border);
    border-radius: 4px;
    padding: 16px;
    width: 240px;
    backdrop-filter: blur(12px);
}
#route-panel .rp-title {
    font-size: 0.65rem; letter-spacing: 0.2em;
    color: var(--cyan); text-transform: uppercase;
    margin-bottom: 12px;
}
#route-panel label {
    font-size: 0.6rem; letter-spacing: 0.15em;
    color: rgba(0,242,255,0.5); text-transform: uppercase;
    display: block; margin-bottom: 4px; margin-top: 10px;
}
#route-panel input, #route-panel select {
    width: 100%; background: rgba(0,242,255,0.04);
    border: 1px solid var(--cyan-border); border-radius: 2px;
    padding: 8px 10px; color: var(--cyan);
    font-family: 'Share Tech Mono', monospace; font-size: 0.8rem;
    outline: none;
}
#route-panel select option { background: #04080f; }
.rp-step {
    font-size: 0.6rem; color: rgba(0,242,255,0.6);
    letter-spacing: 0.1em; margin-top: 10px;
    padding: 8px; border: 1px dashed rgba(0,242,255,0.2);
    border-radius: 2px; text-align: center;
}
.rp-step.done { border-color: var(--good); color: var(--good); }
.rp-btn {
    width: 100%; margin-top: 12px;
    padding: 10px; background: transparent;
    border: 1px solid var(--cyan); border-radius: 2px;
    color: var(--cyan); font-family: 'Share Tech Mono', monospace;
    font-size: 0.65rem; letter-spacing: 0.15em;
    cursor: pointer; text-transform: uppercase;
    transition: background 0.2s;
}
.rp-btn:hover { background: var(--cyan-dim); }
.rp-btn:disabled { opacity: 0.3; cursor: not-allowed; }
.rp-clear {
    width: 100%; margin-top: 6px;
    padding: 6px; background: transparent;
    border: 1px solid rgba(255,59,59,0.3); border-radius: 2px;
    color: var(--bad); font-family: 'Share Tech Mono', monospace;
    font-size: 0.6rem; letter-spacing: 0.1em;
    cursor: pointer; text-transform: uppercase;
}

#route-result {
    display: none;
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    z-index: 1010;
    background: var(--panel);
    border: 1px solid var(--cyan-border);
    border-radius: 6px;
    padding: 24px 28px;
    width: min(360px, 92vw);
    backdrop-filter: blur(20px);
}
#route-result .rr-title {
    font-family: 'Orbitron', sans-serif;
    font-size: 0.75rem; letter-spacing: 0.2em;
    color: var(--cyan); margin-bottom: 16px;
    text-transform: uppercase;
}
.rr-row {
    display: flex; justify-content: space-between;
    align-items: center; padding: 8px 0;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    font-size: 0.7rem;
}
.rr-row:last-of-type { border-bottom: none; }
.rr-key { color: rgba(0,242,255,0.5); letter-spacing: 0.1em; }
.rr-val { color: var(--text); font-weight: bold; }
.rr-val.saving { color: var(--good); }
.rr-val.departure { color: var(--warn); }
.rr-disclaimer {
    margin-top: 14px; padding: 10px;
    border: 1px solid rgba(255,184,0,0.2);
    border-radius: 2px;
    font-size: 0.55rem; color: rgba(255,184,0,0.6);
    line-height: 1.5; letter-spacing: 0.05em;
}
.rr-close {
    width: 100%; margin-top: 14px;
    padding: 10px; background: transparent;
    border: 1px solid rgba(255,255,255,0.15); border-radius: 2px;
    color: rgba(255,255,255,0.4);
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.65rem; letter-spacing: 0.1em;
    cursor: pointer; text-transform: uppercase;
}
.rr-close:hover { color: var(--bad); border-color: var(--bad); }

#disclaimer-modal {
    display: none;
    position: fixed; inset: 0; z-index: 2000;
    background: rgba(0,0,0,0.7);
    align-items: center; justify-content: center;
}
#disclaimer-modal.show { display: flex; }
.dm-box {
    background: var(--panel);
    border: 1px solid var(--warn);
    border-radius: 6px;
    padding: 32px;
    width: min(380px, 92vw);
    backdrop-filter: blur(20px);
}
.dm-title {
    font-family: 'Orbitron', sans-serif;
    color: var(--warn); font-size: 0.8rem;
    letter-spacing: 0.15em; margin-bottom: 16px;
}
.dm-text {
    font-size: 0.7rem; line-height: 1.7;
    color: rgba(255,255,255,0.6); margin-bottom: 20px;
}
.dm-btn {
    width: 100%; padding: 12px;
    background: transparent;
    border: 1px solid var(--warn); border-radius: 2px;
    color: var(--warn);
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.7rem; letter-spacing: 0.15em;
    cursor: pointer; text-transform: uppercase;
    transition: background 0.2s;
}
.dm-btn:hover { background: rgba(255,184,0,0.1); }
    </style>
</head>
<body>

<div id="login-screen">
    <div class="login-bg"></div>
    <div class="login-box">
        <div class="login-logo">REEL IQ</div>
        <div class="login-sub">Ocean Intelligence Platform</div>
        <div class="login-field">
            <label>Vessel Username</label>
            <input type="text" id="username" placeholder="skipper01" autocomplete="off"/>
        </div>
        <div class="login-field">
            <label>Password</label>
            <input type="password" id="password" placeholder="••••••••"/>
        </div>
        <button class="login-btn" onclick="doLogin()">ACCESS VESSEL DATA</button>
        <div class="login-error" id="login-error"></div>
        <div class="login-demo">Demo: username <b>demo</b> / password <b>demo</b></div>
    </div>
</div>

<div id="app-screen">
    <div id="topbar">
        <div class="topbar-logo">REEL IQ</div>
        <div class="topbar-status">
            <span id="live-dot"></span>
            <span id="update-time">—</span>
        </div>
    </div>

    <div id="sst-panel">
        <div class="panel-label">SST Range</div>
        <div class="panel-value" id="sst-range">—</div>
        <div class="panel-sub" id="grid-points">Loading thermal grid...</div>
    </div>

    <div id="legend">
        <div class="legend-title">SST °C</div>
        <div class="legend-row">
            <div class="legend-bar"></div>
            <div class="legend-labels">
                <span>24°</span>
                <span>22°</span>
                <span>20°</span>
                <span>19°</span>
                <span>18°</span>
                <span>16°</span>
            </div>
        </div>
    </div>

    <div id="map"></div>

    <div id="efficiency-bar">
        <div class="eff-block">
            <div class="eff-label">SOG</div>
            <div class="eff-value" id="sog-val">— kts</div>
        </div>
        <div class="eff-divider"></div>
        <div id="eff-status" class="neutral">Loading vessel data...</div>
        <div class="eff-divider"></div>
        <div class="eff-block">
            <div class="eff-label">STW</div>
            <div class="eff-value" id="stw-val">— kts</div>
        </div>
    </div>

    <button id="logout-btn" onclick="doLogout()">LOGOUT</button>
    <button id="route-btn" onclick="toggleRoutePanel()">⚡ PLAN ROUTE</button>

<div id="route-panel">
    <div class="rp-title">Route Planner</div>
    <label>Fuel Type</label>
    <select id="rp-fuel-type">
        <option value="diesel">Diesel</option>
        <option value="petrol">Petrol (95 ULP)</option>
    </select>
    <label>Consumption (L/hr)</label>
    <input type="number" id="rp-consumption" placeholder="e.g. 55" min="1" max="500" step="1"/>
    <div style="font-size:0.55rem;color:rgba(0,242,255,0.35);margin-top:4px;">
        Saved per vessel — edit anytime
    </div>
    <div class="rp-step" id="rp-step-start">Click map to set START point</div>
    <div class="rp-step" id="rp-step-end" style="margin-top:6px;">Then click to set DESTINATION</div>
    <button class="rp-btn" id="rp-calculate-btn" onclick="calculateRoute()" disabled>
        Calculate Route
    </button>
    <button class="rp-clear" onclick="clearRoute()">Clear</button>
</div>

<div id="route-result">
    <div class="rr-title">⚡ Optimal Route</div>
    <div id="rr-rows"></div>
    <div class="rr-disclaimer" id="rr-disclaimer"></div>
    <button class="rr-close" onclick="closeRouteResult()">Close</button>
</div>

<div id="disclaimer-modal">
    <div class="dm-box">
        <div class="dm-title">⚠ Navigation Disclaimer</div>
        <div class="dm-text">
            Route suggestions are based on oceanographic conditions only and are intended as planning guidance.<br><br>
            Always consult up-to-date nautical charts, respect Marine Protected Areas, and navigate responsibly. Reel IQ is <strong>not a certified navigation tool</strong> and accepts no responsibility for decisions made based on route suggestions.
        </div>
        <button class="dm-btn" onclick="acceptDisclaimer()">I Understand — Continue</button>
    </div>
</div>
</div>

<script>
let currentVesselId = null;
let map = null;
let heatLayer = null;
let vesselMarker = null;
let routeLayer = null;
let routeMarkers = [];
let routeMode = null; // 'start' | 'end' | null
let routeStart = null;
let routeEnd = null;

function tempToColor(intensity) {
    const stops = [
        [0,   [0,   0,   255]],
        [0.2, [0,   255, 255]],
        [0.45,[0,   255, 136]],
        [0.65,[255, 255, 0  ]],
        [0.82,[255, 136, 0  ]],
        [1.0, [255, 0,   0  ]],
    ];
    let lower = stops[0], upper = stops[stops.length-1];
    for (let i = 0; i < stops.length - 1; i++) {
        if (intensity >= stops[i][0] && intensity <= stops[i+1][0]) {
            lower = stops[i]; upper = stops[i+1]; break;
        }
    }
    const t = (intensity - lower[0]) / (upper[0] - lower[0]);
    const r = Math.round(lower[1][0] + t * (upper[1][0] - lower[1][0]));
    const g = Math.round(lower[1][1] + t * (upper[1][1] - lower[1][1]));
    const b = Math.round(lower[1][2] + t * (upper[1][2] - lower[1][2]));
    return [r, g, b];
}

L.CanvasHeatOverlay = L.Layer.extend({
    _points: [],
    _cellSize: 0.027,

    setPoints(pts) { this._points = pts; this._redraw(); },

    onAdd(map) {
        this._map = map;
        this._canvas = document.createElement('canvas');
        this._canvas.style.cssText = 'position:absolute;pointer-events:none;';
map.getPanes().overlayPane.appendChild(this._canvas);
        map.on('move zoom resize', this._redraw, this);
map.on('zoomstart', this._hide, this);
map.on('zoomend', this._redraw, this);
        this._redraw();
    },

    onRemove(map) {
        map.getPanes().overlayPane.removeChild(this._canvas);
        map.off('move zoom resize', this._redraw, this); map.off('zoomstart', this._hide, this);
map.off('zoomend', this._redraw, this);
    },
_hide() {
    if (this._canvas) this._canvas.style.opacity = '0';
},

_show() {
    if (this._canvas) this._canvas.style.opacity = '1';
},
    _redraw() {
    if (!this._map || !this._points.length) return;

    const topLeft = this._map.getBounds().getNorthWest();
    const origin = this._map.latLngToLayerPoint(topLeft);
    const size = this._map.getSize();

    this._canvas.width = size.x;
    this._canvas.height = size.y;
    this._canvas.style.transform = '';
    this._canvas.style.left = origin.x + 'px';
    this._canvas.style.top = origin.y + 'px';

    const ctx = this._canvas.getContext('2d');
    ctx.clearRect(0, 0, size.x, size.y);

    for (const [lat, lon, intensity] of this._points) {
        const pxNW = this._map.latLngToLayerPoint([lat + this._cellSize/2, lon - this._cellSize/2]);
        const pxSE = this._map.latLngToLayerPoint([lat - this._cellSize/2, lon + this._cellSize/2]);
        const x = pxNW.x - origin.x;
        const y = pxNW.y - origin.y;
        const w = Math.ceil(pxSE.x - pxNW.x) + 1;
        const h = Math.ceil(pxSE.y - pxNW.y) + 1;
        const [r,g,b] = tempToColor(intensity);
        ctx.filter = 'blur(4px)';
ctx.fillStyle = `rgba(${r},${g},${b},0.65)`;
ctx.fillRect(x, y, w, h);
ctx.filter = 'none';
    }
    this._canvas.style.opacity = '1';
    }
});

async function doLogin() {
    const u = document.getElementById('username').value.trim();
    const p = document.getElementById('password').value.trim();
    const err = document.getElementById('login-error');
    err.textContent = '';
    if (!u || !p) { err.textContent = 'ENTER CREDENTIALS'; return; }
    try {
        const res = await fetch(`/api/login?username=${encodeURIComponent(u)}&password=${encodeURIComponent(p)}`, {method:'POST'});
        if (!res.ok) { err.textContent = 'ACCESS DENIED — CHECK CREDENTIALS'; return; }
        const data = await res.json();
        currentVesselId = data.vessel_id;
        showApp();
    } catch(e) {
        err.textContent = 'CONNECTION ERROR';
    }
}

document.getElementById('password').addEventListener('keydown', e => {
    if (e.key === 'Enter') doLogin();
});

function doLogout() {
    currentVesselId = null;
    document.getElementById('app-screen').style.display = 'none';
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('username').value = '';
    document.getElementById('password').value = '';
}

function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    const appEl = document.getElementById('app-screen');
    appEl.style.display = 'flex';
    appEl.style.position = 'relative';
    if (!map) {
        map = L.map('map', { zoomControl: true, attributionControl: false, zoomAnimation: false })
                .setView([-34.0, 25.85], 10);
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);
        heatLayer = new L.CanvasHeatOverlay().addTo(map);
    }
    startUpdates();
}

function startUpdates() {
    updateHeatmap();
    updateVessel();
    setInterval(updateHeatmap, 30000);
    setInterval(updateVessel, 10000);
}
// ── ROUTE PLANNER ──

function loadSavedFuelPrefs() {
    const saved = localStorage.getItem(`reeliq_fuel_${currentVesselId}`);
    if (saved) {
        const prefs = JSON.parse(saved);
        document.getElementById('rp-fuel-type').value = prefs.fuelType || 'diesel';
        document.getElementById('rp-consumption').value = prefs.consumption || '';
    }
}

function saveFuelPrefs() {
    const prefs = {
        fuelType: document.getElementById('rp-fuel-type').value,
        consumption: document.getElementById('rp-consumption').value
    };
    localStorage.setItem(`reeliq_fuel_${currentVesselId}`, JSON.stringify(prefs));
}

function toggleRoutePanel() {
    const panel = document.getElementById('route-panel');
    const btn = document.getElementById('route-btn');
    const isOpen = panel.style.display === 'block';
    panel.style.display = isOpen ? 'none' : 'block';
    btn.classList.toggle('active', !isOpen);
    if (!isOpen) {
        loadSavedFuelPrefs();
        // Check disclaimer
        if (!localStorage.getItem('reeliq_disclaimer_accepted')) {
            document.getElementById('disclaimer-modal').classList.add('show');
        } else {
            activateRouteMode('start');
        }
    }
}

function acceptDisclaimer() {
    localStorage.setItem('reeliq_disclaimer_accepted', '1');
    document.getElementById('disclaimer-modal').classList.remove('show');
    activateRouteMode('start');
}

function activateRouteMode(step) {
    routeMode = step;
    map.getContainer().style.cursor = 'crosshair';
}

map && map.on('click', function(e) {
    if (!routeMode) return;
    const { lat, lng } = e.latlng;

    if (routeMode === 'start') {
        routeStart = [lat, lng];
        // Drop green start marker
        routeMarkers.forEach(m => map.removeLayer(m));
        routeMarkers = [];
        const m = L.circleMarker([lat, lng], {
            radius: 7, fillColor: '#00ff88',
            color: '#fff', weight: 2, fillOpacity: 1
        }).bindTooltip('START', {permanent: true, className: 'route-tooltip'}).addTo(map);
        routeMarkers.push(m);
        document.getElementById('rp-step-start').textContent = `✓ Start: ${lat.toFixed(4)}, ${lng.toFixed(4)}`;
        document.getElementById('rp-step-start').classList.add('done');
        routeMode = 'end';

    } else if (routeMode === 'end') {
        routeEnd = [lat, lng];
        const m = L.circleMarker([lat, lng], {
            radius: 7, fillColor: '#ff3b3b',
            color: '#fff', weight: 2, fillOpacity: 1
        }).bindTooltip('DEST', {permanent: true, className: 'route-tooltip'}).addTo(map);
        routeMarkers.push(m);
        document.getElementById('rp-step-end').textContent = `✓ Dest: ${lat.toFixed(4)}, ${lng.toFixed(4)}`;
        document.getElementById('rp-step-end').classList.add('done');
        routeMode = null;
        map.getContainer().style.cursor = '';
        document.getElementById('rp-calculate-btn').disabled = false;
    }
});

async function calculateRoute() {
    const consumption = parseFloat(document.getElementById('rp-consumption').value);
    const fuelType = document.getElementById('rp-fuel-type').value;
    if (!consumption || consumption <= 0) {
        alert('Please enter your boat\'s fuel consumption (L/hr)');
        return;
    }
    if (!routeStart || !routeEnd) {
        alert('Please set both start and destination points on the map');
        return;
    }
    saveFuelPrefs();

    const btn = document.getElementById('rp-calculate-btn');
    btn.textContent = 'Calculating...';
    btn.disabled = true;

    try {
        const url = `/api/route?start_lat=${routeStart[0]}&start_lon=${routeStart[1]}&end_lat=${routeEnd[0]}&end_lon=${routeEnd[1]}&consumption_lhr=${consumption}&fuel_type=${fuelType}`;
        const res = await fetch(url);
        const data = await res.json();

        // Draw route on map
        if (routeLayer) map.removeLayer(routeLayer);
        routeLayer = L.polyline(data.route, {
            color: '#00f2ff', weight: 3,
            opacity: 0.85, dashArray: '8 4'
        }).addTo(map);
        map.fitBounds(routeLayer.getBounds(), { padding: [40, 40] });

        // Build result popup
        const rows = document.getElementById('rr-rows');
        rows.innerHTML = `
            <div class="rr-row"><span class="rr-key">Route Distance</span><span class="rr-val">${data.route_dist_km} km</span></div>
            <div class="rr-row"><span class="rr-key">Direct Distance</span><span class="rr-val">${data.direct_dist_km} km</span></div>
            <div class="rr-row"><span class="rr-key">Est. Travel Time</span><span class="rr-val">${(data.route_time_hrs * 60).toFixed(0)} min</span></div>
            <div class="rr-row"><span class="rr-key">Fuel (Optimised)</span><span class="rr-val">${data.optimised_fuel_l} L</span></div>
            <div class="rr-row"><span class="rr-key">Fuel (Direct)</span><span class="rr-val">${data.direct_fuel_l} L</span></div>
            <div class="rr-row"><span class="rr-key">Cost (Optimised)</span><span class="rr-val">R ${data.optimised_cost_zar}</span></div>
            <div class="rr-row"><span class="rr-key">Est. Saving</span><span class="rr-val saving">R ${data.saving_zar} (${data.saving_pct}%)</span></div>
            <div class="rr-row"><span class="rr-key">${fuelType === 'diesel' ? 'Diesel' : 'Petrol'} Price</span><span class="rr-val">R ${data.fuel_price_used}/L</span></div>
            <div class="rr-row"><span class="rr-key">Best Departure</span><span class="rr-val departure">${data.best_departure}</span></div>
            <div class="rr-row" style="font-size:0.6rem;"><span class="rr-key" style="color:rgba(255,184,0,0.5);max-width:60%;">${data.best_departure_reason}</span></div>
        `;
        document.getElementById('rr-disclaimer').textContent = data.disclaimer;
        document.getElementById('route-result').style.display = 'block';

    } catch(e) {
        console.error('Route error:', e);
        alert('Route calculation failed. Please try again.');
    }

    btn.textContent = 'Calculate Route';
    btn.disabled = false;
}

function closeRouteResult() {
    document.getElementById('route-result').style.display = 'none';
}

function clearRoute() {
    routeStart = null; routeEnd = null; routeMode = null;
    routeMarkers.forEach(m => map.removeLayer(m));
    routeMarkers = [];
    if (routeLayer) { map.removeLayer(routeLayer); routeLayer = null; }
    document.getElementById('rp-step-start').textContent = 'Click map to set START point';
    document.getElementById('rp-step-start').classList.remove('done');
    document.getElementById('rp-step-end').textContent = 'Then click to set DESTINATION';
    document.getElementById('rp-step-end').classList.remove('done');
    document.getElementById('rp-calculate-btn').disabled = true;
    document.getElementById('route-result').style.display = 'none';
    map.getContainer().style.cursor = '';
    activateRouteMode('start');
}
```

---

Also add `httpx` to your `requirements.txt` if it's not already there:
```
httpx

async function updateHeatmap() {
    try {
        const res = await fetch('/api/interpolated');
        const points = await res.json();
        if (!points.length) return;
        heatLayer.setPoints(points);
        const MIN_TEMP = 16.0, MAX_TEMP = 24.0;
        const temps = points.map(p => p[2] * (MAX_TEMP - MIN_TEMP) + MIN_TEMP);
        const minT = Math.min(...temps).toFixed(1);
        const maxT = Math.max(...temps).toFixed(1);
        document.getElementById('sst-range').textContent = `${minT}° — ${maxT}°C`;
        document.getElementById('grid-points').textContent = `${points.length} grid cells`;
        document.getElementById('update-time').textContent =
            new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    } catch(e) { console.error('Heatmap error:', e); }
}

async function updateVessel() {
    if (!currentVesselId) return;
    try {
        const res = await fetch(`/api/vessel/${currentVesselId}`);
        if (!res.ok) return;
        const d = await res.json();
        const pos = [d.latitude, d.longitude];
        if (!vesselMarker) {
            vesselMarker = L.circleMarker(pos, {
                radius: 8,
                fillColor: '#00f2ff',
                color: '#ffffff',
                weight: 2,
                fillOpacity: 0.95
            }).addTo(map);
        } else {
            vesselMarker.setLatLng(pos);
        }
        document.getElementById('sog-val').textContent = d.speed_over_ground.toFixed(1) + ' kts';
        document.getElementById('stw-val').textContent = d.speed_through_water.toFixed(1) + ' kts';
        const effEl = document.getElementById('eff-status');
        effEl.textContent = d.efficiency_label;
        effEl.className = d.efficiency;
    } catch(e) { console.error('Vessel error:', e); }
}
</script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
