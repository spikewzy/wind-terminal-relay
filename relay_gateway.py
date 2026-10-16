"""Start only the cloud queue; no Wind SDK or Wind MCP is started."""
import argparse

import uvicorn

from wind_relay.common import read_config
from wind_relay.gateway import create_app
from wind_relay.store import Queue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--maintenance-file", help="Private upgrade marker; blocks new jobs and claims while present")
    parser.add_argument("--resolve-uncertain", metavar="JOB_ID",
                        help="After inspecting the execution host, mark an uncertain job failed; never resubmits it")
    args = parser.parse_args()
    config = read_config(args.config)
    if args.maintenance_file:
        from pathlib import Path
        config["maintenance_file"] = str(Path(args.maintenance_file).absolute())
    if args.resolve_uncertain:
        Queue(config["database"]).resolve_uncertain(args.resolve_uncertain)
        print("Resolved as failed; no Wind request was repeated.")
        return
    uvicorn.run(create_app(config), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
