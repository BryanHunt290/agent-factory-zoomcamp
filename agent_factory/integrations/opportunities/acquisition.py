"""Pure in-memory fake of bounded discovery/detail acquisition. No live backend."""

from .contracts import ObservationError, parse_observations, require


class FakeAcquisition:
    def __init__(self, payload, *, now):
        self._batch = parse_observations(payload, now=now)

    def search(self, *, limit=3):
        require(type(limit) is int and 1 <= limit <= 3)
        return tuple(j["job_id"] for j in self._batch.records()[:limit])

    def get(self, job_id):
        require(type(job_id) is str)
        for job in self._batch.records():
            if job["job_id"] == job_id:
                return job
        raise ObservationError()

    def acquire(self):
        """Same immutable fixture, revalidated for freshness at workflow intake."""
        return self._batch._canonical


def live_acquisition(*args, **kwargs):
    # Do not inspect configuration, credentials, environment, or supplied objects.
    raise ObservationError()
