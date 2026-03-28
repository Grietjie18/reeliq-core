import os, uuid
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime, Integer

# --- 1. DATA SCHEMA & DB ---
class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latitude: float
    longitude: float
    sea_surface_temperature: float 
    speed_over_ground: float
    speed_through_water: float

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MASTER_API_KEY = os.getenv("MASTER_API_KEY", "jbay-science-2026")
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

app = FastAPI()

@app.on_event("startup")
async def startup():
    await database.connect()
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, data: Observation, api_key: str = Query(...)):
    if api_key != MASTER_API_KEY: raise HTTPException(status_code=401)
    query = observations_table.insert().values(
        observation_id=str(data.observation_id), vessel_id=vessel_id,
        timestamp=data.timestamp.replace(tzinfo=None), latitude=data.latitude, 
        longitude=data.longitude, sea_surface_temperature=data.sea_surface_temperature, 
        speed_over_ground=data.speed_over_ground, speed_through_water=data.speed_through_water
    )
    await database.execute(query)
    return {"status": "success"}

@app.get("/api/live")
async def get_live_json():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    time_limit = now_utc - timedelta(hours=12)
    query = observations_table.select().where(observations_table.c.timestamp >= time_limit).order_by(observations_table.c.timestamp.asc())
    rows = await database.fetch_all(query)
    
    vessels = {}
    for r in rows:
        v_id = r['vessel_id']
        if v_id not in vessels: vessels[v_id] = {"path": [], "temps": []}
        vessels[v_id]["path"].append([r['latitude'], r['longitude']])
        vessels[v_id]["temps"].append(r['sea_surface_temperature'])
    return vessels

@app.get("/", response_class=HTMLResponse)
async def get_map():
    html_content = """
    <html>
        <head>
            <title>REEL IQ | Skipper Command</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script src="https://leaflet.github.io/Leaflet.heat/dist/leaflet-heat.js"></script>
            <style>
                body { margin: 0; background: #06090f; font-family: 'Helvetica', sans-serif; overflow: hidden; }
                #map { height: 100vh; width: 100%; }
                #overlay { 
                    position: absolute; top: 20px; left: 20px; z-index: 1000; 
                    background: rgba(0,18,25,0.95); padding: 15px; border-radius: 10px; 
                    border: 1px solid #00f2ff; color: #00f2ff; width: 180px;
                }
                #legend {
                    position: absolute; bottom: 30px; right: 20px; z-index: 1000;
                    background: rgba(0,18,25,0.9); padding: 12px; border-radius: 8px;
                    border: 1px solid #333; color: white; font-size: 11px;
                }
                .gradient-bar {
                    height: 120px; width: 12px; 
                    background: linear-gradient(to top, #0000ff, #00ffff, #00ff00, #ffff00, #ff0000);
                    margin-bottom: 5px; border-radius: 2px;
                }
                .trend-marker { font-weight: bold; font-size: 24px; text-shadow: 0 0 4px #000; cursor: help; }
                .leaflet-heatmap-layer { mix-blend-mode: screen; opacity: 0.8; }
            </style>
        </head>
        <body>
            <div id="overlay">
                <b>REEL IQ COMMAND</b><br>
                <small id="status">Syncing Sensors...</small>
                <div style="margin-top:8px; font-size: 10px; border-top: 1px solid #333; padding-top:5px;">
                    <span style="color:#ff4d4d">▲ Warming</span><br>
                    <span style="color:#4dffff">▼ Cooling</span>
                </div>
            </div>

            <div id="legend">
                <div style="display: flex; flex-direction: row; align-items: flex-end;">
                    <div class="gradient-bar"></div>
                    <div style="margin-left: 10px; display: flex; flex-direction: column; justify-content: space-between; height: 120px;">
                        <span>22.0°C</span><span>20.5°C</span><span>19.0°C</span><span>17.5°C</span><span>16.0°C</span>
                    </div>
                </div>
                <center style="margin-top:8px; font-weight:bold; color:#00f2ff; font-size:10px;">TEMP GRADIENT</center>
            </div>

            <div id="map"></div>

            <script>
                var map = L.map('map', { zoomControl: false, attributionControl: false }).setView([-34.14, 25.02], 11);
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);

                var heatLayer = L.heatLayer([], {
                    radius: 35,
                    blur: 35,
                    max: 0.6,
                    minOpacity: 0.2,
                    gradient: { 0.0: 'blue', 0.25: 'cyan', 0.5: 'lime', 0.75: 'yellow', 1.0: 'red' }
                }).addTo(map);

                var trendGroup = L.layerGroup().addTo(map);

                async function updateMap() {
                    try {
                        const response = await fetch('/api/live');
                        const data = await response.json();
                        let points = [];
                        trendGroup.clearLayers();
                        
                        for (const [v_id, info] of Object.entries(data)) {
                            let lastT = info.temps[info.temps.length - 1];
                            // Compare current temp to roughly 1 hour ago (or first ping)
                            let prevT = info.temps.length > 6 ? info.temps[info.temps.length - 6] : info.temps[0];
                            let lastLoc = info.path[info.path.length - 1];

                            // Map 0.5 degree steps: 16C to 22C
                            let intensity = (lastT - 16) / 6; 
                            intensity = Math.min(Math.max(intensity, 0.05), 1.0);

                            info.path.forEach(coord => {
                                for (let i = 0; i < 5; i++) {
                                    let latJ = (Math.random() - 0.5) * 0.05; 
                                    let lonJ = (Math.random() - 0.5) * 0.05;
                                    points.push([coord[0] + latJ, coord[1] + lonJ, intensity]);
                                }
                            });

                            // Trend Arrows logic
                            let arrow = "";
                            let diff = (lastT - prevT).toFixed(2);
                            if (lastT > prevT + 0.1) {
                                arrow = `<span class='trend-marker' style='color:#ff4d4d' title='Warming: +${diff}°C'>▲</span>`;
                            } else if (lastT < prevT - 0.1) {
                                arrow = `<span class='trend-marker' style='color:#4dffff' title='Cooling: ${diff}°C'>▼</span>`;
                            }
                            
                            if (arrow) {
                                L.marker(lastLoc, {
                                    icon: L.divIcon({ html: arrow, className: 'trend-icon', iconSize: [25, 25] })
                                }).addTo(trendGroup);
                            }
                        }
                        
                        heatLayer.setLatLngs(points);
                        document.getElementById('status').innerText = "Live Sensors: " + Object.keys(data).length;
                    } catch (e) { console.error("Sync Error", e); }
                }

                setInterval(updateMap, 10000);
                updateMap();
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
