def main(argv: list[str] | None = None) -> int:
    # Imported lazily so the web functions don't need the CLI's dependencies (rich).
    from .cli import main as cli_main

    return cli_main(argv)


__all__ = ["main"]
