#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys

# Load .env.api for the n8n webhook key
_env_path = os.path.join(os.path.dirname(__file__), '.env.api')
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#'):
                _k, _sep, _v = _line.partition('=')
                if _sep:
                    os.environ.setdefault(_k.strip(), _v.strip())


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
