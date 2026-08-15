import asyncio
import os

# Standby runs serve MCP over HTTP; normal runs do the batch scrape. The
# platform sets APIFY_META_ORIGIN before the process starts, so the branch
# happens here and main.py stays exactly as it was.
if (os.environ.get("APIFY_META_ORIGIN") or "").strip().upper() == "STANDBY":
    from .mcp_server import main
else:
    from .main import main

asyncio.run(main())
