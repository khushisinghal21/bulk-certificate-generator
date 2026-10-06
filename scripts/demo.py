"""Submit a bulk job, watch its progress and download the results.

python scripts/demo.py                      # 250 generated recipients
python scripts/demo.py --count 2000
python scripts/demo.py --base-url http://localhost:8000 --api-key secret
"""

import argparse
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=250)
    parser.add_argument("--api-key")
    parser.add_argument("--out", default="certificates.zip")
    args = parser.parse_args()

    recipients = [
        {"name": f"Participant {i + 1}", "email": f"participant{i + 1}@example.com", "grade": "A"}
        for i in range(args.count)
    ]
    # Two deliberately invalid rows, to show partial acceptance.
    recipients += [{"name": "No Email"}, {"name": "Bad Email", "email": "not-an-email"}]

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    client = httpx.Client(base_url=args.base_url, headers=headers, timeout=60)

    started = time.perf_counter()
    response = client.post(
        "/api/v1/jobs",
        json={
            "certificate": {
                "course_name": "Drone Survey Fundamentals",
                "issuer_name": "Aereo Academy",
                "description": "Awarded for completing hands-on training in drone survey planning.",
            },
            "recipients": recipients,
        },
    )
    response.raise_for_status()
    job = response.json()
    print(f"Submitted job {job['id']} ({response.status_code}) in {time.perf_counter() - started:.2f}s")
    for rejected in job["rejected_recipients"]:
        print(f"  rejected #{rejected['index']}: {'; '.join(rejected['errors'])}")

    while job["status"] in ("pending", "processing"):
        time.sleep(1)
        job = client.get(job["links"]["job"]).json()
        p = job["progress"]
        print(f"  {job['status']:<22} {p['percent']:6.2f}%  succeeded={p['succeeded']} failed={p['failed']}")

    print(f"Finished with status {job['status']!r} in {time.perf_counter() - started:.2f}s")
    archive = client.get(job["links"]["archive"])
    if archive.status_code != 200:
        print(f"No archive: {archive.status_code} {archive.text}")
        return 1
    with open(args.out, "wb") as f:
        f.write(archive.content)
    print(f"Saved {len(archive.content) // 1024} KB to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
