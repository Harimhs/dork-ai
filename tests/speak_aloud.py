"""Play a phrase through this PC's speakers in a given voice (a stand-in person talking in the room)."""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import edge_tts, sounddevice as sd
from dork import hearing
async def main(text, voice):
    data = b""
    async for c in edge_tts.Communicate(text, voice).stream():
        if c["type"] == "audio": data += c["data"]
    x = hearing.decode(data)
    sd.play(x, 16000); sd.wait()
asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "en-IN-PrabhatNeural"))
