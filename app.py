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

class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latitude: float
    longitude: float
    sea_surface_temperature: float
    speed_over_ground: float
    speed_through_water: float

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
        return points
    except Exception:
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

@app.get("/api/coastline")
async def get_coastline():
    try:
        with open("coastline.geojson", "r") as f:
            return JSONResponse(json.load(f))
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "file not found"})

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
    <title>REEL IQ</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        :root { --cyan: #00f2ff; --bg: #04080f; --panel: rgba(4,12,22,0.92); --text: #cce8ff; --good: #00ff88; --bad: #ff3b3b; --warn: #ffb800; }
        body { background: var(--bg); font-family: 'Share Tech Mono', monospace; color: var(--text); overflow: hidden; height: 100vh; margin:0; }
        #app-screen { display: none; height: 100vh; flex-direction: column; position: relative; }
        #map { flex: 1; }
        #login-screen { position: fixed; inset: 0; z-index: 9999; background: var(--bg); display: flex; align-items: center; justify-content: center; flex-direction: column; }
        .login-box { padding: 40px; border: 1px solid rgba(0,242,255,0.4); background: var(--panel); border-radius: 4px; }
        .login-btn { width: 100%; margin-top: 20px; padding: 10px; background: transparent; border: 1px solid var(--cyan); color: var(--cyan); cursor: pointer; }
        #efficiency-bar { position: absolute; bottom: 0; left: 0; right: 0; z-index: 1000; background: var(--panel); padding: 12px; display: flex; justify-content: space-around; }
    </style>
</head>
<body>
<div id="login-screen">
    <div class="login-box">
        <h2 style="font-family:Orbitron; color:var(--cyan)">REEL IQ</h2>
        <input type="text" id="username" placeholder="Username" style="display:block; margin: 10px 0; padding:8px; width:100%"/>
        <input type="password" id="password" placeholder="Password" style="display:block; margin: 10px 0; padding:8px; width:100%"/>
        <button class="login-btn" onclick="doLogin()">ACCESS</button>
        <div id="login-error" style="color:var(--bad); font-size:12px; margin-top:10px"></div>
    </div>
</div>
<div id="app-screen">
    <div id="map"></div>
    <div id="efficiency-bar">
        <div id="sog-val">SOG: —</div>
        <div id="eff-status">Loading...</div>
        <div id="stw-val">STW: —</div>
    </div>
</div>
<script>
let currentVesselId, map, heatLayer, vesselMarker;

L.CanvasHeatOverlay = L.Layer.extend({
    _points: [], _cellSize: 0.027, _polygonCoords: null,
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
        if (!this._map || !this._canvas) return;
        const size = this._map.getSize();
        this._canvas.width = size.x; this._canvas.height = size.y;
        const topLeft = this._map.getBounds().getNorthWest();
        const origin = this._map.latLngToLayerPoint(topLeft);
        this._canvas.style.left = origin.x + 'px'; this._canvas.style.top = origin.y + 'px';
        const ctx = this._canvas.getContext('2d');
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
        this._points.forEach(([lat, lon, intensity]) => {
            const p = this._map.latLngToLayerPoint([lat, lon]);
            ctx.fillStyle = `rgba(255, 0, 0, ${intensity})`;
            ctx.fillRect(p.x - origin.x, p.y - origin.y, 10, 10);
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
        showApp();
    } else { document.getElementById('login-error').textContent = 'Invalid login'; }
}

async function showApp() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('app-screen').style.display = 'flex';
    if (!map) {
        map = L.map('map').setView([-34.0, 25.85], 10);
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);
        heatLayer = new L.CanvasHeatOverlay().addTo(map);
        try {
            const res = await fetch('/api/coastline');
            const geojson = await res.json();
            const coords = geojson.features.flatMap(f => 
                f.geometry.type === 'Polygon' ? f.geometry.coordinates : 
                f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates[0] : []
            );
            if (coords.length) heatLayer.setPolygon(coords[0]);
        } catch(e) { console.error('Clip load error', e); }
    }
    updateLoop();
}

function updateLoop() {
    updateHeatmap(); setInterval(updateHeatmap, 30000);
}

async function updateHeatmap() {
    const res = await fetch('/api/interpolated');
    const points = await res.json();
    if (points.length) heatLayer.setPoints(points);
}
</script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
