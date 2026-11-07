"""Register the simulated hive devices in the HoneyChain database."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

from honeychain import db, device_auth
from honeychain.app import create_app


HIVES = ("HIVE-001", "HIVE-002", "HIVE-003", "HIVE-004", "HIVE-005")
ORGANIZATION_ID = "ORG-SIMULATED"
ORGANIZATION_NAME = "HoneyChain Simulated Apiary"


def main() -> None:
    app = create_app(load_model=False)
    with app.app_context():
        with db.transaction() as conn:
            organization = db.get_organization_by_org_id(ORGANIZATION_ID)
            if organization is None:
                organization_pk = db.create_organization(
                    conn,
                    org_id=ORGANIZATION_ID,
                    name=ORGANIZATION_NAME,
                    org_type="BEEKEEPER",
                )
            else:
                organization_pk = int(organization["id"])

            for hive_id in HIVES:
                hive_pk = db.get_or_create_hive(conn, hive_id)
                conn.execute(
                    "UPDATE hives SET organization_id = ?, status = 'ACTIVE' "
                    "WHERE id = ?;",
                    (organization_pk, hive_pk),
                )

                device_id = f"DEV-{hive_id.replace('-', '')}"
                version = 1
                secret = device_auth.derive_device_secret(device_id, version)
                credential_hash = device_auth.credential_fingerprint(secret)
                existing = db.get_device_by_device_id(device_id)

                if existing is None:
                    db.create_device(
                        conn,
                        device_id=device_id,
                        hive_pk=hive_pk,
                        organization_pk=organization_pk,
                        credential_hash=credential_hash,
                        firmware_version="simulator",
                        expected_interval_seconds=2,
                        data_origin="SIMULATED",
                    )
                else:
                    conn.execute(
                        "UPDATE devices SET hive_id = ?, organization_id = ?, "
                        "credential_hash = ?, credential_version = ?, "
                        "firmware_version = ?, expected_interval_seconds = ?, "
                        "status = 'ACTIVE', data_origin = 'SIMULATED', "
                        "updated_at = ? WHERE id = ?;",
                        (
                            hive_pk,
                            organization_pk,
                            credential_hash,
                            version,
                            "simulator",
                            2,
                            db.utc_now(),
                            existing["id"],
                        ),
                    )

                conn.execute(
                    "UPDATE devices SET status = 'ACTIVE' "
                    "WHERE device_id = ?;",
                    (device_id,),
                )

    print("SUCCESS: Devices HIVE-001 through HIVE-005 registered!")


if __name__ == "__main__":
    main()
