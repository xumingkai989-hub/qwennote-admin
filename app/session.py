from itsdangerous import BadData, URLSafeTimedSerializer

SESSION_MAX_AGE = 8 * 60 * 60
STATE_MAX_AGE = 10 * 60


class SignedCookie:
    def __init__(self, secret: str, *, salt: str, max_age: int):
        self.serializer = URLSafeTimedSerializer(secret, salt=salt)
        self.max_age = max_age

    def encode(self, value: str) -> str:
        return self.serializer.dumps(value)

    def decode(self, cookie: str | None) -> str | None:
        if not cookie:
            return None
        try:
            value = self.serializer.loads(cookie, max_age=self.max_age)
        except BadData:
            return None
        return value if isinstance(value, str) and value else None
