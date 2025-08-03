from fastapi import APIRouter, HTTPException, Body, WebSocket, WebSocketDisconnect
from ..db import get_db_pool
import random, string, httpx
from collections import defaultdict
import asyncio

router = APIRouter()
active_connections = defaultdict(dict)

# Helper functions
async def assign_words_and_roles(pool, room_code, players, mafia_ids, next_round_num):
    """Helper function to consistently assign words and roles"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://random-word-api.vercel.app/api?words=2")
            words = resp.json()
            civilian_word, spy_word = words[0], words[1]
    except Exception:
        # Fallback words if API fails
        civilian_word, spy_word = "apple", "banana"
    
    async with pool.acquire() as connection:
        async with connection.transaction():
            for player in players:
                is_mafia = player["id"] in mafia_ids
                word = spy_word if is_mafia else civilian_word
                await connection.execute("""
                    UPDATE players 
                    SET is_mafia = $1, word = $2, round = $3, eliminated = FALSE 
                    WHERE id = $4
                """, is_mafia, word, next_round_num, player["id"])
    
    return civilian_word, spy_word

async def generate_unique_room_code(pool):
    for _ in range(10):
        code = ''.join(random.choices(string.ascii_uppercase, k=6))
        exists = await pool.fetchval("SELECT 1 FROM rooms WHERE code = $1", code)
        if not exists:
            return code
    raise HTTPException(status_code=500, detail="Failed to generate unique room code")

async def broadcast(room_code: str, message: str):
    for connection in active_connections.get(room_code, {}):
        try:
            await connection.send_text(message)
        except:
            continue

# WebSocket endpoint
@router.websocket("/ws/{room_code}")
async def websocket_endpoint(websocket: WebSocket, room_code: str):
    await websocket.accept()
    try:
        player_name = await websocket.receive_text()
        active_connections[room_code][websocket] = player_name
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if websocket in active_connections.get(room_code, {}):
            del active_connections[room_code][websocket]

# Endpoints
@router.post("/create-room")
async def create_room(mafia_count: int = Body(...), name: str = Body(...)):
    try:
        pool = await get_db_pool()
        code = await generate_unique_room_code(pool)
        
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO rooms (code, mafia_count, host_name) VALUES ($1, $2, $3)",
                    code, mafia_count, name
                )
                await connection.execute(
                    "INSERT INTO players (room_code, name) VALUES ($1, $2)",
                    code, name
                )
        
        await broadcast(code, "game_started")
        return {"room_code": code, "is_host": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/join-room")
async def join_room(room_code: str = Body(...), name: str = Body(...)):
    if len(name) < 1 or len(name) > 30:
        raise HTTPException(status_code=400, detail="Name too short or too long")
    pool = await get_db_pool()
    room_code = room_code.upper()
    room = await pool.fetchrow("SELECT * FROM rooms WHERE code = $1", room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    try:
        await pool.execute("INSERT INTO players (room_code, name) VALUES ($1, $2)", room_code, name)
    except Exception:
        raise HTTPException(status_code=400, detail="Player name already taken")
    return {"message": f"{name} joined room {room_code}"}

@router.post("/start-round")
async def start_round(room_code: str = Body(...), name: str = Body(...)):
    pool = await get_db_pool()
    room_code = room_code.upper()
    
    # Host verification
    is_host = await pool.fetchval(
        "SELECT host_name FROM rooms WHERE code = $1",
        room_code
    ) == name
    if not is_host:
        raise HTTPException(status_code=403, detail="Only host can start the round")

    room = await pool.fetchrow("SELECT mafia_count FROM rooms WHERE code = $1", room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    
    players = await pool.fetch(
        "SELECT id FROM players WHERE room_code = $1", 
        room_code
    )
    
    if len(players) < room["mafia_count"]:
        raise HTTPException(status_code=400, detail="Not enough players")

    mafia_ids = random.sample([p["id"] for p in players], room["mafia_count"])
    
    # Use helper function
    civilian_word, spy_word = await assign_words_and_roles(
        pool, room_code, players, mafia_ids, 1  # First round is 1
    )

    # Update game state
    await pool.execute("""
        UPDATE rooms 
        SET status = 'ongoing', current_phase = 'discussion' 
        WHERE code = $1
    """, room_code)
    
    await broadcast(room_code, "round_started")
    return {"message": "Round started"}

@router.post("/start-voting")
async def start_voting(room_code: str = Body(...), name: str = Body(...)):
    pool = await get_db_pool()
    room_code = room_code.upper()
    
    # Host verification
    is_host = await pool.fetchval(
        "SELECT host_name FROM rooms WHERE code = $1",
        room_code
    ) == name
    if not is_host:
        raise HTTPException(status_code=403, detail="Only host can start voting")

    room = await pool.fetchrow(
        "SELECT current_phase FROM rooms WHERE code = $1", 
        room_code
    )
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room['current_phase'] != 'discussion':
        raise HTTPException(
            status_code=400, 
            detail="Can only start voting from discussion phase"
        )

    await pool.execute(
        "UPDATE rooms SET current_phase = 'voting' WHERE code = $1",
        room_code
    )
    await broadcast(room_code, "voting_started")
    return {"message": "Voting phase started"}

@router.post("/end-game")
async def end_game(room_code: str = Body(...), name: str = Body(...)):
    pool = await get_db_pool()
    room_code = room_code.upper()
    
    # Host verification
    is_host = await pool.fetchval(
        "SELECT host_name FROM rooms WHERE code = $1",
        room_code
    ) == name
    if not is_host:
        raise HTTPException(status_code=403, detail="Only host can end the game")

    await pool.execute("DELETE FROM votes WHERE room_code = $1", room_code)
    await pool.execute("DELETE FROM round_results WHERE room_code = $1", room_code)
    await pool.execute("DELETE FROM players WHERE room_code = $1", room_code)
    await pool.execute("DELETE FROM rooms WHERE code = $1", room_code)

    await broadcast(room_code, "game_ended")
    return {"message": "Game ended and cleaned up"}

@router.get("/is-host/{room_code}/{name}")
async def check_host(room_code: str, name: str):
    pool = await get_db_pool()
    host_name = await pool.fetchval(
        "SELECT host_name FROM rooms WHERE code = $1",
        room_code.upper()
    )
    return {"is_host": name == host_name}

@router.get("/player-word/{room_code}/{name}")
async def get_player_word(room_code: str, name: str):
    pool = await get_db_pool()
    player = await pool.fetchrow(
        "SELECT word, is_mafia FROM players WHERE room_code = $1 AND name = $2", room_code, name)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"word": player["word"],"is_mafia": player["is_mafia"]}

@router.get("/players/{room_code}")
async def get_players_public(room_code: str):
    pool = await get_db_pool()
    players = await pool.fetch("""
        SELECT name, score FROM players WHERE room_code = $1
    """, room_code)
    return {"players": [{"name": p["name"], "score": p["score"]} for p in players]}

@router.post("/vote")
async def vote(room_code: str = Body(...), voter_name: str = Body(...), voted_name: str = Body(...)):
    pool = await get_db_pool()
    room_code = room_code.upper()
    
    current_round = await pool.fetchval(
        "SELECT MAX(round) FROM round_results WHERE room_code = $1", 
        room_code
    ) or 1

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO votes (room_code, round, voter_name, voted_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (room_code, voter_name, round) DO UPDATE
                SET voted_name = EXCLUDED.voted_name
            """, room_code, current_round, voter_name, voted_name)

            active_players = await conn.fetchval("""
                SELECT COUNT(*) FROM players WHERE room_code = $1
            """, room_code)

            current_votes = await conn.fetchval("""
                SELECT COUNT(DISTINCT voter_name) 
                FROM votes 
                WHERE room_code = $1 AND round = $2
            """, room_code, current_round)

            if current_votes >= active_players:
                await process_round_results(conn, room_code, current_round)
                await broadcast(room_code, "round_ended")

    return {"message": f"{voter_name} voted for {voted_name}"}

async def process_round_results(conn, room_code, current_round):
    vote_result = await conn.fetchrow("""
        SELECT voted_name, COUNT(*) as votes
        FROM votes
        WHERE room_code = $1 AND round = $2
        GROUP BY voted_name
        ORDER BY votes DESC
        LIMIT 1
    """, room_code, current_round)

    if not vote_result:
        raise HTTPException(status_code=400, detail="No votes cast")

    eliminated_name = vote_result["voted_name"]
    eliminated = await conn.fetchrow(
        "SELECT is_mafia FROM players WHERE room_code = $1 AND name = $2", 
        room_code, eliminated_name
    )

    if eliminated["is_mafia"]:
        await conn.execute("""
            UPDATE players 
            SET score = score + 1
            WHERE room_code = $1 
            AND is_mafia = FALSE
            AND name IN (
                SELECT v.voter_name 
                FROM votes v
                JOIN players p ON v.voted_name = p.name
                WHERE v.room_code = $1 
                AND v.round = $2
                AND p.is_mafia = TRUE
                AND p.room_code = $1
            )
        """, room_code, current_round)
    else:
        await conn.execute("""
            UPDATE players
            SET score = score + 1
            WHERE room_code = $1 
            AND is_mafia = TRUE 
            AND name NOT IN (
                SELECT voted_name FROM votes WHERE room_code = $1 AND round = $2
            )
        """, room_code, current_round)

    await conn.execute("""
        INSERT INTO round_results (room_code, round, eliminated_player, was_mafia)
        VALUES ($1, $2, $3, $4)
    """, room_code, current_round, eliminated_name, eliminated["is_mafia"])

    await conn.execute("""
        UPDATE rooms SET status = 'ended' WHERE code = $1
    """, room_code)

@router.get("/vote-count")
async def get_vote_count(room_code: str):
    pool = await get_db_pool()
    current_round = await pool.fetchval(
        "SELECT MAX(round) FROM round_results WHERE room_code = $1", 
        room_code
    ) or 1
    
    vote_count = await pool.fetchval("""
        SELECT COUNT(DISTINCT voter_name) 
        FROM votes 
        WHERE room_code = $1 AND round = $2
    """, room_code, current_round)
    
    return {"count": vote_count}

@router.post("/end-round")
async def end_round(room_code: str = Body(..., embed=True)):
    pool = await get_db_pool()
    room_code = room_code.upper()
    current_round = await pool.fetchval("SELECT MAX(round) FROM round_results WHERE room_code = $1", room_code) or 1

    vote_results = await pool.fetch("""
        SELECT voted_name, COUNT(*) as votes
        FROM votes
        WHERE room_code = $1 AND round = $2
        GROUP BY voted_name
        ORDER BY votes DESC
        LIMIT 1
    """, room_code, current_round)

    if not vote_results:
        raise HTTPException(status_code=400, detail="No votes cast")

    eliminated_name = vote_results[0]["voted_name"]
    eliminated = await pool.fetchrow("SELECT is_mafia FROM players WHERE room_code = $1 AND name = $2", room_code, eliminated_name)
    if not eliminated:
        raise HTTPException(status_code=404, detail="Eliminated player not found")

    is_mafia = eliminated["is_mafia"]

    correct_voters = await pool.fetch("""
        SELECT voter_name FROM votes 
        WHERE room_code = $1 AND round = $2 AND voted_name = $3
    """, room_code, current_round, eliminated_name)

    if is_mafia:
        await pool.execute("""
            UPDATE players 
            SET score = score + 1
            WHERE room_code = $1 AND name = ANY($2) AND is_mafia = FALSE
        """, room_code, [v["voter_name"] for v in correct_voters])
    else:
        await pool.execute("""
            UPDATE players
            SET score = score + 1
            WHERE room_code = $1 AND is_mafia = TRUE AND name NOT IN (
                SELECT voted_name FROM votes WHERE room_code = $1 AND round = $2
            )
        """, room_code, current_round)

    await pool.execute("""
        INSERT INTO round_results (room_code, round, eliminated_player, was_mafia)
        VALUES ($1, $2, $3, $4)
    """, room_code, current_round, eliminated_name, is_mafia)

    await pool.execute("UPDATE rooms SET status = 'ended' WHERE code = $1", room_code)
    await broadcast(room_code, "round_ended")
    return {"message": f"{eliminated_name} was eliminated. Game ended!"}

@router.get("/round-results/{room_code}")
async def get_round_results(room_code: str):
    pool = await get_db_pool()
    room_code = room_code.upper()
    current_round = await pool.fetchval(
        "SELECT MAX(round) FROM round_results WHERE room_code = $1", room_code
    ) or 1

    votes = await pool.fetch(
        "SELECT voter_name, voted_name FROM votes WHERE room_code = $1 AND round = $2",
        room_code, current_round
    )
    elimination = await pool.fetchrow(
        "SELECT eliminated_player, was_mafia FROM round_results WHERE room_code = $1 AND round = $2",
        room_code, current_round
    )
    players = await pool.fetch(
        "SELECT name, score, is_mafia FROM players WHERE room_code = $1",
        room_code
    )
    return {
        "votes": [{"voter": v["voter_name"], "voted": v["voted_name"]} for v in votes],
        "eliminated": elimination["eliminated_player"] if elimination else None,
        "was_mafia": elimination["was_mafia"] if elimination else None,
        "players": [{"name": p["name"], "score": p["score"], "is_mafia": p["is_mafia"]} for p in players]
    }

@router.post("/next-round")
async def next_round(room_code: str = Body(...), name: str = Body(...)):
    pool = await get_db_pool()
    room_code = room_code.upper()

    # Host verification
    is_host = await pool.fetchval(
        "SELECT host_name FROM rooms WHERE code = $1",
        room_code
    ) == name
    
    if not is_host:
        raise HTTPException(status_code=403, detail="Only host can start next round")

    # Get current round
    current_round = await pool.fetchval(
        "SELECT MAX(round) FROM round_results WHERE room_code = $1", 
        room_code
    ) or 0
    next_round_num = current_round + 1

    # Get all players (including previously eliminated ones)
    players = await pool.fetch(
        "SELECT id FROM players WHERE room_code = $1",
        room_code
    )
    mafia_count = await pool.fetchval(
        "SELECT mafia_count FROM rooms WHERE code = $1",
        room_code
    )

    if len(players) < mafia_count:
        raise HTTPException(status_code=400, detail="Not enough players")

    # Select spies for this round from all players
    mafia_ids = random.sample([p["id"] for p in players], mafia_count)

    # Use helper function
    civilian_word, spy_word = await assign_words_and_roles(
        pool, room_code, players, mafia_ids, next_round_num
    )

    # Update game state
    await pool.execute("""
        UPDATE rooms 
        SET status = 'ongoing', current_phase = 'discussion' 
        WHERE code = $1
    """, room_code)

    # Clear previous votes
    await pool.execute("DELETE FROM votes WHERE room_code = $1", room_code)

    # Broadcast to all players
    await broadcast(room_code, "round_started")
    
    return {
        "message": f"Round {next_round_num} started",
        "civilian_word": civilian_word,  # For debugging
        "spy_word": spy_word             # For debugging
    }

@router.get("/leaderboard/{room_code}")
async def leaderboard(room_code: str):
    pool = await get_db_pool()
    room_code = room_code.upper()

    players = await pool.fetch("""
        SELECT name, score FROM players WHERE room_code = $1 ORDER BY score DESC
    """, room_code)
    return {"leaderboard": [{"name": p["name"], "score": p["score"]} for p in players]}

@router.get("/room-status/{room_code}")
async def get_room_status(room_code: str):
    pool = await get_db_pool()
    room_code = room_code.upper()

    status = await pool.fetchrow(
        "SELECT status, current_phase FROM rooms WHERE code = $1", 
        room_code
    )
    if not status:
        raise HTTPException(status_code=404, detail="Room not found")
    return status