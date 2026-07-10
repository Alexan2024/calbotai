"""Расшифровка голосовых через Whisper."""
import io
from openai import AsyncOpenAI
import config

client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    buf = io.BytesIO(audio_bytes)
    buf.name = filename  # OpenAI SDK ориентируется на расширение
    resp = await client.audio.transcriptions.create(
        model=config.WHISPER_MODEL,
        file=buf,
    )
    return resp.text.strip()
