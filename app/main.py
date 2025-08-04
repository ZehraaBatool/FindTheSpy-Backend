from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .models import init_db
from .routers import game

app = FastAPI()

origins = ["http://localhost:3000", "https://find-the-spy-frontend.vercel.app/"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await init_db()

app.include_router(game.router)
