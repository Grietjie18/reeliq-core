import os, uuid
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime, Integer

app = FastAPI()

# --- DATABASE CONFIG ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
if DATABASE_URL.startswith("postgres://"): DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
database = Database(DATABASE_URL)
metadata = MetaData()
observations_table = Table(
    "observations", metadata,
    Column("observation_id", String, primary_key=True),
    Column("vessel_id", String, index=True),
    Column("timestamp", DateTime),
    Column("latitude", Float), Column("longitude", Float),
    Column("sea_surface_temperature", Float), 
    Column("speed_over_ground", Float), Column("speed_through_water", Float)
)

class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latitude: float; longitude: float; sea_surface_temperature: float 
    speed_over_ground: float; speed_through_water: float

@app.on_event("startup")
async def startup():
    await database.connect()
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, data: Observation, api_key: str = Query(...)):
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
    query = observations_table.select().where(observations_table.c.timestamp >= (now_utc - timedelta(hours=6)))
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
            <title>REEL IQ | Precision Coastal Thermal</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script src="https://leaflet.github.io/Leaflet.heat/dist/leaflet-heat.js"></script>
            <style>
                body { margin: 0; background: #06090f; font-family: sans-serif; overflow: hidden; }
                #map { height: 100vh; width: 100%; background: #06090f; }
                #overlay { 
                    position: absolute; top: 20px; left: 20px; z-index: 1000; 
                    background: rgba(0,18,25,0.9); padding: 15px; border-radius: 8px; 
                    border: 1px solid #00f2ff; color: #00f2ff;
                }
                #legend {
                    position: absolute; bottom: 30px; right: 20px; z-index: 1000;
                    background: rgba(0,18,25,0.9); padding: 12px; border-radius: 8px; border: 1px solid #333;
                }
                .gradient-bar {
                    height: 180px; width: 15px; 
                    background: linear-gradient(to top, blue, #00ffff, lime, yellow, orange, red);
                    border-radius: 3px;
                }
                /* Soft Glow effect */
                .leaflet-heatmap-layer { mix-blend-mode: screen; opacity: 0.9; filter: contrast(1.1); }
            </style>
        </head>
        <body>
            <div id="overlay">
                <b>REEL IQ | COASTAL MASK</b><br>
                <small id="status">Clipping to Shoreline...</small>
            </div>
            
            <div id="legend">
                <div style="display:flex; align-items:flex-end;">
                    <div class="gradient-bar"></div>
                    <div style="margin-left:10px; display:flex; flex-direction:column; justify-content:space-between; height:180px; font-size:11px; color:white; font-weight:bold;">
                        <span>22°C</span><span>21°C</span><span>20°C</span><span>19°C</span><span>18°C</span><span>17°C</span>
                    </div>
                </div>
            </div>

            <div id="map"></div>
            
            <script>
                var map = L.map('map', { zoomControl: false, attributionControl: false }).setView([-34.14, 25.02], 11);
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);

                // COASTAL MASK DATA (Simplified J-Bay Coastline)
                var jbayCoast = [
                    [-34.00, 24.80], [-34.02, 24.91], [-34.05, 24.93], 
                    [-34.13, 24.91], [-34.18, 25.02], [-34.25, 25.20],
                    [-34.30, 25.50], [-34.50, 25.50], [-34.50, 24.00], [-34.00, 24.00]
                ];

                // Create a polygon that covers the LAND and 'masks' the heat
                var landMask = L.polygon(jbayCoast, {color: 'transparent', fillColor: '#06090f', fillOpacity: 1.0}).addTo(map);
                landMask.bringToFront();

                var heatLayer = L.heatLayer([], {
                    radius: 35, 
                    blur: 30, 
                    max: 0.6,
                    gradient: { 0.0: 'blue', 0.2: '#00ffff', 0.4: 'lime', 0.6: 'yellow', 0.8: 'orange', 1.0: 'red' }
                }).addTo(map);

                async function update() {
                    try {
                        const res = await fetch('/api/live');
                        const data = await res.json();
                        let pts = [];

                        for (const [id, info] of Object.entries(data)) {
                            let curT = info.temps[info.temps.length-1];
                            let intensity = (curT - 17) / 5;
                            intensity = Math.min(Math.max(intensity, 0.01), 1.0);

                            info.path.forEach(c => {
                                for(let i=0; i<15; i++) {
                                    let rLat = (Math.random()-0.5)*0.18;
                                    let rLon = (Math.random()-0.5)*0.18;
                                    pts.push([c[0]+rLat, c[1]+rLon, intensity]);
                                }
                            });
                        }
                        heatLayer.setLatLngs(pts);
                        document.getElementById('status').innerText = "Vessels Live: " + Object.keys(data).length;
                        
                        // Keep land mask on top to 'hide' heat on land
                        landMask.bringToFront();
                    } catch (e) { console.error(e); }
                }

                setInterval(update, 10000); 
                update();
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
