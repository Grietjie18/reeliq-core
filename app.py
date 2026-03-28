import os, uuid
import numpy as np
import copernicusmarine
import xarray as xr
import numpy as np
from datetime import date
from functools import lru_cache
from scipy.interpolate import RBFInterpolator
from shapely.geometry import Point, Polygon
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime

app = FastAPI()

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
# vessel_id -> (username, password)
VESSEL_CREDENTIALS = {
    "RV_Algoa_0":  ("skipper01", "reef2026"),
    "RV_Algoa_1":  ("skipper02", "reef2026"),
    "RV_Algoa_2":  ("skipper03", "reef2026"),
    "demo":        ("demo",      "demo"),
}
VALID_API_KEYS = {"2026_Reeliq_dev18"}

# --- ALGOA BAY OCEAN POLYGON ---
# Tightly traced to exclude PE harbour, coastline and land
# (lon, lat) pairs — shapely uses (x, y) = (lon, lat)
ALGOA_BAY_POLYGON = Polygon([
    # Offshore SW boundary
    (24.869, -34.300),  # Offshore south of Shark Point
    # Coastline traced west to east (lon, lat)
    (24.869, -34.195),  # Shark Point / St Francis
    (24.841, -34.145),  # Kromriver mouth
    (24.912, -34.085),  # Ashton Bay
    (24.921, -34.079),  # Marina Martinique
    (24.925, -34.052),  # Main Beach JBay
    (24.933, -34.032),  # Supertubes
    (24.931, -34.011),  # Kabeljous Beach
    (24.937, -34.005),  # Kabeljous Estuary
    (25.034, -33.970),  # Gamtoos River Mouth
    (25.213, -33.969),  # Van Stadens River Mouth
    (25.402, -34.034),  # Kini Bay
    (25.584, -34.048),  # Schoenmakerskop
    (25.700, -34.029),  # Cape Recife
    (25.644, -33.955),  # PE Harbour Mouth
    (25.632, -33.865),  # Swartkops Estuary
    (25.694, -33.815),  # Coega Harbour
    (25.830, -33.727),  # East of PE
    (26.080, -33.707),  # Further east
    (26.298, -33.763),  # Eastern bay
    (26.352, -33.760),  # Eastern edge
    # Offshore return boundary
    (26.352, -34.300),  # Offshore SE corner
    (24.869, -34.300),  # Close polygon
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
# --- COPERNICUS SST FETCH ---
# Cached daily — only re-fetches when date changes
_copernicus_cache = {"date": None, "points": []}

async def get_satellite_sst():
    today = date.today().isoformat()
    if _copernicus_cache["date"] == today and _copernicus_cache["points"]:
        return _copernicus_cache["points"]
    
    try:
        username = os.getenv("COPERNICUS_USERNAME")
        password = os.getenv("COPERNICUS_PASSWORD")

        # SST product — Mediterranean/Global L4 analysis
        # OSTIA global product, daily, 0.05 degree resolution
        ds = copernicusmarine.open_dataset(
            dataset_id="SST_GLO_SST_L4_NRT_OBSERVATIONS_010_001",
            username=username,
            password=password,
            minimum_longitude=24.840,
            maximum_longitude=26.360,
            minimum_latitude=-34.300,
            maximum_latitude=-33.700,
            start_datetime=today,
            end_datetime=today,
            variables=["analysed_sst"]
        )

        # Extract SST values — convert from Kelvin to Celsius
        sst_data = ds['analysed_sst'].isel(time=0)
        lats = sst_data.latitude.values
        lons = sst_data.longitude.values
        
        points = []
        for i, lat in enumerate(lats):
            for j, lon in enumerate(lons):
                val = float(sst_data.values[i, j])
                if np.isnan(val):
                    continue
                temp_c = val - 273.15  # Kelvin to Celsius
                if is_ocean(lon, lat):
                    points.append((float(lat), float(lon), temp_c))
        
        _copernicus_cache["date"] = today
        _copernicus_cache["points"] = points
        print(f"✅ Copernicus SST fetched: {len(points)} points")
        return points

    except Exception as e:
        print(f"⚠️ Copernicus fetch failed: {e}")
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

    # --- SATELLITE BASELINE ---
    satellite_points = await get_satellite_sst()

    # --- COMBINE SATELLITE + VESSEL DATA ---
    all_lats = []
    all_lons = []
    all_temps = []

    # Satellite points — base weight 1.0
    for lat, lon, temp in satellite_points:
        all_lats.append(lat)
        all_lons.append(lon)
        all_temps.append(temp)

    # Vessel observations — higher weight, repeat 5x to dominate locally
    # This means real vessel data overrides satellite where vessels have been
    for r in rows:
        for _ in range(5):
            all_lats.append(r['latitude'])
            all_lons.append(r['longitude'])
            all_temps.append(r['sea_surface_temperature'])

    if len(all_lats) < 3:
        return JSONResponse([])

    obs_coords = np.column_stack([all_lats, all_lons])
    obs_temps = np.array(all_temps)

    # Grid across full Algoa Bay
    grid_lats = np.linspace(-34.300, -33.700, 80)
    grid_lons = np.linspace(24.840, 26.360, 80)

    MIN_TEMP, MAX_TEMP = 16.0, 24.0

    interpolator = RBFInterpolator(
        obs_coords, obs_temps,
        kernel='linear',
        smoothing=0.5
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
    diff = stw - sog  # positive = fighting current, negative = riding it
    
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

@app.get("/", response_class=HTMLResponse)
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

        /* ---- LOGIN SCREEN ---- */
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
        .login-field {
            margin-bottom: 16px;
        }
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

        /* ---- MAP SCREEN ---- */
        #app-screen { display: none; height: 100vh; flex-direction: column; }
        #map { flex: 1; background: var(--bg); }

        /* Top bar */
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

        /* Legend */
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
            margin: 0 auto 0 auto;
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

        /* SST info panel top left */
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

        /* Bottom efficiency bar */
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
        .eff-value {
            font-size: 0.85rem;
            font-weight: bold;
            color: white;
        }
        .eff-divider {
            width: 1px; height: 32px;
            background: rgba(255,255,255,0.1);
        }
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

        /* Logout button */
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
    </style>
</head>
<body>

<!-- LOGIN SCREEN -->
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

<!-- APP SCREEN -->
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
                <span>22°</span>
                <span>21°</span>
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
</div>

<script>
// --- STATE ---
let currentVesselId = null;
let map = null;
let heatLayer = null;
let vesselMarker = null;

// --- COLOUR SCALE (temperature → rgba) ---
function tempToColor(intensity) {
    // Blue → Cyan → Green → Yellow → Orange → Red
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

// --- CANVAS OVERLAY (zoom-independent) ---
// Renders the interpolated grid as coloured rectangles on a canvas overlay
// Canvas is redrawn on every map move/zoom — no pixel-bleed artefacts
L.CanvasHeatOverlay = L.Layer.extend({
    _points: [],
    _cellSize: 0.0125, // degrees — matches grid resolution

    setPoints(pts) { this._points = pts; this._redraw(); },

    onAdd(map) {
        this._map = map;
        this._canvas = document.createElement('canvas');
        this._canvas.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;';
        map.getPanes().overlayPane.appendChild(this._canvas);
        map.on('move zoom resize', this._redraw, this);
        this._redraw();
    },

    onRemove(map) {
        map.getPanes().overlayPane.removeChild(this._canvas);
        map.off('move zoom resize', this._redraw, this);
    },

    _redraw() {
        if (!this._map || !this._points.length) return;
        const size = this._map.getSize();
        this._canvas.width  = size.x;
        this._canvas.height = size.y;
        const ctx = this._canvas.getContext('2d');
        ctx.clearRect(0, 0, size.x, size.y);

        const bounds = this._map.getBounds();
        const topLeft = this._map.latLngToContainerPoint(bounds.getNorthWest());

        for (const [lat, lon, intensity] of this._points) {
            // Convert grid cell corners to pixels
            const pxNW = this._map.latLngToContainerPoint([lat + this._cellSize/2, lon - this._cellSize/2]);
            const pxSE = this._map.latLngToContainerPoint([lat - this._cellSize/2, lon + this._cellSize/2]);
            const w = Math.ceil(pxSE.x - pxNW.x) + 1;
            const h = Math.ceil(pxSE.y - pxNW.y) + 1;
            const [r,g,b] = tempToColor(intensity);
            ctx.fillStyle = `rgba(${r},${g},${b},0.72)`;
            ctx.fillRect(pxNW.x, pxNW.y, w, h);
        }
    }
});

// --- LOGIN ---
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

// --- APP INIT ---
function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    const appEl = document.getElementById('app-screen');
    appEl.style.display = 'flex';
    appEl.style.position = 'relative';

    if (!map) {
        map = L.map('map', { zoomControl: true, attributionControl: false })
                .setView([-34.0, 25.85], 10);
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);
        heatLayer = new L.CanvasHeatOverlay().addTo(map);
    }

    startUpdates();
}

// --- UPDATE LOOPS ---
function startUpdates() {
    updateHeatmap();
    updateVessel();
    setInterval(updateHeatmap, 30000);
    setInterval(updateVessel, 10000);
}

async function updateHeatmap() {
    try {
        const res = await fetch('/api/interpolated');
        const points = await res.json();
        if (!points.length) return;

        heatLayer.setPoints(points);

        const MIN_TEMP = 16.0, MAX_TEMP = 22.0;
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

        // Update vessel marker
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

        // Update efficiency bar
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
