import os
import uuid
from typing import List, Optional
from datetime import datetime
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
    
    # Core Marine Variables
    sea_surface_temperature: float 
    sea_surface_salinity: Optional[float] = None
    sea_surface_turbidity: Optional[float] = None
    
    # Propulsion Data
    speed_over_ground: float
    speed_through_water: float
    
    # Quality Control
    qc_flag: int = 0

# --- 2. DATABASE & AUTH CONFIGURATION ---
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Security Key (Matches your Render Environment Variable)
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
async def ingest_data(
    vessel_id: str, 
    data: Observation, 
    api_key: str = Query(...)
):
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

@app.get("/data")
async def get_all_data(limit: int = 100):
    query = observations_table.select().order_by(observations_table.c.timestamp.desc()).limit(limit)
    return await database.fetch_all(query)

@app.get("/map", response_class=HTMLResponse)
async def get_map():
    # Pull latest 50 points from Postgres
    query = observations_table.select().order_by(observations_table.c.timestamp.desc()).limit(50)
    rows = await database.fetch_all(query)
    
    # Generate map markers
    points = ""
    for row in rows:
        points += f"L.marker([{row['latitude']}, {row['longitude']}]).addTo(map)"
        points += f".bindPopup('<b>Vessel:</b> {row['vessel_id']}<br><b>SST:</b> {row['sea_surface_temperature']}°C');\n"

    html_content = f"""
    <html>
        <head>
            <title>REEL IQ Live Map</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <style>#map {{ height: 100vh; width: 100%; }} body {{ margin: 0; }}</style>
        </head>
        <body>
            <div id="map"></div>
            <script>
                var map = L.map('map').setView([-34.05, 24.92], 12);
                L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                    attribution: '&copy; OpenStreetMap contributors'
                }}).addTo(map);
                {points}
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
