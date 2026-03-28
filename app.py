import os, uuid
import numpy as np
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

# --- VALID API KEYS ---
VALID_API_KEYS = {"2026_Reeliq_dev18"}

# --- ALGOA BAY OCEAN POLYGON ---
# Manually traced coastline polygon — only ocean cells pass through
# Points go anticlockwise around the bay ocean area
ALGOA_BAY_POLYGON = Polygon([
    (25.400, -34.180),  # Jeffreys Bay offshore SW
    (25.400, -33.850),  # Jeffreys Bay offshore NW
    (25.700, -33.780),  # Cape Recife area
    (25.900, -33.750),  # PE coast
    (26.100, -33.780),  # East of PE
    (26.300, -33.850),  # Eastern bay
    (26.300, -34.180),  # Eastern offshore SE
    (25.400, -34.180),  # Close polygon
])

def is_ocean(lon, lat):
    """Returns True if point is inside Algoa Bay ocean polygon."""
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

@app.get("/api/heatmap")
async def get_heatmap():
    """Raw vessel points for simple heat map — kept as fallback."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=10)
    query = observations_table.select().where(
        observations_table.c.timestamp >= cutoff
    )
    rows = await database.fetch_all(query)
    MIN_TEMP, MAX_TEMP = 16.0, 22.0
    points = []
    for r in rows:
        intensity = max(0.0, min(1.0, (r['sea_surface_temperature'] - MIN_TEMP) / (MAX_TEMP - MIN_TEMP)))
        points.append([r['latitude'], r['longitude'], intensity])
    return JSONResponse(points)

@app.get("/api/interpolated")
async def get_interpolated():
    """
    IDW interpolation across full Algoa Bay grid.
    Returns array of [lat, lon, intensity] covering entire bay,
    masked to ocean only.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=15)
    query = observations_table.select().where(
        observations_table.c.timestamp >= cutoff
    )
    rows = await database.fetch_all(query)

    if len(rows) < 3:
        return JSONResponse([])

    # Extract observation points
    obs_lats = np.array([r['latitude'] for r in rows])
    obs_lons = np.array([r['longitude'] for r in rows])
    obs_temps = np.array([r['sea_surface_temperature'] for r in rows])

    # Build grid across Algoa Bay
    # 40x40 grid = 1600 points, good balance of resolution vs speed
    grid_lats = np.linspace(-34.180, -33.750, 40)
    grid_lons = np.linspace(25.400, 26.300, 40)

    MIN_TEMP, MAX_TEMP = 16.0, 22.0

    # RBF interpolation (more accurate than basic IDW)
    # Uses observation points to estimate temperature everywhere
    obs_coords = np.column_stack([obs_lats, obs_lons])
    interpolator = RBFInterpolator(
        obs_coords,
        obs_temps,
        kernel='linear',
        smoothing=0.1
    )

    # Evaluate grid and mask to ocean only
    result = []
    for lat in grid_lats:
        for lon in grid_lons:
            if not is_ocean(lon, lat):
                continue  # Skip land cells
            temp = float(interpolator([[lat, lon]])[0])
            intensity = max(0.0, min(1.0, (temp - MIN_TEMP) / (MAX_TEMP - MIN_TEMP)))
            result.append([round(lat, 4), round(lon, 4), round(intensity, 3)])

    return JSONResponse(result)

@app.get("/api/vessel/{vessel_id}")
async def get_vessel(vessel_id: str, api_key: str = Query(...)):
    """Returns latest observation for a single vessel only."""
    if api_key not in VALID_API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(hours=1)
    query = observations_table.select().where(
        observations_table.c.vessel_id == vessel_id,
        observations_table.c.timestamp >= cutoff
    ).order_by(observations_table.c.timestamp.desc()).limit(1)
    row = await database.fetch_one(query)
    if not row:
        raise HTTPException(status_code=404, detail="Vessel not found")
    return {
        "vessel_id": vessel_id,
        "latitude": row['latitude'],
        "longitude": row['longitude'],
        "sea_surface_temperature": row['sea_surface_temperature'],
        "speed_over_ground": row['speed_over_ground'],
        "speed_through_water": row['speed_through_water'],
        "timestamp": row['timestamp'].isoformat()
    }

@app.get("/", response_class=HTMLResponse)
async def get_map():
    html_content = """
    <html>
        <head>
            <title>REEL IQ | Adaptive Thermal</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script src="https://leaflet.github.io/Leaflet.heat/dist/leaflet-heat.js"></script>
            <style>
                body { margin: 0; background: #06090f; font-family: sans-serif; overflow: hidden; }
                #map { height: 100vh; width: 100%; background: #06090f; }
                #overlay {
                    position: absolute; top: 20px; left: 60px; z-index: 1000;
                    background: rgba(0,18,25,0.9); padding: 15px; border-radius: 8px;
                    border: 1px solid #00f2ff; color: #00f2ff; min-width: 220px;
                }
                #legend {
                    position: absolute; bottom: 30px; right: 20px; z-index: 1000;
                    background: rgba(0,18,25,0.9); padding: 12px; border-radius: 8px;
                    border: 1px solid #333;
                }
                .gradient-bar {
                    height: 200px; width: 15px;
                    background: linear-gradient(to top, #0000ff, #00ffff, #00ff00, #ffff00, #ff8000, #ff0000);
                    border-radius: 3px;
                }
            </style>
        </head>
        <body>
            <div id="overlay">
                <b>REEL IQ | ADAPTIVE THERMAL</b><br><br>
                <small id="range" style="color:white;">Loading...</small><br>
                <small id="points" style="font-size:9px; opacity:0.7;"></small><br>
                <small id="updated" style="font-size:9px; opacity:0.5;"></small>
            </div>

            <div id="legend">
                <div style="display:flex; align-items:flex-end;">
                    <div class="gradient-bar"></div>
                    <div style="margin-left:10px; display:flex; flex-direction:column;
                                justify-content:space-between; height:200px;
                                font-size:11px; color:white; font-weight:bold;">
                        <span>22°C</span>
                        <span>21°C</span>
                        <span>20°C</span>
                        <span>19°C</span>
                        <span>18°C</span>
                        <span>16°C</span>
                    </div>
                </div>
            </div>

            <div id="map"></div>

            <script>
                var map = L.map('map', {
                    zoomControl: true,
                    attributionControl: false
                }).setView([-34.0, 25.85], 10);

                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);

                // Interpolated full-bay heat layer
                var heatLayer = L.heatLayer([], {
                    radius: 30,
                    blur: 20,
                    max: 1.0,
                    gradient: {
                        0.0: '#0000ff',
                        0.2: '#00ffff',
                        0.4: '#00ff00',
                        0.6: '#ffff00',
                        0.8: '#ff8000',
                        1.0: '#ff0000'
                    }
                }).addTo(map);

                async function update() {
                    try {
                        const res = await fetch('/api/interpolated');
                        const points = await res.json();

                        if (points.length === 0) {
                            document.getElementById('range').innerText = 'Waiting for data...';
                            return;
                        }

                        heatLayer.setLatLngs(points);

                        // Recalculate temp range from intensities
                        const MIN_TEMP = 16.0;
                        const MAX_TEMP = 22.0;
                        const temps = points.map(p => p[2] * (MAX_TEMP - MIN_TEMP) + MIN_TEMP);
                        const minT = Math.min(...temps).toFixed(1);
                        const maxT = Math.max(...temps).toFixed(1);

                        document.getElementById('range').innerText = `SST Range: ${minT}° - ${maxT}°C`;
                        document.getElementById('points').innerText = `Grid points: ${points.length}`;
                        document.getElementById('updated').innerText =
                            `Updated: ${new Date().toLocaleTimeString()}`;

                    } catch(e) {
                        console.error(e);
                        document.getElementById('range').innerText = 'Connection error';
                    }
                }

                // Update every 30 seconds — interpolation is heavier than raw points
                setInterval(update, 30000);
                update();
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)


