"""Диагностика звука для HUD: список микрофонов, выбор, проверка записи и озвучки."""

from __future__ import annotations

import logging
from typing import Any

from .mic_setup import analyze_levels, input_devices, is_virtual

log = logging.getLogger(__name__)


def _sd():
    try:
        import sounddevice as sd  # type: ignore
    except (ImportError, OSError) as e:  # OSError: нет библиотеки PortAudio
        raise RuntimeError(f"sounddevice недоступен: {e}") from e
    return sd


def list_microphones(selected: str = "") -> dict[str, Any]:
    """Микрофоны в формате для интерфейса; spec — «имя|интерфейс», как сохраняет мастер."""
    try:
        sd = _sd()
        devices = input_devices(sd)
        default = sd.default.device[0]
    except Exception as e:  # noqa: BLE001
        return {"devices": [], "selected": selected, "error": str(e)}
    out = [{"index": i, "name": name, "api": api, "rate": rate, "spec": f"{name}|{api}",
            "virtual": is_virtual(name), "default": i == default} for i, name, api, rate in devices]
    if not out:
        return {"devices": [], "selected": selected,
                "error": "Микрофоны не найдены. Подключите микрофон и проверьте Параметры Windows → Звук."}
    return {"devices": out, "selected": selected, "error": ""}


def test_microphone(device: str | int | None, record=None, seconds: float = 3.0) -> dict[str, Any]:
    """Запись: 1 с тишины и речь — громкость, шум, советы. Только когда голос не слушает."""
    from .audio_input import resolve_device
    from .mic_setup import _record
    sd = _sd()
    dev = resolve_device(sd, device) if device not in (None, "") else None
    rec = record or (lambda secs: _record(sd, dev, secs))
    audio = rec(seconds + 1)
    noise, speech = audio[:16000], audio[16000:]
    rep = analyze_levels(noise, speech)
    return {"noise": round(rep.noise, 4), "speech": round(rep.speech, 4), "snr": round(rep.snr, 1),
            "verdict": rep.verdict, "advice": rep.advice}
