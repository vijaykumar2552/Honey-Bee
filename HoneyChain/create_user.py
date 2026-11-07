import sys
import secrets
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

from honeychain.app import create_app
from honeychain import auth, db


EMAIL = "puttavijaykumar2552@gmail.com"
PASSWORD = "yourpassword123"
ROLE = "ADMIN"


def main() -> None:
    app = create_app(load_model=False)
    with app.app_context():
        password_hash = auth.hash_password(PASSWORD)
        user = db.get_user_by_email(EMAIL)

        with db.transaction() as conn:
            if user is None:
                db.create_user(
                    conn,
                    user_id=f"USR-{secrets.token_hex(4).upper()}",
                    organization_pk=None,
                    email=EMAIL,
                    password_hash=password_hash,
                    name="Admin User",
                    role=ROLE,
                )
            else:
                conn.execute(
                    "UPDATE users "
                    "SET password_hash = ?, role = ?, status = 'ACTIVE', updated_at = ? "
                    "WHERE id = ?;",
                    (password_hash, ROLE, db.utc_now(), user["id"]),
                )

    print("User created/updated successfully!")


if __name__ == "__main__":
    main()