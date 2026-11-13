"""SQLite storage. All mutating statements commit atomically."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import replace

from models import Filters, now_utc


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                max_price_usd INTEGER NOT NULL DEFAULT 0,
                rooms INTEGER NOT NULL DEFAULT 0,
                owner_only INTEGER NOT NULL DEFAULT 0,
                watching INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS seen (
                user_id INTEGER NOT NULL, key TEXT NOT NULL,
                sent_at TEXT NOT NULL, PRIMARY KEY (user_id, key)
            );
            CREATE TABLE IF NOT EXISTS route_cache (
                point TEXT PRIMARY KEY,
                seconds REAL NOT NULL,
                meters REAL,
                measured_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS addresses (
                addr TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_status (
                name TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
                ok INTEGER NOT NULL, total INTEGER NOT NULL, error TEXT
            );
        ''')
        self.db.commit()

    def get_user(self, user_id: int) -> Filters:
        self.db.execute('INSERT OR IGNORE INTO users(user_id) VALUES (?)', (user_id,))
        self.db.commit()
        row = self.db.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
        return Filters(
            max_price_usd=row['max_price_usd'], rooms=row['rooms'],
            owner_only=bool(row['owner_only']), watching=bool(row['watching']),
        )

    def update_user(self, user_id: int, **changes) -> Filters:
        current = self.get_user(user_id)
        updated = replace(current, **changes)
        self.db.execute(
            'UPDATE users SET max_price_usd=?, rooms=?, owner_only=?, watching=? WHERE user_id=?',
            (updated.max_price_usd, updated.rooms, int(updated.owner_only), int(updated.watching), user_id),
        )
        self.db.commit()
        return updated

    def watchers(self) -> list[int]:
        return [int(r[0]) for r in self.db.execute('SELECT user_id FROM users WHERE watching=1')]

    def has_seen(self, user_id: int, key: str) -> bool:
        return self.db.execute('SELECT 1 FROM seen WHERE user_id=? AND key=?', (user_id, key)).fetchone() is not None

    def mark_seen(self, user_id: int, keys: list[str]) -> None:
        self.db.executemany(
            'INSERT OR IGNORE INTO seen(user_id,key,sent_at) VALUES (?,?,?)',
            ((user_id, key, now_utc()) for key in keys),
        )
        self.db.commit()

    def get_route(self, point: str, *, max_age_days: int = 28) -> tuple[float, float | None] | None:
        row = self.db.execute('SELECT * FROM route_cache WHERE point=?', (point,)).fetchone()
        if row and time.time() - row['measured_at'] <= max_age_days * 86400:
            return float(row['seconds']), float(row['meters']) if row['meters'] is not None else None
        return None

    def save_route(self, point: str, seconds: float, meters: float | None) -> None:
        self.db.execute('''INSERT INTO route_cache(point,seconds,meters,measured_at) VALUES (?,?,?,?)
                           ON CONFLICT(point) DO UPDATE SET seconds=excluded.seconds,
                           meters=excluded.meters, measured_at=excluded.measured_at''',
                        (point, seconds, meters, time.time()))
        self.db.commit()

    def replace_addresses(self, addresses: dict[str, tuple[float, float]]) -> None:
        # Keep old keys to avoid losing location information from temporary OSM changes.
        self.db.executemany('''INSERT INTO addresses(addr,lat,lon) VALUES (?,?,?)
                               ON CONFLICT(addr) DO UPDATE SET lat=excluded.lat,lon=excluded.lon''',
                            ((key, lat, lon) for key, (lat, lon) in addresses.items()))
        self.db.commit()

    def address_count(self) -> int:
        return self.db.execute('SELECT COUNT(*) FROM addresses').fetchone()[0]

    def lookup_address(self, key: str) -> tuple[float, float] | None:
        row = self.db.execute('SELECT lat,lon FROM addresses WHERE addr=?', (key,)).fetchone()
        return (float(row['lat']), float(row['lon'])) if row else None

    def get_meta_float(self, key: str) -> float:
        row = self.db.execute('SELECT value FROM meta WHERE name=?', (key,)).fetchone()
        try:
            return float(row['value']) if row else 0.0
        except ValueError:
            return 0.0

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute('''INSERT INTO meta(name,value) VALUES (?,?)
                           ON CONFLICT(name) DO UPDATE SET value=excluded.value''', (key, value))
        self.db.commit()

    def set_source_status(self, source: str, ok: bool, total: int, error: str | None = None) -> None:
        self.db.execute('''INSERT INTO source_status(name, checked_at, ok, total, error) VALUES(?,?,?,?,?)
                           ON CONFLICT(name) DO UPDATE SET checked_at=excluded.checked_at,
                           ok=excluded.ok,total=excluded.total,error=excluded.error''',
                        (source, now_utc(), int(ok), total, (error or '')[:300]))
        self.db.commit()

    def statuses(self) -> list[sqlite3.Row]:
        return self.db.execute('SELECT * FROM source_status ORDER BY name').fetchall()

    def close(self) -> None:
        self.db.close()
