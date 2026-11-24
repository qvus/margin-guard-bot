"""Initialize the stage-one application resources."""

from .config import get_settings
from .database import initialize_database


def main() -> None:
    settings = get_settings()
    initialize_database(settings.database_path)
    print(f"База данных готова: {settings.database_path}")


if __name__ == "__main__":
    main()
