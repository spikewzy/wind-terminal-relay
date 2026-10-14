"""Read administrator-only aggregate usage; never starts Wind or exposes results."""
import argparse
import asyncio
import json

from wind_relay.common import read_json
from wind_relay.http import Connection


async def usage(config, days):
    connection = Connection(config)
    try:
        return await connection.request("GET", "/v1/admin/usage?days=" + str(days))
    finally:
        await connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Private admin.json, never a client or worker credential")
    parser.add_argument("--days", type=int, default=1, choices=range(1, 32), metavar="1..31")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(usage(read_json(args.config), args.days)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
