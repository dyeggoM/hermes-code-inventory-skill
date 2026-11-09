from .validator import check


class Service:
    """Saves orders."""

    def save(self, user_id):
        check(user_id)
        self._persist(user_id)

    def _persist(self, user_id):
        print(user_id)
