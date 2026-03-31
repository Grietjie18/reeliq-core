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
    "demo":        ("demo",      "demo"),
}
VALID_API_KEYS = {"2026_Reeliq_dev18"}

# --- OCEAN BOUNDARY ---
ALGOA_BAY_COORDS = [
    (24.869, -34.300), (24.869, -34.195), (24.841, -34.145), (24.912, -34.085),
    (25.034, -33.970), (25.213, -33.969), (25.402, -34.034), (25.584, -34.048),
    (25.700, -34.029), (25.644, -33.955), (25.632, -33.865), (25.830, -33.727),
    (26.352, -33.760), (26.352, -34.300), (24.869, -34.300),
]
ALGOA_BAY_POLYGON = Polygon(ALGOA_BAY_COORDS)

def is_ocean(lon, lat):
    return ALGOA_BAY_POLYGON.contains(Point(lon, lat))

class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    latitude: float
    longitude: float
    sea_surface_temperature: float
    speed_over_ground: float
    speed_through_water: float

@app.on_event("startup")
async def startup():
    await database.connect()
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

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
        return JSONResponse(status_code=404, content={"error": "file missing"})

@app.get("/api/interpolated")
async def get_interpolated():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=15)
    query = observations_table.select().where(observations_table.c.timestamp >= cutoff)
    rows = await database.fetch_all(query)
    
    if len(rows) < 3: return JSONResponse([])

    obs_coords = np.column_stack([[r['latitude'] for r in rows], [r['longitude'] for r in rows]])
    obs_temps = np.array([r['sea_surface_temperature'] for r in rows])
    
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
    if not row: raise HTTPException(status_code=404)
    return row

@app.get("/", response_class=HTMLResponse)
async def get_map():
    return r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>REEL IQ</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        :root {
            --brand-yellow: #ffb800;
            --brand-yellow-dim: rgba(255, 184, 0, 0.1);
            --bg: #0b1a2a;
            --panel: rgba(11, 26, 42, 0.95);
            --text: #ffffff;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: var(--bg); font-family: 'Share Tech Mono', monospace; color: var(--text); overflow: hidden; height: 100vh; }
        
        #login-screen { position: fixed; inset: 0; z-index: 9999; background: var(--bg); display: flex; align-items: center; justify-content: center; flex-direction: column; }
        .login-box { width: min(400px, 90vw); padding: 40px; border: 1px solid var(--brand-yellow-dim); background: var(--panel); border-radius: 4px; text-align: center; }
        .login-logo { font-family: 'Orbitron'; font-size: 2rem; color: var(--brand-yellow); margin-bottom: 30px; letter-spacing: 0.2em; }
        .login-field input { width: 100%; padding: 12px; margin-bottom: 15px; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,184,0,0.2); color: white; font-family: inherit; }
        .login-btn { width: 100%; padding: 14px; background: var(--brand-yellow); border: none; font-family: 'Orbitron'; font-weight: 900; cursor: pointer; color: #000; }
        
        #app-screen { display: none; height: 100vh; flex-direction: column; position: relative; }
        #map { flex: 1; background: #000; }
        
        #sst-panel { position: absolute; top: 20px; left: 20px; z-index: 1000; background: var(--panel); border-left: 3px solid var(--brand-yellow); padding: 12px 16px; }
        .panel-label { font-size: 0.6rem; color: var(--brand-yellow); letter-spacing: 0.1em; margin-bottom: 4px; }
        .panel-value { font-size: 1.2rem; font-weight: bold; }
        
        #efficiency-bar { position: absolute; bottom: 0; left: 0; right: 0; z-index: 1000; background: var(--panel); padding: 15px; display: flex; justify-content: space-around; border-top: 1px solid rgba(255,255,255,0.1); }
        .eff-val { font-weight: bold; color: var(--brand-yellow); }
    </style>
</head>
<body>

<div id="login-screen">
    <div class="login-box">
        <div class="login-logo">REEL IQ</div>
        <div class="login-field"><input type="text" id="username" placeholder="USERNAME"></div>
        <div class="login-field"><input type="password" id="password" placeholder="PASSWORD"></div>
        <button class="login-btn" onclick="doLogin()">ACCESS SYSTEM</button>
    </div>
</div>

<div id="app-screen">
    <div id="sst-panel">
        <div class="panel-label">SST RANGE</div>
        <div class="panel-value" id="sst-range">--°C</div>
    </div>
    <div id="map"></div>
    <div id="efficiency-bar">
        <div>SOG: <span id="sog-val" class="eff-val">--</span></div>
        <div id="eff-status">Searching...</div>
        <div>STW: <span id="stw-val" class="eff-val">--</span></div>
    </div>
</div>

<script>
let map, heatLayer, currentVesselId;

function tempToColor(intensity) {
    const stops = [
        [0, [0, 0, 255]], [0.2, [0, 255, 255]], [0.45, [0, 255, 136]],
        [0.65, [255, 255, 0]], [0.82, [255, 136, 0]], [1.0, [255, 0, 0]]
    ];
    let lower = stops[0], upper = stops[stops.length-1];
    for (let i = 0; i < stops.length - 1; i++) {
        if (intensity >= stops[i][0] && intensity <= stops[i+1][0]) {
            lower = stops[i]; upper = stops[i+1]; break;
        }
    }
    const t = (intensity - lower[0]) / (upper[0] - lower[0]);
    return [
        Math.round(lower[1][0] + t * (upper[1][0] - lower[1][0])),
        Math.round(lower[1][1] + t * (upper[1][1] - lower[1][1])),
        Math.round(lower[1][2] + t * (upper[1][2] - lower[1][2]))
    ];
}

L.CanvasHeatOverlay = L.Layer.extend({
    _points: [], _cellSize: 0.027, _poly: null,
    setPoints(pts) { this._points = pts; this._redraw(); },
    setPolygon(coords) { this._poly = coords; this._redraw(); },
    onAdd(map) {
        this._map = map;
        this._canvas = document.createElement('canvas');
        this._canvas.style.position = 'absolute';
        map.getPanes().overlayPane.appendChild(this._canvas);
        map.on('move zoom resize', this._redraw, this);
        this._redraw();
    },
    _redraw() {
        if (!this._map || !this._points.length) return;
        const size = this._map.getSize();
        const origin = this._map.latLngToLayerPoint(this._map.getBounds().getNorthWest());
        this._canvas.width = size.x; this._canvas.height = size.y;
        this._canvas.style.left = origin.x + 'px'; this._canvas.style.top = origin.y + 'px';
        const ctx = this._canvas.getContext('2d');
        ctx.clearRect(0, 0, size.x, size.y);

        ctx.save();
        if (this._poly) {
            ctx.beginPath();
            this._poly.forEach(([lon, lat], i) => {
                const pt = this._map.latLngToLayerPoint([lat, lon]);
                if (i === 0) ctx.moveTo(pt.x - origin.x, pt.y - origin.y);
                else ctx.lineTo(pt.x - origin.x, pt.y - origin.y);
            });
            ctx.clip();
        }

        this._points.forEach(([lat, lon, intensity]) => {
            const nw = this._map.latLngToLayerPoint([lat + 0.0135, lon - 0.0135]);
            const se = this._map.latLngToLayerPoint([lat - 0.0135, lon + 0.0135]);
            const [r, g, b] = tempToColor(intensity);
            ctx.fillStyle = `rgba(${r},${g},${b},0.7)`;
            ctx.filter = 'blur(4px)';
            ctx.fillRect(nw.x - origin.x, nw.y - origin.y, se.x - nw.x, se.y - nw.y);
        });
        ctx.restore();
    }
});

async function doLogin() {
    const u = document.getElementById('username').value;
    const p = document.getElementById('password').value;
    const res = await fetch(`/api/login?username=${u}&password=${p}`, {method:'POST'});
    if (res.ok) {
        const data = await res.json();
        currentVesselId = data.vessel_id;
        document.getElementById('login-screen').style.display = 'none';
        document.getElementById('app-screen').style.display = 'flex';
        initMap();
    }
}

async function initMap() {
    map = L.map('map').setView([-34.0, 25.85], 10);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);
    heatLayer = new L.CanvasHeatOverlay().addTo(map);

    const cRes = await fetch('/api/coastline');
    const geo = await cRes.json();
    heatLayer.setPolygon(geo.features[0].geometry.coordinates[0]);

    setInterval(updateHeatmap, 30000);
    setInterval(updateVessel, 10000);
    updateHeatmap(); updateVessel();
}

async function updateHeatmap() {
    const res = await fetch('/api/interpolated');
    const pts = await res.json();
    if (pts.length) {
        heatLayer.setPoints(pts);
        const temps = pts.map(p => p[2] * 8 + 16);
        document.getElementById('sst-range').textContent = `${Math.min(...temps).toFixed(1)}° - ${Math.max(...temps).toFixed(1)}°C`;
    }
}

async function updateVessel() {
    if (!currentVesselId) return;
    const res = await fetch(`/api/vessel/${currentVesselId}`);
    if (res.ok) {
        const d = await res.json();
        document.getElementById('sog-val').textContent = d.speed_over_ground.toFixed(1) + ' kts';
        document.getElementById('stw-val').textContent = d.speed_through_water.toFixed(1) + ' kts';
    }
}
</script>
</body>
</html>
"""
