import logging
from typing import Optional

from kalinka_plugin_sdk.inputmodule import ContinuationRequest

logger = logging.getLogger(__name__.split(".")[-1])

#: Seeds Qobuz is asked to analyse, the latest heard.
SEEDS_ANALYSED = 5


def autoplay_seed(body: dict) -> dict:
    """What Qobuz's suggestion service reads of one seed, from its
    ``track/getList`` body; whatever the body lacks is left out as None."""
    album = body.get("album") or {}
    performer = body.get("performer") or album.get("artist") or {}
    return {
        "artist_id": performer.get("id"),
        "genre_id": (album.get("genre") or {}).get("id"),
        "label_id": (album.get("label") or {}).get("id"),
        "track_id": body.get("id"),
    }


class QobuzAutoplay:
    """
    @brief Proposes Qobuz tracks that carry a queue on, from Qobuz's own
    suggestion service (``dynamic/suggest``).

    One request returns a batch; proposals are served from it while the
    queue carries on from it: its latest seed is one the batch was asked for
    or one it served, and the listener has turned nothing more down. Else, or
    once it runs short, a new batch is asked for with the queue's latest
    seeds. Never touches the play queue: the server appends what it proposes.
    """

    def __init__(self, qobuz_client, amount_to_request: int = 50):
        """
        @param qobuz_client The linked Qobuz client.
        @param amount_to_request Suggestions asked for at a time.
        """
        self.qobuz_client = qobuz_client
        self.amount_to_request = amount_to_request
        self.remaining_tracks: list[str] = []
        self._batch_follows: frozenset[str] = frozenset()
        self._avoided: Optional[frozenset[str]] = None

    async def propose(self, request: ContinuationRequest) -> list[str]:
        unwanted = set(request.exclude) | set(request.avoid)
        if not self._batch_fits(request):
            self.remaining_tracks = []
        fresh = [t for t in self.remaining_tracks if t not in unwanted]
        if len(fresh) < request.count:
            suggested = [t for t in await self._suggest(request) if t not in unwanted]
            fresh = list(dict.fromkeys([*fresh, *suggested]))
        proposed, self.remaining_tracks = fresh[: request.count], fresh[request.count :]
        return proposed

    def _batch_fits(self, request: ContinuationRequest) -> bool:
        return (
            self._avoided == frozenset(request.avoid)
            and bool(request.seeds)
            and request.seeds[-1] in self._batch_follows
        )

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
                "track_to_analysed": [autoplay_seed(body) for body in bodies],
            },
        )
        response.raise_for_status()
        body = response.json()
        track_ids = [str(track["id"]) for track in body["tracks"]["items"]]
        self._avoided = frozenset(request.avoid)
        self._batch_follows = frozenset([*seeds, *track_ids])
        logger.info(
            "Retrieved %d new recommendation(s) using algorithm %s",
            len(track_ids),
            body.get("algorithm"),
        )
        return track_ids
