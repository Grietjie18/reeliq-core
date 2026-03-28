import os
import uuid
from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime, Integer

# --- 1. DATA SCHEMA ---
class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    sea_surface_temperature: float 
    sea_surface_salinity: Optional[float] = None
    sea_surface_turbidity: Optional[float] = None
    speed_over_ground: float
    speed_through_water: float
    qc_flag: int = 0

# --- 2. DATABASE & AUTH CONFIGURATION ---
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MASTER_API_KEY = os.getenv("MASTER_API_KEY", "jbay-science-2026")

database = Database(DATABASE_URL)
metadata = MetaData()

observations_table = Table(
    "observations",
    metadata,
    Column("observation_id", String, primary_key=True),
    Column("vessel_id", String, index=True),
    Column("timestamp", DateTime),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("sea_surface_temperature", Float),
    Column("sea_surface_salinity", Float, nullable=True),
    Column("sea_surface_turbidity", Float, nullable=True),
    Column("speed_over_ground", Float),
    Column("speed_through_water", Float),
    Column("qc_flag", Integer, default=0)
)

# --- 3. APP INITIALIZATION ---
app = FastAPI(title="REEL IQ Core API")

@app.on_event("startup")
async def startup():
    await database.connect()
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# --- 4. ENDPOINTS ---

@app.get("/")
async def root():
    return {"message": "REEL IQ Core Online", "status": "Secure"}

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, data: Observation, api_key: str = Query(...)):
    if api_key != MASTER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    try:
        query = observations_table.insert().values(
            observation_id=str(data.observation_id),
            vessel_id=vessel_id,
            timestamp=data.timestamp,
            latitude=data.latitude,
            longitude=data.longitude,
            sea_surface_temperature=data.sea_surface_temperature,
            sea_surface_salinity=data.sea_surface_salinity,
            sea_surface_turbidity=data.sea_surface_turbidity,
            speed_over_ground=data.speed_over_ground,
            speed_through_water=data.speed_through_water,
            qc_flag=data.qc_flag
        )
        await database.execute(query)
        return {"status": "success", "vessel": vessel_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/map", response_class=HTMLResponse)
async def get_map():
    # 🌊 Demo Window: 3 minutes
    time_limit = datetime.utcnow() - timedelta(minutes=3)
    
    query = observations_table.select().where(
        observations_table.c.timestamp >= time_limit
    ).order_by(observations_table.c.vessel_id, observations_table.c.timestamp.asc())
    
    rows = await database.fetch_all(query)
    
    vessel_paths = {}
    for row in rows:
        v_id = row['vessel_id']
        if v_id not in vessel_paths:
            vessel_paths[v_id] = []
        vessel_paths[v_id].append([row['latitude'], row['longitude']])

    track_scripts = ""
    for v_id, coords in vessel_paths.items():
        if len(coords) > 1:
            track_scripts += f"L.polyline({coords}, {{color: '#00f2ff', weight: 2, opacity: 0.5}}).addTo(map);\n"
        
        last_coord = coords[-1]
        track_scripts += f"""
            L.circleMarker({last_coord}, {{
                radius: 7, fillColor: "#00f2ff", color: "#fff", weight: 2, fillOpacity: 1
            }}).addTo(map).bindPopup('<b>Vessel:</b> {v_id}');
        """

    html_content = f"""
    <html>
        <head>
            <title>REEL IQ | Live Monitor</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <style>
                body {{ margin: 0; background: #06090f; }}
                #map {{ height: 100vh; width: 100%; }}
            </style>
        </head>
        <body>
            <div id="map"></div>
            <script>
                var map = L.map('map', {{ zoomControl: false }}).setView([-34.14, 25.02], 11);
                L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);
                {track_scripts}

                setTimeout(function(){{ location.reload(); }}, 15000);
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
