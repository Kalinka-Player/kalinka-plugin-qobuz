import logging
from typing import Optional

from kalinka_plugin_sdk.datamodel import Track
from kalinka_plugin_sdk.inputmodule import ContinuationRequest

logger = logging.getLogger(__name__.split(".")[-1])

#: Seeds Qobuz is asked to analyse, the latest heard.
SEEDS_ANALYSED = 5


def autoplay_seed(track: Track) -> dict:
    """What Qobuz's suggestion service reads of one seed track."""
    return {
        "artist_id": int(track.performer.id.id) if track.performer else None,
        "genre_id": (int(track.album.genres[0].id.id) if track.album.genres else None),
        "label_id": int(track.album.label.id.id) if track.album.label else None,
        "track_id": int(track.id.id) if track.id else None,
    }


class QobuzAutoplay:
    """
    @brief Proposes Qobuz tracks that carry a queue on, from Qobuz's own
    suggestion service (``dynamic/suggest``).

    One request returns a batch; proposals are served from it until it runs
    dry or the listener turns a track down, when a new batch is asked for with
    the queue's latest seeds. Never touches the play queue: the server appends
    what it proposes.
    """

    def __init__(self, qobuz_client, to_track, amount_to_request: int = 50):
        """
        @param qobuz_client The linked Qobuz client.
        @param to_track Turns a Qobuz track body into an SDK Track.
        @param amount_to_request Suggestions asked for at a time.
        """
        self.qobuz_client = qobuz_client
        self._to_track = to_track
        self.amount_to_request = amount_to_request
        self.remaining_tracks: list[str] = []
        self._avoided: Optional[frozenset[str]] = None

    async def propose(self, request: ContinuationRequest) -> list[str]:
        unwanted = set(request.exclude) | set(request.avoid)
        if self._avoided != frozenset(request.avoid):
            self.remaining_tracks = []
        self._avoided = frozenset(request.avoid)
        fresh = [t for t in self.remaining_tracks if t not in unwanted]
        if len(fresh) < request.count:
            fresh = [t for t in await self._suggest(request) if t not in unwanted]
        proposed, self.remaining_tracks = fresh[: request.count], fresh[request.count :]
        return proposed

    async def _suggest(self, request: ContinuationRequest) -> list[str]:
        seeds = [s for s in request.seeds if s not in request.avoid][-SEEDS_ANALYSED:]
        if not seeds:
            logger.debug("No Qobuz tracks to continue from")
            return []
        bodies = await self.qobuz_client.get_tracks_meta([int(s) for s in seeds])
        listened = [int(t) for t in request.exclude if t.isdigit() and t not in seeds]
        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + "dynamic/suggest",
            json={
                "limit": self.amount_to_request,
                "listened_tracks_ids": listened,
                "track_to_analysed": [autoplay_seed(self._to_track(body)) for body in bodies],
            },
        )
        response.raise_for_status()
        body = response.json()
        track_ids = [str(track["id"]) for track in body["tracks"]["items"]]
        logger.info(
            "Retrieved %d new recommendation(s) using algorithm %s",
            len(track_ids),
            body.get("algorithm"),
        )
        return track_ids
