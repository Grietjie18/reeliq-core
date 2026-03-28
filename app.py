import os, uuid, json
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
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
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
            <title>REEL IQ | Adaptive Front Detection</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script src="https://leaflet.github.io/Leaflet.heat/dist/leaflet-heat.js"></script>
            <style>
                body { margin: 0; background: #06090f; font-family: sans-serif; overflow: hidden; }
                #map { height: 100vh; width: 100%; background: #06090f; }
                #overlay { 
                    position: absolute; top: 20px; left: 20px; z-index: 1000; 
                    background: rgba(0,18,25,0.9); padding: 15px; border-radius: 8px; 
                    border: 1px solid #00f2ff; color: #00f2ff; min-width: 200px;
                }
                #legend {
                    position: absolute; bottom: 30px; right: 20px; z-index: 1000;
                    background: rgba(0,18,25,0.9); padding: 12px; border-radius: 8px; border: 1px solid #333;
                }
                .gradient-bar {
                    height: 200px; width: 15px; 
                    background: linear-gradient(to top, #0000ff, #00ffff, #00ff00, #ffff00, #ff8000, #ff0000);
                    border-radius: 3px;
                }
                .leaflet-heatmap-layer { mix-blend-mode: screen; opacity: 0.8; filter: contrast(1.2) saturate(1.4); }
            </style>
        </head>
        <body>
            <div id="overlay">
                <b>REEL IQ | ADAPTIVE THERMAL</b><br>
                <small id="range">Detecting Fronts...</small><br>
                <small id="status" style="font-size: 9px; opacity: 0.7;"></small>
            </div>
            
            <div id="legend">
                <div style="display:flex; align-items:flex-end;">
                    <div class="gradient-bar"></div>
                    <div id="legend-labels" style="margin-left:10px; display:flex; flex-direction:column; justify-content:space-between; height:200px; font-size:11px; color:white; font-weight:bold;">
                        <span>MAX</span><span>-</span><span>-</span><span>-</span><span>-</span><span>MIN</span>
                    </div>
                </div>
            </div>

            <div id="map"></div>
            
            <script>
                var map = L.map('map', { zoomControl: false, attributionControl: false }).setView([-34.05, 25.10], 11);
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);

                var heatLayer = L.heatLayer([], {
                    radius: 45, 
                    blur: 40, 
                    max: 0.8,
                    gradient: { 0.0: '#0000ff', 0.2: '#00ffff', 0.4: '#00ff00', 0.6: '#ffff00', 0.8: '#ff8000', 1.0: '#ff0000' }
                }).addTo(map);

                async function update() {
                    try {
                        const res = await fetch('/api/live');
                        const data = await res.json();
                        let pts = [];
                        let allTemps = [];

                        // 1. Collect all current temps to find the dynamic range
                        for (const id in data) {
                            allTemps.push(...data[id].temps);
                        }

                        if (allTemps.length === 0) return;

                        let minT = Math.min(...allTemps);
                        let maxT = Math.max(...allTemps);
                        let range = maxT - minT;

                        // 2. Update Overlay & Legend
                        document.getElementById('range').innerText = `Range: ${minT.toFixed(1)}° - ${maxT.toFixed(1)}°C`;
                        document.getElementById('status').innerText = `Sensors: ${Object.keys(data).length} | Sensitivity: ${(range/5).toFixed(2)}°/Color`;
                        
                        let labels = document.getElementById('legend-labels').children;
                        labels[0].innerText = maxT.toFixed(1) + "°";
                        labels[5].innerText = minT.toFixed(1) + "°";

                        // 3. Map intensity based ONLY on the current range
                        for (const id in data) {
                            let curT = data[id].temps[data[id].temps.length-1];
                            
                            // Normalized intensity: 0.0 at minT, 1.0 at maxT
                            let intensity = range > 0 ? (curT - minT) / range : 0.5;

                            data[id].path.forEach(c => {
                                for(let i=0; i<30; i++) { // Dense interpolation
                                    let rLat = (Math.random()-0.5)*0.3; 
                                    let rLon = (Math.random()-0.5)*0.3;
                                    pts.push([c[0]+rLat, c[1]+rLon, intensity]);
                                }
                            });
                        }
                        heatLayer.setLatLngs(pts);
                    } catch (e) { console.error(e); }
                }

                setInterval(update, 5000); 
                update();
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
