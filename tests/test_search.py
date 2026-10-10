"""Web search timing/quality: 4 real queries through Dork Search."""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dork import websearch
Q = ["ISRO latest launch", "Chennai weather", "how to install python on windows", "Droidal company Chennai"]
async def main():
    for q in Q:
        t = time.time()
        r = await websearch.search([q], freshness="recent")
        sites = sorted({x["site"] for x in r})
        print(f"{time.time() - t:5.1f}s  {len(r)} passages  {q!r}  from {', '.join(sites)[:110]}")
asyncio.run(main())
