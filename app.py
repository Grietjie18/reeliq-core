import asyncio, os, uuid, json, re
import numpy as np
from scipy.interpolate import RBFInterpolator
from shapely.geometry import Point, Polygon
from datetime import datetime, timedelta, timezone, date
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime, text

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
    Column("sea_surface_salinity", Float),
    Column("speed_over_ground", Float),
    Column("speed_through_water", Float),
    Column("heading", Float),
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
ALGOA_BAY_COORDS = [
    (24.869, -34.300), (24.869, -34.195), (24.841, -34.145), (24.912, -34.085),
    (24.921, -34.079), (24.925, -34.052), (24.933, -34.032), (24.931, -34.011),
    (24.937, -34.005), (25.034, -33.970), (25.213, -33.969), (25.402, -34.034),
    (25.584, -34.048), (25.700, -34.029), (25.644, -33.955), (25.632, -33.865),
    (25.694, -33.815), (25.830, -33.727), (26.080, -33.707), (26.298, -33.763),
    (26.352, -33.760), (26.352, -34.300), (24.869, -34.300),
]
ALGOA_BAY_POLYGON = Polygon(ALGOA_BAY_COORDS)

def is_ocean(lon, lat):
    return ALGOA_BAY_POLYGON.contains(Point(lon, lat))

# --- PYDANTIC MODEL ---
class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latitude: float
    longitude: float
    sea_surface_temperature: float
    sea_surface_salinity: float = Field(default=35.2)
    speed_over_ground: float
    speed_through_water: float
    heading: float = Field(default=0.0)

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
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE observations ADD COLUMN IF NOT EXISTS sea_surface_salinity FLOAT DEFAULT 35.2"))
        conn.execute(text("ALTER TABLE observations ADD COLUMN IF NOT EXISTS heading FLOAT DEFAULT 0.0"))
        conn.commit()
    asyncio.create_task(cleanup_old_data())

async def cleanup_old_data():
    while True:
        await asyncio.sleep(300)  # runs every 5 minutes
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        await database.execute(
            observations_table.delete().where(observations_table.c.timestamp < cutoff)
        )
        print("🧹 Old data cleaned up")

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
        sea_surface_salinity=data.sea_surface_salinity,
        speed_over_ground=data.speed_over_ground,
        speed_through_water=data.speed_through_water,
        heading=data.heading,
    )
    await database.execute(query)
    return {"status": "success"}

@app.post("/api/login")
async def login(username: str = Query(...), password: str = Query(...)):
    for vessel_id, (u, p) in VESSEL_CREDENTIALS.items():
        if u == username and p == password:
            return {"status": "ok", "vessel_id": vessel_id}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/api/coastline")
async def get_coastline():
    try:
        with open("coastline.geojson", "r") as f:
            return JSONResponse(json.load(f))
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "coastline.geojson not found"})

@app.get("/api/polygon")
async def get_polygon():
    return JSONResponse(ALGOA_BAY_COORDS)

@app.get("/api/interpolated")
async def get_interpolated(variable: str = Query(default="sst")):
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=15)
    query = observations_table.select().where(observations_table.c.timestamp >= cutoff)
    rows = await database.fetch_all(query)

    all_lats, all_lons, all_values = [], [], []

    if variable == "sst":
        satellite_points = await get_satellite_sst()
        for lat, lon, temp in satellite_points:
            all_lats.append(lat); all_lons.append(lon); all_values.append(temp)
        for r in rows:
            for _ in range(3):
                all_lats.append(r['latitude']); all_lons.append(r['longitude']); all_values.append(r['sea_surface_temperature'])
        VAL_MIN, VAL_MAX = 16.0, 24.0
    elif variable == "salinity":
        for r in rows:
            sal = r['sea_surface_salinity']
            if sal is not None:
                for _ in range(3):
                    all_lats.append(r['latitude']); all_lons.append(r['longitude']); all_values.append(sal)
        VAL_MIN, VAL_MAX = 34.5, 35.6
    else:
        raise HTTPException(status_code=400, detail="variable must be 'sst' or 'salinity'")

    if len(all_lats) < 3:
        return JSONResponse([])

    obs_coords = np.column_stack([all_lats, all_lons])
    obs_values = np.array(all_values)
    grid_lats = np.linspace(-34.300, -33.700, 60)
    grid_lons = np.linspace(24.840, 26.360, 60)
    interpolator = RBFInterpolator(obs_coords, obs_values, kernel='thin_plate_spline', smoothing=1.5)

    result = []
    for lat in grid_lats:
        for lon in grid_lons:
            if not is_ocean(lon, lat): continue
            val = float(interpolator([[lat, lon]])[0])
            intensity = max(0.0, min(1.0, (val - VAL_MIN) / (VAL_MAX - VAL_MIN)))
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
    if not row: raise HTTPException(status_code=404, detail="No recent data")

    sog, stw = row['speed_over_ground'], row['speed_through_water']
    diff = stw - sog
    if diff > 0.5:
        eff, label = "poor", "Fighting Current 🔴"
    elif diff < -0.3:
        eff, label = "good", "Riding Current 🟢"
    else:
        eff, label = "neutral", "Neutral Conditions 🟡"

    return {
        "vessel_id": vessel_id,
        "latitude": row['latitude'],
        "longitude": row['longitude'],
        "sea_surface_temperature": row['sea_surface_temperature'],
        "sea_surface_salinity": row['sea_surface_salinity'],
        "speed_over_ground": sog,
        "speed_through_water": stw,
        "heading": row['heading'] or 0.0,
        "efficiency": eff,
        "efficiency_label": label,
        "timestamp": row['timestamp'].isoformat()
    }

@app.get("/", response_class=HTMLResponse)
async def get_landing():
    with open("landing.html", "r") as f: return HTMLResponse(content=f.read())

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
    <link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@300;400;600;700&family=Barlow:wght@300;400;600&family=Montserrat:wght@700;900&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        :root {
            --orange:     #F39102;
            --orange-dim: rgba(243,145,2,0.12);
            --orange-border: rgba(243,145,2,0.35);
            --navy:       #000F2C;
            --deep:       #020B12;
            --steel:      #6C7177;
            --offwhite:   #EBEBE9;
            --light-grey: #C8C9CB;
            --mid-grey:   #95989C;
            --border:     rgba(108,113,119,0.18);
            --panel:      rgba(2,11,18,0.92);
            --good:       #00ff88;
            --bad:        #ff3b3b;
            --warn:       #ffb800;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--deep);
            font-family: 'Barlow', sans-serif;
            color: var(--offwhite);
            overflow: hidden;
            height: 100vh;
        }

        /* ── WORDMARK ── */
        .wordmark {
            display: inline-flex; align-items: center; gap: 0; line-height: 1;
        }
        .wordmark__reel {
            font-family: 'Barlow Condensed', sans-serif;
            font-weight: 700; letter-spacing: 0.06em;
            color: var(--offwhite); text-transform: uppercase;
        }
        .wordmark__rule {
            background: var(--orange); opacity: 0.65;
            flex-shrink: 0; align-self: center;
            width: 1.5px; height: 13px; margin: 0 10px;
        }
        .wordmark__iq {
            font-family: 'Montserrat', sans-serif;
            font-weight: 900; letter-spacing: 0.08em;
            color: var(--orange); text-transform: uppercase;
        }
        .wordmark--nav .wordmark__reel,
        .wordmark--nav .wordmark__iq { font-size: 18px; }

        /* ── LOGIN ── */
        #login-screen {
            position: fixed; inset: 0; z-index: 9999;
            background: var(--deep);
            display: flex; align-items: center; justify-content: center;
            flex-direction: column;
        }
        .login-bg {
            position: absolute; inset: 0;
            background:
                radial-gradient(ellipse 60% 50% at 50% 110%, rgba(0,15,44,0.95) 0%, transparent 70%),
                radial-gradient(ellipse 35% 35% at 82% 38%, rgba(243,145,2,0.05) 0%, transparent 55%),
                linear-gradient(175deg, var(--deep) 0%, var(--navy) 55%, var(--deep) 100%);
        }
        .login-bg::after {
            content: '';
            position: absolute; bottom: 32%;
            left: 0; right: 0; height: 1px;
            background: linear-gradient(90deg,
                transparent,
                rgba(200,201,203,0.06) 20%,
                rgba(243,145,2,0.2) 50%,
                rgba(200,201,203,0.06) 80%,
                transparent);
        }
        .login-box {
            position: relative; z-index: 2;
            width: min(400px, 92vw);
            padding: 48px 40px;
            border: 1px solid var(--orange-border);
            border-radius: 2px;
            background: rgba(0,15,44,0.7);
            backdrop-filter: blur(20px);
        }
        .login-logo {
            margin-bottom: 4px;
        }
        .login-sub {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 10px; color: var(--steel);
            letter-spacing: 0.35em; margin-bottom: 40px;
            text-transform: uppercase;
        }
        .login-field { margin-bottom: 14px; }
        .login-field label {
            display: block;
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 10px; letter-spacing: 0.3em;
            color: var(--steel); margin-bottom: 8px;
            text-transform: uppercase;
        }
        .login-field input {
            width: 100%;
            background: rgba(0,15,44,0.7);
            border: 1px solid rgba(108,113,119,0.22);
            border-radius: 2px; padding: 12px 16px;
            color: var(--offwhite);
            font-family: 'Barlow', sans-serif; font-size: 14px;
            outline: none; transition: border-color 0.2s;
        }
        .login-field input:focus { border-color: var(--orange-border); }
        .login-field input::placeholder { color: var(--steel); }
        .login-btn {
            width: 100%; margin-top: 24px; padding: 14px;
            background: var(--orange);
            border: none; border-radius: 2px;
            color: var(--deep);
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 13px; font-weight: 700;
            letter-spacing: 0.35em; cursor: pointer;
            text-transform: uppercase;
            transition: opacity 0.2s, transform 0.15s;
        }
        .login-btn:hover { opacity: 0.85; transform: translateY(-1px); }
        .login-error {
            margin-top: 12px; font-size: 11px; color: var(--bad);
            text-align: center; letter-spacing: 0.15em; min-height: 16px;
            font-family: 'Barlow Condensed', sans-serif; text-transform: uppercase;
        }
        .login-demo {
            margin-top: 20px; font-size: 11px;
            color: var(--steel); text-align: center;
            letter-spacing: 0.1em;
            font-family: 'Barlow Condensed', sans-serif;
        }
        .login-demo b { color: var(--light-grey); }

        /* ── APP ── */
        #app-screen { display: none; height: 100vh; flex-direction: column; position: relative; }
        #map { flex: 1; background: var(--deep); }

        #topbar {
            position: absolute; top: 0; left: 0; right: 0; z-index: 1000;
            display: flex; align-items: center; justify-content: space-between;
            padding: 12px 16px;
            background: linear-gradient(to bottom, rgba(2,11,18,0.97), transparent);
            pointer-events: none;
        }
        .topbar-status {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 11px; color: var(--steel);
            letter-spacing: 0.2em; text-align: right; text-transform: uppercase;
        }
        #live-dot {
            display: inline-block; width: 6px; height: 6px; border-radius: 50%;
            background: var(--good); margin-right: 6px; animation: pulse 1.5s infinite;
        }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

        /* ── LAYER TOGGLE ── */
        #layer-toggle {
            position: absolute; top: 60px; left: 50%; transform: translateX(-50%);
            z-index: 1001; display: flex;
            border: 1px solid var(--orange-border);
            border-radius: 2px; overflow: hidden;
            background: var(--panel);
        }
        .layer-btn {
            padding: 7px 20px;
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 11px; letter-spacing: 0.25em;
            text-transform: uppercase; cursor: pointer;
            border: none; background: transparent;
            color: var(--steel);
            transition: background 0.15s, color 0.15s;
        }
        .layer-btn.active { background: var(--orange-dim); color: var(--orange); }
        .layer-btn + .layer-btn { border-left: 1px solid var(--orange-border); }

        /* ── LEGEND ── */
        #legend {
            position: absolute; top: 60px; right: 16px; z-index: 1000;
            background: var(--panel); border: 1px solid var(--border);
            border-radius: 2px; padding: 12px 10px;
        }
        .legend-bar-sst {
            width: 12px; height: 160px; border-radius: 2px;
            /* Colorblind-safe: blue → white → orange */
            background: linear-gradient(to top,
                #0051a8 0%,
                #4a90d9 20%,
                #a8c8f0 40%,
                #ffffff 55%,
                #f9c06a 70%,
                #F39102 85%,
                #c86400 100%);
        }
        .legend-bar-sal {
            width: 12px; height: 160px; border-radius: 2px;
            /* Colorblind-safe: yellow → dark blue */
            background: linear-gradient(to top,
                #003380 0%,
                #1a5fa8 25%,
                #4d9fd4 50%,
                #a8d4f0 70%,
                #ffe566 100%);
        }
        .legend-labels {
            display: flex; flex-direction: column; justify-content: space-between;
            height: 160px; font-size: 0.6rem; color: var(--mid-grey); margin-left: 6px;
            font-family: 'Barlow Condensed', sans-serif; letter-spacing: 0.05em;
        }
        .legend-row { display: flex; align-items: flex-start; gap: 6px; }
        .legend-title {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 9px; color: var(--steel);
            letter-spacing: 0.2em; text-align: center;
            margin-bottom: 6px; text-transform: uppercase;
        }

        /* ── INFO PANEL ── */
        #info-panel {
            position: absolute; top: 60px; left: 16px; z-index: 1000;
            background: var(--panel); border: 1px solid var(--orange-border);
            border-radius: 2px; padding: 12px 16px; min-width: 180px;
        }
        .panel-label {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 9px; color: var(--steel);
            letter-spacing: 0.25em; text-transform: uppercase; margin-bottom: 4px;
        }
        .panel-value {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 1.1rem; color: var(--orange); font-weight: 700;
            letter-spacing: 0.05em;
        }
        .panel-sub {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 9px; color: var(--steel); margin-top: 2px;
            letter-spacing: 0.1em;
        }

        /* ── EFFICIENCY BAR ── */
        #efficiency-bar {
            position: absolute; bottom: 0; left: 0; right: 0; z-index: 1000;
            background: var(--panel); border-top: 1px solid var(--border);
            padding: 12px 20px; display: flex; align-items: center;
            justify-content: space-between; gap: 16px;
        }
        .eff-block { display: flex; flex-direction: column; align-items: center; flex: 1; }
        .eff-label {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 9px; color: var(--steel);
            letter-spacing: 0.2em; text-transform: uppercase; margin-bottom: 2px;
        }
        .eff-value {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 1rem; font-weight: 700; color: var(--offwhite);
        }
        .eff-divider { width: 1px; height: 32px; background: var(--border); }
        #eff-status {
            font-family: 'Barlow Condensed', sans-serif;
            font-size: 0.9rem; font-weight: 700; text-align: center;
            flex: 2; letter-spacing: 0.08em; text-transform: uppercase;
        }
        #eff-status.good { color: var(--good); }
        #eff-status.poor { color: var(--bad); }
        #eff-status.neutral { color: var(--warn); }

        #logout-btn {
            position: absolute; bottom: 68px; right: 16px; z-index: 1001;
            background: transparent; border: 1px solid var(--border);
            border-radius: 2px; color: var(--steel);
            font-family: 'Barlow Condensed', sans-serif; font-size: 9px;
            letter-spacing: 0.2em; padding: 6px 12px; cursor: pointer;
            text-transform: uppercase; transition: color 0.2s, border-color 0.2s;
        }
        #logout-btn:hover { color: var(--bad); border-color: var(--bad); }

        /* ── VESSEL ARROW ── */
        .vessel-arrow-icon { background: none; border: none; }
    </style>
</head>
<body>

<!-- LOGIN -->
<div id="login-screen">
    <div class="login-bg"></div>
    <div class="login-box">
        <div class="login-logo">
            <div class="wordmark wordmark--nav">
                <span class="wordmark__reel">Reel</span>
                <div class="wordmark__rule"></div>
                <span class="wordmark__iq">IQ</span>
            </div>
        </div>
        <div class="login-sub">Ocean Intelligence Platform</div>
        <div class="login-field">
            <label>Vessel Username</label>
            <input type="text" id="username" placeholder="skipper01" autocomplete="off"/>
        </div>
        <div class="login-field">
            <label>Password</label>
            <input type="password" id="password" placeholder="••••••••"/>
        </div>
        <button class="login-btn" onclick="doLogin()">Access Vessel Data</button>
        <div class="login-error" id="login-error"></div>
        <div class="login-demo">Demo: username <b>demo</b> / password <b>demo</b></div>
    </div>
</div>

<!-- APP -->
<div id="app-screen">
    <div id="topbar">
        <div class="wordmark wordmark--nav">
            <span class="wordmark__reel">Reel</span>
            <div class="wordmark__rule"></div>
            <span class="wordmark__iq">IQ</span>
        </div>
        <div class="topbar-status">
            <span id="live-dot"></span>
            <span id="update-time">—</span>
        </div>
    </div>

    <div id="layer-toggle">
        <button class="layer-btn active" id="btn-sst" onclick="switchLayer('sst')">SST</button>
        <button class="layer-btn" id="btn-salinity" onclick="switchLayer('salinity')">Salinity</button>
    </div>

    <div id="info-panel">
        <div class="panel-label" id="panel-label">SST Range</div>
        <div class="panel-value" id="panel-value">—</div>
        <div class="panel-sub" id="grid-points">Loading grid...</div>
    </div>

    <div id="legend">
        <div class="legend-title" id="legend-title">SST °C</div>
        <div class="legend-row">
            <div id="legend-bar" class="legend-bar-sst"></div>
            <div class="legend-labels" id="legend-labels">
                <span>24°</span><span>22°</span><span>20°</span>
                <span>19°</span><span>18°</span><span>16°</span>
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
    <button id="logout-btn" onclick="doLogout()">Logout</button>
</div>

<script>
let currentVesselId = null;
let map = null;
let heatLayer = null;
let vesselMarker = null;
let currentLayer = 'sst';

// Cache for instant layer switching
let cachedPoints = { sst: [], salinity: [] };

// ── Colorblind-safe colour maps ──
// SST: blue → white → orange (safe for deuteranopia/protanopia)
function sstColor(intensity) {
    const stops = [
        [0.0,  [0,   81,  168]],   // deep blue
        [0.25, [74,  144, 217]],   // mid blue
        [0.45, [168, 200, 240]],   // pale blue
        [0.55, [255, 255, 255]],   // white midpoint
        [0.70, [249, 192, 106]],   // pale orange
        [0.85, [243, 145, 2  ]],   // orange
        [1.0,  [200, 100, 0  ]],   // deep orange
    ];
    return interpColor(stops, intensity);
}

// Salinity: yellow → pale blue → dark blue (colorblind-safe)
function salinityColor(intensity) {
    const stops = [
        [0.0,  [255, 229, 102]],   // yellow (fresh)
        [0.3,  [168, 212, 240]],   // pale blue
        [0.6,  [77,  159, 212]],   // mid blue
        [0.8,  [26,  95,  168]],   // deeper blue
        [1.0,  [0,   51,  128]],   // dark navy (salty Agulhas)
    ];
    return interpColor(stops, intensity);
}

function interpColor(stops, intensity) {
    intensity = Math.max(0, Math.min(1, intensity));
    let lower = stops[0], upper = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
        if (intensity >= stops[i][0] && intensity <= stops[i+1][0]) {
            lower = stops[i]; upper = stops[i+1]; break;
        }
    }
    const t = (upper[0] - lower[0]) === 0 ? 0 : (intensity - lower[0]) / (upper[0] - lower[0]);
    return [
        Math.round(lower[1][0] + t * (upper[1][0] - lower[1][0])),
        Math.round(lower[1][1] + t * (upper[1][1] - lower[1][1])),
        Math.round(lower[1][2] + t * (upper[1][2] - lower[1][2])),
    ];
}

// ── Canvas heat layer ──
L.CanvasHeatOverlay = L.Layer.extend({
    _points: [], _cellSize: 0.027, _polygonCoords: null, _colorFn: sstColor,

    setPoints(pts) { this._points = pts; this._redraw(); },
    setPolygon(coords) { this._polygonCoords = coords; this._redraw(); },
    setColorFn(fn) { this._colorFn = fn; this._redraw(); },

    onAdd(map) {
        this._map = map;
        this._canvas = document.createElement('canvas');
        this._canvas.style.cssText = 'position:absolute;pointer-events:none;';
        map.getPanes().overlayPane.appendChild(this._canvas);
        map.on('zoomend moveend resize', this._redraw, this);
        this._redraw();
    },

    _redraw() {
        if (!this._map || !this._points.length) return;
        const topLeft = this._map.getBounds().getNorthWest();
        const origin = this._map.latLngToLayerPoint(topLeft);
        const size = this._map.getSize();
        this._canvas.width = size.x;
        this._canvas.height = size.y;
        this._canvas.style.left = origin.x + 'px';
        this._canvas.style.top = origin.y + 'px';

        const ctx = this._canvas.getContext('2d');
        ctx.clearRect(0, 0, size.x, size.y);
        ctx.save();

        if (this._polygonCoords) {
            ctx.beginPath();
            this._polygonCoords.forEach(([lon, lat], i) => {
                const pt = this._map.latLngToLayerPoint([lat, lon]);
                if (i === 0) ctx.moveTo(pt.x - origin.x, pt.y - origin.y);
                else ctx.lineTo(pt.x - origin.x, pt.y - origin.y);
            });
            ctx.closePath(); ctx.clip();
        }

        for (const [lat, lon, intensity] of this._points) {
            const pxNW = this._map.latLngToLayerPoint([lat + this._cellSize/2, lon - this._cellSize/2]);
            const pxSE = this._map.latLngToLayerPoint([lat - this._cellSize/2, lon + this._cellSize/2]);
            const x = pxNW.x - origin.x, y = pxNW.y - origin.y;
            const w = Math.ceil(pxSE.x - pxNW.x) + 1;
            const h = Math.ceil(pxSE.y - pxNW.y) + 1;
            const [r,g,b] = this._colorFn(intensity);
            ctx.filter = 'none';
            ctx.fillStyle = `rgba(${r},${g},${b},0.65)`;
            ctx.fillRect(x, y, w, h);
        }
        ctx.restore(); ctx.filter = 'none';
    }
});

// ── Vessel arrow icon ──
function createVesselIcon(heading) {
    // Leaflet heading: 0 = north, 90 = east
    // SVG arrow points up (north) by default
    return L.divIcon({
        className: 'vessel-arrow-icon',
        html: `<svg width="22" height="22" viewBox="0 0 22 22" xmlns="http://www.w3.org/2000/svg"
                    style="transform:rotate(${heading}deg);transform-origin:center;display:block;">
                 <polygon points="11,2 18,19 11,14 4,19"
                          fill="#F39102" stroke="#020B12" stroke-width="1.5" stroke-linejoin="round"/>
               </svg>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
    });
}

// ── Layer switch (instant with cache) ──
function switchLayer(layer) {
    if (layer === currentLayer) return;
    currentLayer = layer;

    document.getElementById('btn-sst').classList.toggle('active', layer === 'sst');
    document.getElementById('btn-salinity').classList.toggle('active', layer === 'salinity');

    if (heatLayer) heatLayer.setColorFn(layer === 'sst' ? sstColor : salinityColor);

    // Show cached result instantly if available
    if (cachedPoints[layer].length && heatLayer) {
        heatLayer.setPoints(cachedPoints[layer]);
        updatePanelDisplay(cachedPoints[layer]);
    } else {
        document.getElementById('panel-value').textContent = '—';
        document.getElementById('grid-points').textContent = 'Loading...';
    }

    // Update legend
    const bar = document.getElementById('legend-bar');
    const labels = document.getElementById('legend-labels');
    const title = document.getElementById('legend-title');
    const panelLabel = document.getElementById('panel-label');

    if (layer === 'sst') {
        bar.className = 'legend-bar-sst'; title.textContent = 'SST °C';
        panelLabel.textContent = 'SST Range';
        labels.innerHTML = '<span>24°</span><span>22°</span><span>20°</span><span>19°</span><span>18°</span><span>16°</span>';
    } else {
        bar.className = 'legend-bar-sal'; title.textContent = 'Salinity PSU';
        panelLabel.textContent = 'Salinity Range';
        labels.innerHTML = '<span>35.6</span><span>35.4</span><span>35.2</span><span>35.0</span><span>34.8</span><span>34.5</span>';
    }

    // Fetch fresh data in background
    updateHeatmap();
}

function updatePanelDisplay(points) {
    if (!points.length) return;
    if (currentLayer === 'sst') {
        const vals = points.map(p => p[2] * (24.0 - 16.0) + 16.0);
        document.getElementById('panel-value').textContent =
            `${Math.min(...vals).toFixed(1)}° — ${Math.max(...vals).toFixed(1)}°C`;
    } else {
        const vals = points.map(p => p[2] * (35.6 - 34.5) + 34.5);
        document.getElementById('panel-value').textContent =
            `${Math.min(...vals).toFixed(2)} — ${Math.max(...vals).toFixed(2)} PSU`;
    }
    document.getElementById('grid-points').textContent = `${points.length} grid cells`;
}

// ── Auth ──
async function doLogin() {
    const u = document.getElementById('username').value.trim();
    const p = document.getElementById('password').value.trim();
    const err = document.getElementById('login-error');
    err.textContent = '';
    if (!u || !p) { err.textContent = 'Enter credentials'; return; }
    try {
        const res = await fetch(`/api/login?username=${encodeURIComponent(u)}&password=${encodeURIComponent(p)}`, {method:'POST'});
        if (!res.ok) { err.textContent = 'Access denied'; return; }
        const data = await res.json();
        currentVesselId = data.vessel_id;
        showApp();
    } catch(e) { err.textContent = 'Connection error'; }
}

function doLogout() {
    currentVesselId = null;
    document.getElementById('app-screen').style.display = 'none';
    document.getElementById('login-screen').style.display = 'flex';
}

async function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app-screen').style.display = 'flex';

    if (!map) {
        map = L.map('map', { zoomControl: true, attributionControl: false, zoomAnimation: false })
                .setView([-34.0, 25.85], 10);
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);
        heatLayer = new L.CanvasHeatOverlay().addTo(map);

        try {
            const res = await fetch('/api/coastline');
            const geojson = await res.json();
            const feature = geojson.features[0];
            const coords = feature.geometry.type === 'Polygon'
                ? feature.geometry.coordinates[0]
                : feature.geometry.coordinates[0][0];
            heatLayer.setPolygon(coords);
        } catch(e) { console.error('Clip error:', e); }
    }
    startUpdates();
}

function startUpdates() {
    updateHeatmap();
    updateVessel();
    setInterval(updateHeatmap, 30000);
    setInterval(updateVessel, 10000);
}

async function updateHeatmap() {
    try {
        const layer = currentLayer;
        const res = await fetch(`/api/interpolated?variable=${layer}`);
        const points = await res.json();
        if (!points.length) return;

        cachedPoints[layer] = points;

        // Only update display if this is still the active layer
        if (layer === currentLayer) {
            heatLayer.setPoints(points);
            updatePanelDisplay(points);
            document.getElementById('update-time').textContent =
                new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        }
    } catch(e) { console.error(e); }
}

async function updateVessel() {
    if (!currentVesselId) return;
    try {
        const res = await fetch(`/api/vessel/${currentVesselId}`);
        if (!res.ok) return;
        const d = await res.json();
        const pos = [d.latitude, d.longitude];
        const heading = d.heading || 0;

        if (!vesselMarker) {
            vesselMarker = L.marker(pos, { icon: createVesselIcon(heading) }).addTo(map);
        } else {
            vesselMarker.setLatLng(pos);
            vesselMarker.setIcon(createVesselIcon(heading));
        }

        document.getElementById('sog-val').textContent = d.speed_over_ground.toFixed(1) + ' kts';
        document.getElementById('stw-val').textContent = d.speed_through_water.toFixed(1) + ' kts';
        const effEl = document.getElementById('eff-status');
        effEl.textContent = d.efficiency_label;
        effEl.className = d.efficiency;
    } catch(e) { console.error(e); }
}
</script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
