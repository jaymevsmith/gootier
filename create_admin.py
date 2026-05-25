"""Bootstrap a first admin user.

Usage:
    python create_admin.py <username> <email> <password>
"""
import sys

from dotenv import load_dotenv

load_dotenv()

from auth import hash_password  # noqa: E402
from database import SessionLocal  # noqa: E402
from models import User, init_db  # noqa: E402


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    username, email, password = sys.argv[1], sys.argv[2], sys.argv[3]

    init_db()
    db = SessionLocal()
    try:
        if db.query(User).filter((User.username == username) | (User.email == email)).first():
            print(f"User with username '{username}' or email '{email}' already exists.")
            return 1
        user = User(
            username=username,
            email=email,
            hashed_password=hash_password(password),
            role="admin",
            tier="gold",
            is_active=True,
            is_verified=True,
        )
        db.add(user)
        db.commit()
        print(f"Created admin user id={user.id} username={username}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
