from .db import get_db_pool

async def init_db():
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Rooms table
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id SERIAL PRIMARY KEY,
            code VARCHAR(6) UNIQUE,
            mafia_count INT,
            status TEXT DEFAULT 'waiting'  -- waiting, ongoing, ended
        );
        """)

        # Players table
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            room_code VARCHAR(6),
            name TEXT,
            is_mafia BOOLEAN,
            eliminated BOOLEAN DEFAULT FALSE,
            UNIQUE(room_code, name) ,
            word TEXT,
            score INT DEFAULT 0,
            round INT DEFAULT 1
        );
        """)

        # Votes table
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id SERIAL PRIMARY KEY,
            room_code VARCHAR(6),
            round INT,
            voter_name TEXT,
            voted_name TEXT
        );
        """)

        # Round result table (for tracking scores and round outcomes)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS round_results (
            id SERIAL PRIMARY KEY,
            room_code VARCHAR(6),
            round INT,
            eliminated_player TEXT,
            was_mafia BOOLEAN,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );
        """)
