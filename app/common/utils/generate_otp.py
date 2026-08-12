import secrets


def generate_otp(length: int = 6) -> str:
    digits: str = "0123456789"
    return "".join(secrets.choice(digits) for _ in range(length))
