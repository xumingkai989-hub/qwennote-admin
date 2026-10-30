from zoneinfo import ZoneInfo


class FakeMinutesSource:
    """Mutable, deterministic source; supports duplicates, failures and a blocking detail."""
    def __init__(self, recordings=(), details=None):
        self.recordings = list(recordings)
        self.details = dict(details or {})
        self.list_error = None
        self.detail_errors = {}
        self.detail_entered = None
        self.detail_release = None
        self.list_calls = []
        self.detail_calls = []

    def list_by_date(self, day):
        self.list_calls.append(day)
        if self.list_error:
            raise self.list_error
        return [r for r in self.recordings if r.created_at.astimezone(ZoneInfo('Asia/Shanghai')).date() == day]

    def get_detail(self, recording_id):
        self.detail_calls.append(recording_id)
        if self.detail_entered:
            self.detail_entered.set()
            if not self.detail_release.wait(5):
                raise TimeoutError('Test detail was not released')
        if recording_id in self.detail_errors:
            raise self.detail_errors[recording_id]
        return self.details[recording_id]
