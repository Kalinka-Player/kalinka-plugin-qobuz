"""Qobuz's editorial playlists, matched to a mood or description.

Each playlist's name, genres, themes and description go through the
server's shared text embedder once. The vectors persist in a cache database next to the
playlist's Qobuz record, keyed by a hash of the text they came from, so a
refresh embeds only the playlists that are new or changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Iterator, Optional

import numpy as np
from kalinka_plugin_sdk import paths
from kalinka_plugin_sdk.embedding import TextEmbedder

from .auth import AuthenticationError

if TYPE_CHECKING:
    from .qobuz import QobuzClient

logger = logging.getLogger(__name__.split(".")[-1])

# Their descriptions are biographies, which match a mood on stray words.
EXCLUDED_CATEGORIES = frozenset({"artist", "danslecasque", "label"})
# Nonsense queries top out near 0.36; good matches start around 0.42.
MIN_SCORE = 0.40
PAGE = 100
REFRESH_S = 24 * 3600
RETRY_S = 600
SAVE_EVERY = 64
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS playlists (id TEXT PRIMARY KEY,"
    " record TEXT NOT NULL, text_hash TEXT, embedding BLOB)",
)


def default_index_path() -> str:
    return os.path.join(paths.cache_dir(), "qobuz", "playlists.sqlite")


@dataclass(frozen=True)
class EditorialPlaylist:
    """A playlist's Qobuz record, and the text it is matched on."""

    record: dict
    text: str


async def editorial_playlists(client: QobuzClient) -> list[EditorialPlaylist]:
    """Qobuz's editorial playlists, less those whose categories are all in
    :data:`EXCLUDED_CATEGORIES`. A playlist's tags name its categories
    among others; playlist/getTags says which tags are categories."""
    tags = (await _get(client, "playlist/getTags", {}))["tags"]
    categories = {tag["slug"] for tag in tags}
    kept = []
    for playlist in await _featured(client):
        slugs = {tag.get("slug") for tag in playlist.get("tags") or []}
        listed_under = slugs & categories
        if listed_under and listed_under <= EXCLUDED_CATEGORIES:
            continue
        kept.append(EditorialPlaylist(playlist, text_of(playlist, categories)))
    return kept


async def _featured(client: QobuzClient) -> list[dict]:
    playlists: list[dict] = []
    while True:
        page = (
            await _get(
                client,
                "playlist/getFeatured",
                {"type": "editor-picks", "limit": PAGE, "offset": len(playlists)},
            )
        )["playlists"]
        playlists += page["items"]
        if not page["items"] or len(playlists) >= page["total"]:
            return playlists


async def _get(client: QobuzClient, endpoint: str, params: dict) -> dict:
    response = await client.session.get(client.base + endpoint, params=params)
    response.raise_for_status()
    return response.json()


def text_of(playlist: dict, categories: set[str]) -> str:
    """What a playlist is matched on: its name, its genres, its tags that are
    neither genres nor ``categories`` (themes such as Summer or Christmas),
    and its description last, where the embedder may cut it short."""
    genres = _unique(genre.get("name") for genre in playlist.get("genres") or [])
    themes = _unique(
        _english_name(tag)
        for tag in playlist.get("tags") or []
        if not tag.get("genre_tag") and tag.get("slug") not in categories
    )
    parts = (
        playlist.get("name"),
        "Genres: " + ", ".join(genres) if genres else None,
        "Tags: " + ", ".join(themes) if themes else None,
        playlist.get("description"),
    )
    return ". ".join(part.strip() for part in parts if part and part.strip())


def _english_name(tag: dict) -> Optional[str]:
    return json.loads(tag.get("name_json") or "{}").get("en")


def _unique(names) -> list[str]:
    return list(dict.fromkeys(name for name in names if name))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PlaylistStore:
    """The playlists on disk: each one's Qobuz record, and the vector of its
    text with that text's hash once embedded.

    Vectors from another model are not comparable, so opening the store
    under a different ``(model_id, model_version)`` empties it, and so does
    opening a file that is not a readable database. A file deleted while it
    is in use comes back empty. Every call blocks and opens its own
    connection, so callers run them in a thread.
    """

    def __init__(self, path: str, model: tuple[str, int]):
        self._path = path
        self._model = json.dumps(list(model))

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self._path, timeout=30)
        try:
            with con:
                for statement in _SCHEMA:
                    con.execute(statement)
                yield con
        finally:
            con.close()

    def open(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            self._claim_for_model()
        except sqlite3.DatabaseError as e:
            if isinstance(e, sqlite3.OperationalError):
                raise
            logger.warning("starting over the unreadable %s: %s", self._path, e)
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self._path + suffix)
            self._claim_for_model()

    def _claim_for_model(self) -> None:
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            row = con.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
            if row is None or row[0] != self._model:
                con.execute("DELETE FROM playlists")
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('model', ?)", (self._model,)
                )

    def sync(self, playlists: list[dict]) -> dict[str, Optional[str]]:
        """Keep exactly these playlists, with their current records; answer
        each one's text hash, None where it has no vector yet."""
        records = {
            str(p["id"]): json.dumps(p, sort_keys=True, separators=(",", ":"))
            for p in playlists
        }
        with self._connect() as con:
            con.execute("CREATE TEMP TABLE listed (id TEXT PRIMARY KEY)")
            con.executemany("INSERT INTO listed VALUES (?)", ((i,) for i in records))
            con.execute("DELETE FROM playlists WHERE id NOT IN (SELECT id FROM listed)")
            con.executemany(
                "INSERT INTO playlists (id, record) VALUES (?, ?) ON CONFLICT(id)"
                " DO UPDATE SET record = excluded.record"
                " WHERE record != excluded.record",
                records.items(),
            )
            return dict(con.execute("SELECT id, text_hash FROM playlists"))

    def save_vectors(self, vectors: list[tuple[str, str, np.ndarray]]) -> None:
        """Store ``(id, text_hash, vector)`` for playlists already synced."""
        with self._connect() as con:
            con.executemany(
                "UPDATE playlists SET text_hash = ?, embedding = ? WHERE id = ?",
                (
                    (text_hash, vector.astype(np.float32).tobytes(), playlist_id)
                    for playlist_id, text_hash, vector in vectors
                ),
            )

    def vectors(self) -> tuple[list[str], Optional[np.ndarray]]:
        """The embedded playlists' ids, and their vectors as matrix rows."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, embedding FROM playlists WHERE embedding IS NOT NULL"
            ).fetchall()
        if not rows:
            return [], None
        ids, blobs = zip(*rows)
        matrix = np.frombuffer(b"".join(blobs), dtype=np.float32)
        return list(ids), matrix.reshape(len(ids), -1)

    def records(self, ids: list[str]) -> list[dict]:
        """The records of these playlists, in this order, less any since gone."""
        if not ids:
            return []
        with self._connect() as con:
            found = dict(
                con.execute(
                    "SELECT id, record FROM playlists WHERE id IN"
                    f" ({','.join('?' * len(ids))})",
                    ids,
                )
            )
        return [json.loads(found[i]) for i in ids if i in found]


class PlaylistIndex:
    """Suggests editorial playlists for a mood or a description.

    :meth:`run` keeps the store in step with Qobuz for as long as it runs;
    :meth:`search` answers from what it has embedded so far, which is
    nothing until the first refresh embeds the playlists.
    """

    def __init__(
        self,
        store: PlaylistStore,
        embedder: TextEmbedder,
        fetch: Callable[[], Awaitable[list[EditorialPlaylist]]],
        linked: Callable[[], Awaitable[None]],
    ):
        """``linked`` returns once a Qobuz account is linked, at once while
        one is."""
        self._store = store
        self._embedder = embedder
        self._fetch = fetch
        self._linked = linked
        self._loaded: tuple[list[str], Optional[np.ndarray]] = ([], None)
        self._embedder_failed = False

    def can_answer(self) -> bool:
        """Whether a search can be embedded on this box: the shared embedder
        has its runtime, and had its model when :meth:`run` started."""
        return not self._embedder_failed and not self._embedder.missing_runtime()

    async def run(self) -> None:
        """Idle until an account is linked, so a Qobuz nobody uses neither
        loads nor downloads the embedder; then answer from the stored vectors
        and refresh them daily while linked, retrying later when the store or
        a refresh fails. Returns without an embedder, which only a restart
        can bring."""
        await self._linked()
        if not await self._embedder.available():
            self._embedder_failed = True
            logger.warning("text embedder unavailable; no playlist suggestions")
            return
        while True:
            await self._linked()
            try:
                await self.load()
                await self.refresh()
                delay = REFRESH_S
            except AuthenticationError:
                # Raised only once the credential is gone, so the wait holds.
                continue
            except Exception as e:
                logger.warning("could not refresh the Qobuz playlists: %s", e)
                delay = RETRY_S
            await asyncio.sleep(delay)

    async def load(self) -> None:
        """Answer from the vectors a previous run stored."""
        await asyncio.to_thread(self._store.open)
        self._loaded = await asyncio.to_thread(self._store.vectors)

    async def refresh(self) -> None:
        playlists = await self._fetch()
        stored = await asyncio.to_thread(
            self._store.sync, [playlist.record for playlist in playlists]
        )
        changed = []
        for playlist in playlists:
            playlist_id, text_hash = str(playlist.record["id"]), _hash(playlist.text)
            if stored.get(playlist_id) != text_hash:
                changed.append((playlist_id, playlist.text, text_hash))
        logger.info(
            "%d Qobuz playlists, %d to embed", len(playlists), len(changed)
        )
        batch: list[tuple[str, str, np.ndarray]] = []
        for playlist_id, text, text_hash in changed:
            # One at a time: the embedder is shared, and a search must not
            # wait behind the whole refresh.
            [vector] = await self._embedder.embed([text])
            batch.append((playlist_id, text_hash, vector))
            if len(batch) == SAVE_EVERY:
                await asyncio.to_thread(self._store.save_vectors, batch)
                batch = []
        await asyncio.to_thread(self._store.save_vectors, batch)
        self._loaded = await asyncio.to_thread(self._store.vectors)
        logger.info("Qobuz playlist index ready: %d playlists", len(self._loaded[0]))

    async def search(self, query: str, limit: int) -> list[dict]:
        """The records of the playlists closest to ``query``, best first."""
        ids, vectors = self._loaded
        if not ids:
            return []
        [query_vector] = await self._embedder.embed([query])
        return await asyncio.to_thread(self._best, ids, vectors, query_vector, limit)

    def _best(
        self, ids: list[str], vectors: np.ndarray, query_vector: np.ndarray, limit: int
    ) -> list[dict]:
        scores = vectors @ query_vector
        order = np.argsort(-scores)[:limit]
        return self._store.records([ids[i] for i in order if scores[i] >= MIN_SCORE])
