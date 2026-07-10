"""Расшифровка голосовых в текст (Whisper).

Claude не принимает аудио, поэтому транскрипция — отдельный провайдер.
Поддерживаются OpenAI и Groq (оба через OpenAI-совместимый endpoint),
либо провайдер можно отключить (STT_PROVIDER=none) — тогда голос игнорируется.
"""
import io
from openai import AsyncOpenAI

import config

_client = None


def enabled() -> bool:
    return config.STT_PROVIDER in ("openai", "groq")


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if config.STT_PROVIDER == "groq":
            _client = AsyncOpenAI(
                api_key=config.GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
        else:  # openai
            _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    buf = io.BytesIO(audio_bytes)
    buf.name = filename  # SDK ориентируется на расширение
    resp = await _get_client().audio.transcriptions.create(
        model=config.STT_MODEL,
        file=buf,
    )
    return resp.text.strip()
