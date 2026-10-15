"""JARVIS — локальный голосовой помощник для Windows 10/11."""

__version__ = "0.1.0"


def console_utf8() -> None:
    """Вывод в UTF-8: в Windows, когда вывод идёт не в консоль (труба, файл, CI),
    Python берёт кодировку системы (cp1252/cp1251), и русский текст может падать."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
