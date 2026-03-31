import os, uuid, json, re
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

# --- NEW COASTLINE ENDPOINT ---
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
async def get_interpolated():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=15)
    query = observations_table.select().where(observations_table.c.timestamp >= cutoff)
    rows = await database.fetch_all(query)
    satellite_points = await get_satellite_sst()

    all_lats, all_lons, all_temps = [], [], []
    for lat, lon, temp in satellite_points:
        all_lats.append(lat); all_lons.append(lon); all_temps.append(temp)
    for r in rows:
        for _ in range(3):
            all_lats.append(r['latitude']); all_lons.append(r['longitude']); all_temps.append(r['sea_surface_temperature'])

    if len(all_lats) < 3: return JSONResponse([])

    obs_coords = np.column_stack([all_lats, all_lons])
    obs_temps = np.array(all_temps)
    grid_lats = np.linspace(-34.300, -33.700, 60)
    grid_lons = np.linspace(24.840, 26.360, 60)

    interpolator = RBFInterpolator(obs_coords, obs_temps, kernel='thin_plate_spline', smoothing=1.5)

    result = []
    for lat in grid_lats:
        for lon in grid_lons:
            if not is_ocean(lon, lat): continue
            temp = float(interpolator([[lat, lon]])[0])
            intensity = max(0.0, min(1.0, (temp - 16.0) / (24.0 - 16.0)))
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
        "vessel_id": vessel_id, "latitude": row['latitude'], "longitude": row['longitude'],
        "sea_surface_temperature": row['sea_surface_temperature'], "speed_over_ground": sog,
        "speed_through_water": stw, "efficiency": eff, "efficiency_label": label,
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
            font-size: 2rem; font-weight: 900;
            color: var(--cyan); letter-spacing: 0.15em;
            margin-bottom: 4px;
            text-shadow: 0 0 30px rgba(0,242,255,0.5);
        }
        .login-sub {
            font-size: 0.7rem; color: rgba(0,242,255,0.5);
            letter-spacing: 0.2em; margin-bottom: 40px;
            text-transform: uppercase;
        }
        .login-field { margin-bottom: 16px; }
        .login-field label {
            display: block; font-size: 0.65rem;
            letter-spacing: 0.2em; color: rgba(0,242,255,0.6);
            margin-bottom: 8px; text-transform: uppercase;
        }
        .login-field input {
            width: 100%; background: rgba(0,242,255,0.04);
            border: 1px solid var(--cyan-border); border-radius: 2px;
            padding: 12px 16px; color: var(--cyan);
            font-family: 'Share Tech Mono', monospace; font-size: 0.95rem;
            outline: none; transition: border-color 0.2s, background 0.2s;
        }
        .login-field input:focus { border-color: var(--cyan); background: rgba(0,242,255,0.08); }
        .login-btn {
            width: 100%; margin-top: 24px; padding: 14px;
            background: transparent; border: 1px solid var(--cyan);
            border-radius: 2px; color: var(--cyan);
            font-family: 'Orbitron', sans-serif; font-size: 0.8rem;
            font-weight: 700; letter-spacing: 0.2em; cursor: pointer;
            text-transform: uppercase; transition: background 0.2s, box-shadow 0.2s;
        }
        .login-btn:hover { background: var(--cyan-dim); box-shadow: 0 0 20px rgba(0,242,255,0.2); }
        .login-error {
            margin-top: 12px; font-size: 0.7rem; color: var(--bad);
            text-align: center; letter-spacing: 0.1em; min-height: 16px;
        }
        .login-demo {
            margin-top: 20px; font-size: 0.65rem;
            color: rgba(255,255,255,0.2); text-align: center; letter-spacing: 0.1em;
        }
        #app-screen { display: none; height: 100vh; flex-direction: column; position: relative; }
        #map { flex: 1; background: var(--bg); }
        #topbar {
            position: absolute; top: 0; left: 0; right: 0; z-index: 1000;
            display: flex; align-items: center; justify-content: space-between;
            padding: 12px 16px;
            background: linear-gradient(to bottom, rgba(4,8,15,0.95), transparent);
            pointer-events: none;
        }
        .topbar-logo {
            font-family: 'Orbitron', sans-serif; font-size: 1rem; font-weight: 900;
            color: var(--cyan); letter-spacing: 0.15em;
            text-shadow: 0 0 15px rgba(0,242,255,0.4);
        }
        .topbar-status { font-size: 0.65rem; color: rgba(0,242,255,0.5); letter-spacing: 0.12em; text-align: right; }
        #live-dot {
            display: inline-block; width: 6px; height: 6px; border-radius: 50%;
            background: var(--good); margin-right: 6px; animation: pulse 1.5s infinite;
        }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        #legend {
            position: absolute; top: 60px; right: 16px; z-index: 1000;
            background: var(--panel); border: 1px solid rgba(255,255,255,0.08);
            border-radius: 4px; padding: 12px 10px;
        }
        .legend-bar {
            width: 12px; height: 160px;
            background: linear-gradient(to top, #0000ff 0%, #00ffff 25%, #00ff88 45%, #ffff00 65%, #ff8800 82%, #ff0000 100%);
            border-radius: 2px;
        }
        .legend-labels {
            display: flex; flex-direction: column; justify-content: space-between;
            height: 160px; font-size: 0.6rem; color: rgba(255,255,255,0.5); margin-left: 6px;
        }
        .legend-row { display: flex; align-items: flex-start; gap: 6px; }
        .legend-title { font-size: 0.55rem; color: rgba(255,255,255,0.3); letter-spacing: 0.1em; text-align: center; margin-bottom: 6px; }
        #sst-panel {
            position: absolute; top: 60px; left: 16px; z-index: 1000;
            background: var(--panel); border: 1px solid var(--cyan-border);
            border-radius: 4px; padding: 12px 16px; min-width: 180px;
        }
        .panel-label { font-size: 0.6rem; color: rgba(0,242,255,0.5); letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 4px; }
        .panel-value { font-size: 1.1rem; color: var(--cyan); font-weight: bold; }
        .panel-sub { font-size: 0.6rem; color: rgba(255,255,255,0.3); margin-top: 2px; }
        #efficiency-bar {
            position: absolute; bottom: 0; left: 0; right: 0; z-index: 1000;
            background: var(--panel); border-top: 1px solid rgba(255,255,255,0.08);
            padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; gap: 16px;
        }
        .eff-block { display: flex; flex-direction: column; align-items: center; flex: 1; }
        .eff-label { font-size: 0.55rem; color: rgba(255,255,255,0.3); letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 2px; }
        .eff-value { font-size: 0.85rem; font-weight: bold; color: white; }
        .eff-divider { width: 1px; height: 32px; background: rgba(255,255,255,0.1); }
        #eff-status { font-size: 0.75rem; font-weight: bold; text-align: center; flex: 2; letter-spacing: 0.05em; }
        #eff-status.good { color: var(--good); }
        #eff-status.poor { color: var(--bad); }
        #eff-status.neutral { color: var(--warn); }
        #logout-btn {
            position: absolute; bottom: 68px; right: 16px; z-index: 1001;
            background: transparent; border: 1px solid rgba(255,255,255,0.15);
            border-radius: 2px; color: rgba(255,255,255,0.3);
            font-family: 'Share Tech Mono', monospace; font-size: 0.6rem;
            letter-spacing: 0.1em; padding: 6px 10px; cursor: pointer;
        }
        #logout-btn:hover { color: var(--bad); border-color: var(--bad); }
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
    <button id="logout-btn" onclick="doLogout()">LOGOUT</button>
</div>

<script>
let currentVesselId = null;
let map = null;
let heatLayer = null;
let vesselMarker = null;
let oceanPolygonCoords = null; 

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
    _polygonCoords: null, 

    setPoints(pts) { this._points = pts; this._redraw(); },
    setPolygon(coords) { this._polygonCoords = coords; this._redraw(); },

    onAdd(map) {
        this._map = map;
        this._canvas = document.createElement('canvas');
        this._canvas.style.cssText = 'position:absolute;pointer-events:none;';
        map.getPanes().overlayPane.appendChild(this._canvas);
        map.on('move zoom resize', this._redraw, this);
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
        
        // --- COASTLINE CLIPPING FIX ---
        if (this._polygonCoords) {
            ctx.beginPath();
            this._polygonCoords.forEach(([lon, lat], i) => {
                const pt = this._map.latLngToLayerPoint([lat, lon]);
                const x = pt.x - origin.x;
                const y = pt.y - origin.y;
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.closePath();
            ctx.clip();
        }

        for (const [lat, lon, intensity] of this._points) {
            const pxNW = this._map.latLngToLayerPoint([lat + this._cellSize/2, lon - this._cellSize/2]);
            const pxSE = this._map.latLngToLayerPoint([lat - this._cellSize/2, lon + this._cellSize/2]);
            const x = pxNW.x - origin.x;
            const y = pxNW.y - origin.y;
            const w = Math.ceil(pxSE.x - pxNW.x) + 1;
            const h = Math.ceil(pxSE.y - pxNW.y) + 1;
            const [r,g,b] = tempToColor(intensity);
            ctx.filter = 'blur(3px)';
            ctx.fillStyle = `rgba(${r},${g},${b},0.65)`;
            ctx.fillRect(x, y, w, h);
        }

        ctx.restore();
        ctx.filter = 'none';
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
        if (!res.ok) { err.textContent = 'ACCESS DENIED'; return; }
        const data = await res.json();
        currentVesselId = data.vessel_id;
        showApp();
    } catch(e) { err.textContent = 'CONNECTION ERROR'; }
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
            // Extracts the first available polygon ring from the Ocean GeoJSON
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
        const res = await fetch('/api/interpolated');
        const points = await res.json();
        if (!points.length) return;
        heatLayer.setPoints(points);
        const MIN_TEMP = 16.0, MAX_TEMP = 24.0;
        const temps = points.map(p => p[2] * (MAX_TEMP - MIN_TEMP) + MIN_TEMP);
        document.getElementById('sst-range').textContent = `${Math.min(...temps).toFixed(1)}° — ${Math.max(...temps).toFixed(1)}°C`;
        document.getElementById('grid-points').textContent = `${points.length} grid cells`;
        document.getElementById('update-time').textContent = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    } catch(e) { console.error(e); }
}

async function updateVessel() {
    if (!currentVesselId) return;
    try {
        const res = await fetch(`/api/vessel/${currentVesselId}`);
        if (!res.ok) return;
        const d = await res.json();
        const pos = [d.latitude, d.longitude];
        if (!vesselMarker) {
            vesselMarker = L.circleMarker(pos, { radius: 8, fillColor: '#00f2ff', color: '#ffffff', weight: 2, fillOpacity: 0.95 }).addTo(map);
        } else { vesselMarker.setLatLng(pos); }
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
