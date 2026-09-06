"""ASGI entry point (uvicorn). See PLAN.md §3.8 for the deployment shape."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "reeltalk.settings")

application = get_asgi_application()
